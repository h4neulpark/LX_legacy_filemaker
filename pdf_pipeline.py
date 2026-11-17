"""Bounded, cancellable PDF workers. Only plain data crosses process boundaries."""
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
import json
import multiprocessing
import statistics
import time


def file_identity(path):
    path = Path(path).resolve()
    stat = path.stat()
    return str(path), stat.st_mtime_ns, stat.st_size


def render_key(path, page, zoom, width):
    return ('render', file_identity(path), page, zoom, width)


def region_key(path, page, bounds):
    return ('region', file_identity(path), page, tuple(bounds))


@dataclass(frozen=True)
class RenderedPage:
    ppm: bytes
    width: int
    height: int
    zoom: float
    seconds: float


@dataclass(frozen=True)
class PreparedRegion:
    text: str
    ocr: bool
    seconds: float


def render_document(path, page_index, zoom, width):
    import pymupdf
    started = time.perf_counter()
    with pymupdf.open(path) as doc:
        page = doc[page_index]
        if width:
            zoom = max(width / page.rect.width, .25)
        # Keep IPC, Tk image allocation, and speculative memory bounded.
        pixels = page.rect.width * page.rect.height * zoom * zoom
        if pixels > 16_000_000:
            zoom *= (16_000_000 / pixels) ** .5
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False,
                              colorspace=pymupdf.csRGB)
        return RenderedPage(pix.tobytes('ppm'), pix.width, pix.height, zoom,
                            time.perf_counter() - started)


def prepare_region(path, page_index, bounds):
    import pymupdf
    from pdf_region import extract_region_text
    from ocr_engine import recognize_pdf_region
    started = time.perf_counter()
    with pymupdf.open(path) as doc:
        text = extract_region_text(doc[page_index], bounds)
    if text:
        return PreparedRegion(text, False, time.perf_counter() - started)
    result = recognize_pdf_region(path, page_index, bounds)
    return PreparedRegion(result.text, True, time.perf_counter() - started)


def _worker(connection):
    try:
        while True:
            key, operation, args = connection.recv()
            try:
                result = (render_document if operation == 'render' else prepare_region)(*args)
                connection.send((key, result, ''))
            except Exception as exc:
                connection.send((key, None, str(exc)))
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()


class ProcessSlot:
    """One reusable worker, at most one request; replacements stop old work."""
    def __init__(self, timeout=60):
        self.timeout = timeout
        self.process = None
        self.connection = None
        self.key = None
        self.started = 0

    def submit(self, key, operation, args):
        if self.key == key:
            return
        if self.key is not None:
            self.close()
        if self.process is not None and not self.process.is_alive():
            self.close()
        try:
            if self.process is None:
                ctx = multiprocessing.get_context('spawn')
                self.connection, child = ctx.Pipe()
                self.process = ctx.Process(target=_worker, args=(child,), daemon=True)
                try:
                    self.process.start()
                finally:
                    child.close()
            self.key = key
            self.started = time.monotonic()
            self.connection.send((key, operation, args))
        except Exception:
            self.close()
            raise

    def poll(self):
        if self.key is None:
            return None
        key = self.key
        try:
            if self.connection.poll():
                result = self.connection.recv()
                self.key = None
                return result
            if not self.process.is_alive():
                raise RuntimeError('PDF 작업 프로세스가 종료됐습니다.')
            if time.monotonic() - self.started > self.timeout:
                raise TimeoutError(f'PDF 작업 제한 시간({self.timeout}초)을 초과했습니다.')
        except Exception as exc:
            self.close()
            return key, None, str(exc)
        return None

    def close(self):
        process, self.process = self.process, None
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self.key = None
        if process is not None and process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=.1)
            if process.is_alive():
                process.kill()
                process.join(timeout=.1)
            if not process.is_alive():
                process.close()


class BoundedCache:
    def __init__(self, max_bytes=64 * 1024 * 1024, max_items=8):
        self.max_bytes, self.max_items = max_bytes, max_items
        self.items = OrderedDict()
        self.bytes = 0

    def get(self, key):
        entry = self.items.get(key)
        if entry is None:
            return None
        self.items.move_to_end(key)
        return entry[0]

    def put(self, key, value, weight):
        old = self.items.pop(key, None)
        if old is not None:
            self.bytes -= old[1]
        if weight > self.max_bytes:
            return
        self.items[key] = value, weight
        self.bytes += weight
        while self.bytes > self.max_bytes or len(self.items) > self.max_items:
            _, (_, size) = self.items.popitem(last=False)
            self.bytes -= size


