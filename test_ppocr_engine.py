import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ppocr_engine
from pdf_pipeline import survey_key


@unittest.skipUnless(importlib.util.find_spec('onnxruntime') and importlib.util.find_spec('pymupdf'),
                     'Requires ONNX Runtime and PyMuPDF')
class PpocrEngineTests(unittest.TestCase):
    def test_cpu_model_and_dictionary_are_packaged_and_reused(self):
        session, characters=ppocr_engine.load_recognizer()
        self.assertEqual(session.get_providers(),['CPUExecutionProvider'])
        self.assertGreater(len(characters),10000)
        self.assertIs(ppocr_engine.load_recognizer()[0],session)

    def test_ctc_keeps_repeats_separated_by_blank_and_composes_hangul(self):
        import numpy as np
        tokens=[1,1,0,1,2,2,0,3,4]
        probabilities=np.eye(5,dtype=np.float32)[tokens]
        self.assertEqual(ppocr_engine.decode_ctc(probabilities,['','구',' ','지','동']),'구구 지동')

    def test_blank_header_does_not_invent_an_address(self):
        import pymupdf
        with pymupdf.open() as doc:
            page=doc.new_page(width=600,height=100)
            self.assertEqual(ppocr_engine.recognize_header(page.get_pixmap()),'')

    def test_hidden_wrong_text_cannot_replace_visible_address(self):
        import pymupdf
        from survey_smoke import make_fixture, HEADER_REGION, LOT_REGION
        from survey_ocr import read_survey
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'visible.pdf'
            hidden=Path(folder)/'hidden.pdf'
            make_fixture(source)
            with pymupdf.open(source) as doc:
                doc[0].insert_text((40,55),'WRONG HIDDEN ADDRESS',render_mode=3)
                doc.save(hidden)
            with patch('pdf_region.extract_region_text',side_effect=AssertionError('Must read header image')):
                result=read_survey(hidden,HEADER_REGION,LOT_REGION)
            self.assertIn('수원시팔달구지동',''.join(result.header.split()))
            self.assertNotIn('WRONG',result.header)
            self.assertEqual([r.lot for r in result.candidates],['301-6','301-37','301-45'])
            self.assertEqual(survey_key(hidden,HEADER_REGION,LOT_REGION)[-1],ppocr_engine.HEADER_ENGINE)

    def test_missing_or_corrupt_models_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as folder:
            model=Path(folder)/'missing.onnx'
            ppocr_engine.load_recognizer.cache_clear()
            self.addCleanup(ppocr_engine.load_recognizer.cache_clear)
            with patch('ppocr_engine.MODEL_PATH',model):
                with self.assertRaises(FileNotFoundError):
                    ppocr_engine.load_recognizer()
                model.write_bytes(b'corrupt')
                with self.assertRaises(ValueError):
                    ppocr_engine.load_recognizer()


if __name__=='__main__':
    unittest.main()
