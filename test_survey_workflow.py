from pathlib import Path
import tempfile
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app
from pdf_pipeline import file_identity
from survey_batch import BatchItem, BatchOutcome, BatchResult
from survey_ui import SurveyWorkflow, SurveyRow


class SurveyWorkflowTests(unittest.TestCase):
    def finish_ui(self):
        ui=SimpleNamespace(copy_source=Path('/source/example.pdf'),survey_batch_context='a'*64,
            survey_ledger={},survey_batches={'a'*64:{}},defer_state_save=False,
            copy_auto_next=True,close_after_copy=False,copy_busy=True,copy_future=object(),
            survey_batch=True,survey_auto_pending=False,copy_started=0,mark_source_processed=Mock(),update_processed_source_row=Mock(),
            operation_status=Mock(),open_next_unprocessed=Mock(),auto_save_work_state=Mock(),
            set_copy_busy_ui=Mock(),performance=Mock(),refresh_survey_rows=Mock(),refresh_output_list=Mock(),
            last_copy_destination=None,destroy=Mock())
        ui.finish=MethodType(SurveyWorkflow.finish_survey_batch,ui)
        return ui

    def outcomes(self,failed=False):
        results=[]
        for lot in ('301-6','301-37','301-45'):
            plan=app.DestinationPlan(Path('/output')/f'{lot}.pdf')
            item=BatchItem('a'*64+':'+lot,lot,lot,plan)
            status='failed' if failed and lot=='301-37' else 'copied'
            results.append(BatchOutcome(item,plan,status,'test error' if status=='failed' else ''))
        return tuple(results)

    def test_only_all_success_marks_source_and_moves_next(self):
        ui=self.finish_ui()
        ledger={o.item.key:dict(status=o.status) for o in self.outcomes()}
        ui.finish(BatchResult(self.outcomes(),ledger,expected=3))
        ui.mark_source_processed.assert_called_once_with(ui.copy_source)
        ui.open_next_unprocessed.assert_called_once_with(ui.copy_source)
        ui.auto_save_work_state.assert_called_once()
        self.assertEqual(ui.survey_ledger,{})
        self.assertEqual(ui.survey_batches,{})
        self.assertFalse(ui.copy_busy)

    def test_partial_failure_preserves_journal_and_stays_on_current_source(self):
        ui=self.finish_ui()
        outcomes=self.outcomes(failed=True)
        ledger={o.item.key:dict(status=o.status) for o in outcomes}
        with patch('survey_ui.messagebox.showerror') as error:
            ui.finish(BatchResult(outcomes,ledger,expected=3))
        error.assert_called_once()
        ui.mark_source_processed.assert_not_called()
        ui.open_next_unprocessed.assert_not_called()
        self.assertEqual(ui.survey_ledger,ledger)
        self.assertTrue(ui.survey_batches)
        self.assertFalse(ui.copy_busy)

    def test_review_required_before_batch_starts(self):
        ui=SimpleNamespace(copy_busy=False,survey_reading=False,survey_drag=None,survey_edit_target=None,
                           survey_rows=[SurveyRow('301-6')],survey_reviewed=Mock(),survey_status=Mock())
        ui.survey_reviewed.get.return_value=False
        SurveyWorkflow.copy_survey_files(ui)
        ui.survey_status.set.assert_called_once()

    def test_stale_result_never_changes_address(self):
        ui=SimpleNamespace(is_survey=lambda:True,survey_request=None,load_generation=2,
                           survey_revision=3,address_query=Mock())
        SurveyWorkflow.handle_survey_result(ui,('survey','old'),Mock(),'')
        ui.address_query.set.assert_not_called()

    def test_auto_read_waits_for_copy_cleanup_and_respects_manual_input(self):
        ui=SimpleNamespace(is_survey=lambda:True,survey_auto_pending=True,copy_busy=True,
                           region_page=lambda:object(),address_query=Mock(),survey_auto_query='',
                           read_survey_regions=Mock())
        SurveyWorkflow.maybe_read_survey(ui)
        self.assertTrue(ui.survey_auto_pending)
        ui.copy_busy=False
        ui.address_query.get.return_value='manual input'
        SurveyWorkflow.maybe_read_survey(ui)
        ui.read_survey_regions.assert_not_called()
        ui.survey_auto_pending=True
        ui.address_query.get.return_value=''
        SurveyWorkflow.maybe_read_survey(ui)
        ui.read_survey_regions.assert_called_once()

    def test_invalid_state_drafts_are_ignored(self):
        ui=SimpleNamespace()
        SurveyWorkflow.restore_survey_state(ui,{'survey_regions':{'header':[0,[0,0,0,0]]},
            'survey_progress':{'invalid':{}},'survey_batches':{'a'*64:{'identity':['x',1,2],
            'year':'2025','code':'4111513900','output':'/output','updated':1,
            'rows':[{'lot':123,'enabled':True}]}}})
        self.assertEqual(ui.survey_ledger,{})
        self.assertEqual(ui.survey_batches,{})

    def test_manual_corrected_draft_and_legal_code_are_restored(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.pdf'
            source.write_bytes(b'PDF')
            identity=file_identity(source)
            dong=app.LegalDong('4111513900','경기도 수원시 팔달구 지동','경기도수원시팔달구지동',())
            ui=SimpleNamespace(survey_batches={'a'*64:dict(identity=list(identity),year='2025',
                output=folder,updated=1,code=dong.code,rows=[dict(lot='301-37',enabled=True),dict(lot='301-45',enabled=False)])},
                survey_identity=identity,year=Mock(),output_folder=Mock(),legal_dongs=[dong],
                survey_rows=[SurveyRow('301-6')],address_query=Mock(),refresh_suggestions=Mock(),selected_info=Mock())
            ui.year.get.return_value='2025'
            ui.output_folder.get.return_value=folder
            self.assertTrue(SurveyWorkflow.restore_survey_draft(ui))
            self.assertEqual([(r.lot,r.enabled) for r in ui.survey_rows],[('301-37',True),('301-45',False)])
            self.assertEqual(ui.selected_dong.code,'4111513900')


if __name__=='__main__':
    unittest.main()
