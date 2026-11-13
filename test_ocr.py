import hashlib
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app
import ocr_engine
import pymupdf
import test_pdf_region as region_tests
from pdf_region import AddressRegion


SAMPLES = [
    ('2024_*1111013000100020122*__1.pdf', (.32, .115, .53, .15), '와룡동', '2-122', '1111013000100020122'),
    ('2025_1130510400101060001*', (.13, .143, .32, .208), '우이동', '106-1', '1130510400101060001'),
]


class OcrEngineTests(unittest.TestCase):
    def test_address_cleanup_keeps_lot_not_adjacent_numbers(self):
        examples = {
            '종로구와룡동2-122 번지외4 필': '종로구와룡동 2-122',
            '서\n울특별시강북구우이동\n106-1\n축\n3X\n1:': '서울특별시강북구우이동 106-1',
            '부산광역시 서구 부민동3가 22-220': '부산광역시서구부민동3가 22-220',
            '서울특별시 은평구 진관동 산3—3': '서울특별시은평구진관동 산3-3',
            '인식   불명\n문자': '인식 불명 문자',
            '': '',
        }
        for raw, expected in examples.items():
            with self.subTest(raw=raw):
                self.assertEqual(ocr_engine.clean_ocr_address(raw), expected)

    def test_model_hashes_match_pinned_official_models(self):
        expected = {
            'kor.traineddata': 'f888d4038348a0c3d25151e7f452bda0d74ca275b18cab146798bcbb94084fff',
            'eng.traineddata': '8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba',
        }
        for filename, digest in expected.items():
            self.assertEqual(hashlib.sha256((ocr_engine.MODEL_ROOT/filename).read_bytes()).hexdigest(), digest)

    def test_missing_models_and_invalid_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                ocr_engine.recognize_pdf_region('missing.pdf', 0, (0, 0, 1, 1), tmp)
        for bounds in ((0, 0, 0, 1), (0, 0, 1.2, 1), (0, float('nan'), 1, 1)):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                ocr_engine.recognize_pdf_region('missing.pdf', 0, bounds)

    def test_timeout_stops_worker(self):
        job = object.__new__(ocr_engine.OcrJob)
        job.receiver = Mock()
        job.receiver.poll.return_value = False
        job.process = Mock()
        job.process.is_alive.return_value = True
        job.started = time.monotonic()-61
        result = job.poll()
        self.assertIn('제한 시간', result.error)
        job.process.terminate.assert_called_once()
        job.receiver.close.assert_called_once()


