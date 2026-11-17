import tempfile
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

import app
from workspace_index import SourceIndex
from pdf_pipeline import render_key, RenderedPage, region_key


class AsyncViewerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'current.pdf'
        self.path.write_bytes(b'PDF placeholder')
        self.key = render_key(self.path, 0, 1, 600)
        self.request = (self.key, 2, 0)
        self.ui = SimpleNamespace(
            render_request=self.request, load_generation=2,
            pdf_state=app.PdfRenderState(path=self.path, page_count=1),
            pdf_canvas=Mock(), region_keyboard_edit=False, draw_address_region=Mock(),
            pdf_status=Mock(), performance=Mock(), load_started=None,
            address_region=None, address_query=Mock(), pending_region_read=None,
            read_address_region=Mock(), schedule_prefetch=Mock(), clear_pdf_viewer=Mock(),
        )
        self.ui.address_query.get.return_value = ''
        self.ui.apply_rendered_page = MethodType(app.FileMakerApp.apply_rendered_page, self.ui)
        self.image = RenderedPage(b'P6 image', 600, 800, 1, .02)

    def test_old_generation_and_old_zoom_never_replace_current_image(self):
        with patch('app.tk.PhotoImage') as image:
            self.ui.apply_rendered_page((self.key, 1, 0), self.image)
            zoom_key = render_key(self.path, 0, 2, 0)
            self.ui.apply_rendered_page((zoom_key, 2, 0), self.image)
        image.assert_not_called()
        self.ui.pdf_canvas.delete.assert_not_called()

    def test_replaced_file_never_displays_cached_image(self):
        self.path.write_bytes(b'new PDF with different contents')
        with patch('app.tk.PhotoImage') as image:
            self.ui.apply_rendered_page(self.request, self.image)
        image.assert_not_called()
        self.ui.pdf_status.set.assert_called_once()

    def test_user_address_edit_while_rendering_prevents_auto_extraction(self):
        region = ('address region',)
        self.ui.address_region = region
        self.ui.pending_region_read = (2, 0, region, '')
        self.ui.address_query.get.return_value = 'manually edited address'
        with patch('app.tk.PhotoImage'):
            self.ui.apply_rendered_page(self.request, self.image)
        self.ui.read_address_region.assert_not_called()
        self.assertEqual(self.ui.rendered_page_index, 0)
        self.ui.clear_pdf_viewer.assert_not_called()

    def test_matching_request_extracts_only_after_image_is_ready(self):
        region = ('address region',)
        self.ui.address_region = region
        self.ui.pending_region_read = (2, 0, region, '')
        with patch('app.tk.PhotoImage'):
            self.ui.apply_rendered_page(self.request, self.image)
        self.ui.read_address_region.assert_called_once()
        self.ui.schedule_prefetch.assert_called_once()
        self.assertIsNone(self.ui.pending_region_read)

    def test_resize_events_cancel_previous_render_timer(self):
        ui = SimpleNamespace(pdf_state=app.PdfRenderState(), current_pdf_doc=object(),
                             resize_after_id=None, after=Mock(return_value='latest'),
                             after_cancel=Mock(), render_after_resize=Mock())
        for _ in range(20):
            app.FileMakerApp.on_pdf_canvas_resize(ui, None)
        self.assertEqual(ui.after_cancel.call_count, 19)
        ui.after.assert_called_with(120, ui.render_after_resize)

    def test_prefetch_only_prepares_next_unprocessed_file_without_selecting_it(self):
        done = Path(self.tmp.name) / 'done.pdf'
        next_file = Path(self.tmp.name) / 'next.pdf'
        done.write_bytes(b'PDF')
        next_file.write_bytes(b'PDF')
        self.ui.source_pdf_files = [self.path, done, next_file]
        self.ui.processed_sources = {done.resolve()}
        self.ui.source_index = SourceIndex(self.ui.source_pdf_files, self.ui.processed_sources)
        self.ui.rendered_page_index = 0
        self.ui.copy_busy = False
        self.ui.ocr_job = None
        self.ui.region_drag = None
        self.ui.prefetch_after_id = 'pending'
        self.ui.address_region = SimpleNamespace(page_index=0, bounds=(0, 0, 1, .2))
        self.ui.pipeline = Mock()
        self.ui.ensure_pipeline_poll = Mock()
        self.ui.load_pdf = Mock()
        self.ui.source_file = Mock()
        self.ui.pdf_canvas.winfo_width.return_value = 824
        app.FileMakerApp.prefetch_next_source(self.ui)
        self.ui.pipeline.request_render.assert_called_once_with(render_key(next_file, 0, 1, 800))
        self.ui.pipeline.request_region.assert_called_once_with(region_key(next_file, 0, (0, 0, 1, .2)))
        self.ui.load_pdf.assert_not_called()
        self.ui.source_file.set.assert_not_called()

    def test_cached_filename_preview_does_not_replace_fresh_collision_check(self):
        ui = SimpleNamespace(selected_dong=app.LegalDong('1168010100', '서울 강남구 역삼동',
                                                       '서울강남구역삼동', ('서울', '강남구', '역삼동')),
                             address_query=Mock(), source_file=Mock(), output_folder=Mock(),
                             year=Mock(), doc_type=Mock(), performance=Mock(),
                             preview_destination_cache=None)
        ui.address_query.get.return_value = '역삼동 123-4'
        ui.source_file.get.return_value = str(self.path)
        ui.output_folder.get.return_value = self.tmp.name
        ui.year.get.return_value = '2026'
        ui.doc_type.get.return_value = '실태조사서'
        first = app.FileMakerApp.preview_values(ui)
        first[2].write_bytes(b'existing destination')
        self.assertEqual(app.FileMakerApp.preview_values(ui), first)
        fresh = app.build_destination_plan(self.path, Path(self.tmp.name), '2026',
                                          '실태조사서', first[0], first[1])
        self.assertNotEqual(fresh.destination, first[2])
        self.assertTrue(fresh.needs_confirmation)
        ui.preview_destination_cache = None
        self.assertEqual(app.FileMakerApp.preview_values(ui)[2], fresh.destination)


