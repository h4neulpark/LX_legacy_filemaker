import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import test_pdf_region as region_tests


class KeyboardWorkflowTests(unittest.TestCase):
    setUp = region_tests.RegionGuiTests.setUp
    make_pdf = region_tests.RegionGuiTests.make_pdf
    event = region_tests.RegionGuiTests.event
    select_address = region_tests.RegionGuiTests.select_address

    def focus(self, widget):
        self.root.deiconify()
        self.root.update()
        widget.focus_force()
        self.root.update()

    def press(self, sequence):
        self.root.focus_get().event_generate(sequence)
        self.root.update()

    def workspace(self):
        self.make_pdf('two.pdf', '서울특별시 종로구 이화동 1-4', pages=2)
        self.make_pdf('three.pdf', '서울특별시 종로구 세종로 76-4')
        self.root.source_folder.set(self.tmp.name)
        self.root.refresh_source_list(load_state=False)
        self.root.select_source_file_in_list(self.pdf)
        self.root.select_source_pdf()

    def test_control_enter_has_priority_and_never_searches(self):
        widgets = [self.root.source_list, self.root.address_entry, self.root.suggestion_list,
                   self.root.output_list, self.root.pdf_canvas]
        widgets += [w for w in self.root.app_widgets() if w.winfo_class() in ('TButton', 'TCombobox', 'TCheckbutton')]
        with patch.object(self.root, 'copy_file') as copy, patch.object(self.root, 'refresh_suggestions') as search:
            for widget in widgets:
                with self.subTest(widget=str(widget)):
                    self.focus(widget)
                    self.press('<Control-Return>')
            self.assertEqual(copy.call_count, len(widgets))
            search.assert_not_called()

    def test_address_shift_arrows_select_text(self):
        self.focus(self.root.address_entry)
        self.root.address_query.set('example address')
        self.root.address_entry.selection_clear()
        self.root.address_entry.icursor(8)
        with patch.object(self.root, 'pan_pdf') as pan:
            self.press('<Shift-Left>')
            self.assertTrue(self.root.address_entry.selection_present())
            pan.assert_not_called()

    def test_source_enter_preserves_manual_address(self):
        self.workspace()
        self.focus(self.root.source_list)
        self.root.address_query.set('manual edit')
        with patch.object(self.root, 'load_pdf') as load:
            self.press('<Return>')
            load.assert_not_called()
        self.assertEqual(self.root.address_query.get(), 'manual edit')

    def test_file_navigation_and_page_navigation_are_separate(self):
        self.workspace()
        self.focus(self.root.address_entry)
        self.press('<Control-Next>')
        self.assertEqual(Path(self.root.source_file.get()).name, 'three.pdf')
        self.press('<Control-Next>')
        self.assertEqual(Path(self.root.source_file.get()).name, 'two.pdf')
        self.assertEqual(self.root.pdf_state.page_index, 0)
        self.press('<Next>')
        self.assertEqual(self.root.pdf_state.page_index, 1)
        self.press('<Control-Prior>')
        self.assertEqual(Path(self.root.source_file.get()).name, 'three.pdf')
        self.press('<Control-Prior>')
        self.press('<Control-Prior>')
        self.assertEqual(Path(self.root.source_file.get()).name, 'one.pdf')

    def test_keyboard_create_move_resize_cancel_and_commit(self):
        self.focus(self.root.address_entry)
        self.press('<Alt-5>')
        self.assertTrue(self.root.region_keyboard_edit)
        self.assertIs(self.root.focus_get(), self.root.pdf_canvas)
        initial = self.root.address_region.bounds
        self.press('<Right>')
        self.assertAlmostEqual(self.root.address_region.bounds[0], initial[0]+2/600)
        moved = self.root.address_region.bounds
        self.press('<Control-Shift-Down>')
        self.assertAlmostEqual(self.root.address_region.bounds[3], moved[3]+.5/800)
        self.press('<Escape>')
        self.assertIsNone(self.root.address_region)
        self.assertIs(self.root.focus_get(), self.root.address_entry)
        self.select_address()
        original = self.root.address_region
        self.focus(self.root.source_list)
        self.press('<Alt-5>')
        self.press('<Right>')
        self.press('<Return>')
        self.assertFalse(self.root.region_keyboard_edit)
        self.assertNotEqual(self.root.address_region, original)
        self.assertIn('세종로', self.root.address_query.get())
        self.assertIs(self.root.focus_get(), self.root.source_list)

    def test_edit_blocks_copy_cancels_on_focus_change_and_preserves_zoom(self):
        self.select_address()
        self.focus(self.root.source_list)
        self.press('<Alt-5>')
        original = self.root.address_region
        self.press('<Right>')
        moved = self.root.address_region
        with patch.object(self.root, 'copy_file') as copy:
            self.press('<Control-Return>')
            copy.assert_not_called()
        self.press('<Control-equal>')
        self.assertTrue(self.root.region_keyboard_edit)
        self.assertEqual(self.root.address_region, moved)
        self.press('<Alt-2>')
        self.assertFalse(self.root.region_keyboard_edit)
        self.assertEqual(self.root.address_region, original)

    def test_read_region_shortcut_and_pdf_arrow_scroll(self):
        self.select_address()
        self.focus(self.root.address_entry)
        self.root.address_query.set('manual')
        self.press('<Control-r>')
        self.assertIn('세종로', self.root.address_query.get())
        self.focus(self.root.pdf_canvas)
        with patch.object(self.root, 'pan_pdf') as pan:
            self.press('<Down>')
            pan.assert_called_once_with(dx_units=0, dy_units=3)

    def test_copy_auto_next_skips_processed_wraps_and_finishes(self):
        self.workspace()
        self.select_address()
        output = Path(self.tmp.name) / 'output'
        output.mkdir()
        self.root.output_folder.set(str(output))
        three = Path(self.tmp.name) / 'three.pdf'
        self.root.processed_sources.add(three.resolve())
        with patch('app.messagebox.showinfo') as success:
            self.root.copy_file()
            self.assertEqual(Path(self.root.source_file.get()).name, 'two.pdf')
            self.assertEqual(len(list(output.glob('*.pdf'))), 1)
            self.root.copy_file()
            self.assertEqual(Path(self.root.source_file.get()).name, 'two.pdf')
            self.assertIn('모든 PDF', self.root.operation_status.get())
            success.assert_not_called()
        # A pending earlier item is found after reaching the end of the list.
        self.root.processed_sources.discard(self.pdf.resolve())
        self.root.open_next_unprocessed(Path(self.tmp.name) / 'two.pdf')
        self.assertEqual(Path(self.root.source_file.get()).name, 'one.pdf')

    def test_copy_stays_when_option_off_and_failure_does_not_advance(self):
        self.workspace()
        self.select_address()
        output = Path(self.tmp.name) / 'output'
        output.mkdir()
        self.root.output_folder.set(str(output))
        self.root.auto_next.set(False)
        self.root.copy_file()
        self.assertEqual(Path(self.root.source_file.get()).name, 'one.pdf')
        self.root.auto_next.set(True)
        self.root.processed_sources.clear()
        with patch('app.copy_with_destination_plan', side_effect=OSError('test failure')), \
             patch('app.messagebox.showerror') as error, \
             patch.object(self.root, 'confirm_destination_collision', return_value=True):
            self.root.copy_file()
            error.assert_called_once()
        self.assertEqual(Path(self.root.source_file.get()).name, 'one.pdf')
        self.assertFalse(self.root.processed_sources)

    def test_cancelled_collision_does_not_advance(self):
        self.workspace()
        self.select_address()
        output = Path(self.tmp.name) / 'output'
        output.mkdir()
        self.root.output_folder.set(str(output))
        with patch.object(self.root, 'confirm_destination_collision', return_value=False), \
             patch('app.copy_with_destination_plan') as copy:
            self.root.copy_file()
            copy.assert_not_called()
        self.assertFalse(self.root.processed_sources)
        self.assertEqual(Path(self.root.source_file.get()).name, 'one.pdf')

    def test_keyboard_region_clamps_at_page_edges(self):
        from pdf_region import AddressRegion
        self.root.address_region = AddressRegion(0, (.999, .999, 1, 1))
        self.focus(self.root.address_entry)
        self.press('<Alt-5>')
        for key in ('<Right>', '<Down>', '<Shift-Right>', '<Shift-Down>'):
            self.press(key)
            self.assertTrue(all(0 <= coordinate <= 1 for coordinate in self.root.address_region.bounds))
        self.press('<Escape>')

    def test_keyboard_edit_scrolls_region_into_view(self):
        from pdf_region import AddressRegion
        self.focus(self.root.address_entry)
        self.root.set_zoom(2)
        self.root.address_region = AddressRegion(0, (.8, .8, .9, .9))
        self.root.pdf_canvas.xview_moveto(0)
        self.root.pdf_canvas.yview_moveto(0)
        self.press('<Alt-5>')
        self.press('<Down>')
        x0, y0, x1, y1 = self.root.region_canvas_bounds()
        canvas = self.root.pdf_canvas
        self.assertGreaterEqual(x0, canvas.canvasx(0))
        self.assertLessEqual(x1, canvas.canvasx(canvas.winfo_width()))
        self.assertGreaterEqual(y0, canvas.canvasy(0))
        self.assertLessEqual(y1, canvas.canvasy(canvas.winfo_height()))
        self.press('<Escape>')

    def test_tooltip_focus_display_dismissal_and_dialog_isolation(self):
        self.focus(self.root.address_entry)
        tip = next(t for t in self.root.tooltips if t.widget is self.root.address_entry)
        self.assertIn('Shift+', tip.text)
        self.assertIsNotNone(tip.pending)
        tip.hide()
        tip.show()
        self.root.update()
        self.assertIsNotNone(tip.window)
        self.assertIs(self.root.focus_get(), self.root.address_entry)
        self.press('<Control-r>')
        self.assertIsNone(tip.window)
        dialog = tk.Toplevel(self.root)
        try:
            entry = tk.Entry(dialog)
            entry.pack()
            self.focus(entry)
            with patch.object(self.root, 'copy_file') as copy:
                self.press('<Control-Return>')
                copy.assert_not_called()
        finally:
            dialog.destroy()


if __name__ == '__main__':
    unittest.main()
