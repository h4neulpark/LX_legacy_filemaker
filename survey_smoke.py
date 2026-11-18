"""Synthetic color document and frozen-GUI check; never reads user PDFs."""
from pathlib import Path
import json
import tempfile
import time

HEADER_REGION = (0, (.05, .04, .95, .12))
LOT_REGION = (0, (.05, .20, .4, .65))


def write_report(path, data):
    temporary=path.with_suffix('.report.tmp')
    temporary.write_text(json.dumps(data),encoding='utf-8')
    temporary.replace(path)


def make_fixture(path, scanned=False, split_pages=False):
    import pymupdf
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 70), '경기도 수원시 팔달구 지동 301-6번지 외13필',
                         fontname='korea', fontsize=14)
        if split_pages:
            doc.new_page(width=600, height=800)
            page = doc.new_page(width=600, height=800)
        for y, lot, color in ((200,'301-6',(0,0,0)), (240,'301-6',(1,0,0)),
                               (280,'301-37',(0,0,0)), (320,'301-37',(1,0,0)),
                               (360,'301-45',(0,0,0)), (400,'301-45',(1,0,0)),
                               (440,'999-9',(1,0,0))):
            page.insert_text((50,y), lot, fontsize=13, color=color)
            page.insert_text((300,y), '1234', fontsize=13)
        if scanned:
            with pymupdf.open() as image_doc:
                for original in doc:
                    pix = original.get_pixmap(dpi=300)
                    target = image_doc.new_page(width=600, height=800)
                    target.insert_image(target.rect, pixmap=pix)
                image_doc.save(path)
        else:
            doc.save(path)


def run(app_class, output):
    from survey_batch import file_digest
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    release = path.with_suffix('.release')
    release.unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    result = {'error':'GUI check did not complete'}
    with tempfile.TemporaryDirectory(prefix='survey-ui-check-') as folder:
        source_folder, destination = Path(folder)/'source', Path(folder)/'output'
        source_folder.mkdir()
        destination.mkdir()
        source = source_folder/'survey.pdf'
        make_fixture(source)
        duplicate = source_folder/'survey-duplicate.pdf'
        duplicate.write_bytes(source.read_bytes())
        root = app_class()
        # Exercise actual spawned hashing in the frozen Windows executable.
        root.hash_scanner.workers = 2
        root.hash_scanner.cold_parallel_bytes = 0
        for tooltip in root.tooltips:
            tooltip.delay = 100000
        root.hide_tooltips()
        width, height = min(1450,root.winfo_screenwidth()-30), min(920,root.winfo_screenheight()-80)
        root.geometry(f'{width}x{height}+10+10')
        root.load_source_workspace(source_folder)
        root.survey_regions = {'header':HEADER_REGION, 'lots':LOT_REGION}
        root.output_folder.set(str(destination))
        root.year.set('2025')
        root.doc_type.set('측량성과도')
        root.auto_next.set(False)
        root.select_source_file_in_list(source)
        root.select_source_pdf()
        deadline = time.monotonic()+55
        phase = 'reading'

        def fail(error):
            result.clear()
            result['error'] = str(error)
            write_report(path,result)
            root.destroy()

        def tick():
            nonlocal phase
            try:
                if time.monotonic()>deadline:
                    raise TimeoutError(root.survey_status.get()+' / '+root.operation_status.get())
                if phase=='reading' and not root.survey_reading and len(root.survey_rows)==3:
                    assert [r.lot for r in root.survey_rows]==['301-6','301-37','301-45']
                    assert root.selected_dong.code=='4111513900'
                    root.survey_tree.selection_set('0')
                    root.set_zoom(.55)
                    phase='rendering'
                elif (phase=='rendering' and root.rendered_page_index==root.pdf_state.page_index
                      and root.hash_scan_result is not None):
                    assert root.hash_scan_result == ([duplicate], 0)
                    assert root.hash_notice.winfo_manager() == 'pack'
                    root.hide_tooltips()
                    root.update_idletasks()
                    preview = dict(phase='ready', x=root.winfo_rootx(), y=root.winfo_rooty(),
                                   width=root.winfo_width(), height=root.winfo_height())
                    write_report(path,preview)
                    phase='preview'
                elif phase=='preview' and release.exists():
                    root.survey_reviewed.set(True)
                    root.copy_button.invoke()
                    assert root.copy_busy
                    phase='copying'
                elif phase=='copying' and not root.copy_busy:
                    outputs=list(destination.glob('*.pdf'))
                    assert len(outputs)==3, root.operation_status.get()
                    assert all(file_digest(p)==file_digest(source) for p in outputs)
                    assert source.resolve() in root.processed_sources
                    assert not root.survey_ledger
                    result.clear()
                    from ppocr_engine import HEADER_ENGINE
                    result.update(phase='passed', copies=len(outputs), header_engine=HEADER_ENGINE)
                    write_report(path,result)
                    root.destroy()
                    return
                root.after(50,tick)
            except Exception as exc:
                fail(exc)

        root.after(50,tick)
        root.mainloop()
    return 1 if result.get('error') else 0
