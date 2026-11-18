"""Selection regressions, with desktop event checks on Windows."""
import hashlib
import os
import sys
import tempfile
import tkinter as tk
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

import app
from pdf_hash import PdfHashScanner
from pdf_pipeline import file_identity
from workspace_index import SourceIndex


class SourceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.files = [self.folder / f'{i:02}.pdf' for i in range(10)]
        for path in self.files:
            path.write_bytes(b'PDF')
        self.selected = [5]
        self.rows = [p.name for p in self.files]
        self.listbox = Mock()
        self.listbox.curselection.side_effect = lambda: tuple(self.selected)
        self.listbox.selection_clear.side_effect = lambda *_: self.selected.clear()
        self.listbox.selection_set.side_effect = lambda i: self.selected.append(i)
        self.listbox.nearest.return_value = 3
        self.listbox.yview.return_value = (.3, .6)
        self.listbox.xview.return_value = (.1, .8)

        def delete(first, last=None):
            if last == tk.END:
                self.rows.clear()
                self.selected.clear()
            else:
                self.rows.pop(first)
                self.selected[:] = [i - (i > first) for i in self.selected if i != first]

        def insert(index, text):
            self.rows.insert(len(self.rows) if index == tk.END else index, text)

        self.listbox.delete.side_effect = delete
        self.listbox.insert.side_effect = insert
        self.ui = SimpleNamespace(
            source_mouse_path=None, copy_busy=False, source_list=self.listbox,
            source_file=Mock(), source_folder=Mock(),
            source_pdf_files=list(self.files), processed_sources=set(),
            source_count_text=Mock(), pdf_state=app.PdfRenderState(path=self.files[5]),
            load_pdf=Mock(), auto_save_work_state=Mock(),
            cancel_ocr=Mock(), cancel_region_keyboard=Mock(), cancel_region_drag=Mock(),
            address_query=Mock(), current_suggestions=[], suggestion_list=Mock(),
            selected_dong=None, selected_info=Mock(), update_preview=Mock(),
            clear_pdf_viewer=Mock(), refresh_output_list=Mock(),
            performance=Mock(), operation_status=Mock(),
            source_path_at_pointer=Mock(return_value=self.files[2]),
        )
        self.ui.source_file.get.return_value = str(self.files[5])
        self.ui.source_folder.get.return_value = str(self.folder)
        self.ui.source_index = SourceIndex(self.files, self.ui.processed_sources)
        for name in ('select_source_file_in_list', 'select_source_pdf',
                     'on_source_mouse_press', 'on_source_mouse_release',
                     'on_source_list_select', 'refresh_source_list',
                     'update_source_counts', 'update_processed_source_row',
                     'mark_source_processed', 'complete_source_without_copy',
                     'shortcut_refresh_lists'):
            setattr(self.ui, name, MethodType(getattr(app.FileMakerApp, name), self.ui))

    def test_click_opens_only_on_release_and_only_once(self):
        event = SimpleNamespace(x=4, y=4)
        self.ui.on_source_mouse_press(event)
        self.ui.on_source_list_select()
        self.ui.load_pdf.assert_not_called()
        self.ui.on_source_mouse_release(event)
        self.ui.load_pdf.assert_called_once_with(self.files[2])
        self.ui.on_source_mouse_release(event)
        self.ui.load_pdf.assert_called_once()

    def test_drag_to_other_row_cancels_instead_of_opening_wrong_file(self):
        self.ui.on_source_mouse_press(None)
        self.ui.source_path_at_pointer.return_value = self.files[3]
        self.ui.on_source_mouse_release(None)
        self.ui.load_pdf.assert_not_called()
        self.assertEqual(self.selected, [5])

    def test_refresh_between_press_and_release_cancels_click(self):
        self.ui.on_source_mouse_press(None)
        self.ui.refresh_source_list(load_state=False)
        self.ui.on_source_mouse_release(None)
        self.ui.load_pdf.assert_not_called()
        self.assertEqual(self.selected, [5])

    def test_release_without_press_after_busy_does_not_select(self):
        self.ui.copy_busy = True
        self.ui.on_source_mouse_press(None)
        self.ui.copy_busy = False
        self.ui.on_source_mouse_release(None)
        self.ui.load_pdf.assert_not_called()

    def test_same_file_selection_does_not_reload_or_reset_inputs(self):
        self.ui.select_source_pdf()
        self.ui.load_pdf.assert_not_called()
        self.ui.source_file.set.assert_not_called()

    def test_refresh_preserves_selection_and_visible_top_file_when_order_changes(self):
        earlier = self.folder / '00-before.pdf'
        earlier.write_bytes(b'PDF')
        self.ui.refresh_source_list(load_state=False)
        self.assertEqual(self.ui.source_pdf_files[self.selected[0]], self.files[5])
        self.listbox.yview.assert_called_with(self.ui.source_pdf_files.index(self.files[3]))
        self.listbox.xview_moveto.assert_called_with(.1)
        self.listbox.see.assert_not_called()
        self.ui.load_pdf.assert_not_called()

    def test_refresh_deleted_current_file_clears_viewer_and_input(self):
        self.files[5].unlink()
        self.ui.refresh_source_list(load_state=False)
        self.ui.source_file.set.assert_called_with('')
        self.ui.clear_pdf_viewer.assert_called_once()
        self.ui.cancel_ocr.assert_called_once()
        self.ui.address_query.set.assert_called_with('')
        self.assertEqual(self.selected, [])

    def test_copy_updates_only_completed_row_and_preserves_selection(self):
        self.ui.source_index.mark(self.files[5], self.ui.processed_sources)
        self.ui.update_processed_source_row(self.files[5])
        self.listbox.delete.assert_called_once_with(5)
        self.assertEqual(self.selected, [5])
        self.assertEqual(self.rows[5], app.mark_processed_name(self.files[5], self.ui.processed_sources))
        self.assertEqual(self.rows[4], self.files[4].name)
        self.listbox.yview_moveto.assert_called_with(.3)
        self.listbox.see.assert_not_called()

    def test_manual_completion_marks_only_the_open_pdf_and_saves_state(self):
        self.selected[:] = [2]
        self.ui.complete_source_without_copy()
        self.assertEqual(self.ui.processed_sources, {self.files[5].resolve()})
        self.assertEqual(self.rows[5], f'[완료] {self.files[5].name}')
        self.assertEqual(self.rows[2], self.files[2].name)
        self.assertEqual(self.selected, [5])
        self.ui.auto_save_work_state.assert_called_once()
        self.ui.load_pdf.assert_not_called()

    def test_duplicate_lookup_compares_bytes_and_excludes_the_open_pdf(self):
        source, duplicate, different, missing = self.files[:4]
        different.write_bytes(b'pdf')
        missing.unlink()
        scanner = PdfHashScanner(workers=2)
        scanner.cold_parallel_bytes = 0
        self.addCleanup(scanner.close, wait=True)
        identity = file_identity(source)
        digest = Future()
        paths = [source, duplicate, different, missing]
        self.assertEqual(scanner.submit(source, identity, paths, digest).result(15), ([duplicate], 1))
        self.assertIsNotNone(scanner.pool)
        self.assertEqual(digest.result(), hashlib.sha256(b'PDF').hexdigest())
        with patch.object(scanner, '_compute', return_value=iter(())) as compute:
            self.assertEqual(scanner.submit(source, identity, paths, Future()).result(15), ([duplicate], 1))
            self.assertEqual(compute.call_args.args[0], {})
        original = duplicate.stat()
        replacement = self.folder / 'replacement.pdf'
        replacement.write_bytes(b'new')
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        replacement.replace(duplicate)
        self.assertEqual(scanner.submit(source, identity, paths, Future()).result(15), ([], 1))
        source.write_bytes(b'changed PDF')
        with self.assertRaises(OSError):
            scanner.submit(source, identity, paths, Future()).result(15)

    def test_stale_hash_result_does_not_change_the_current_alert(self):
        old = Future()
        old.set_result(([self.files[0]], 0))
        current = Future()
        ui = SimpleNamespace(hash_job=current, hash_notice=Mock(), show_matching_pdfs=Mock())
        app.FileMakerApp.poll_source_hash(ui, old, Future(), True)
        ui.hash_notice.pack.assert_not_called()
        ui.show_matching_pdfs.assert_not_called()

    def test_current_hash_match_shows_the_alert_without_opening_a_window(self):
        job = Future()
        job.set_result(([self.files[0]], 0))
        digest = Future()
        digest.set_result(hashlib.sha256(b'PDF').hexdigest())
        ui = SimpleNamespace(
            hash_job=job, copy_busy=False, hash_match_button=Mock(), hash_notice=Mock(),
            hash_notice_label=Mock(), source_hash_text=Mock(), open_hash_notice=Mock(),
        )
        app.FileMakerApp.poll_source_hash(ui, job, digest, False)
        ui.hash_notice_label.configure.assert_called_once_with(text="같은 해시 파일 1개")
        ui.hash_notice.pack.assert_called_once()
        ui.open_hash_notice.assert_not_called()