class TimingStats:
    """Bounded numeric diagnostics, with no filenames or PDF contents."""
    def __init__(self):
        self.samples = {}

    def record(self, stage, seconds):
        self.samples.setdefault(stage, deque(maxlen=128)).append(seconds)

    def write(self, path):
        data = {stage: dict(samples=len(values), median_ms=statistics.median(values)*1000,
                            max_ms=max(values)*1000)
                for stage, values in self.samples.items() if values}
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


class PdfPipeline:
    def __init__(self):
        self.render = ProcessSlot(timeout=30)
        self.prepare = ProcessSlot(timeout=60)
        self.images = BoundedCache()
        self.regions = BoundedCache(max_bytes=1024*1024, max_items=16)

    def request_render(self, key):
        cached = self.images.get(key)
        if cached is None:
            _, identity, page, zoom, width = key
            self.render.submit(key, 'render', (identity[0], page, zoom, width))
        return cached

    def request_region(self, key):
        if self.regions.get(key) is None:
            _, identity, page, bounds = key
            self.prepare.submit(key, 'region', (identity[0], page, bounds))

    def poll(self, include_prepare=True):
        results = []
        for slot in ((self.render, self.prepare) if include_prepare else (self.render,)):
            result = slot.poll()
            if result is not None:
                key, value, error = result
                if not error:
                    cache = self.images if key[0] == 'render' else self.regions
                    weight = len(value.ppm) if key[0] == 'render' else len(value.text.encode('utf-8'))
                    cache.put(key, value, weight)
                results.append(result)
        return results

    def close(self):
        self.render.close()
        self.prepare.close()


class PreparedOcrJob:
    """Promote speculative OCR to a cancellable foreground job."""
    def __init__(self, pipeline, key):
        self.pipeline, self.key = pipeline, key
        self.error = ''

    def poll(self):
        from ocr_engine import OcrResult
        cached = self.pipeline.regions.get(self.key)
        if cached is not None:
            return OcrResult(text=cached.text, seconds=cached.seconds)
        if self.pipeline.prepare.key != self.key:
            return OcrResult(error='준비 중인 OCR 작업이 취소됐습니다.')
        result = self.pipeline.prepare.poll()
        if result is not None:
            key, value, error = result
            if key == self.key:
                if error:
                    self.error = error
                else:
                    self.pipeline.regions.put(key, value, len(value.text.encode('utf-8')))
                    return OcrResult(text=value.text, seconds=value.seconds)
        cached = self.pipeline.regions.get(self.key)
        if cached is not None:
            return OcrResult(text=cached.text, seconds=cached.seconds)
        if self.error or self.pipeline.prepare.key != self.key:
            return OcrResult(error=self.error or '준비 중인 OCR 작업이 취소됐습니다.')
        return None

    def cancel(self):
        if self.pipeline.prepare.key == self.key:
            self.pipeline.prepare.close()


def check_cli(arguments):
    """Verify spawned PDF/OCR workers in both source and frozen builds."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('pdf')
    parser.add_argument('--region', type=float, nargs=4, default=(0, 0, 1, .3))
    parser.add_argument('--output', required=True)
    args = parser.parse_args(arguments)
    pipeline = PdfPipeline()
    started = time.perf_counter()
    report = {}
    try:
        image_key = render_key(args.pdf, 0, 1, 600)
        text_key = region_key(args.pdf, 0, args.region)
        pipeline.request_render(image_key)
        pipeline.request_region(text_key)
        deadline = time.monotonic() + 65
        while time.monotonic() < deadline:
            for key, value, error in pipeline.poll():
                if error:
                    raise RuntimeError(error)
                if key == image_key:
                    report['render'] = dict(width=value.width, height=value.height, seconds=value.seconds)
                if key == text_key:
                    report['region'] = dict(text=value.text, ocr=value.ocr, seconds=value.seconds)
            if 'render' in report and 'region' in report:
                break
            time.sleep(.01)
        else:
            raise TimeoutError('PDF pipeline check timed out')
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        pipeline.close()
    report['total_seconds'] = time.perf_counter() - started
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 1 if report.get('error') else 0


if __name__ == '__main__':
    multiprocessing.freeze_support()
    import sys
    raise SystemExit(check_cli(sys.argv[1:]))
