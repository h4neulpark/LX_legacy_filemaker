"""Exercise asynchronous copy orchestration without a desktop session."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace, MethodType
from unittest.mock import Mock, patch

import app


class CopyWorkflowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.source = Path(tmp.name) / 'source.pdf'
        self.source.write_bytes(b'original PDF contents')
        self.output = Path(tmp.name) / 'output'
        self.output.mkdir()
        self.ui = SimpleNamespace(
            copy_busy=False, copy_future=None, copy_poll_id=None,
            close_after_copy=False, ocr_job=None, region_keyboard_edit=False,
            region_drag=None, auto_next=Mock(), year=Mock(), doc_type=Mock(),
            operation_status=Mock(), region_status=Mock(),
            processed_sources=set(), last_copy_destination=None,
            validate_before_copy=Mock(return_value=(self.source, self.output,
                                                   '1111010100100010000', '서울 청운동 1')),
            confirm_destination_collision=Mock(return_value=True),
            set_copy_busy_ui=Mock(), auto_save_work_state=Mock(),
            refresh_source_list=Mock(), restore_source_selection=Mock(),
            update_processed_source_row=Mock(),
            refresh_output_list=Mock(), update_preview=Mock(),
            open_next_unprocessed=Mock(), after=Mock(return_value='poll'),
            destroy=Mock(),
            performance=Mock(),
            defer_state_save=False, prepare_copy_list_updates=Mock(),
            update_output_after_copy=Mock(),
        )
        self.ui.auto_next.get.return_value = True
        self.ui.year.get.return_value = '2026'
        self.ui.doc_type.get.return_value = '실태조사서'
        self.ui.mark_source_processed = lambda path: self.ui.processed_sources.add(path.resolve())
        for name in ('copy_file', 'poll_copy', 'finish_copy'):
            setattr(self.ui, name, MethodType(getattr(app.FileMakerApp, name), self.ui))

    def finish(self):
        # Waiting is confined to tests; the GUI polls without blocking.
        try:
            self.ui.copy_future.result(timeout=3)
        except OSError:
            pass
        self.ui.poll_copy()

    def test_slow_copy_runs_off_ui_thread_blocks_duplicates_and_finishes(self):
        started, release = threading.Event(), threading.Event()
        ui_thread = threading.get_ident()
        real_copy = app.copy_with_destination_plan

        def slow_copy(source, plan):
            self.assertNotEqual(threading.get_ident(), ui_thread)
            started.set()
            if not release.wait(3):
                raise TimeoutError('Test did not release worker')
            real_copy(source, plan)

        with patch('app.copy_with_destination_plan', side_effect=slow_copy) as copy:
            try:
                self.ui.copy_file()
                self.assertTrue(started.wait(2))
                self.assertTrue(self.ui.copy_busy)
                self.ui.set_copy_busy_ui.assert_called_once_with(True)
                self.ui.operation_status.set.assert_called_with('복사 중…')
                self.ui.copy_file()
                self.ui.poll_copy()
                copy.assert_called_once()
                self.ui.validate_before_copy.assert_called_once()
                self.ui.auto_save_work_state.assert_not_called()
                self.assertFalse(self.ui.copy_future.done())
                # Later option changes cannot alter the submitted operation.
                self.ui.auto_next.get.return_value = False
            finally:
                release.set()
                self.finish()
        self.assertFalse(self.ui.copy_busy)
        self.assertIsNone(self.ui.copy_future)
        self.assertEqual(self.ui.last_copy_destination.read_bytes(), self.source.read_bytes())
        self.assertIn(self.source.resolve(), self.ui.processed_sources)
        self.ui.update_processed_source_row.assert_called_once_with(self.source)
        self.ui.refresh_source_list.assert_not_called()
        self.ui.open_next_unprocessed.assert_called_once_with(self.source)
        self.ui.set_copy_busy_ui.assert_called_with(False)

    def test_copy_failure_restores_ui_without_marking_complete(self):
        with patch('app.copy_with_destination_plan', side_effect=OSError('Disk full')), \
             patch('app.messagebox.showerror') as error:
            self.ui.copy_file()
            self.finish()
        error.assert_called_once()
        self.assertIn('Disk full', error.call_args.args[1])
        self.assertFalse(self.ui.copy_busy)
        self.assertFalse(self.ui.processed_sources)
        self.ui.set_copy_busy_ui.assert_called_with(False)
        self.ui.auto_save_work_state.assert_not_called()
        self.ui.open_next_unprocessed.assert_not_called()
        # A failed operation must leave the app able to try again.
        self.ui.copy_file()
        self.finish()
        self.assertIsNotNone(self.ui.last_copy_destination)

    def test_cancelled_collision_never_starts_progress_or_worker(self):
        self.ui.confirm_destination_collision.return_value = False
        with patch('app.copy_with_destination_plan') as copy:
            self.ui.copy_file()
        copy.assert_not_called()
        self.ui.set_copy_busy_ui.assert_not_called()
        self.assertFalse(self.ui.copy_busy)

    def test_worker_start_failure_restores_ui(self):
        with patch('app.ThreadPoolExecutor', side_effect=RuntimeError('Cannot start thread')), \
             patch('app.messagebox.showerror') as error:
            self.ui.copy_file()
        error.assert_called_once()
        self.assertFalse(self.ui.copy_busy)
        self.ui.set_copy_busy_ui.assert_called_with(False)

    def test_close_waits_for_copy_and_saves_before_destroy(self):
        self.ui.copy_file()
        app.FileMakerApp.destroy(self.ui)
        self.assertTrue(self.ui.close_after_copy)
        self.ui.destroy.assert_not_called()
        self.finish()
        self.ui.auto_save_work_state.assert_called_once()
        self.ui.open_next_unprocessed.assert_not_called()
        self.ui.destroy.assert_called_once()

    def test_auto_next_off_keeps_current_file(self):
        self.ui.auto_next.get.return_value = False
        self.ui.copy_file()
        self.finish()
        self.ui.open_next_unprocessed.assert_not_called()
        self.assertFalse(self.ui.copy_busy)


if __name__ == '__main__':
    unittest.main()
