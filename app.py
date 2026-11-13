from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import shutil
import sys
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import pymupdf as fitz
    from pdf_region import AddressRegion, canvas_to_relative, extract_region_text, move_bounds, normalized_bounds
except ImportError:  # pragma: no cover - exercised only on machines missing the optional GUI dependency.
    fitz = None


APP_TITLE = "법정동 PNU PDF 정리 도구"
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DEFAULT_CODE_FILE = APP_DIR / "법정동코드 전체자료.txt"
ENCODINGS = ("utf-8-sig", "cp949", "euc-kr")
DOC_TYPES = ("측량성과도", "실태조사서")
INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')
LOT_PATTERN = re.compile(r"(?P<mountain>산\s*)?(?P<main>\d{1,4})(?:\s*-\s*(?P<sub>\d{1,4}))?")
PDF_SUFFIX = ".pdf"
WORK_STATE_FILENAME = ".filemaker_work_state.json"


@dataclass(frozen=True)
class LegalDong:
    code: str
    name: str
    compact_name: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class LotNumber:
    is_mountain: bool
    main: int
    sub: int

    @property
    def display(self) -> str:
        prefix = "산" if self.is_mountain else ""
        if self.sub:
            return f"{prefix}{self.main}-{self.sub}"
        return f"{prefix}{self.main}"


@dataclass(frozen=True)
class PdfRenderState:
    path: Path | None = None
    page_index: int = 0
    page_count: int = 0
    zoom: float = 1.0
    fit_to_width: bool = True


@dataclass(frozen=True)
class DestinationPlan:
    destination: Path
    renames: tuple[tuple[Path, Path], ...] = ()

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.renames)


def read_text_with_fallback(path: Path) -> str:
    last_error: UnicodeDecodeError | None = None
    for encoding in ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error:
        raise last_error
    return path.read_text()


def split_whitespace_table_line(line: str) -> list[str]:
    return re.split(r"\s{2,}|\t", line.strip())


def parse_legal_dong_rows(text: str) -> list[dict[str, str]]:
    sample = "\n".join(text.splitlines()[:5])
    if "\t" in sample:
        reader = csv.DictReader(text.splitlines(), delimiter="\t")
        return [dict(row) for row in reader]

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    headers = split_whitespace_table_line(lines[0])
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        values = split_whitespace_table_line(line)
        if len(values) == len(headers):
            rows.append(dict(zip(headers, values)))
    return rows


def load_legal_dongs(path: Path) -> list[LegalDong]:
    text = read_text_with_fallback(path)
    rows = parse_legal_dong_rows(text)
    result: list[LegalDong] = []

    for row in rows:
        code = (row.get("법정동코드") or row.get("법정동 코드") or "").strip()
        name = (row.get("법정동명") or row.get("법정동 명") or "").strip()
        exists = (
            row.get("존재여부")
            or row.get("폐지여부")
            or row.get("폐지 여부")
            or row.get("존폐여부")
            or ""
        ).strip()
        if not code or not name or exists != "존재":
            continue
        compact_name = normalize_text(name)
        tokens = tuple(part for part in name.split() if part)
        result.append(LegalDong(code=code, name=name, compact_name=compact_name, tokens=tokens))

    return result


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def strip_lot_from_query(query: str) -> str:
    match = find_lot_match(query)
    if not match:
        return query.strip()
    return (query[: match.start()] + " " + query[match.end() :]).strip()


def find_lot_match(query: str) -> re.Match[str] | None:
    matches = list(LOT_PATTERN.finditer(query))
    return matches[-1] if matches else None


def parse_lot_number(query: str) -> LotNumber | None:
    match = find_lot_match(query)
    if not match:
        return None
    main = int(match.group("main"))
    sub = int(match.group("sub") or 0)
    if not (0 <= main <= 9999 and 0 <= sub <= 9999):
        return None
    return LotNumber(is_mountain=bool(match.group("mountain")), main=main, sub=sub)


def make_pnu(legal_dong_code: str, lot: LotNumber) -> str:
    mountain_code = "2" if lot.is_mountain else "1"
    return f"{legal_dong_code}{mountain_code}{lot.main:04d}{lot.sub:04d}"


def score_match(query: str, dong: LegalDong) -> int:
    stripped = strip_lot_from_query(query)
    compact_query = normalize_text(stripped)
    if not compact_query:
        return 0

    score = 0
    if compact_query == dong.compact_name:
        score += 1000
    elif compact_query in dong.compact_name:
        score += 700
    elif dong.compact_name in compact_query:
        score += 500

    query_tokens = [token for token in stripped.split() if token]
    for token in query_tokens:
        compact_token = normalize_text(token)
        if not compact_token:
            continue
        if any(compact_token == normalize_text(dong_token) for dong_token in dong.tokens):
            score += 120
        elif compact_token in dong.compact_name:
            score += 60

    if score:
        score += min(len(dong.name), 80)
    return score


def search_legal_dongs(query: str, dongs: list[LegalDong], limit: int = 20) -> list[LegalDong]:
    scored = [(score_match(query, dong), dong) for dong in dongs]
    filtered = [(score, dong) for score, dong in scored if score > 0]
    filtered.sort(key=lambda item: (-item[0], item[1].name))
    return [dong for _, dong in filtered[:limit]]


def sanitize_filename_part(value: str) -> str:
    value = INVALID_FILENAME_CHARS.sub("_", value)
    value = re.sub(r"\s+", "_", value.strip())
    value = re.sub(r"_+", "_", value)
    return value.strip("._ ")


def numbered_destination_path(folder: Path, stem: str, suffix: str, number: int) -> Path:
    return folder / f"{stem}__{number}{suffix}"


def existing_numbered_destinations(folder: Path, stem: str, suffix: str) -> dict[int, Path]:
    if not folder.is_dir():
        return {}

    prefix = f"{stem}__"
    result: dict[int, Path] = {}
    for path in folder.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if not name.startswith(prefix):
            continue
        if suffix:
            if not name.endswith(suffix):
                continue
            number_text = name[len(prefix):-len(suffix)]
        else:
            number_text = name[len(prefix):]
        if number_text.isdigit():
            number = int(number_text)
            if number > 0:
                result[number] = path
    return result


def destination_plan_for_stem(folder: Path, stem: str, suffix: str) -> DestinationPlan:
    base = folder / f"{stem}{suffix}"
    numbered = existing_numbered_destinations(folder, stem, suffix)
    if base.exists():
        renames: list[tuple[Path, Path]] = []
        for number, path in sorted(numbered.items(), reverse=True):
            renames.append((path, numbered_destination_path(folder, stem, suffix, number + 2)))
        renames.append((base, numbered_destination_path(folder, stem, suffix, 1)))
        return DestinationPlan(numbered_destination_path(folder, stem, suffix, 2), tuple(renames))

    if numbered:
        counter = 1
        while counter in numbered:
            counter += 1
        return DestinationPlan(numbered_destination_path(folder, stem, suffix, counter))

    return DestinationPlan(base)