class SearchEquivalenceTests(unittest.TestCase):
    def test_prepared_search_preserves_legacy_scores_and_order(self):
        dongs = [app.LegalDong(str(i), name, app.normalize_text(name), tuple(name.split()))
                 for i, name in enumerate(('서울특별시 강남구 역삼동', '서울특별시 종로구 와룡동',
                                           '서울특별시 강북구 우이동', '부산광역시 서구 부민동3가',
                                           '서울특별시 은평구 진관동', 'Mixed CASE Town'))]

        def legacy(query, dong):
            stripped = app.strip_lot_from_query(query)
            compact = app.normalize_text(stripped)
            if not compact:
                return 0
            score = (1000 if compact == dong.compact_name else 700 if compact in dong.compact_name
                     else 500 if dong.compact_name in compact else 0)
            for token in stripped.split():
                token = app.normalize_text(token)
                if any(token == app.normalize_text(t) for t in dong.tokens):
                    score += 120
                elif token in dong.compact_name:
                    score += 60
            return score + min(len(dong.name), 80) if score else 0

        for query in ('', '123-4', '역삼', '강남 역삼 123-4', '서울특별시 종로구 와룡동 2-122',
                      '부민동3가 22-220', '진관동 산12-3', 'mixed case', '우이동 106-1', '없는주소'):
            with self.subTest(query=query):
                expected = [(legacy(query, d), d) for d in dongs]
                expected = sorted((pair for pair in expected if pair[0] > 0),
                                  key=lambda pair: (-pair[0], pair[1].name))
                self.assertEqual(app.search_legal_dongs(query, dongs), [d for _, d in expected[:20]])


if __name__ == '__main__':
    unittest.main()
