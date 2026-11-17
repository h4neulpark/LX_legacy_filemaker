import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path

from pdf_pipeline import (BoundedCache, PdfPipeline, PreparedOcrJob, ProcessSlot,
                          TimingStats, file_identity, region_key, render_key)


class CacheTests(unittest.TestCase):
    def test_memory_limit_and_lru_eviction(self):
        cache = BoundedCache(max_bytes=10, max_items=3)
        cache.put('a', 'A', 4)
        cache.put('b', 'B', 4)
        self.assertEqual(cache.get('a'), 'A')
        cache.put('c', 'C', 4)
        self.assertIsNone(cache.get('b'))
        self.assertEqual(cache.bytes, 8)
        cache.put('a', 'too big', 11)
        self.assertIsNone(cache.get('a'))
        self.assertLessEqual(cache.bytes, 10)

    def test_file_replacement_invalidates_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sample.pdf'
            path.write_bytes(b'first')
            old = render_key(path, 0, 1, 800)
            path.write_bytes(b'replaced PDF')
            self.assertNotEqual(old, render_key(path, 0, 1, 800))
            self.assertNotEqual(region_key(path, 0, (0, 0, 1, 1)),
                                region_key(path, 0, (0, 0, .5, 1)))

    def test_timing_report_is_bounded_and_contains_only_numeric_metrics(self):
        stats = TimingStats()
        for i in range(200):
            stats.record('render', i / 1000)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'timings.json'
            stats.write(path)
            data = json.loads(path.read_text())
        self.assertEqual(data['render']['samples'], 128)
        self.assertAlmostEqual(data['render']['max_ms'], 199)


@unittest.skipUnless(importlib.util.find_spec('pymupdf'), 'Requires PyMuPDF')
class PipelineProcessTests(unittest.TestCase):
    def setUp(self):
        import pymupdf
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'sample.pdf'
        with pymupdf.open() as doc:
            page = doc.new_page(width=600, height=800)
            page.insert_text((60, 100), 'Seoul 123-4', fontsize=24)
            doc.new_page(width=800, height=600)
            doc.save(self.path)
        self.pipeline = PdfPipeline()
        self.addCleanup(self.pipeline.close)

    def wait(self, key):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for item in self.pipeline.poll():
                if item[0] == key:
                    return item
            time.sleep(.01)
        self.fail(f'Worker did not complete: {key[0]}')

    def test_render_and_region_work_in_separate_reusable_processes(self):
        image_key = render_key(self.path, 0, 1, 600)
        text_key = region_key(self.path, 0, (0, 0, 1, .3))
        self.pipeline.request_render(image_key)
        self.pipeline.request_region(text_key)
        image_pid = self.pipeline.render.process.pid
        text_pid = self.pipeline.prepare.process.pid
        self.assertNotEqual(image_pid, text_pid)
        # Polling either slot also stores the other slot's completed result.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self.pipeline.poll()
            if self.pipeline.images.get(image_key) and self.pipeline.regions.get(text_key):
                break
            time.sleep(.01)
        image = self.pipeline.images.get(image_key)
        self.assertIsNotNone(image)
        self.assertTrue(image.ppm.startswith(b'P6'))
        self.assertEqual((image.width, image.height), (600, 800))
        self.assertIn('123-4', self.pipeline.regions.get(text_key).text)
        other = render_key(self.path, 1, 1, 800)
        self.pipeline.request_render(other)
        key, rendered, error = self.wait(other)
        self.assertFalse(error)
        self.assertEqual((rendered.width, rendered.height), (800, 600))
        self.assertEqual(self.pipeline.render.process.pid, image_pid)
        self.assertIsNotNone(self.pipeline.request_render(image_key))
        self.assertIsNone(self.pipeline.render.key)

    def test_new_request_stops_old_work_and_only_delivers_latest(self):
        old = render_key(self.path, 0, 1, 1600)
        latest = render_key(self.path, 1, 1, 400)
        self.pipeline.request_render(old)
        old_pid = self.pipeline.render.process.pid
        self.pipeline.request_render(latest)
        self.assertNotEqual(self.pipeline.render.process.pid, old_pid)
        key, rendered, error = self.wait(latest)
        self.assertEqual(key, latest)
        self.assertFalse(error)
        self.assertEqual(rendered.width, 400)
        self.assertIsNone(self.pipeline.images.get(old))

    def test_invalid_pdf_error_does_not_break_next_request(self):
        broken = Path(self.tmp.name) / 'broken.pdf'
        broken.write_bytes(b'not a PDF')
        bad = render_key(broken, 0, 1, 600)
        self.pipeline.request_render(bad)
        self.assertTrue(self.wait(bad)[2])
        good = render_key(self.path, 0, 1, 600)
        self.pipeline.request_render(good)
        self.assertFalse(self.wait(good)[2])

    def test_timeout_terminates_worker_and_allows_retry(self):
        key = render_key(self.path, 0, 1, 600)
        self.pipeline.request_render(key)
        self.pipeline.render.started = time.monotonic() - 31
        result = self.pipeline.render.poll()
        self.assertIn('제한 시간', result[2])
        self.assertIsNone(self.pipeline.render.process)
        self.pipeline.request_render(key)
        self.assertFalse(self.wait(key)[2])

    def test_background_region_is_promoted_without_new_process(self):
        key = region_key(self.path, 0, (0, 0, 1, .3))
        self.pipeline.request_region(key)
        pid = self.pipeline.prepare.process.pid
        job = PreparedOcrJob(self.pipeline, key)
        deadline = time.monotonic() + 20
        result = None
        while result is None and time.monotonic() < deadline:
            result = job.poll()
            time.sleep(.01)
        self.assertIsNotNone(result)
        self.assertFalse(result.error)
        self.assertIn('123-4', result.text)
        self.assertEqual(self.pipeline.prepare.process.pid, pid)
        self.assertIsNotNone(self.pipeline.regions.get(key))

    def test_actual_scanned_region_ocr_is_cached(self):
        import pymupdf
        scanned = Path(self.tmp.name) / 'scanned.pdf'
        with pymupdf.open(self.path) as original, pymupdf.open() as doc:
            pix = original[0].get_pixmap(dpi=150)
            page = doc.new_page(width=600, height=800)
            page.insert_image(page.rect, pixmap=pix)
            doc.save(scanned)
        key = region_key(scanned, 0, (.05, .05, .8, .2))
        self.pipeline.request_region(key)
        _, result, error = self.wait(key)
        self.assertFalse(error)
        self.assertTrue(result.ocr)
        self.assertIn('123', result.text)
        self.assertIn('123', PreparedOcrJob(self.pipeline, key).poll().text)

    def test_cancelling_promoted_work_terminates_its_process(self):
        key = region_key(self.path, 0, (0, 0, 1, 1))
        self.pipeline.request_region(key)
        job = PreparedOcrJob(self.pipeline, key)
        job.cancel()
        self.assertIsNone(self.pipeline.prepare.process)
        self.assertTrue(job.poll().error)

    def test_cached_result_does_not_consume_other_background_request(self):
        cached = region_key(self.path, 0, (0, 0, 1, .3))
        self.pipeline.request_region(cached)
        self.assertFalse(self.wait(cached)[2])
        other = region_key(self.path, 0, (.05, .05, .9, .4))
        self.pipeline.request_region(other)
        result = PreparedOcrJob(self.pipeline, cached).poll()
        self.assertIn('123', result.text)
        self.assertEqual(self.pipeline.prepare.key, other)
        self.assertFalse(self.wait(other)[2])


if __name__ == '__main__':
    unittest.main()
