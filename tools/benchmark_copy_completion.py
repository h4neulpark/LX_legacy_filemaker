"""Compare bookkeeping only; list painting, PDF rendering, and copy I/O are excluded."""
import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import types
from types import MethodType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from pdf_pipeline import TimingStats
from test_copy_scaling import Rows, workspace
from workspace_index import SourceIndex, OutputIndex


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref')
    parser.add_argument('--output', default='build/copy-completion-benchmark.json')
    args = parser.parse_args()
    versions = [('current', app)]
    if args.baseline_ref:
        baseline = types.ModuleType('benchmark_baseline')
        baseline.__file__ = app.__file__
        sys.modules[baseline.__name__] = baseline
        code = subprocess.check_output(['git', 'show', f'{args.baseline_ref}:app.py'], text=True)
        exec(compile(code, 'baseline_app.py', 'exec'), baseline.__dict__)
        versions.insert(0, (args.baseline_ref, baseline))
    reports = []
    with tempfile.TemporaryDirectory(prefix='filemaker-benchmark-') as temporary:
        for version, module in versions:
            for total, completed in ((700, 1), (1300, 1000)):
                case = Path(temporary) / f'{version}-{total}'
                case.mkdir()
                ui = workspace(case, total, completed)
                original_processed = set(ui.processed_sources)
                original_outputs = list(ui.output_files)
                ui.copy_plan.destination.write_bytes(b'copied PDF')
                for name in ('select_source_file_in_list', 'select_source_pdf',
                             'update_source_counts', 'update_processed_source_row',
                             'refresh_output_list', 'current_source_folder_path',
                             'auto_save_work_state', 'open_next_unprocessed', 'finish_copy',
                             'focus_source_list'):
                    setattr(ui, name, MethodType(getattr(module.FileMakerApp, name), ui))

                def reset():
                    ui.processed_sources = set(original_processed)
                    ui.source_index = SourceIndex(ui.source_pdf_files, ui.processed_sources)
                    ui.source_file.set(str(ui.copy_source))
                    ui.pdf_state = app.PdfRenderState(path=ui.copy_source)
                    ui.source_list = Rows(p.name for p in ui.source_pdf_files)
                    ui.output_files = list(original_outputs)
                    ui.output_index = OutputIndex(ui.output_files)
                    ui.output_list = Rows(p.name for p in ui.output_files)
                    ui.copy_busy = True
                    ui.performance = TimingStats()
                    ui.copy_started = time.perf_counter()

                samples = []
                for _ in range(5):
                    reset()
                    started = time.perf_counter()
                    ui.finish_copy()
                    samples.append((time.perf_counter() - started) * 1000)
                reset()
                resolve_calls = [0]
                real_resolve = Path.resolve

                def counted(path, *positional, **keywords):
                    resolve_calls[0] += 1
                    return real_resolve(path, *positional, **keywords)

                with patch.object(Path, 'resolve', counted):
                    ui.finish_copy()
                reports.append(dict(version=version, files=total, completed=completed,
                                    median_ms=round(statistics.median(samples), 3),
                                    resolve_calls=resolve_calls[0]))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
