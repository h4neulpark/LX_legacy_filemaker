import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace

import pymupdf

import app
from pdf_region import AddressRegion, canvas_to_relative, extract_region_text, move_bounds, normalized_bounds


class RegionGeometryTests(unittest.TestCase):
    def test_reverse_drag_and_page_clamping(self):
        self.assertEqual(normalized_bounds((1.2, .8), (-.1, .2)), (0, .2, 1, .8))
        self.assertEqual(move_bounds((.2, .3, .5, .6), 2, -2), (.7, 0, 1, .3))

    def test_zoom_offset_and_scrolled_canvas_coordinates(self):
        rect = pymupdf.Rect(0, 0, 600, 800)
        self.assertEqual(canvas_to_relative(610, 810, rect, 2), (.5, .5))
        self.assertEqual(canvas_to_relative(-20, 3000, rect, 2), (0, 1))

    def test_extraction_rotations_and_cropbox(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation), pymupdf.open() as doc:
                page = doc.new_page(width=600, height=800)
                page.insert_text((100, 150), 'inside address')
                page.insert_text((400, 500), 'outside text')
                page.set_cropbox(pymupdf.Rect(40, 40, 560, 760))
                word_rect = pymupdf.Rect(50, 85, 250, 120)
                page.set_rotation(rotation)
                displayed = word_rect * page.rotation_matrix
                bounds = (displayed.x0/page.rect.width, displayed.y0/page.rect.height,
                          displayed.x1/page.rect.width, displayed.y1/page.rect.height)
                self.assertEqual(extract_region_text(page, bounds), 'inside address')

    def test_multiline_whitespace_and_blank_area(self):
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((50, 60), 'first   line\nsecond line')
            self.assertEqual(extract_region_text(page, (0, 0, .8, .2)), 'first line second line')
            self.assertEqual(extract_region_text(page, (.8, .8, 1, 1)), '')


class RegionGuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = app.FileMakerApp()
        self.addCleanup(self.root.destroy)
        self.addCleanup(self.root.close_pdf_doc)
        self.root.update()
        self.root.withdraw()
        self.pdf = self.make_pdf('one.pdf', '서울특별시 종로구 세종로 76-2')
        self.root.load_pdf(self.pdf)

    def make_pdf(self, name, text='', pages=1, width=600, height=800):
        path = Path(self.tmp.name) / name
        with pymupdf.open() as doc:
            for _ in range(pages):
                page = doc.new_page(width=width, height=height)
                if text:
                    page.insert_text((60, 100), text, fontname='korea', fontsize=12)
            doc.save(path)
        return path

    def event(self, relative_x, relative_y):
        page = self.root.region_page()
        x = 10 + relative_x * page.rect.width * self.root.render_zoom
        y = 10 + relative_y * page.rect.height * self.root.render_zoom
        return SimpleNamespace(x=x-self.root.pdf_canvas.canvasx(0),
                               y=y-self.root.pdf_canvas.canvasy(0))

    def select_address(self):
        self.root.on_region_start(self.event(.07, .09))
        self.root.on_region_end(self.event(.85, .15))

    def test_drag_search_and_next_file_automatic_extraction(self):
        self.select_address()
        self.assertIn('세종로', self.root.address_query.get())
        self.assertIsNotNone(self.root.selected_dong)
        self.assertEqual(self.root.selected_dong.code, '1111011900')
        region = self.root.address_region
        other = self.make_pdf('two.pdf', '서울특별시 종로구 이화동 1-4')
        self.root.load_pdf(other)
        self.assertEqual(self.root.address_region, region)
        self.assertIn('이화동', self.root.address_query.get())
        self.assertEqual(self.root.selected_dong.code, '1111016500')

    def test_move_resize_cancel_and_clear(self):
        self.select_address()
        original = self.root.address_region
        self.root.on_region_start(self.event(.4, .12))
        self.root.on_region_move(self.event(.42, .14))
        self.assertNotEqual(self.root.address_region, original)
        self.root.cancel_region_drag()
        self.assertEqual(self.root.address_region, original)
        self.root.on_region_start(self.event(.85, .15))
        self.root.on_region_end(self.event(.9, .18))
        self.assertAlmostEqual(self.root.address_region.bounds[2], .9, delta=.002)
        self.assertAlmostEqual(self.root.address_region.bounds[3], .18, delta=.002)
        text = self.root.address_query.get()
        self.root.clear_address_region()
        self.assertIsNone(self.root.address_region)
        self.assertEqual(self.root.address_query.get(), text)
        self.root.load_pdf(self.make_pdf('no_region.pdf', '다른 주소'))
        self.assertEqual(self.root.address_query.get(), '')

    def test_empty_same_file_preserves_input_next_file_clears(self):
        self.select_address()
        text = self.root.address_query.get()
        self.root.address_region = AddressRegion(0, (.8, .8, 1, 1))
        self.root.read_address_region()
        self.assertEqual(self.root.address_query.get(), text)
        self.root.load_pdf(self.make_pdf('blank.pdf'))
        self.assertEqual(self.root.address_query.get(), '')
        self.assertIsNone(self.root.selected_dong)
        self.assertEqual(self.root.pnu_preview.get(), '-')
        self.assertIn('텍스트가 없습니다', self.root.region_status.get())

    def test_missing_page_navigation_and_target_page_reuse(self):
        multi = self.make_pdf('multi.pdf', '서울특별시 종로구 세종로 76-2', pages=2)
        self.root.load_pdf(multi)
        self.root.next_page()
        self.select_address()
        self.assertEqual(self.root.address_region.page_index, 1)
        text = self.root.address_query.get()
        self.root.previous_page()
        self.assertEqual(self.root.address_query.get(), text)
        self.assertIsNone(self.root.region_canvas_bounds())
        self.root.load_pdf(self.pdf)
        self.assertEqual(self.root.pdf_state.page_index, 0)
        self.assertEqual(self.root.address_query.get(), '')
        self.assertIn('페이지가 없습니다', self.root.region_status.get())
        self.root.load_pdf(multi)
        self.assertEqual(self.root.pdf_state.page_index, 1)
        self.assertIn('세종로', self.root.address_query.get())

    def test_zoom_scroll_resize_and_new_selection(self):
        self.select_address()
        region = self.root.address_region
        self.root.set_zoom(2)
        self.root.pdf_canvas.xview_moveto(.2)
        self.root.pdf_canvas.yview_moveto(.1)
        self.root.on_region_start(self.event(.4, .12))
        self.root.on_region_end(self.event(.42, .12))
        self.assertAlmostEqual(self.root.address_region.bounds[0], region.bounds[0]+.02)
        self.root.fit_width()
        self.assertEqual(len(self.root.pdf_canvas.find_withtag('address_region')), 5)
        self.root.new_address_region()
        self.root.on_region_start(self.event(.9, .2))
        self.root.on_region_end(self.event(.05, .08))
        self.assertAlmostEqual(self.root.address_region.bounds[0], .05, delta=.002)
        self.assertIn('세종로', self.root.address_query.get())

    def test_small_drag_cancel_and_extraction_error(self):
        from unittest.mock import patch
        self.select_address()
        original = self.root.address_region
        self.root.new_address_region()
        self.root.on_region_start(self.event(.5, .5))
        self.root.on_region_end(self.event(.5, .5))
        self.assertEqual(self.root.address_region, original)
        text = self.root.address_query.get()
        with patch('app.extract_region_text', side_effect=RuntimeError('test error')):
            self.root.read_address_region()
        self.assertEqual(self.root.address_query.get(), text)
        self.assertIn('추출 실패', self.root.region_status.get())

    def test_selection_not_persisted(self):
        self.select_address()
        self.root.source_folder.set(self.tmp.name)
        self.root.auto_save_work_state()
        state = app.load_work_state(Path(self.tmp.name))
        self.assertFalse(any('region' in key for key in state))
        other = app.FileMakerApp()
        try:
            self.assertIsNone(other.address_region)
        finally:
            other.destroy()


if __name__ == '__main__':
    unittest.main()
