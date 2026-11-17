import importlib.util
from pathlib import Path
import tempfile
import time
import unittest

from pdf_pipeline import PdfPipeline, survey_key
from survey_ocr import read_survey, neutral_mask, DEFAULT_HEADER, DEFAULT_LOTS
from survey_smoke import make_fixture, HEADER_REGION, LOT_REGION


@unittest.skipUnless(importlib.util.find_spec('pymupdf'), 'Requires PyMuPDF')
class SurveyOcrTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_native_and_scanned_documents_keep_only_black_lot_column(self):
        for scanned in (False,True):
            with self.subTest(scanned=scanned):
                path=Path(self.tmp.name)/f'{scanned}.pdf'
                make_fixture(path,scanned)
                result=read_survey(path,HEADER_REGION,LOT_REGION)
                self.assertEqual([row.lot for row in result.candidates],['301-6','301-37','301-45'])
                self.assertGreater(result.red_pixels,0)
                self.assertIn('지동',result.header)
                for row in result.candidates:
                    self.assertTrue(all(0<=v<=1 for v in row.bounds))

    def test_survey_process_and_cached_reuse(self):
        path=Path(self.tmp.name)/'survey.pdf'
        make_fixture(path)
        pipeline=PdfPipeline()
        self.addCleanup(pipeline.close)
        key=survey_key(path,HEADER_REGION,LOT_REGION)
        self.assertIsNone(pipeline.request_survey(key))
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            results=pipeline.poll()
            if results:
                self.assertFalse(results[0][2],results[0][2])
                break
            time.sleep(.01)
        cached=pipeline.request_survey(key)
        self.assertIsNotNone(cached)
        self.assertEqual([row.lot for row in cached.candidates],['301-6','301-37','301-45'])
        self.assertIsNone(pipeline.prepare.key)

    def test_user_sample_black_lots_and_header(self):
        paths=list(Path('test_files').glob('2025_4111511000103010006_*.pdf'))
        if not paths:
            self.skipTest('Local user sample is not uploaded to GitHub')
        result=read_survey(paths[0],DEFAULT_HEADER,DEFAULT_LOTS)
        self.assertEqual([r.lot for r in result.candidates],['301-6','301-37','301-45'])
        self.assertIn('수원시팔달구지동',''.join(result.header.split()))

    def test_missing_page_is_an_actionable_error(self):
        path=Path(self.tmp.name)/'survey.pdf'
        make_fixture(path)
        with self.assertRaises(ValueError):
            read_survey(path,(4,HEADER_REGION[1]),LOT_REGION)

    def test_header_and_lots_on_different_pages(self):
        for scanned in (False, True):
            with self.subTest(scanned=scanned):
                path=Path(self.tmp.name)/f'multipage-{scanned}.pdf'
                make_fixture(path,scanned,split_pages=True)
                result=read_survey(path,HEADER_REGION,(2,LOT_REGION[1]))
                self.assertIn('지동',result.header)
                self.assertEqual([row.lot for row in result.candidates],['301-6','301-37','301-45'])
                self.assertGreater(result.red_pixels,0)

    def test_dark_red_is_removed_even_near_gray_threshold(self):
        import pymupdf
        samples=bytes((0,0,0, 80,50,50, 255,0,0, 180,180,180, 240,240,240))
        pix=pymupdf.Pixmap(pymupdf.csRGB,5,1,samples,False)
        masked,_uncertain,red=neutral_mask(pix)
        self.assertEqual(masked.samples[:3],b'\x00\x00\x00')
        self.assertEqual(masked.samples[3:9],b'\xff'*6)
        self.assertEqual(masked.samples[9:12],bytes((180,180,180)))
        self.assertEqual(red,2)


if __name__=='__main__':
    unittest.main()
