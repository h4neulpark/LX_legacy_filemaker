from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import app
from survey_smoke import make_fixture, HEADER_REGION, LOT_REGION
from survey_batch import file_digest
from pdf_pipeline import render_key, survey_key


@unittest.skipUnless(sys.platform=='win32','Windows Tk survey workflow')
class SurveyGuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source_folder=Path(self.tmp.name)/'input'
        self.output=Path(self.tmp.name)/'output'
        self.source_folder.mkdir()
        self.output.mkdir()
        self.source=self.source_folder/'001.pdf'
        make_fixture(self.source)
        self.root=app.FileMakerApp()
        self.addCleanup(self.root.destroy)
        self.addCleanup(self.root.close_pdf_doc)
        self.root.load_source_workspace(self.source_folder)
        self.root.survey_regions={'header':HEADER_REGION,'lots':LOT_REGION}
        self.root.year.set('2025')
        self.root.output_folder.set(str(self.output))
        self.root.doc_type.set('측량성과도')
        self.root.auto_next.set(False)
        self.root.select_source_file_in_list(self.source)
        self.root.select_source_pdf()
        self.wait(lambda:not self.root.survey_reading and len(self.root.survey_rows)==3)

    def wait(self,condition):
        deadline=time.monotonic()+35
        while not condition() and time.monotonic()<deadline:
            self.root.update()
            time.sleep(.01)
        self.assertTrue(condition(),self.root.survey_status.get()+' / '+self.root.operation_status.get())

    def test_visible_toolbox_and_three_verified_copies(self):
        self.assertTrue(self.root.survey_tools.winfo_ismapped())
        self.assertFalse(self.root.single_preview.winfo_ismapped())
        self.assertEqual(self.root.selected_dong.code,'4111513900')
        self.root.copy_button.invoke()
        self.assertFalse(self.root.copy_busy,'Unreviewed rows must not copy')
        self.root.survey_reviewed.set(True)
        self.root.copy_button.invoke()
        self.assertTrue(self.root.copy_busy)
        self.wait(lambda:not self.root.copy_busy)
        outputs=list(self.output.glob('*.pdf'))
        self.assertEqual(len(outputs),3)
        self.assertTrue(all(file_digest(p)==file_digest(self.source) for p in outputs))
        self.assertIn(self.source.resolve(),self.root.processed_sources)
        self.assertEqual(self.root.survey_ledger,{})

    def test_mode_switch_and_manual_edit_cancel_pending_survey_read(self):
        self.root.doc_type.set('실태조사서')
        self.root.update()
        self.assertFalse(self.root.survey_tools.winfo_ismapped())
        self.assertTrue(self.root.single_preview.winfo_ismapped())
        self.root.doc_type.set('측량성과도')
        self.wait(lambda:not self.root.survey_reading and len(self.root.survey_rows)==3)
        self.root.read_survey_regions(force=True)
        self.root.address_query.set('직접 입력한 주소')
        self.root.update()
        self.assertFalse(self.root.survey_reading)
        self.assertIsNone(self.root.survey_request)
        self.assertEqual(self.root.address_query.get(),'직접 입력한 주소')

    def test_next_file_reads_even_when_render_is_already_cached(self):
        second=self.source_folder/'002.pdf'
        make_fixture(second)
        self.root.refresh_source_list(load_state=False)
        self.root.auto_next.set(True)
        self.root.prefetch_next_source()
        image_key=render_key(second,0,1.0,max(self.root.pdf_canvas.winfo_width()-24,200))
        read_key=survey_key(second,HEADER_REGION,LOT_REGION)
        self.wait(lambda:self.root.pipeline.images.get(image_key) is not None
                  and self.root.pipeline.regions.get(read_key) is not None)
        self.root.survey_reviewed.set(True)
        self.root.copy_button.invoke()
        self.wait(lambda:not self.root.copy_busy and self.root.source_file.get()==str(second)
                  and not self.root.survey_reading and len(self.root.survey_rows)==3)
        self.assertFalse(self.root.survey_reviewed.get())
        self.assertNotIn(second.resolve(),self.root.processed_sources)
        self.root.survey_reviewed.set(True)
        with patch('survey_ui.messagebox.askyesno',return_value=True):
            self.root.copy_button.invoke()
        self.wait(lambda:not self.root.copy_busy)
        self.assertEqual(len(list(self.output.glob('*.pdf'))),6)
        self.assertEqual(self.root.source_index.completed,2)


if __name__=='__main__':
    unittest.main()
