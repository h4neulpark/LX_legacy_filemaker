"""Two-region survey recognition with color filtering before OCR."""
from dataclasses import dataclass
import re
import time
import unicodedata

DEFAULT_HEADER = (0, (605/1684, 110/1191, 943/1684, 143/1191))
DEFAULT_LOTS = (0, (.037, .61, .073, .882))


@dataclass(frozen=True)
class SurveyCandidate:
    lot: str
    bounds: tuple
    warning: str = ''


@dataclass(frozen=True)
class SurveyResult:
    header: str
    candidates: tuple
    red_pixels: int
    seconds: float

    @property
    def text(self):
        return self.header + '\n' + '\n'.join(row.lot for row in self.candidates)


def normalize_survey_lot(text):
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'[−–—]', '-', text)
    text = re.sub(r'\s+', '', text).strip('_|[](){}.,:;~=')
    if not re.fullmatch(r'산?\d{1,4}(?:-\d{1,4})?', text):
        return None
    mountain = text.startswith('산')
    values = text.lstrip('산').split('-')
    main, sub = int(values[0]), int(values[1]) if len(values) > 1 else 0
    if main == 0:
        return None
    return ('산' if mountain else '') + str(main) + (f'-{sub}' if sub else '')


def validate_region(region):
    import math
    page, bounds = region
    if not isinstance(page, int) or isinstance(page, bool) or page < 0 or len(bounds) != 4:
        raise ValueError('영역 페이지가 올바르지 않습니다.')
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 1 for v in bounds):
        raise ValueError('영역 좌표가 올바르지 않습니다.')
    if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise ValueError('영역의 너비와 높이가 필요합니다.')
    return page, tuple(bounds)


def neutral_mask(pix):
    import pymupdf
    samples = bytearray(pix.samples)
    ambiguous = bytearray(pix.width * pix.height)
    red = 0
    for pixel, offset in enumerate(range(0, len(samples), 3)):
        r, g, b = samples[offset:offset+3]
        high, low = max(r, g, b), min(r, g, b)
        is_red = r >= 70 and r - g >= 25 and r - b >= 25
        if is_red:
            red += 1
        if not is_red and high - low <= 35 and high < 190:
            continue
        if not is_red and low < 150 and high < 210:
            ambiguous[pixel] = 1
        samples[offset:offset+3] = b'\xff\xff\xff'
    return pymupdf.Pixmap(pymupdf.csRGB, pix.width, pix.height, bytes(samples), False), ambiguous, red


def _region_pixmap(doc, region, dpi):
    import pymupdf
    page_index, bounds = validate_region(region)
    if page_index >= len(doc):
        raise ValueError(f'지정한 {page_index + 1}페이지가 없습니다. 영역을 다시 지정하세요.')
    page = doc[page_index]
    x0, y0, x1, y1 = bounds
    rect = pymupdf.Rect(x0*page.rect.width, y0*page.rect.height,
                        x1*page.rect.width, y1*page.rect.height)
    pixels = rect.get_area() * (dpi/72)**2
    if pixels > 6_000_000:
        dpi = max(36, int(dpi * (6_000_000/pixels)**.5))
    if rect.get_area() * (dpi/72)**2 > 6_100_000:
        raise ValueError('읽기 영역이 너무 큽니다. 토지소재 또는 지번 열만 좁게 지정하세요.')
    return page.get_pixmap(clip=rect, dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)


def _ocr(pix, models):
    import pymupdf
    data = pix.pdfocr_tobytes(language='kor+eng', tessdata=str(models))
    return pymupdf.open(stream=data, filetype='pdf')


def read_survey(path, header_region, lot_region):
    import pymupdf
    from ocr_engine import MODEL_ROOT
    from ppocr_engine import recognize_header
    started = time.perf_counter()
    header_region, lot_region = validate_region(header_region), validate_region(lot_region)
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise ValueError('암호가 필요한 PDF입니다.')
        if header_region[0] >= len(doc):
            raise ValueError('토지소재 페이지가 없습니다. 영역을 다시 지정하세요.')
        # Always read the image: existing/hidden PDF text may already be incorrect.
        header = recognize_header(_region_pixmap(doc, header_region, 300))
        # Render even PDFs with text layers: hidden OCR text has no reliable color.
        original = _region_pixmap(doc, lot_region, 450)
        masked, ambiguous, red = neutral_mask(original)
    lines = {}
    with _ocr(masked, MODEL_ROOT) as recognized:
        page = recognized[0]
        for word in page.get_text('words', sort=True):
            lines.setdefault((word[5], word[6]), []).append(word)
        candidates, seen = [], set()
        rx0, ry0, rx1, ry1 = lot_region[1]
        for words in lines.values():
            lot = normalize_survey_lot(' '.join(word[4] for word in words))
            if lot is None or lot in seen:
                continue
            seen.add(lot)
            x0, y0 = min(w[0] for w in words)/page.rect.width, min(w[1] for w in words)/page.rect.height
            x1, y1 = max(w[2] for w in words)/page.rect.width, max(w[3] for w in words)/page.rect.height
            left, right = max(0, int(x0*masked.width)), min(masked.width, int(x1*masked.width)+1)
            top, bottom = max(0, int(y0*masked.height)), min(masked.height, int(y1*masked.height)+1)
            uncertain = sum(sum(ambiguous[y*masked.width+left:y*masked.width+right])
                            for y in range(top, bottom))
            warning = '색상 확인 필요' if uncertain > max(5, (right-left)*(bottom-top)*.015) else ''
            bounds = (rx0+x0*(rx1-rx0), ry0+y0*(ry1-ry0), rx0+x1*(rx1-rx0), ry0+y1*(ry1-ry0))
            candidates.append(SurveyCandidate(lot, bounds, warning))
    return SurveyResult(header, tuple(candidates), red, time.perf_counter() - started)


def check_cli(arguments):
    import argparse
    from dataclasses import asdict
    import json
    from pathlib import Path
    from pdf_pipeline import ProcessSlot, survey_key
    parser=argparse.ArgumentParser(description='측량성과도 색상 분리·두 영역 읽기 검사')
    parser.add_argument('pdf')
    parser.add_argument('--header',type=float,nargs=4,default=DEFAULT_HEADER[1])
    parser.add_argument('--lots',type=float,nargs=4,default=DEFAULT_LOTS[1])
    parser.add_argument('--header-page',type=int,default=1)
    parser.add_argument('--lots-page',type=int,default=1)
    parser.add_argument('--output',required=True)
    args=parser.parse_args(arguments)
    slot=ProcessSlot(timeout=60)
    try:
        header=(args.header_page-1,tuple(args.header))
        lots=(args.lots_page-1,tuple(args.lots))
        key=survey_key(args.pdf,header,lots)
        slot.submit(key,'survey',(args.pdf,header,lots))
        while True:
            reply=slot.poll()
            if reply is not None:
                _,result,error=reply
                report={'error':error} if error else asdict(result)
                break
            time.sleep(.02)
    except Exception as exc:
        report={'error':str(exc) or type(exc).__name__}
    finally:
        slot.close()
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 1 if report.get('error') else 0


if __name__=='__main__':
    import multiprocessing
    import sys
    multiprocessing.freeze_support()
    raise SystemExit(check_cli(sys.argv[1:]))