@unittest.skipUnless(sys.platform == 'win32', 'Windows Tk event regression checks')
class SourceSelectionGuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = app.FileMakerApp()
        self.addCleanup(self.root.destroy)
        for i in range(40):
            (Path(self.tmp.name) / f'{i:02}.pdf').write_bytes(b'PDF placeholder')
        self.root.source_folder.set(self.tmp.name)
        self.root.refresh_source_list(load_state=False)
        self.root.update()

        def load(path):
            self.root.pdf_state = app.PdfRenderState(path=path)

        self.root.load_pdf = Mock(side_effect=load)

    def click_event(self, sequence, index):
        x, y, width, height = self.root.source_list.bbox(index)
        self.root.source_list.event_generate(sequence, x=x+2, y=y+height//2)
        self.root.update()

    def test_real_mouse_click_drag_and_repeated_click(self):
        self.click_event('<ButtonPress-1>', 2)
        self.root.load_pdf.assert_not_called()
        self.click_event('<B1-Motion>', 3)
        self.click_event('<ButtonRelease-1>', 3)
        self.root.load_pdf.assert_not_called()
        self.click_event('<ButtonPress-1>', 2)
        self.click_event('<ButtonRelease-1>', 2)
        self.root.load_pdf.assert_called_once_with(self.root.source_pdf_files[2])
        self.click_event('<ButtonPress-1>', 2)
        self.click_event('<ButtonRelease-1>', 2)
        self.root.load_pdf.assert_called_once()

    def test_refresh_button_and_f5_preserve_view_and_selection(self):
        current = self.root.source_pdf_files[18]
        self.root.select_source_file_in_list(current)
        self.root.select_source_pdf()
        self.root.source_list.yview(14)
        self.root.update()
        before = self.root.source_list.yview()
        refresh = next(w for w in self.root.app_widgets()
                       if isinstance(w, app.ttk.Button) and w.cget('text') == '새로고침')
        refresh.invoke()
        self.root.update()
        self.assertEqual(self.root.source_list.curselection(), (18,))
        self.assertAlmostEqual(self.root.source_list.yview()[0], before[0])
        self.root.source_list.focus_force()
        self.root.update()
        self.root.source_list.event_generate('<F5>')
        self.root.update()
        self.assertEqual(self.root.source_list.curselection(), (18,))
        self.assertAlmostEqual(self.root.source_list.yview()[0], before[0])
        self.root.load_pdf.assert_called_once()

    def test_manual_click_after_auto_next_opens_clicked_file(self):
        source = self.root.source_pdf_files[0]
        self.root.select_source_file_in_list(source)
        self.root.select_source_pdf()
        self.root.mark_source_processed(source)
        self.root.update_processed_source_row(source)
        self.root.open_next_unprocessed(source)
        self.root.update()
        self.assertEqual(self.root.source_file.get(), str(self.root.source_pdf_files[1]))
        self.click_event('<ButtonPress-1>', 7)
        self.assertEqual(self.root.source_file.get(), str(self.root.source_pdf_files[1]))
        self.click_event('<ButtonRelease-1>', 7)
        self.assertEqual(self.root.source_file.get(), str(self.root.source_pdf_files[7]))
        self.assertEqual(self.root.source_list.curselection(), (7,))
        self.root.load_pdf.assert_called_with(self.root.source_pdf_files[7])


if __name__ == '__main__':
    unittest.main()