class OcrGuiTests(unittest.TestCase):
    setUp = region_tests.RegionGuiTests.setUp
    make_pdf = region_tests.RegionGuiTests.make_pdf
    event = region_tests.RegionGuiTests.event
    select_address = region_tests.RegionGuiTests.select_address

    def select_blank_region(self):
        self.root.address_region = AddressRegion(0, (.8, .8, 1, 1))
        self.root.read_address_region()

    def deliver_result(self):
        if self.root.ocr_poll_id is not None:
            self.root.after_cancel(self.root.ocr_poll_id)
        self.root.poll_region_ocr()

    def test_native_text_does_not_start_ocr_and_f8_forces_it(self):
        with patch('app.OcrJob') as job:
            self.select_address()
            job.assert_not_called()
            self.root.shortcut_force_ocr()
            job.assert_called_once()
            self.assertIsNotNone(self.root.ocr_job)

    def test_manual_edit_cancels_ocr_and_copy_is_blocked(self):
        with patch('app.OcrJob') as job:
            self.select_blank_region()
            with patch.object(self.root, 'validate_before_copy') as validate:
                self.root.copy_file()
                validate.assert_not_called()
            with patch.object(self.root, 'copy_file') as copy:
                self.root.shortcut_copy(None)
                copy.assert_not_called()
            self.root.address_query.set('manual address')
            self.assertIsNone(self.root.ocr_job)
            job.return_value.cancel.assert_called_once()

    def test_escape_and_file_change_cancel_pending_ocr(self):
        with patch('app.OcrJob') as job:
            self.select_blank_region()
            self.root.shortcut_escape()
            self.assertIsNone(self.root.ocr_job)
            self.select_blank_region()
            self.root.address_region = None
            self.root.load_pdf(self.pdf)
            self.assertIsNone(self.root.ocr_job)
            self.assertEqual(self.root.address_query.get(), '')
            self.assertEqual(job.return_value.cancel.call_count, 2)

    def test_stale_result_is_discarded(self):
        with patch('app.OcrJob') as job:
            self.select_blank_region()
            job.return_value.poll.return_value = ocr_engine.OcrResult(text='종로구와룡동 2-122')
            self.root.address_region = AddressRegion(0, (.1, .1, .5, .5))
            self.deliver_result()
            self.assertEqual(self.root.address_query.get(), '')

    def test_ocr_error_and_empty_result_keep_existing_input(self):
        for result in (ocr_engine.OcrResult(error='missing model'), ocr_engine.OcrResult()):
            with self.subTest(result=result), patch('app.OcrJob') as job:
                self.root.address_query.set('manual')
                self.select_blank_region()
                job.return_value.poll.return_value = result
                self.deliver_result()
                self.assertEqual(self.root.address_query.get(), 'manual')
                self.assertIsNone(self.root.ocr_job)

    def wait_for_ocr(self):
        deadline = time.monotonic()+20
        heartbeats=[]
        self.root.after(1, lambda: heartbeats.append(True))
        while self.root.ocr_job is not None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertIsNone(self.root.ocr_job, self.root.region_status.get())
        self.assertTrue(heartbeats, 'Tk event loop must remain responsive during OCR')

    def test_actual_two_sample_pdfs_ocr_search_and_pnu(self):
        reports=[]
        source_folder = Path(self.tmp.name)/'inputs'
        source_folder.mkdir()
        output_folder = Path(self.tmp.name)/'outputs'
        output_folder.mkdir()
        self.root.source_folder.set(str(source_folder))
        self.root.output_folder.set(str(output_folder))
        self.root.auto_next.set(False)
        for pattern, bounds, dong, lot, pnu in SAMPLES:
            paths=list(Path('test_files').glob(pattern))
            if not paths:
                self.skipTest('Local sample PDFs are not present')
            path=paths[0].resolve()
            original_digest=hashlib.sha256(path.read_bytes()).hexdigest()
            staged = source_folder/path.name
            shutil.copyfile(path, staged)
            self.root.refresh_source_list(load_state=False)
            self.root.address_region = None
            self.root.source_file.set(str(staged))
            self.root.load_pdf(staged)
            self.assertEqual(self.root.region_page().get_text().strip(), '')
            self.root.new_address_region()
            self.root.on_region_start(self.event(bounds[0], bounds[1]))
            self.root.on_region_end(self.event(bounds[2], bounds[3]))
            self.assertIsNotNone(self.root.ocr_job)
            self.wait_for_ocr()
            self.assertIn(dong, self.root.address_query.get())
            self.assertIn(lot, self.root.address_query.get())
            self.assertEqual(self.root.pnu_preview.get(), pnu)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original_digest)
            report = dict(file=path.name, bounds=self.root.address_region.bounds, raw_text=self.root.last_ocr_text,
                                address=self.root.address_query.get(), pnu=self.root.pnu_preview.get(),
                                status=self.root.region_status.get(), source_unchanged=True)
            self.root.copy_file()
            outputs = list(output_folder.glob(f'*{pnu}*.pdf'))
            self.assertEqual(len(outputs), 1)
            self.assertEqual(hashlib.sha256(outputs[0].read_bytes()).hexdigest(), original_digest)
            report['copy_verified'] = True
            report['copied_filename'] = outputs[0].name
            reports.append(report)
        Path('build').mkdir(exist_ok=True)
        Path('build/ocr-actual-results.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')

    def test_actual_worker_cancel_does_not_overwrite_query(self):
        self.select_blank_region()
        job = self.root.ocr_job
        self.assertIsNotNone(job)
        self.root.shortcut_escape()
        self.assertFalse(job.process.is_alive())
        self.root.address_query.set('manual')
        self.root.update()
        self.assertEqual(self.root.address_query.get(), 'manual')


if __name__ == '__main__':
    unittest.main()
