"""Survey-only controls and orchestration; workers never access Tk widgets."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import json
from pathlib import Path
import queue
import re
import time
import tkinter as tk
from tkinter import ttk, messagebox

from pdf_pipeline import file_identity, survey_key
from survey_ocr import DEFAULT_HEADER, DEFAULT_LOTS, normalize_survey_lot, validate_region
from survey_batch import BatchItem, BatchResult, batch_context, copy_batch


@dataclass
class SurveyRow:
    lot: str
    enabled: bool = True
    bounds: tuple | None = None
    warning: str = ''
    status: str = '검토 대기'


class SurveyWorkflow:
    def init_survey(self):
        self.survey_regions = {'header': DEFAULT_HEADER, 'lots': DEFAULT_LOTS}
        self.survey_rows = []
        self.survey_ledger = {}
        self.survey_batches = {}
        self.survey_restore_draft = False
        self.survey_reveal_after_render = False
        self.survey_revision = 0
        self.survey_request = None
        self.survey_reading = False
        self.survey_identity = None
        self.survey_edit_target = None
        self.survey_drag = None
        self.survey_batch = False
        self.survey_applying = False
        self.survey_auto_pending = False
        self.survey_auto_query = ''
        self.survey_last_type = None
        self.survey_edit_text = tk.StringVar()
        self.survey_common = tk.StringVar(value='공통 법정동: 오른쪽에서 검색·선택하세요.')
        self.survey_status = tk.StringVar(value='토지소재와 지번 열 영역을 확인한 뒤 읽으세요.')
        self.survey_reviewed = tk.BooleanVar(value=False)

    def is_survey(self):
        return self.doc_type.get() == '측량성과도'

    def invalidate_survey_review(self):
        self.survey_reviewed.set(False)
        for row in self.survey_rows:
            row.status = '검토 대기'

    def build_survey_tools(self, parent):
        frame = self.survey_tools = ttk.LabelFrame(parent, text='측량성과도 · 여러 지번 복사', padding=6)
        frame.grid(row=5, column=0, sticky=tk.EW, pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        buttons = ttk.Frame(frame)
        buttons.grid(row=0, column=0, sticky=tk.EW)
        for text, command in (
            ('토지소재 영역', lambda: self.choose_survey_region('header')),
            ('지번 열 영역', lambda: self.choose_survey_region('lots')),
            ('두 영역 읽기', lambda: self.read_survey_regions(force=True)),
            ('읽기 취소', self.cancel_survey_read),
        ):
            ttk.Button(buttons, text=text, command=command).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(frame, textvariable=self.survey_common, wraplength=650).grid(row=1, column=0, sticky=tk.W, pady=(4, 2))
        table = ttk.Frame(frame)
        table.grid(row=2, column=0, sticky=tk.EW)
        table.columnconfigure(0, weight=1)
        self.survey_tree = ttk.Treeview(table, columns=('include', 'lot', 'pnu', 'status', 'filename'),
                                       show='headings', height=4, selectmode='browse', takefocus=True)
        for column, title, width in (('include', '복사', 48), ('lot', '지번', 88),
                                     ('pnu', 'PNU', 175), ('status', '상태', 100),
                                     ('filename', '파일명', 280)):
            self.survey_tree.heading(column, text=title)
            self.survey_tree.column(column, width=width, minwidth=width, stretch=False)
        self.survey_tree.grid(row=0, column=0, sticky=tk.EW)
        scroll = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.survey_tree.yview)
        scroll.grid(row=0, column=1, sticky=tk.NS)
        horizontal = ttk.Scrollbar(table, orient=tk.HORIZONTAL, command=self.survey_tree.xview)
        horizontal.grid(row=1, column=0, sticky=tk.EW)
        self.survey_tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.survey_tree.bind('<ButtonRelease-1>', self.on_survey_table_click)
        self.survey_tree.bind('<space>', lambda _e: self.toggle_survey_row())
        self.survey_tree.bind('<<TreeviewSelect>>', self.on_survey_table_select)
        edit = ttk.Frame(frame)
        edit.grid(row=3, column=0, sticky=tk.EW, pady=(4, 0))
        ttk.Label(edit, text='지번').pack(side=tk.LEFT)
        self.survey_entry = ttk.Entry(edit, textvariable=self.survey_edit_text, width=12)
        self.survey_entry.pack(side=tk.LEFT, padx=(4, 4))
        for text, command in (('추가', self.add_survey_row), ('수정', self.edit_survey_row),
                              ('삭제', self.delete_survey_row), ('포함/제외', self.toggle_survey_row)):
            ttk.Button(edit, text=text, command=command).pack(side=tk.LEFT, padx=(0, 3))
        review = ttk.Frame(frame)
        review.grid(row=4, column=0, sticky=tk.EW, pady=(4, 0))
        ttk.Checkbutton(review, text='지번·공통주소를 확인했습니다', variable=self.survey_reviewed).pack(side=tk.LEFT)
        self.survey_read_progress = ttk.Progressbar(review, mode='indeterminate', length=80)
        self.survey_read_progress.pack(side=tk.RIGHT)
        self.survey_read_progress.pack_forget()
        ttk.Label(frame, textvariable=self.survey_status, wraplength=650).grid(row=5, column=0, sticky=tk.W, pady=(2, 0))

    def sync_survey_mode(self):
        if self.is_survey():
            self.survey_tools.grid()
            self.address_region_bar.grid_remove()
            self.address_region_status.grid_remove()
            self.single_preview.grid_remove()
        else:
            self.survey_tools.grid_remove()
            self.address_region_bar.grid()
            self.address_region_status.grid()
            self.single_preview.grid()
        if self.survey_last_type != self.doc_type.get():
            self.cancel_survey_read()
            self.cancel_ocr()
            if self.pipeline.prepare.key is not None:
                self.pipeline.prepare.close()
            self.survey_rows = []
            self.survey_identity = None
            self.survey_edit_target = None
            self.survey_drag = None
            self.survey_reviewed.set(False)
            self.survey_revision += 1
            self.survey_last_type = self.doc_type.get()
            self.survey_auto_pending = self.is_survey()
            self.survey_auto_query = self.address_query.get()
            if self.is_survey() and not self.restoring_state and self.region_page() is not None:
                self.after_idle(self.maybe_read_survey)
        self.refresh_survey_rows()
        self.draw_survey_regions()

    def export_survey_state(self):
        return {'survey_regions': self.survey_regions, 'survey_progress': self.survey_ledger,
                'survey_batches': self.survey_batches}

    def restore_survey_state(self, state):
        regions = state.get('survey_regions', {})
        self.survey_regions = {'header': DEFAULT_HEADER, 'lots': DEFAULT_LOTS}
        if isinstance(regions, dict):
            for name in self.survey_regions:
                try:
                    self.survey_regions[name] = validate_region(regions[name])
                except (KeyError, TypeError, ValueError):
                    pass
        self.survey_ledger = {}
        self.survey_batches = {}
        ledger = state.get('survey_progress', {})
        if isinstance(ledger, dict):
            for key, record in ledger.items():
                if (isinstance(key, str) and re.fullmatch(r'[0-9a-f]{64}:산?\d{1,4}(?:-\d{1,4})?', key)
                        and isinstance(record, dict) and isinstance(record.get('destination'), str)
                        and isinstance(record.get('digest'), str)
                        and re.fullmatch(r'[0-9a-f]{64}', record['digest'])
                        and record.get('status') in ('pending', 'copied', 'failed')):
                    self.survey_ledger[key] = record
        batches = state.get('survey_batches', {})
        if isinstance(batches, dict):
            for key, draft in batches.items():
                if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key) or not isinstance(draft, dict):
                    continue
                identity, rows = draft.get('identity'), draft.get('rows')
                if (not isinstance(identity, list) or len(identity) != 3 or not isinstance(identity[0], str)
                        or not all(isinstance(v, int) for v in identity[1:])
                        or not isinstance(draft.get('code'), str) or not re.fullmatch(r'\d{10}', draft['code'])
                        or not isinstance(draft.get('year'), str) or not draft['year'].isdigit()
                        or not isinstance(draft.get('output'), str) or not isinstance(rows, list)
                        or not isinstance(draft.get('updated'), int)):
                    continue
                if (rows and all(isinstance(row, dict) and isinstance(row.get('lot'), str)
                                  and normalize_survey_lot(row['lot']) == row['lot']
                                  and isinstance(row.get('enabled'), bool) for row in rows)
                        and len({row['lot'] for row in rows}) == len(rows)):
                    self.survey_batches[key] = draft

    def reset_survey_source(self, path=None):
        keep = None
        if path is not None and self.is_survey():
            try:
                keep = survey_key(path, self.survey_regions['header'], self.survey_regions['lots'])
            except OSError:
                pass
        self.cancel_survey_read(keep_key=keep)
        self.survey_rows = []
        self.survey_restore_draft = True
        self.survey_identity = None
        self.survey_auto_pending = True
        self.survey_auto_query = ''
        self.survey_revision += 1
        self.survey_reviewed.set(False)
        self.survey_drag = None
        self.survey_reveal_after_render = False
        self.survey_edit_target = None
        self.survey_status.set('PDF 표시 후 토지소재와 검정 지번을 읽습니다.')
        self.refresh_survey_rows()

    def cancel_survey_read(self, keep_key=None):
        self.survey_auto_pending = False
        key = self.pipeline.prepare.key
        if key is not None and key[0] == 'survey' and key != keep_key:
            self.pipeline.prepare.close()
        if self.survey_reading:
            self.survey_status.set('읽기를 취소했습니다. 기존 입력은 유지합니다.')
        self.survey_reading = False
        self.survey_request = None
        self.survey_read_progress.stop()
        self.survey_read_progress.pack_forget()

    def read_survey_regions(self, force=False):
        if not self.is_survey() or self.copy_busy:
            return 'break'
        if self.pdf_state.path is None or self.region_page() is None:
            self.survey_status.set('PDF 표시가 끝난 뒤 읽으세요.')
            return 'break'
        self.cancel_ocr()
        self.survey_auto_pending = False
        self.survey_edit_target = None
        self.survey_drag = None
        try:
            key = survey_key(self.pdf_state.path, self.survey_regions['header'], self.survey_regions['lots'])
            if force:
                self.pipeline.regions.discard(key)
                if self.pipeline.prepare.key == key:
                    self.pipeline.prepare.close()
            self.survey_request = (key, self.load_generation, self.survey_revision)
            self.survey_reading = True
            self.survey_reviewed.set(False)
            self.survey_status.set('토지소재·검정 지번 읽는 중… Esc: 취소')
            self.survey_read_progress.pack(side=tk.RIGHT)
            self.survey_read_progress.start(20)
            cached = self.pipeline.request_survey(key)
            if cached is not None:
                self.handle_survey_result(key, cached, '')
            self.ensure_pipeline_poll()
        except Exception as exc:
            self.cancel_survey_read()
            self.survey_status.set(f'영역 읽기 실패: {exc}')
        return 'break'

    def maybe_read_survey(self):
        if (not self.is_survey() or not self.survey_auto_pending or self.copy_busy
                or self.region_page() is None):
            return
        self.survey_auto_pending = False
        if self.address_query.get() == self.survey_auto_query:
            self.read_survey_regions()

    def handle_survey_result(self, key, result, error):
        if (not self.is_survey() or self.survey_request != (key, self.load_generation, self.survey_revision)):
            return
        try:
            if self.pdf_state.path is None or file_identity(self.pdf_state.path) != key[1]:
                self.cancel_survey_read()
                self.survey_status.set('원본 PDF가 변경됐습니다. 다시 읽으세요.')
                return
        except OSError:
            self.cancel_survey_read()
            return
        self.survey_reading = False
        self.survey_request = None
        self.survey_read_progress.stop()
        self.survey_read_progress.pack_forget()
        if error:
            self.survey_status.set(f'영역 읽기 실패: {error}')
            return
        self.survey_identity = key[1]
        header_text = self.survey_clean_header(result.header)
        header_lot = self.survey_parse_lot(header_text)
        mountain = header_lot is not None and header_lot.is_mountain
        self.survey_rows = [SurveyRow('산'+c.lot if mountain and not c.lot.startswith('산') else c.lot,
                                      not bool(c.warning), c.bounds,
                                      c.warning or ('토지소재 산 표기 적용' if mountain and not c.lot.startswith('산') else ''))
                            for c in result.candidates]
        self.survey_applying = True
        try:
            self.address_query.set(header_text)
            self.refresh_suggestions()
            restored = self.restore_survey_draft() if self.survey_restore_draft else False
        finally:
            self.survey_applying = False
            self.survey_restore_draft = False
        note = '빨강 제외' if result.red_pixels else '빨강 미검출 · 흑백 원본 여부 확인'
        prefix = '이전 미완료 목록 복원' if restored else f'검정 지번 {len(self.survey_rows)}개'
        self.survey_status.set(f'{prefix} · {note} · 원문과 공통주소를 확인하세요.')
        self.performance.record('survey_read', result.seconds)
        self.refresh_survey_rows()
        self.draw_survey_regions()
        self.schedule_prefetch()

    def restore_survey_draft(self):
        matches = []
        for draft in self.survey_batches.values():
            if tuple(draft['identity']) != self.survey_identity or draft['year'] != self.year.get():
                continue
            try:
                if Path(draft['output']).resolve() == Path(self.output_folder.get()).resolve():
                    matches.append(draft)
            except OSError:
                pass
        if not matches:
            return False
        draft = max(matches, key=lambda value: value['updated'])
        dong = next((d for d in self.legal_dongs if d.code == draft['code']), None)
        if dong is None:
            return False
        bounds = {row.lot:row.bounds for row in self.survey_rows}
        self.survey_rows = [SurveyRow(row['lot'], row['enabled'], bounds.get(row['lot']), status='이전 미완료 목록')
                            for row in draft['rows']]
        self.address_query.set(f"{dong.name} {self.survey_rows[0].lot}")
        self.refresh_suggestions()
        self.selected_dong = dong
        self.selected_info.set(f'선택: {dong.code} {dong.name}')
        return True

    def choose_survey_region(self, target):
        if self.copy_busy or self.pdf_state.path is None:
            return
        self.cancel_survey_read()
        self.survey_edit_target = target
        page = self.survey_regions[target][0]
        if page < self.pdf_state.page_count and page != self.pdf_state.page_index:
            self.pdf_state = replace(self.pdf_state, page_index=page)
            self.render_pdf_page()
        label = '토지소재 글씨' if target == 'header' else '면적표의 지번 열만'
        self.survey_status.set(f'{label} 드래그하세요. 다른 페이지에서도 지정할 수 있습니다. Esc: 취소')
        self.pdf_canvas.focus_set()

    def survey_pointer(self, event):
        page = self.region_page()
        if page is None:
            return None
        x = (self.pdf_canvas.canvasx(event.x)-10)/(page.rect.width*self.render_zoom)
        y = (self.pdf_canvas.canvasy(event.y)-10)/(page.rect.height*self.render_zoom)
        return max(0, min(1, x)), max(0, min(1, y))

    def survey_region_start(self, event):
        if self.survey_edit_target is None:
            self.survey_status.set('영역을 바꾸려면 ‘토지소재 영역’ 또는 ‘지번 열 영역’을 누르세요.')
            return
        point = self.survey_pointer(event)
        if point is not None:
            self.cancel_survey_read()
            self.survey_drag = (self.survey_edit_target, point, point)

    def survey_region_move(self, event):
        if self.survey_drag is not None:
            point = self.survey_pointer(event)
            if point is not None:
                target, start, _end = self.survey_drag
                self.survey_drag = target, start, point
                self.draw_survey_regions()

    def survey_region_end(self, event):
        if self.survey_drag is None:
            return
        self.survey_region_move(event)
        target, start, end = self.survey_drag
        self.survey_drag = None
        bounds = (min(start[0], end[0]), min(start[1], end[1]), max(start[0], end[0]), max(start[1], end[1]))
        page = self.region_page()
        if page is None or (bounds[2]-bounds[0])*page.rect.width < 4 or (bounds[3]-bounds[1])*page.rect.height < 4:
            self.survey_status.set('영역이 너무 작습니다. 글씨 전체를 포함해 다시 지정하세요.')
            self.draw_survey_regions()
            return
        self.survey_regions[target] = self.pdf_state.page_index, bounds
        self.survey_edit_target = None
        self.survey_revision += 1
        self.survey_restore_draft = False
        self.survey_rows = []
        self.survey_identity = None
        self.survey_reviewed.set(False)
        self.refresh_survey_rows()
        self.draw_survey_regions()
        self.auto_save_work_state()
        self.read_survey_regions(force=True)

    def draw_survey_regions(self):
        self.pdf_canvas.delete('survey_region')
        if not self.is_survey():
            return
        page = self.region_page()
        if page is None:
            return
        for name, label, color in (('header', '토지소재', '#1565c0'), ('lots', '지번 열', '#985100')):
            page_index, bounds = self.survey_regions[name]
            if self.survey_drag is not None and self.survey_drag[0] == name:
                _, start, end = self.survey_drag
                page_index = self.pdf_state.page_index
                bounds = min(start[0],end[0]), min(start[1],end[1]), max(start[0],end[0]), max(start[1],end[1])
            if page_index != self.pdf_state.page_index:
                continue
            x0,y0,x1,y1 = self.survey_canvas_bounds(bounds, page)
            self.pdf_canvas.create_rectangle(x0,y0,x1,y1,outline=color,width=2,tags='survey_region')
            self.pdf_canvas.create_text(x0+3,y0-3,text=label,anchor=tk.SW,fill=color,tags='survey_region')
        row = self.selected_survey_row()
        if row is not None and row.bounds and self.survey_regions['lots'][0] == self.pdf_state.page_index:
            self.pdf_canvas.create_rectangle(*self.survey_canvas_bounds(row.bounds, page),
                                             outline='#7030a0',width=3,tags='survey_region')

    def survey_canvas_bounds(self, bounds, page):
        x0,y0,x1,y1 = bounds
        return (10+x0*page.rect.width*self.render_zoom, 10+y0*page.rect.height*self.render_zoom,
                10+x1*page.rect.width*self.render_zoom, 10+y1*page.rect.height*self.render_zoom)

    def selected_survey_row(self):
        selected = self.survey_tree.selection()
        if selected and selected[0].isdigit() and int(selected[0]) < len(self.survey_rows):
            return self.survey_rows[int(selected[0])]
        return None

    def refresh_survey_rows(self):
        if not hasattr(self, 'survey_tree'):
            return
        selected = self.survey_tree.selection()
        self.survey_tree.delete(*self.survey_tree.get_children())
        dong = self.selected_dong
        self.survey_common.set(f'공통 법정동: {dong.name} · {dong.code}' if dong else '공통 법정동: 오른쪽에서 검색·선택하세요.')
        for i, row in enumerate(self.survey_rows):
            pnu, filename = '', '법정동 선택 필요'
            if dong:
                lot = self.survey_parse_lot(row.lot)
                pnu = self.survey_make_pnu(dong.code, lot)
                address = f'{dong.name} {lot.display}'
                suffix = Path(self.source_file.get()).suffix or '.pdf'
                filename = f'{self.year.get()}_측량성과도_{pnu}_{self.survey_sanitize(address)}{suffix}'
            self.survey_tree.insert('', tk.END, iid=str(i), values=(
                '포함' if row.enabled else '제외', row.lot, pnu, row.warning or row.status, filename))
        if selected and self.survey_tree.exists(selected[0]):
            self.survey_tree.selection_set(selected[0])
        if not self.copy_busy:
            count = sum(row.enabled for row in self.survey_rows)
            self.copy_button.configure(text=f'{count}개 복사 (Ctrl+Enter)' if self.is_survey() else '복사 (Ctrl+Enter)')

    def on_survey_table_click(self, event):
        if not self.copy_busy and self.survey_tree.identify_column(event.x) == '#1':
            item = self.survey_tree.identify_row(event.y)
            if item:
                self.survey_tree.selection_set(item)
                self.toggle_survey_row()

    def on_survey_table_select(self, _event=None):
        if self.copy_busy:
            return
        row = self.selected_survey_row()
        if row:
            self.survey_edit_text.set(row.lot)
            page = self.survey_regions['lots'][0]
            if page < self.pdf_state.page_count and page != self.pdf_state.page_index:
                self.survey_reveal_after_render = True
                self.pdf_state = replace(self.pdf_state, page_index=page)
                self.render_pdf_page()
            else:
                self.draw_survey_regions()
                self.reveal_survey_row()

    def reveal_survey_row(self):
        self.survey_reveal_after_render = False
        row, page = self.selected_survey_row(), self.region_page()
        if row is None or row.bounds is None or page is None or self.survey_regions['lots'][0] != self.pdf_state.page_index:
            return
        x0,y0,x1,y1 = self.survey_canvas_bounds(row.bounds,page)
        canvas = self.pdf_canvas
        for start,end,offset,viewport,total,scroll in (
            (x0,x1,canvas.canvasx(0),canvas.winfo_width(),page.rect.width*self.render_zoom+20,canvas.xview_moveto),
            (y0,y1,canvas.canvasy(0),canvas.winfo_height(),page.rect.height*self.render_zoom+20,canvas.yview_moveto)):
            if start < offset or end > offset+viewport:
                scroll(max(0,((start+end)/2-viewport/2)/total))

    def toggle_survey_row(self):
        row = self.selected_survey_row()
        if row and not self.copy_busy:
            row.enabled = not row.enabled
            self.survey_reviewed.set(False)
            self.refresh_survey_rows()
        return 'break'

    def add_survey_row(self):
        self.change_survey_row(False)

    def edit_survey_row(self):
        self.change_survey_row(True)

    def change_survey_row(self, editing):
        if self.copy_busy or self.pdf_state.path is None:
            return
        lot = normalize_survey_lot(self.survey_edit_text.get())
        row = self.selected_survey_row() if editing else None
        if not lot or (editing and row is None):
            self.survey_status.set('지번을 입력하세요. 예: 301-6, 산12-3. 수정할 때는 행을 선택하세요.')
            return
        if any(other.lot == lot and other is not row for other in self.survey_rows):
            self.survey_status.set('이미 목록에 있는 지번입니다.')
            return
        self.cancel_survey_read()
        if row is not None:
            row.lot, row.warning, row.status = lot, '', '수동 수정'
        else:
            self.survey_rows.append(SurveyRow(lot, status='수동 추가'))
        self.survey_identity = file_identity(self.pdf_state.path)
        self.survey_reviewed.set(False)
        self.refresh_survey_rows()

    def delete_survey_row(self):
        row = self.selected_survey_row()
        if row is not None and not self.copy_busy:
            self.survey_rows.remove(row)
            self.survey_reviewed.set(False)
            self.refresh_survey_rows()

    def copy_survey_files(self):
        if self.copy_busy:
            return
        if self.survey_reading or self.survey_drag or self.survey_edit_target:
            self.operation_status.set('영역 읽기·지정을 끝낸 뒤 복사하세요.')
            return
        rows = [row for row in self.survey_rows if row.enabled]
        if not rows or not self.survey_reviewed.get():
            self.survey_status.set('복사할 지번을 선택하고 ‘지번·공통주소를 확인했습니다’를 체크하세요.')
            return
        source = Path(self.source_file.get())
        output = Path(self.output_folder.get())
        folder = self.current_source_folder_path()
        dong = self.selected_dong
        if folder is None or dong is None or not self.legal_dongs or not self.output_folder.get() or not output.is_dir():
            messagebox.showerror('확인 필요', '법정동과 출력 폴더를 먼저 선택하세요.')
            return
        if not self.year.get().isdigit() or not source.is_file() or source.suffix.lower() != '.pdf':
            messagebox.showerror('확인 필요', '연도와 원본 PDF를 확인하세요.')
            return
        try:
            identity = file_identity(source)
            if source.parent.resolve() != folder.resolve():
                raise ValueError('원본 폴더와 현재 PDF가 다릅니다. 폴더를 다시 선택하세요.')
            if identity != self.survey_identity:
                raise ValueError('원본 PDF가 변경됐습니다. 다시 읽은 뒤 확인하세요.')
            context = batch_context(identity, dong.code, self.year.get(), output)
            items = []
            for row in rows:
                lot = self.survey_parse_lot(row.lot)
                pnu = self.survey_make_pnu(dong.code, lot)
                if not re.fullmatch(r'[0-9]{19}',pnu):
                    raise ValueError('법정동 코드와 지번으로 19자리 PNU를 만들 수 없습니다.')
                address = f'{dong.name} {lot.display}'
                plan = self.survey_build_plan(source, output, self.year.get(), '측량성과도', pnu, address)
                stem = f'{self.year.get()}_측량성과도_{pnu}_{self.survey_sanitize(address)}'
                items.append(BatchItem(f'{context}:{row.lot}', row.lot, stem, plan))
            conflicts = [item for item in items if item.plan.needs_confirmation]
            if conflicts:
                details = '\n'.join(item.plan.destination.name for item in conflicts[:6])
                if not messagebox.askyesno('일괄 복사 · 파일명 충돌 확인',
                        f'{len(items)}개 중 {len(conflicts)}개에 같은 이름의 파일이 있습니다.\n'
                        f'기존 번호 규칙으로 이름을 변경하고 복사합니다.\n\n{details}\n\n'
                        '이전 성공 항목은 내용 검증 후 건너뜁니다. 계속할까요?', icon='warning'):
                    return
            self.survey_batches[context] = dict(identity=list(identity), code=dong.code,
                                                year=self.year.get(), output=str(output.resolve()),
                                                updated=time.time_ns(),
                                                rows=[dict(lot=row.lot, enabled=row.enabled) for row in self.survey_rows])
            state = self.survey_build_state(Path(self.source_folder.get()), output, self.processed_sources,
                                             source, self.year.get(), self.doc_type.get())
            state.update(self.export_survey_state())
            snapshot = json.loads(json.dumps(state))
            folder = Path(self.source_folder.get())
            save = self.survey_save_state

            def checkpoint(ledger):
                snapshot['survey_progress'] = ledger
                save(folder, snapshot)

            self.cancel_survey_read()
            self.survey_batch = True
            self.copy_busy = True
            self.copy_started = time.perf_counter()
            self.copy_source = source
            self.copy_auto_next = self.auto_next.get()
            self.survey_batch_context = context
            self.survey_events = queue.Queue()
            self.survey_delivered = set()
            self.set_copy_busy_ui(True)
            self.prepare_copy_list_updates()
            self.operation_status.set(f'0/{len(items)} 복사 준비 중…')
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                self.copy_future = executor.submit(copy_batch, source, identity, tuple(items), self.survey_ledger,
                                                   checkpoint, self.survey_copy_plan, self.survey_plan_factory,
                                                   self.survey_plan_type, self.survey_events)
            finally:
                executor.shutdown(wait=False)
            self.copy_poll_id = self.after(50, self.poll_survey_batch)
        except Exception as exc:
            if self.copy_busy:
                self.set_copy_busy_ui(False)
                self.copy_busy = False
                self.survey_batch = False
            messagebox.showerror('일괄 복사 시작 실패', str(exc))

    def poll_survey_batch(self):
        self.copy_poll_id = None
        while True:
            try:
                event = self.survey_events.get_nowait()
            except queue.Empty:
                break
            if event[0] == 'progress':
                self.operation_status.set(f'{event[1]}/{event[2]} 복사 중…')
            else:
                self.apply_survey_outcome(event[1])
        if not self.copy_future.done():
            self.copy_poll_id = self.after(50, self.poll_survey_batch)
            return
        try:
            result = self.copy_future.result()
        except Exception as exc:
            result = BatchResult((), self.survey_ledger, str(exc))
        for outcome in result.outcomes:
            self.apply_survey_outcome(outcome)
        self.finish_survey_batch(result)

    def apply_survey_outcome(self, outcome):
        if outcome.item.key in self.survey_delivered:
            return
        self.survey_delivered.add(outcome.item.key)
        for row in self.survey_rows:
            if row.lot == outcome.item.lot:
                row.status = {'copied':'복사 완료', 'skipped':'이전 성공 확인', 'failed':'실패'}[outcome.status]
                break
        if outcome.status in ('copied', 'skipped'):
            self.update_output_after_copy(outcome.plan)
            self.last_copy_destination = outcome.plan.destination
        self.refresh_survey_rows()

    def finish_survey_batch(self, result):
        started = time.perf_counter()
        self.survey_ledger = result.ledger
        previous = self.defer_state_save
        self.defer_state_save = True
        try:
            if result.complete:
                self.mark_source_processed(self.copy_source)
                self.update_processed_source_row(self.copy_source)
                prefix = self.survey_batch_context + ':'
                self.survey_ledger = {k:v for k,v in self.survey_ledger.items() if not k.startswith(prefix)}
                self.survey_batches.pop(self.survey_batch_context, None)
                self.operation_status.set(f'{len(result.outcomes)}개 복사 완료 · 현재 파일을 유지합니다.')
                if self.copy_auto_next and not self.close_after_copy:
                    self.open_next_unprocessed(self.copy_source)
            else:
                successful = sum(o.status in ('copied', 'skipped') for o in result.outcomes)
                errors = [f'{o.item.lot}: {o.error}' for o in result.outcomes if o.error]
                self.operation_status.set(f'{successful}개 성공 · 나머지는 다시 복사해 재시도하세요.')
                # A failed rename may have partially changed the directory.
                self.refresh_output_list(self.last_copy_destination)
                messagebox.showerror('일괄 복사 미완료', result.error or '\n'.join(errors[:8]))
        finally:
            self.defer_state_save = previous
            self.auto_save_work_state()
            self.set_copy_busy_ui(False)
            self.copy_busy = False
            self.copy_future = None
            self.survey_batch = False
            self.performance.record('survey_batch_postprocess', time.perf_counter()-started)
            self.performance.record('survey_batch_transaction', time.perf_counter()-self.copy_started)
            self.refresh_survey_rows()
            if self.survey_auto_pending and not self.close_after_copy:
                self.after_idle(self.maybe_read_survey)
            if self.close_after_copy:
                self.destroy()