def build_destination_plan(
    source_file: Path,
    output_folder: Path,
    year: str,
    doc_type: str,
    pnu: str,
    full_address: str,
) -> DestinationPlan:
    safe_address = sanitize_filename_part(full_address)
    safe_doc_type = sanitize_filename_part(doc_type)
    stem = f"{year}_{safe_doc_type}_{pnu}_{safe_address}"
    return destination_plan_for_stem(output_folder, stem, source_file.suffix)


def build_destination_path(
    source_file: Path,
    output_folder: Path,
    year: str,
    doc_type: str,
    pnu: str,
    full_address: str,
) -> Path:
    return build_destination_plan(source_file, output_folder, year, doc_type, pnu, full_address).destination


def temporary_copy_path(destination: Path) -> Path:
    counter = 0
    while True:
        suffix = ".tmp" if counter == 0 else f".tmp{counter}"
        candidate = destination.with_name(f".{destination.name}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def copy_with_destination_plan(source_file: Path, plan: DestinationPlan) -> None:
    if not plan.renames:
        shutil.copy2(source_file, plan.destination)
        return

    temp_destination = temporary_copy_path(plan.destination)
    temp_created = False
    try:
        shutil.copy2(source_file, temp_destination)
        temp_created = True
        for old_path, new_path in plan.renames:
            old_path.rename(new_path)
        temp_destination.rename(plan.destination)
        temp_created = False
    finally:
        if temp_created and temp_destination.exists():
            try:
                temp_destination.unlink()
            except OSError:
                pass


def list_pdf_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        [path for path in folder.iterdir() if path.is_file() and path.suffix.lower() == PDF_SUFFIX],
        key=lambda path: path.name.lower(),
    )


def list_output_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted([path for path in folder.iterdir() if path.is_file()], key=lambda path: path.name.lower())


def source_pdf_counts(pdf_files: list[Path], processed_sources: set[Path]) -> tuple[int, int, int]:
    total = len(pdf_files)
    processed = sum(1 for path in pdf_files if path.resolve() in processed_sources)
    remaining = total - processed
    return total, processed, remaining


def mark_processed_name(path: Path, processed_sources: set[Path]) -> str:
    status = "[완료]" if path.resolve() in processed_sources else "[대기]"
    return f"{status} {path.name}"


def work_state_path(source_folder: Path) -> Path:
    return source_folder / WORK_STATE_FILENAME


def load_work_state(source_folder: Path) -> dict[str, object]:
    path = work_state_path(source_folder)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_work_state(source_folder: Path, state: dict[str, object]) -> None:
    source_folder.mkdir(parents=True, exist_ok=True)
    work_state_path(source_folder).write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def build_work_state(
    source_folder: Path,
    output_folder: Path | None,
    processed_sources: set[Path],
    last_selected_pdf: Path | None,
    year: str,
    doc_type: str,
) -> dict[str, object]:
    processed_names = sorted({path.name for path in processed_sources if path.parent.resolve() == source_folder.resolve()})
    return {
        "version": 1,
        "source_folder": str(source_folder),
        "output_folder": str(output_folder) if output_folder else "",
        "processed_files": processed_names,
        "last_selected_pdf": last_selected_pdf.name if last_selected_pdf else "",
        "year": year,
        "doc_type": doc_type,
    }


def processed_sources_from_state(source_folder: Path, pdf_files: list[Path], state: dict[str, object]) -> set[Path]:
    raw_names = state.get("processed_files", [])
    if not isinstance(raw_names, list):
        return set()
    processed_names = {name for name in raw_names if isinstance(name, str)}
    return {path.resolve() for path in pdf_files if path.name in processed_names and path.parent.resolve() == source_folder.resolve()}


class FileMakerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1320x820")
        self.minsize(1080, 680)

        self.legal_dongs: list[LegalDong] = []
        self.current_suggestions: list[LegalDong] = []
        self.selected_dong: LegalDong | None = None
        self.source_pdf_files: list[Path] = []
        self.output_files: list[Path] = []
        self.processed_sources: set[Path] = set()
        self.restoring_state = False
        self.current_pdf_doc = None
        self.current_pdf_image: tk.PhotoImage | None = None
        self.pdf_state = PdfRenderState()
        self.address_region = None
        self.region_drag = None
        self.region_new = False
        self.render_zoom = 1.0
        self.rendered_page_index = None

        self.code_file = tk.StringVar(value=str(DEFAULT_CODE_FILE))
        self.source_folder = tk.StringVar()
        self.source_file = tk.StringVar()
        self.output_folder = tk.StringVar()
        self.status_text = tk.StringVar(value="법정동 코드 파일을 로드하세요.")
        self.pdf_status = tk.StringVar(value="원본 PDF를 선택하세요.")
        self.region_status = tk.StringVar(value="주소 부분을 왼쪽 버튼으로 드래그하세요. (텍스트 PDF)")
        self.source_count_text = tk.StringVar(value="전체 PDF 0개 · 남은 0개")
        self.address_query = tk.StringVar()
        self.selected_info = tk.StringVar(value="선택된 법정동이 없습니다.")
        self.pnu_preview = tk.StringVar(value="-")
        self.address_preview = tk.StringVar(value="-")
        self.filename_preview = tk.StringVar(value="-")

        current_year = dt.date.today().year
        years = [str(year) for year in range(current_year - 5, current_year + 2)]
        self.year = tk.StringVar(value=str(current_year))
        self.doc_type = tk.StringVar(value=DOC_TYPES[0])

        self._build_widgets(years)
        self._bind_events()
        if DEFAULT_CODE_FILE.exists():
            self.load_code_file(DEFAULT_CODE_FILE)

    def _build_widgets(self, years: list[str]) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.grid(row=0, column=0, sticky=tk.NSEW)

        left = ttk.Frame(main, padding=10)
        center = ttk.Frame(main, padding=(6, 10))
        right = ttk.Frame(main, padding=10)
        main.add(left, weight=1)
        main.add(center, weight=4)
        main.add(right, weight=2)

        self._build_source_panel(left)
        self._build_pdf_panel(center)
        self._build_output_panel(right, years)

    def _build_source_panel(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)

        ttk.Label(parent, text="원본 PDF 탐색기", font=("TkDefaultFont", 11, "bold")).grid(row=0, column=0, sticky=tk.W)
        folder_row = ttk.Frame(parent)
        folder_row.grid(row=1, column=0, sticky=tk.EW, pady=(8, 4))
        folder_row.columnconfigure(0, weight=1)
        ttk.Entry(folder_row, textvariable=self.source_folder).grid(row=0, column=0, sticky=tk.EW)
        ttk.Button(folder_row, text="폴더", command=self.choose_source_folder).grid(row=0, column=1, padx=(6, 0))

        ttk.Label(parent, text="직속 PDF 목록 (Alt+1)").grid(row=2, column=0, sticky=tk.W, pady=(8, 2))
        list_frame = ttk.Frame(parent)
        list_frame.grid(row=3, column=0, sticky=tk.NSEW)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.source_list = tk.Listbox(list_frame, activestyle="dotbox", exportselection=False)
        self.source_list.grid(row=0, column=0, sticky=tk.NSEW)
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.source_list.yview)
        scrollbar.grid(row=0, column=1, sticky=tk.NS)
        self.source_list.configure(yscrollcommand=scrollbar.set)

        ttk.Label(parent, textvariable=self.source_count_text).grid(row=4, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Button(parent, text="새로고침", command=self.refresh_source_list).grid(row=5, column=0, sticky=tk.EW, pady=(8, 0))

    def _build_pdf_panel(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(4, weight=1)

        ttk.Label(parent, text="PDF 뷰어", font=("TkDefaultFont", 11, "bold")).grid(row=0, column=0, sticky=tk.W)
        toolbar = ttk.Frame(parent)
        toolbar.grid(row=1, column=0, sticky=tk.EW, pady=(8, 6))
        for index in range(8):
            toolbar.columnconfigure(index, weight=0)
        toolbar.columnconfigure(8, weight=1)
        ttk.Button(toolbar, text="이전 (PageUp)", command=self.previous_page).grid(row=0, column=0, padx=(0, 4))
        ttk.Button(toolbar, text="다음 (PageDown)", command=self.next_page).grid(row=0, column=1, padx=(0, 12))
        ttk.Button(toolbar, text="축소 (Ctrl+-)", command=self.zoom_out).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(toolbar, text="확대 (Ctrl++)", command=self.zoom_in).grid(row=0, column=3, padx=(0, 4))
        ttk.Button(toolbar, text="폭 맞춤 (Ctrl+0)", command=self.fit_width).grid(row=0, column=4, padx=(0, 12))
        ttk.Button(toolbar, text="외부 열기", command=self.open_external_pdf).grid(row=0, column=5, padx=(0, 12))
        ttk.Label(toolbar, textvariable=self.pdf_status).grid(row=0, column=8, sticky=tk.E)

        region_bar = ttk.Frame(parent)
        region_bar.grid(row=2, column=0, sticky=tk.EW, pady=(0, 4))
        ttk.Button(region_bar, text="영역 새로 지정", command=self.new_address_region).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(region_bar, text="영역 읽기", command=self.read_address_region).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(region_bar, text="영역 해제", command=self.clear_address_region).pack(side=tk.LEFT)
        ttk.Label(parent, textvariable=self.region_status, wraplength=500).grid(row=3, column=0, sticky=tk.W, pady=(0, 4))

        viewer = ttk.Frame(parent)
        viewer.grid(row=4, column=0, sticky=tk.NSEW)
        viewer.columnconfigure(0, weight=1)
        viewer.rowconfigure(0, weight=1)
        self.pdf_canvas = tk.Canvas(viewer, background="#f2f2f2", highlightthickness=0)
        self.pdf_canvas.grid(row=0, column=0, sticky=tk.NSEW)
        y_scroll = ttk.Scrollbar(viewer, orient=tk.VERTICAL, command=self.pdf_canvas.yview)
        y_scroll.grid(row=0, column=1, sticky=tk.NS)
        x_scroll = ttk.Scrollbar(viewer, orient=tk.HORIZONTAL, command=self.pdf_canvas.xview)
        x_scroll.grid(row=1, column=0, sticky=tk.EW)
        self.pdf_canvas.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.pdf_canvas.bind("<Configure>", self.on_pdf_canvas_resize)
        self.pdf_canvas.bind("<MouseWheel>", self.on_pdf_mouse_wheel)
        self.pdf_canvas.bind("<ButtonPress-2>", self.on_pdf_pan_start)
        self.pdf_canvas.bind("<B2-Motion>", self.on_pdf_pan_move)
        self.pdf_canvas.bind("<ButtonPress-1>", self.on_region_start)
        self.pdf_canvas.bind("<B1-Motion>", self.on_region_move)
        self.pdf_canvas.bind("<ButtonRelease-1>", self.on_region_end)
        self.pdf_canvas.bind("<Motion>", self.on_region_hover)
        self.bind_all("<Escape>", self.cancel_region_drag)

    def _build_output_panel(self, parent: ttk.Frame, years: list[str]) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(12, weight=1)

        ttk.Label(parent, text="출력 및 파일명", font=("TkDefaultFont", 11, "bold")).grid(row=0, column=0, sticky=tk.W)

        code_frame = ttk.LabelFrame(parent, text="법정동 코드")
        code_frame.grid(row=1, column=0, sticky=tk.EW, pady=(8, 8))
        code_frame.columnconfigure(0, weight=1)
        ttk.Entry(code_frame, textvariable=self.code_file).grid(row=0, column=0, columnspan=2, sticky=tk.EW, padx=8, pady=(8, 4))
        ttk.Button(code_frame, text="파일 선택", command=self.choose_code_file).grid(row=1, column=0, sticky=tk.EW, padx=(8, 4), pady=(0, 8))
        ttk.Button(code_frame, text="로드", command=self.reload_code_file).grid(row=1, column=1, sticky=tk.EW, padx=(4, 8), pady=(0, 8))
        ttk.Label(code_frame, textvariable=self.status_text, wraplength=330).grid(row=2, column=0, columnspan=2, sticky=tk.W, padx=8, pady=(0, 8))

        out_row = ttk.Frame(parent)
        out_row.grid(row=2, column=0, sticky=tk.EW, pady=(0, 8))
        out_row.columnconfigure(0, weight=1)
        ttk.Entry(out_row, textvariable=self.output_folder).grid(row=0, column=0, sticky=tk.EW)
        ttk.Button(out_row, text="출력 폴더", command=self.choose_output_folder).grid(row=0, column=1, padx=(6, 0))

        option_frame = ttk.Frame(parent)
        option_frame.grid(row=3, column=0, sticky=tk.EW, pady=(0, 8))
        ttk.Label(option_frame, text="연도").grid(row=0, column=0, sticky=tk.W)
        ttk.Combobox(option_frame, textvariable=self.year, values=years, state="readonly", width=10).grid(row=0, column=1, padx=(6, 14))
        ttk.Label(option_frame, text="종류").grid(row=0, column=2, sticky=tk.W)
        ttk.Combobox(option_frame, textvariable=self.doc_type, values=DOC_TYPES, state="readonly", width=14).grid(row=0, column=3, padx=(6, 0))

        ttk.Label(parent, text="주소 검색 (Alt+2, Enter)").grid(row=4, column=0, sticky=tk.W)
        address_row = ttk.Frame(parent)
        address_row.grid(row=5, column=0, sticky=tk.EW, pady=(2, 6))
        address_row.columnconfigure(0, weight=1)
        self.address_entry = ttk.Entry(address_row, textvariable=self.address_query)
        self.address_entry.grid(row=0, column=0, sticky=tk.EW)
        ttk.Button(address_row, text="검색 (Enter)", command=self.refresh_suggestions).grid(row=0, column=1, padx=(6, 0))

        ttk.Label(parent, text="추천 목록 (Alt+3)").grid(row=6, column=0, sticky=tk.W, pady=(2, 2))
        suggestion_frame = ttk.Frame(parent)
        suggestion_frame.grid(row=7, column=0, sticky=tk.NSEW)
        suggestion_frame.columnconfigure(0, weight=1)
        suggestion_frame.rowconfigure(0, weight=1)
        self.suggestion_list = tk.Listbox(suggestion_frame, height=8, activestyle="dotbox", exportselection=False)
        self.suggestion_list.grid(row=0, column=0, sticky=tk.NSEW)
        suggestion_scroll = ttk.Scrollbar(suggestion_frame, orient=tk.VERTICAL, command=self.suggestion_list.yview)
        suggestion_scroll.grid(row=0, column=1, sticky=tk.NS)
        self.suggestion_list.configure(yscrollcommand=suggestion_scroll.set)

        ttk.Label(parent, textvariable=self.selected_info, wraplength=340).grid(row=8, column=0, sticky=tk.W, pady=(6, 8))

        preview = ttk.LabelFrame(parent, text="미리보기")
        preview.grid(row=9, column=0, sticky=tk.EW, pady=(0, 8))
        preview.columnconfigure(1, weight=1)
        ttk.Label(preview, text="PNU").grid(row=0, column=0, sticky=tk.W, padx=8, pady=3)
        ttk.Label(preview, textvariable=self.pnu_preview).grid(row=0, column=1, sticky=tk.W, padx=8, pady=3)
        ttk.Label(preview, text="주소").grid(row=1, column=0, sticky=tk.W, padx=8, pady=3)
        ttk.Label(preview, textvariable=self.address_preview, wraplength=260).grid(row=1, column=1, sticky=tk.W, padx=8, pady=3)
        ttk.Label(preview, text="파일명").grid(row=2, column=0, sticky=tk.W, padx=8, pady=3)
        ttk.Label(preview, textvariable=self.filename_preview, wraplength=260).grid(row=2, column=1, sticky=tk.W, padx=8, pady=3)

        ttk.Button(parent, text="복사 (Ctrl+Enter)", command=self.copy_file).grid(row=10, column=0, sticky=tk.EW, pady=(0, 10))

        ttk.Label(parent, text="출력 폴더 파일 (Alt+4)").grid(row=11, column=0, sticky=tk.W)
        output_frame = ttk.Frame(parent)
        output_frame.grid(row=12, column=0, sticky=tk.NSEW, pady=(2, 0))
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)
        self.output_list = tk.Listbox(output_frame, activestyle="dotbox", exportselection=False)
        self.output_list.grid(row=0, column=0, sticky=tk.NSEW)
        output_scroll = ttk.Scrollbar(output_frame, orient=tk.VERTICAL, command=self.output_list.yview)
        output_scroll.grid(row=0, column=1, sticky=tk.NS)
        self.output_list.configure(yscrollcommand=output_scroll.set)
        ttk.Button(parent, text="출력 목록 새로고침", command=self.refresh_output_list).grid(row=13, column=0, sticky=tk.EW, pady=(8, 0))
        ttk.Label(parent, text="Alt+1 원본목록 · Alt+2 주소입력 · Shift+방향키 PDF 이동 · Ctrl+Enter 복사", wraplength=340).grid(row=14, column=0, sticky=tk.W, pady=(8, 0))

    def _bind_events(self) -> None:
        self.year.trace_add("write", lambda *_: self.on_option_changed())
        self.doc_type.trace_add("write", lambda *_: self.on_option_changed())
        self.source_file.trace_add("write", lambda *_: self.update_preview())
        self.output_folder.trace_add("write", lambda *_: self.on_output_folder_changed())
        self.source_list.bind("<<ListboxSelect>>", lambda _event: self.select_source_pdf())
        self.suggestion_list.bind("<<ListboxSelect>>", lambda _event: self.select_current_suggestion())
        self.address_entry.bind("<Return>", lambda _event: self.refresh_suggestions())
        self.bind_shortcuts()

    def bind_shortcuts(self) -> None:
        shortcut_bindings = {
            "<Control-Return>": self.shortcut_copy,
            "<Control-KP_Enter>": self.shortcut_copy,
            "<Alt-KeyPress-1>": self.shortcut_focus_source_list,
            "<Alt-Key-1>": self.shortcut_focus_source_list,
            "<Alt-1>": self.shortcut_focus_source_list,
            "<Alt-KeyPress-2>": self.shortcut_focus_address_entry,
            "<Alt-Key-2>": self.shortcut_focus_address_entry,
            "<Alt-2>": self.shortcut_focus_address_entry,
            "<Alt-KeyPress-3>": self.shortcut_focus_suggestion_list,
            "<Alt-Key-3>": self.shortcut_focus_suggestion_list,
            "<Alt-3>": self.shortcut_focus_suggestion_list,
            "<Alt-KeyPress-4>": self.shortcut_focus_output_list,
            "<Alt-Key-4>": self.shortcut_focus_output_list,
            "<Alt-4>": self.shortcut_focus_output_list,
            "<F5>": self.shortcut_refresh_lists,
            "<Prior>": self.shortcut_previous_page,
            "<Next>": self.shortcut_next_page,
            "<Control-minus>": self.shortcut_zoom_out,
            "<Control-KP_Subtract>": self.shortcut_zoom_out,
            "<Control-plus>": self.shortcut_zoom_in,
            "<Control-KeyPress-plus>": self.shortcut_zoom_in,
            "<Control-equal>": self.shortcut_zoom_in,
            "<Control-KP_Add>": self.shortcut_zoom_in,
            "<Control-KeyPress-0>": self.shortcut_fit_width,
            "<Shift-Up>": self.shortcut_pan_up,
            "<Shift-KeyPress-Up>": self.shortcut_pan_up,
            "<Shift-Down>": self.shortcut_pan_down,
            "<Shift-KeyPress-Down>": self.shortcut_pan_down,
            "<Shift-Left>": self.shortcut_pan_left,
            "<Shift-KeyPress-Left>": self.shortcut_pan_left,
            "<Shift-Right>": self.shortcut_pan_right,
            "<Shift-KeyPress-Right>": self.shortcut_pan_right,
        }
        for sequence, handler in shortcut_bindings.items():
            self.bind_all(sequence, handler)

        for widget in (self.source_list, self.suggestion_list, self.output_list, self.address_entry):
            widget.bind("<Prior>", self.shortcut_previous_page)
            widget.bind("<Next>", self.shortcut_next_page)
            widget.bind("<Shift-Up>", self.shortcut_pan_up)
            widget.bind("<Shift-KeyPress-Up>", self.shortcut_pan_up)
            widget.bind("<Shift-Down>", self.shortcut_pan_down)
            widget.bind("<Shift-KeyPress-Down>", self.shortcut_pan_down)
            widget.bind("<Shift-Left>", self.shortcut_pan_left)
            widget.bind("<Shift-KeyPress-Left>", self.shortcut_pan_left)
            widget.bind("<Shift-Right>", self.shortcut_pan_right)
            widget.bind("<Shift-KeyPress-Right>", self.shortcut_pan_right)

        self.source_list.bind("<Return>", self.shortcut_open_selected_source)
        self.suggestion_list.bind("<Return>", self.shortcut_select_suggestion)

    def select_source_file_in_list(self, source: Path) -> bool:
        try:
            resolved_source = source.resolve()
        except OSError:
            return False
        for index, pdf_path in enumerate(self.source_pdf_files):
            if pdf_path.resolve() == resolved_source:
                self.source_list.selection_clear(0, tk.END)
                self.source_list.selection_set(index)
                self.source_list.activate(index)
                self.source_list.see(index)
                return True
        return False

    def focus_source_list(self) -> None:
        self.source_list.focus_set()
        if self.source_file.get() and self.select_source_file_in_list(Path(self.source_file.get())):
            return
        if self.source_pdf_files and not self.source_list.curselection():
            self.source_list.selection_set(0)
            self.source_list.activate(0)
            self.source_list.see(0)

    def focus_address_entry(self) -> None:
        self.address_entry.focus_set()
        self.address_entry.selection_range(0, tk.END)

    def focus_suggestion_list(self) -> None:
        self.suggestion_list.focus_set()
        if self.current_suggestions and not self.suggestion_list.curselection():
            self.suggestion_list.selection_set(0)
            self.suggestion_list.see(0)

    def focus_output_list(self) -> None:
        self.output_list.focus_set()
        if self.output_files and not self.output_list.curselection():
            self.output_list.selection_set(0)
            self.output_list.see(0)
    def pan_pdf(self, dx_units: int = 0, dy_units: int = 0) -> None:
        if self.current_pdf_doc is None:
            return
        if dx_units:
            self.pdf_canvas.xview_scroll(dx_units, "units")
        if dy_units:
            self.pdf_canvas.yview_scroll(dy_units, "units")

    def shortcut_copy(self, _event: tk.Event) -> str:
        self.copy_file()
        return "break"

    def shortcut_focus_source_list(self, _event: tk.Event) -> str:
        self.focus_source_list()
        return "break"

    def shortcut_focus_address_entry(self, _event: tk.Event) -> str:
        self.focus_address_entry()
        return "break"

    def shortcut_focus_suggestion_list(self, _event: tk.Event) -> str:
        self.focus_suggestion_list()
        return "break"

    def shortcut_focus_output_list(self, _event: tk.Event) -> str:
        self.focus_output_list()
        return "break"

    def shortcut_refresh_lists(self, _event: tk.Event) -> str:
        self.refresh_source_list()
        self.refresh_output_list()
        return "break"

    def shortcut_previous_page(self, _event: tk.Event) -> str:
        self.previous_page()
        return "break"

    def shortcut_next_page(self, _event: tk.Event) -> str:
        self.next_page()
        return "break"

    def shortcut_zoom_out(self, _event: tk.Event) -> str:
        self.zoom_out()
        return "break"

    def shortcut_zoom_in(self, _event: tk.Event) -> str:
        self.zoom_in()
        return "break"

    def shortcut_fit_width(self, _event: tk.Event) -> str:
        self.fit_width()
        return "break"
    def shortcut_pan_up(self, _event: tk.Event) -> str:
        self.pan_pdf(dy_units=-3)
        return "break"

    def shortcut_pan_down(self, _event: tk.Event) -> str:
        self.pan_pdf(dy_units=3)
        return "break"

    def shortcut_pan_left(self, _event: tk.Event) -> str:
        self.pan_pdf(dx_units=-3)
        return "break"

    def shortcut_pan_right(self, _event: tk.Event) -> str:
        self.pan_pdf(dx_units=3)
        return "break"

    def shortcut_open_selected_source(self, _event: tk.Event) -> str:
        if not self.source_list.curselection() and self.source_pdf_files:
            self.source_list.selection_set(0)
            self.source_list.see(0)
        self.select_source_pdf()
        return "break"

    def shortcut_select_suggestion(self, _event: tk.Event) -> str:
        if not self.suggestion_list.curselection() and self.current_suggestions:
            self.suggestion_list.selection_set(0)
            self.suggestion_list.see(0)
        self.select_current_suggestion()
        return "break"
    def choose_code_file(self) -> None:
        path = filedialog.askopenfilename(
            title="법정동 코드 파일 선택",
            filetypes=(("Text files", "*.txt"), ("All files", "*.*")),
        )
        if path:
            self.code_file.set(path)
            self.load_code_file(Path(path))

    def reload_code_file(self) -> None:
        self.load_code_file(Path(self.code_file.get()))

    def choose_source_folder(self) -> None:
        path = filedialog.askdirectory(title="원본 PDF 폴더 선택")
        if path:
            self.load_source_workspace(Path(path))

    def choose_output_folder(self) -> None:
        path = filedialog.askdirectory(title="출력 폴더 선택")
        if path:
            self.output_folder.set(path)

    def on_output_folder_changed(self) -> None:
        self.refresh_output_list()
        self.update_preview()
        self.auto_save_work_state()

    def on_option_changed(self) -> None:
        self.update_preview()
        self.auto_save_work_state()

    def load_source_workspace(self, folder: Path) -> None:
        self.restoring_state = True
        try:
            self.source_folder.set(str(folder))
            state = load_work_state(folder)
            saved_output = state.get("output_folder")
            if isinstance(saved_output, str) and saved_output and Path(saved_output).is_dir():
                self.output_folder.set(saved_output)
            saved_year = state.get("year")
            if isinstance(saved_year, str) and saved_year.isdigit():
                self.year.set(saved_year)
            saved_doc_type = state.get("doc_type")
            if isinstance(saved_doc_type, str) and saved_doc_type in DOC_TYPES:
                self.doc_type.set(saved_doc_type)
            self.refresh_source_list(load_state=False)
            self.processed_sources = processed_sources_from_state(folder, self.source_pdf_files, state)
            self.refresh_source_list(load_state=False)
            self.refresh_output_list()
            self.restore_last_selected_pdf(state)
        finally:
            self.restoring_state = False
        self.auto_save_work_state()

    def restore_last_selected_pdf(self, state: dict[str, object]) -> None:
        last_selected = state.get("last_selected_pdf")
        if not isinstance(last_selected, str) or not last_selected:
            return
        for index, pdf_path in enumerate(self.source_pdf_files):
            if pdf_path.name == last_selected:
                self.source_list.selection_clear(0, tk.END)
                self.source_list.selection_set(index)
                self.source_list.see(index)
                self.source_file.set(str(pdf_path))
                self.load_pdf(pdf_path)
                return

    def current_source_folder_path(self) -> Path | None:
        if not self.source_folder.get():
            return None
        folder = Path(self.source_folder.get())
        return folder if folder.is_dir() else None

    def auto_save_work_state(self) -> None:
        if self.restoring_state:
            return
        folder = self.current_source_folder_path()
        if folder is None:
            return
        output = Path(self.output_folder.get()) if self.output_folder.get() else None
        selected = Path(self.source_file.get()) if self.source_file.get() else None
        state = build_work_state(folder, output, self.processed_sources, selected, self.year.get(), self.doc_type.get())
        try:
            save_work_state(folder, state)
        except OSError as exc:
            self.status_text.set(f"작업 상태 저장 실패: {exc}")

    def load_code_file(self, path: Path) -> None:
        try:
            dongs = load_legal_dongs(path)
        except Exception as exc:
            messagebox.showerror("로드 실패", f"법정동 코드 파일을 읽지 못했습니다.\n\n{exc}")
            self.status_text.set("로드 실패")
            return

        if not dongs:
            messagebox.showwarning("로드 결과 없음", "존재 상태의 법정동 코드를 찾지 못했습니다.")
            self.status_text.set("로드된 법정동 없음")
            return

        self.legal_dongs = dongs
        self.selected_dong = None
        self.status_text.set(f"{len(dongs):,}개 법정동 로드됨: {path.name}")
        self.selected_info.set("선택된 법정동이 없습니다.")
        self.refresh_suggestions()

    def refresh_source_list(self, load_state: bool = True) -> None:
        folder = Path(self.source_folder.get()) if self.source_folder.get() else Path()
        self.source_pdf_files = list_pdf_files(folder)
        if load_state and folder.is_dir():
            state = load_work_state(folder)
            self.processed_sources = processed_sources_from_state(folder, self.source_pdf_files, state)
        self.source_list.delete(0, tk.END)
        for pdf_path in self.source_pdf_files:
            self.source_list.insert(tk.END, mark_processed_name(pdf_path, self.processed_sources))
        total, _processed, remaining = source_pdf_counts(self.source_pdf_files, self.processed_sources)
        self.source_count_text.set(f"전체 PDF {total:,}개 · 남은 {remaining:,}개")
        if not self.source_pdf_files:
            self.source_file.set("")
            self.clear_pdf_viewer("선택한 폴더에 PDF가 없습니다.")

    def refresh_output_list(self, selected_path: Path | None = None) -> None:
        folder = Path(self.output_folder.get()) if self.output_folder.get() else Path()
        self.output_files = list_output_files(folder)
        self.output_list.delete(0, tk.END)
        selected_index: int | None = None
        for index, file_path in enumerate(self.output_files):
            self.output_list.insert(tk.END, file_path.name)
            if selected_path and file_path.resolve() == selected_path.resolve():
                selected_index = index
        if selected_index is not None:
            self.output_list.selection_clear(0, tk.END)
            self.output_list.selection_set(selected_index)
            self.output_list.see(selected_index)

    def select_source_pdf(self) -> None:
        selection = self.source_list.curselection()
        if not selection:
            return
        index = selection[0]
        if index >= len(self.source_pdf_files):
            return
        pdf_path = self.source_pdf_files[index]
        self.source_file.set(str(pdf_path))
        self.load_pdf(pdf_path)
        self.auto_save_work_state()

    def refresh_suggestions(self) -> None:
        query = self.address_query.get()
        self.current_suggestions = search_legal_dongs(query, self.legal_dongs)
        self.suggestion_list.delete(0, tk.END)
        for dong in self.current_suggestions:
            self.suggestion_list.insert(tk.END, f"{dong.code}  {dong.name}")

        if self.current_suggestions:
            self.suggestion_list.selection_clear(0, tk.END)
            self.suggestion_list.selection_set(0)
            self.suggestion_list.see(0)
            self.selected_dong = self.current_suggestions[0]
            self.selected_info.set(f"선택: {self.selected_dong.code}  {self.selected_dong.name}")
        else:
            self.selected_dong = None
            self.selected_info.set("검색 결과가 없습니다.")

        self.update_preview()

    def select_current_suggestion(self) -> None:
        selection = self.suggestion_list.curselection()
        if not selection:
            return
        index = selection[0]
        if index >= len(self.current_suggestions):
            return
        self.selected_dong = self.current_suggestions[index]
        self.selected_info.set(f"선택: {self.selected_dong.code}  {self.selected_dong.name}")
        self.update_preview()

    def clear_pdf_viewer(self, message: str) -> None:
        self.close_pdf_doc()
        self.rendered_page_index = None
        self.pdf_state = PdfRenderState()
        self.current_pdf_image = None
        self.pdf_canvas.delete("all")
        self.pdf_canvas.create_text(24, 24, anchor=tk.NW, text=message, fill="#555555")
        self.pdf_canvas.configure(scrollregion=self.pdf_canvas.bbox("all"))
        self.pdf_status.set(message)

    def close_pdf_doc(self) -> None:
        if self.current_pdf_doc is not None:
            try:
                self.current_pdf_doc.close()
            except Exception:
                pass
        self.current_pdf_doc = None

    def load_pdf(self, path: Path) -> None:
        self.cancel_region_drag()
        self.address_query.set("")
        self.current_suggestions = []
        self.suggestion_list.delete(0, tk.END)
        self.selected_dong = None
        self.selected_info.set("선택된 법정동이 없습니다.")
        self.update_preview()
        self.region_status.set("주소 부분을 드래그해서 지정하세요.")
        if fitz is None:
            self.clear_pdf_viewer("PyMuPDF가 없어 PDF를 표시할 수 없습니다.")
            return
        try:
            self.close_pdf_doc()
            self.current_pdf_doc = fitz.open(path)
            page_count = len(self.current_pdf_doc)
            if page_count == 0:
                self.clear_pdf_viewer("페이지가 없는 PDF입니다.")
                return
            target = self.address_region.page_index if self.address_region else 0
            page_index = target if target < page_count else 0
            self.pdf_state = PdfRenderState(path=path, page_index=page_index, page_count=page_count, zoom=1.0, fit_to_width=True)
            self.render_pdf_page()
            if self.address_region:
                if target >= page_count:
                    self.region_status.set(f"지정한 {target + 1}페이지가 없습니다. 첫 페이지에서 영역을 새로 지정하세요.")
                else:
                    self.read_address_region()
        except Exception as exc:
            self.clear_pdf_viewer(f"PDF를 열 수 없습니다.\n{exc}")

    def render_pdf_page(self) -> None:
        self.cancel_region_drag()
        if fitz is None or self.current_pdf_doc is None or self.pdf_state.path is None:
            return
        try:
            page = self.current_pdf_doc.load_page(self.pdf_state.page_index)
            zoom = self.pdf_state.zoom
            if self.pdf_state.fit_to_width:
                canvas_width = max(self.pdf_canvas.winfo_width() - 24, 200)
                page_width = float(page.rect.width) or 1.0
                zoom = max(canvas_width / page_width, 0.25)
            matrix = fitz.Matrix(zoom, zoom)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            self.current_pdf_image = tk.PhotoImage(data=pixmap.tobytes("ppm"))
            self.pdf_canvas.delete("all")
            self.pdf_canvas.create_image(10, 10, image=self.current_pdf_image, anchor=tk.NW)
            self.render_zoom = zoom
            self.rendered_page_index = self.pdf_state.page_index
            self.draw_address_region()
            width = self.current_pdf_image.width() + 20
            height = self.current_pdf_image.height() + 20
            self.pdf_canvas.configure(scrollregion=(0, 0, width, height))
            mode = "폭 맞춤" if self.pdf_state.fit_to_width else f"{int(self.pdf_state.zoom * 100)}%"
            self.pdf_status.set(f"{self.pdf_state.page_index + 1}/{self.pdf_state.page_count} 페이지 · {mode}")
        except Exception as exc:
            self.clear_pdf_viewer(f"PDF 페이지를 표시할 수 없습니다.\n{exc}")

    def region_page(self):
        if self.current_pdf_doc is None or self.rendered_page_index != self.pdf_state.page_index:
            return None
        return self.current_pdf_doc[self.pdf_state.page_index]

    def region_canvas_bounds(self):
        page = self.region_page()
        region = self.address_region
        if page is None or region is None or region.page_index != self.pdf_state.page_index:
            return None
        x0, y0, x1, y1 = region.bounds
        return (10 + x0 * page.rect.width * self.render_zoom,
                10 + y0 * page.rect.height * self.render_zoom,
                10 + x1 * page.rect.width * self.render_zoom,
                10 + y1 * page.rect.height * self.render_zoom)

    def draw_address_region(self):
        self.pdf_canvas.delete("address_region")
        bounds = self.region_canvas_bounds()
        if bounds is None:
            return
        x0, y0, x1, y1 = bounds
        self.pdf_canvas.create_rectangle(*bounds, outline="#1565c0", width=2, tags="address_region")
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            self.pdf_canvas.create_rectangle(x-4, y-4, x+4, y+4, fill="white", outline="#1565c0", tags="address_region")

    def region_hit(self, x, y):
        bounds = self.region_canvas_bounds()
        if bounds is None or self.region_new:
            return "new"
        x0, y0, x1, y1 = bounds
        for name, cx, cy in (("nw", x0, y0), ("ne", x1, y0), ("se", x1, y1), ("sw", x0, y1)):
            if abs(x-cx) <= 7 and abs(y-cy) <= 7:
                return name
        return "move" if x0 <= x <= x1 and y0 <= y <= y1 else "new"

    def on_region_hover(self, event):
        if self.region_drag or self.region_page() is None:
            return
        mode = self.region_hit(self.pdf_canvas.canvasx(event.x), self.pdf_canvas.canvasy(event.y))
        self.pdf_canvas.configure(cursor="fleur" if mode == "move" else "crosshair")

    def new_address_region(self):
        self.cancel_region_drag()
        self.region_new = True
        self.pdf_canvas.configure(cursor="crosshair")
        self.region_status.set("주소 영역을 새로 드래그하세요. Esc로 취소합니다.")

    def clear_address_region(self):
        self.cancel_region_drag()
        self.address_region = None
        self.draw_address_region()
        self.pdf_canvas.configure(cursor="")
        self.region_status.set("영역을 해제했습니다. 주소 입력은 유지됩니다.")

    def cancel_region_drag(self, _event=None):
        if self.region_drag:
            self.address_region = self.region_drag[2]
        self.region_drag = None
        self.region_new = False
        if hasattr(self, "pdf_canvas"):
            self.draw_address_region()
            self.pdf_canvas.configure(cursor="")
        return "break"

    def on_region_start(self, event):
        page = self.region_page()
        if page is None:
            return
        x, y = self.pdf_canvas.canvasx(event.x), self.pdf_canvas.canvasy(event.y)
        if not (10 <= x <= 10 + page.rect.width*self.render_zoom and
                10 <= y <= 10 + page.rect.height*self.render_zoom):
            return
        self.pdf_canvas.focus_set()
        point = canvas_to_relative(x, y, page.rect, self.render_zoom)
        self.region_drag = (self.region_hit(x, y), point, self.address_region)
        if self.region_drag[0] == "new":
            self.address_region = AddressRegion(self.pdf_state.page_index, (*point, *point))
        self.draw_address_region()

    def on_region_move(self, event):
        page = self.region_page()
        if self.region_drag is None or page is None:
            return
        mode, start, original = self.region_drag
        point = canvas_to_relative(self.pdf_canvas.canvasx(event.x), self.pdf_canvas.canvasy(event.y), page.rect, self.render_zoom)
        if mode == "new":
            bounds = normalized_bounds(start, point)
        elif mode == "move":
            bounds = move_bounds(original.bounds, point[0]-start[0], point[1]-start[1])
        else:
            x0, y0, x1, y1 = original.bounds
            opposite = {"nw": (x1, y1), "ne": (x0, y1), "se": (x0, y0), "sw": (x1, y0)}[mode]
            bounds = normalized_bounds(opposite, point)
        self.address_region = AddressRegion(self.pdf_state.page_index, bounds)
        self.draw_address_region()

    def on_region_end(self, event):
        if self.region_drag is None:
            return
        self.on_region_move(event)
        bounds = self.region_canvas_bounds()
        if bounds is None or bounds[2]-bounds[0] < 4 or bounds[3]-bounds[1] < 4:
            self.cancel_region_drag()
            self.region_status.set("영역이 너무 작습니다. 주소 전체를 포함해 드래그하세요.")
            return
        self.region_drag = None
        self.region_new = False
        self.read_address_region()

    def read_address_region(self):
        page = self.region_page()
        if self.region_drag:
            return
        if page is None or self.address_region is None:
            self.region_status.set("PDF를 열고 주소 영역을 먼저 지정하세요.")
            return
        if self.address_region.page_index != self.pdf_state.page_index:
            self.region_status.set(f"영역은 {self.address_region.page_index + 1}페이지에 있습니다. 해당 페이지로 이동하거나 새로 지정하세요.")
            return
        try:
            text = extract_region_text(page, self.address_region.bounds)
            if not text:
                self.region_status.set("영역에 추출 가능한 텍스트가 없습니다. 영역을 조절하거나 직접 입력하세요. (OCR 미지원)")
                return
            self.address_query.set(text)
            self.refresh_suggestions()
            self.region_status.set("주소 입력·검색 완료 · 영역 내부 드래그: 이동 / 모서리: 크기 조절")
        except Exception as exc:
            self.region_status.set(f"텍스트 추출 실패: {exc} · 직접 입력할 수 있습니다.")

    def on_pdf_canvas_resize(self, _event: tk.Event) -> None:
        if self.pdf_state.fit_to_width and self.current_pdf_doc is not None:
            self.after_idle(self.render_pdf_page)

    def on_pdf_mouse_wheel(self, event: tk.Event) -> str:
        if not self.current_pdf_doc:
            return "break"
        if getattr(event, "delta", 0) > 0:
            self.set_zoom(min(self.pdf_state.zoom + 0.2, 4.0))
        else:
            self.set_zoom(max(self.pdf_state.zoom - 0.2, 0.25))
        return "break"

    def on_pdf_pan_start(self, event: tk.Event) -> None:
        self.pdf_canvas.scan_mark(event.x, event.y)

    def on_pdf_pan_move(self, event: tk.Event) -> None:
        self.pdf_canvas.scan_dragto(event.x, event.y, gain=1)

    def previous_page(self) -> None:
        if not self.current_pdf_doc or self.pdf_state.page_index <= 0:
            return
        self.pdf_state = PdfRenderState(
            path=self.pdf_state.path,
            page_index=self.pdf_state.page_index - 1,
            page_count=self.pdf_state.page_count,
            zoom=self.pdf_state.zoom,
            fit_to_width=self.pdf_state.fit_to_width,
        )
        self.render_pdf_page()

    def next_page(self) -> None:
        if not self.current_pdf_doc or self.pdf_state.page_index >= self.pdf_state.page_count - 1:
            return
        self.pdf_state = PdfRenderState(
            path=self.pdf_state.path,
            page_index=self.pdf_state.page_index + 1,
            page_count=self.pdf_state.page_count,
            zoom=self.pdf_state.zoom,
            fit_to_width=self.pdf_state.fit_to_width,
        )
        self.render_pdf_page()

    def zoom_in(self) -> None:
        self.set_zoom(min(self.pdf_state.zoom + 0.2, 4.0))

    def zoom_out(self) -> None:
        self.set_zoom(max(self.pdf_state.zoom - 0.2, 0.25))

    def set_zoom(self, zoom: float) -> None:
        if not self.current_pdf_doc:
            return
        self.pdf_state = PdfRenderState(
            path=self.pdf_state.path,
            page_index=self.pdf_state.page_index,
            page_count=self.pdf_state.page_count,
            zoom=zoom,
            fit_to_width=False,
        )
        self.render_pdf_page()

    def fit_width(self) -> None:
        if not self.current_pdf_doc:
            return
        self.pdf_state = PdfRenderState(
            path=self.pdf_state.path,
            page_index=self.pdf_state.page_index,
            page_count=self.pdf_state.page_count,
            zoom=self.pdf_state.zoom,
            fit_to_width=True,
        )
        self.render_pdf_page()

    def open_external_pdf(self) -> None:
        if not self.pdf_state.path:
            messagebox.showinfo("PDF 없음", "외부로 열 PDF를 먼저 선택하세요.")
            return
        try:
            os.startfile(self.pdf_state.path)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("열기 실패", f"PDF를 외부 프로그램으로 열지 못했습니다.\n\n{exc}")

    def preview_values(self) -> tuple[str, str, Path] | None:
        if not self.selected_dong:
            return None
        lot = parse_lot_number(self.address_query.get())
        if not lot:
            return None
        source = Path(self.source_file.get())
        output = Path(self.output_folder.get()) if self.output_folder.get() else Path()
        pnu = make_pnu(self.selected_dong.code, lot)
        full_address = f"{self.selected_dong.name} {lot.display}"
        filename = build_destination_path(source, output, self.year.get(), self.doc_type.get(), pnu, full_address)
        return pnu, full_address, filename

    def update_preview(self) -> None:
        values = self.preview_values()
        if not values:
            self.pnu_preview.set("-")
            self.address_preview.set("-")
            self.filename_preview.set("-")
            return
        pnu, full_address, destination = values
        self.pnu_preview.set(pnu)
        self.address_preview.set(full_address)
        self.filename_preview.set(destination.name)

    def validate_before_copy(self) -> tuple[Path, Path, str, str] | None:
        source = Path(self.source_file.get())
        output = Path(self.output_folder.get())

        if not self.legal_dongs:
            messagebox.showerror("확인 필요", "법정동 코드 파일을 먼저 로드하세요.")
            return None
        if not source.is_file():
            messagebox.showerror("확인 필요", "원본 PDF를 선택하세요.")
            return None
        if source.suffix.lower() != PDF_SUFFIX:
            messagebox.showerror("확인 필요", "PDF 파일만 원본으로 선택할 수 있습니다.")
            return None
        if not output.is_dir():
            messagebox.showerror("확인 필요", "출력 폴더를 선택하세요.")
            return None
        if self.doc_type.get() not in DOC_TYPES:
            messagebox.showerror("확인 필요", "문서 종류를 선택하세요.")
            return None
        if not self.year.get().isdigit():
            messagebox.showerror("확인 필요", "연도를 선택하세요.")
            return None
        if not self.selected_dong:
            messagebox.showerror("확인 필요", "추천 목록에서 법정동을 선택하세요.")
            return None

        lot = parse_lot_number(self.address_query.get())
        if not lot:
            messagebox.showerror("확인 필요", "주소에 지번을 포함해 입력하세요. 예: 역삼동 123-4")
            return None

        pnu = make_pnu(self.selected_dong.code, lot)
        full_address = f"{self.selected_dong.name} {lot.display}"
        return source, output, pnu, full_address

    def confirm_destination_collision(self, plan: DestinationPlan) -> bool:
        if not plan.needs_confirmation:
            return True

        base_old, base_new = plan.renames[-1]
        shifted_count = len(plan.renames) - 1
        shifted_message = ""
        if shifted_count:
            shifted_message = f"\n기존 __번호 파일 {shifted_count}개도 번호가 뒤로 밀립니다."
        return messagebox.askyesno(
            "파일명 충돌 확인",
            "같은 이름의 파일이 출력 폴더에 이미 있습니다.\n\n"
            f"기존 파일을 다음 이름으로 변경합니다.\n{base_old.name}\n→ {base_new.name}\n\n"
            f"새로 복사할 파일은 다음 이름으로 생성합니다.\n{plan.destination.name}"
            f"{shifted_message}\n\n계속하시겠습니까?",
            icon="warning",
        )

    def copy_file(self) -> None:
        values = self.validate_before_copy()
        if not values:
            return
        source, output, pnu, full_address = values
        plan = build_destination_plan(source, output, self.year.get(), self.doc_type.get(), pnu, full_address)
        if not self.confirm_destination_collision(plan):
            return
        destination = plan.destination
        try:
            copy_with_destination_plan(source, plan)
        except Exception as exc:
            messagebox.showerror("복사 실패", f"파일을 복사하지 못했습니다.\n\n{exc}")
            return

        self.processed_sources.add(source.resolve())
        self.auto_save_work_state()
        self.refresh_source_list(load_state=False)
        self.restore_source_selection(source)
        self.refresh_output_list(destination)
        self.update_preview()
        messagebox.showinfo("복사 완료", f"파일을 복사했습니다.\n\n{destination}")
    def restore_source_selection(self, source: Path) -> None:
        self.select_source_file_in_list(source)


def main() -> None:
    app = FileMakerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
















