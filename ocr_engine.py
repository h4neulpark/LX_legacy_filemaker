"""Offline region OCR in a cancellable process, separate from the Tk PDF viewer."""

import math
import multiprocessing
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pymupdf


MODEL_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'assets/ocr/tessdata'
OCR_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class OcrResult:
    text: str = ''
    seconds: float = 0.0
    error: str = ''


def clean_ocr_address(raw: str) -> str:
    """Keep the first address/lot pair; do not invent or correct OCR characters.

    Survey sheets may append '번지 외4필', and a cadastre selection may
    include a neighbouring scale cell. Neither should replace the lot number.
    The original OCR text remains available to the user in the app tooltip.
    """
    text = ' '.join(raw.split())
    compact = re.sub(r'\s+', '', text)
    compact = re.sub(r'(?<=\d)[−–—](?=\d)', '-', compact)
    match = re.match(r'(.+?(?:동(?:\d+가)?|리|가|읍|면))(산?\d{1,4}(?:-\d{1,4})?)(?![\d가])', compact)
    if match:
        return f'{match[1]} {match[2]}'
    return text


def recognize_pdf_region(path, page_index, bounds, model_root=None) -> OcrResult:
    started = time.monotonic()
    models = Path(model_root) if model_root else MODEL_ROOT
    for language in ('kor', 'eng'):
        if not (models / f'{language}.traineddata').is_file():
            raise FileNotFoundError(f'OCR 언어 모델이 없습니다: {models / (language + ".traineddata")}')
    if len(bounds) != 4 or not all(math.isfinite(v) and 0 <= v <= 1 for v in bounds):
        raise ValueError('OCR 영역 좌표가 올바르지 않습니다.')
    x0, y0, x1, y1 = bounds
    if x1 <= x0 or y1 <= y0:
        raise ValueError('OCR 영역의 너비와 높이가 필요합니다.')
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise ValueError('암호가 필요한 PDF입니다.')
        page = doc[page_index]
        # Rendering clips use the displayed (rotated) page coordinate space.
        rect = pymupdf.Rect(x0*page.rect.width, y0*page.rect.height,
                            x1*page.rect.width, y1*page.rect.height)
        # Large drawings must not allocate an unbounded 300-DPI image.
        dpi = max(36, min(300, int(72 * math.sqrt(20_000_000 / rect.get_area()))))
        if rect.get_area() * (dpi/72)**2 > 21_000_000:
            raise ValueError('OCR 영역이 너무 큽니다. 주소 부분만 선택하세요.')
        pix = page.get_pixmap(clip=rect, dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
        if pix.width < 8 or pix.height < 8:
            return OcrResult(seconds=time.monotonic()-started)
        data = pix.pdfocr_tobytes(language='kor+eng', tessdata=str(models))
    with pymupdf.open(stream=data, filetype='pdf') as recognized:
        text = recognized[0].get_text('text', sort=True).strip()
    return OcrResult(text=text, seconds=time.monotonic()-started)


def _recognize_worker(sender, path, page_index, bounds, model_root):
    try:
        try:
            result = recognize_pdf_region(path, page_index, bounds, model_root)
        except Exception as exc:
            result = OcrResult(error=str(exc))
        sender.send(result)
    finally:
        sender.close()


class OcrJob:
    def __init__(self, path, page_index, bounds, model_root=None):
        ctx = multiprocessing.get_context('spawn')
        self.receiver, sender = ctx.Pipe(duplex=False)
        self.process = ctx.Process(target=_recognize_worker,
                                   args=(sender, str(path), page_index, bounds, str(model_root or MODEL_ROOT)),
                                   daemon=True)
        self.started = time.monotonic()
        try:
            self.process.start()
        except Exception:
            self.receiver.close()
            raise
        finally:
            sender.close()

    def poll(self):
        if self.receiver.poll():
            try:
                result = self.receiver.recv()
            except EOFError:
                result = OcrResult(error='OCR 프로세스가 결과 없이 종료됐습니다.')
            self.cancel()
            return result
        if not self.process.is_alive():
            if self.receiver.poll():
                return self.poll()
            code = self.process.exitcode
            self.cancel()
            return OcrResult(error=f'OCR 프로세스가 종료됐습니다. (코드 {code})')
        if time.monotonic()-self.started > OCR_TIMEOUT_SECONDS:
            self.cancel()
            return OcrResult(error='OCR 제한 시간(60초)을 초과했습니다. 영역을 줄여 다시 시도하세요.')
        return None

    def cancel(self):
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=.2)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=.2)
        self.receiver.close()


def check_cli(arguments):
    """Reproducible OCR smoke check for source and frozen executables."""
    import argparse
    import json
    parser = argparse.ArgumentParser(description='선택 영역 오프라인 OCR 검사')
    parser.add_argument('pdf')
    parser.add_argument('--region', type=float, nargs=4, required=True)
    parser.add_argument('--page', type=int, default=1)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(arguments)
    try:
        job = OcrJob(args.pdf, args.page-1, args.region)
        try:
            result = job.poll()
            while result is None:
                time.sleep(.05)
                result = job.poll()
        finally:
            job.cancel()
        report = {'raw_text': result.text, 'address': clean_ocr_address(result.text), 'seconds': result.seconds, 'error': result.error}
    except Exception as exc:
        report = {'raw_text': '', 'address': '', 'error': str(exc)}
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 1 if report['error'] else 0
