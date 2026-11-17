"""Exercise copy bookkeeping with real files and lightweight list widgets."""
import tempfile
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

import app
from workspace_index import SourceIndex, OutputIndex


class Value:
    def __init__(self, value=''):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Rows:
    def __init__(self, values=()):
        self.rows = list(values)
        self.selected = None
        self.delete_calls = []

    def delete(self, first, last=None):
        self.delete_calls.append((first, last))
        if last == app.tk.END:
            self.rows.clear()
        else:
            self.rows.pop(first)

    def insert(self, index, value):
        self.rows.insert(len(self.rows) if index == app.tk.END else index, value)

    def selection_clear(self, *args): self.selected = None
    def selection_set(self, index): self.selected = index
    def curselection(self): return () if self.selected is None else (self.selected,)
    def yview(self, *args): return (.2, .8)
    def xview(self): return (0, 1)
    def yview_moveto(self, *args): pass
    def xview_moveto(self, *args): pass
    def see(self, *args): pass
    def activate(self, *args): pass
    def selection_anchor(self, *args): pass
    def focus_set(self): pass


def workspace(folder, total=1300, completed=1000):
    source, output = folder / 'source', folder / 'output'
    source.mkdir()
    output.mkdir()
    files = [source / f'{i:04}.pdf' for i in range(total)]
    for path in files:
        path.write_bytes(b'PDF')
    for i in range(completed):
        (output / f'{i:04}.pdf').write_bytes(b'PDF')
    current = files[max(0, completed - 1)]
    processed = {p.resolve() for p in files[:max(0, completed - 1)]}
    outputs = app.list_output_files(output)
    ui = SimpleNamespace(
        source_folder=Value(str(source)), source_file=Value(str(current)),
        output_folder=Value(str(output)), year=Value('2026'), doc_type=Value('실태조사서'),
        processed_sources=processed, source_pdf_files=files, source_index=SourceIndex(files, processed),
        source_list=Rows(p.name for p in files), source_count_text=Value(),
        output_files=outputs, output_index=OutputIndex(outputs), output_index_folder=output,
        output_list=Rows(p.name for p in outputs), operation_status=Value(), status_text=Value(),
        copy_busy=True, copy_future=None, copy_auto_next=True, close_after_copy=False,
        copy_source=current, copy_started=0, copy_plan=app.DestinationPlan(output / 'new.pdf'),
        defer_state_save=False, restoring_state=False, preview_destination_cache=None,
        performance=app.TimingStats(), pdf_state=app.PdfRenderState(path=current),
        set_copy_busy_ui=Mock(), prepare_copy_list_updates=Mock(), update_preview=Mock(),
        export_survey_state=Mock(return_value={}),
    )

    def load(path):
        ui.pdf_state = app.PdfRenderState(path=path)

    ui.load_pdf = Mock(side_effect=load)
    for name in ('select_source_file_in_list', 'select_source_pdf', 'mark_source_processed',
                 'update_source_counts', 'update_processed_source_row', 'update_output_after_copy',
                 'refresh_output_list', 'current_source_folder_path', 'auto_save_work_state',
                 'open_next_unprocessed', 'finish_copy'):
        setattr(ui, name, MethodType(getattr(app.FileMakerApp, name), ui))
    return ui


class CopyScalingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_large_workspace_copy_saves_once_without_full_output_reload(self):
        ui = workspace(Path(self.tmp.name))
        ui.copy_plan.destination.write_bytes(b'new copy')
        real_save = app.save_work_state
        real_resolve = Path.resolve
        calls = []

        def resolve(path, *args, **kwargs):
            calls.append(path)
            return real_resolve(path, *args, **kwargs)

        with patch('app.save_work_state', wraps=real_save) as save, \
             patch('app.list_output_files', side_effect=AssertionError('Full output scan during copy')), \
             patch.object(Path, 'resolve', resolve):
            ui.finish_copy()
        save.assert_called_once()
        self.assertLess(len(calls), 30, 'Copy bookkeeping must not resolve every source/output path')
        self.assertEqual(ui.source_index.completed, 1000)
        self.assertIn('남은 300개', ui.source_count_text.get())
        self.assertEqual(ui.source_file.get(), str(ui.source_pdf_files[1000]))
        state = app.load_work_state(Path(ui.source_folder.get()))
        self.assertEqual(len(state['processed_files']), 1000)
        self.assertEqual(state['last_selected_pdf'], '1000.pdf')
        self.assertEqual(ui.output_list.rows, [p.name for p in ui.output_index.files])
        self.assertEqual(len(ui.output_list.rows), 1001)
        self.assertFalse(ui.copy_busy)
        self.assertFalse(ui.defer_state_save)
        self.assertTrue(all(last is None for _first, last in ui.output_list.delete_calls))

    def test_collision_renumbering_matches_full_directory_listing(self):
        ui = workspace(Path(self.tmp.name), total=5, completed=1)
        output = Path(ui.output_folder.get())
        for name in ('report.pdf', 'report__1.pdf', 'report__4.pdf'):
            (output / name).write_bytes(name.encode())
        ui.refresh_output_list()
        plan = app.destination_plan_for_stem(output, 'report', '.pdf')
        app.copy_with_destination_plan(ui.copy_source, plan)
        ui.update_output_after_copy(plan)
        actual = app.list_output_files(output)
        self.assertEqual(ui.output_files, actual)
        self.assertEqual(ui.output_list.rows, [p.name for p in actual])
        self.assertEqual(ui.output_list.rows[ui.output_list.selected], plan.destination.name)

    def test_atomic_save_failure_preserves_previous_state_and_cleans_temp(self):
        folder = Path(self.tmp.name)
        app.save_work_state(folder, {'last_selected_pdf': 'old.pdf'})
        with patch('app.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                app.save_work_state(folder, {'last_selected_pdf': 'new.pdf'})
        self.assertEqual(app.load_work_state(folder), {'last_selected_pdf': 'old.pdf'})
        self.assertEqual(list(folder.glob('*.tmp')), [])

    def test_fresh_collision_check_detects_external_file(self):
        folder = Path(self.tmp.name)
        (folder / 'report__90.pdf').mkdir()
        (folder / 'report__2.pdf').write_bytes(b'external copy')
        (folder / 'unrelated.pdf').write_bytes(b'other')
        self.assertEqual(app.destination_plan_for_stem(folder, 'report', '.pdf').destination.name,
                         'report__1.pdf')
        (folder / 'report.pdf').write_bytes(b'new external base')
        plan = app.destination_plan_for_stem(folder, 'report', '.pdf')
        self.assertTrue(plan.needs_confirmation)
        self.assertIn((folder / 'report.pdf', folder / 'report__1.pdf'), plan.renames)

    def test_display_error_still_saves_completed_copy_and_releases_busy_guard(self):
        ui = workspace(Path(self.tmp.name), total=5, completed=1)
        ui.update_output_after_copy = Mock(side_effect=RuntimeError('UI error'))
        with self.assertRaises(RuntimeError):
            ui.finish_copy()
        state = app.load_work_state(Path(ui.source_folder.get()))
        self.assertEqual(state['processed_files'], ['0000.pdf'])
        self.assertFalse(ui.copy_busy)
        self.assertFalse(ui.defer_state_save)
        ui.set_copy_busy_ui.assert_called_once_with(False)


if __name__ == '__main__':
    unittest.main()
