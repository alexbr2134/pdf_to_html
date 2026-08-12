"""Table discovery for vector PDFs: Camelot detect -> pdfplumber lines/text -> Camelot extract.

Публичный API: find_tables_with_source / find_tables_smart,
table_looks_like_prose, scan_samples_table_stats.
"""

from __future__ import annotations

import logging
import re
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

TableSource = Literal["none", "lines", "text", "camelot"]

_CAMELOT_FLAVORS = ("stream", "lattice")

_TABLE_LINES = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
}

_TABLE_TEXT = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
}


@dataclass
class _TableRow:
    """Строка таблицы: список bbox ячеек (или None для spanned)."""
    cells: list[tuple[float, float, float, float] | None]


@dataclass
class TableAdapter:
    """Minimal pdfplumber.Table-like object for build_cells()."""

    bbox: tuple[float, float, float, float]
    rows: list[_TableRow]


def _read_camelot_page(pdf_path: str | Path, page_num: int, flavor: str) -> Any | None:
    """Читает одну страницу PDF через Camelot (flavor); None при ошибке."""
    import camelot

    try:
        return camelot.read_pdf(
            str(pdf_path),
            pages=str(page_num),
            flavor=flavor,
            suppress_stdout=True,
        )
    except Exception:
        return None


def camelot_page_has_tables(pdf_path: str | Path, page_num: int) -> bool:
    """Первичная детекция: Camelot stream, затем lattice."""
    for flavor in _CAMELOT_FLAVORS:
        result = _read_camelot_page(pdf_path, page_num, flavor)
        if result is not None and result.n > 0:
            return True
    return False


def _find_tables_lines(page) -> list:
    """pdfplumber find_tables со стратегией lines."""
    return page.find_tables(_TABLE_LINES)


def _find_tables_text(page) -> list:
    """pdfplumber find_tables со стратегией text."""
    return page.find_tables(_TABLE_TEXT)


def _camelot_cell_bbox(
    cell: Any,
    page_height: float,
) -> tuple[float, float, float, float]:
    """Camelot PDF coords (bottom-left origin) -> pdfplumber (top-left)."""
    return (cell.x1, page_height - cell.y2, cell.x2, page_height - cell.y1)


def _camelot_table_bbox(table: Any, page_height: float) -> tuple[float, float, float, float]:
    """Bbox таблицы Camelot в координатах pdfplumber (top-left)."""
    if table._bbox is not None:
        x0, y0, x1, y1 = table._bbox
        return (x0, page_height - y1, x1, page_height - y0)

    xs: list[float] = []
    ys_top: list[float] = []
    ys_bottom: list[float] = []
    for row in table.cells:
        for cell in row:
            w = cell.x2 - cell.x1
            h = cell.y2 - cell.y1
            if w <= 0 or h <= 0:
                continue
            xs.extend([cell.x1, cell.x2])
            ys_top.append(page_height - cell.y2)
            ys_bottom.append(page_height - cell.y1)

    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys_top), max(xs), max(ys_bottom))


def _camelot_cell_is_covered(cell: Any) -> bool:
    """True, если ячейка Camelot покрыта span (vspan/hspan) или пустая."""
    if cell.vspan and not cell.top:
        return True
    if cell.hspan and not cell.left:
        return True
    w = cell.x2 - cell.x1
    h = cell.y2 - cell.y1
    return w <= 0 or h <= 0


def _camelot_table_to_adapter(table: Any, page_height: float) -> TableAdapter | None:
    """Конвертирует таблицу Camelot в TableAdapter для build_cells()."""
    if not table.cells:
        return None

    adapter_rows: list[_TableRow] = []
    for row in table.cells:
        cells: list[tuple[float, float, float, float] | None] = []
        for cell in row:
            if _camelot_cell_is_covered(cell):
                cells.append(None)
            else:
                cells.append(_camelot_cell_bbox(cell, page_height))
        adapter_rows.append(_TableRow(cells=cells))

    return TableAdapter(bbox=_camelot_table_bbox(table, page_height), rows=adapter_rows)


def _camelot_result_to_adapters(result: Any, page_height: float) -> list[TableAdapter]:
    """Список TableAdapter из результата Camelot.read_pdf."""
    adapters: list[TableAdapter] = []
    if result is None:
        return adapters
    for table in result:
        adapter = _camelot_table_to_adapter(table, page_height)
        if adapter is not None:
            adapters.append(adapter)
    adapters.sort(key=lambda t: (t.bbox[1], t.bbox[0]))
    return adapters


def find_tables_camelot(
    pdf_path: str | Path,
    page_num: int,
    page_height: float,
) -> list[TableAdapter]:
    """Извлечение структуры таблиц Camelot flavor=auto."""
    result = _read_camelot_page(pdf_path, page_num, "auto")
    return _camelot_result_to_adapters(result, page_height)


@dataclass
class PageTableResult:
    """Результат детекции таблиц на странице: список + источник."""
    tables: list
    source: TableSource


@dataclass
class SamplesTableStats:
    """Сводка прогона samples/: страницы, источники, тайминги."""
    total_pages: int
    pages_with_tables: int
    pages_lines: int
    pages_text: int
    pages_camelot: int
    total_seconds: float
    avg_seconds_per_page: float
    avg_seconds_by_source: dict[str, float]
    processed_files: list[str] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)


def _bbox_intersection_area(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    """Площадь пересечения двух bbox (x0, top, x1, bottom)."""
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def _bbox_area(bbox: tuple[float, float, float, float]) -> float:
    """Площадь bbox; 0 если вырожденный."""
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _word_in_bbox_center(w: dict, bbox: tuple[float, float, float, float]) -> bool:
    """True, если центр слова попадает внутрь bbox."""
    cx = (w["x0"] + w["x1"]) / 2
    cy = (w["top"] + w["bottom"]) / 2
    x0, top, x1, bottom = bbox
    return x0 <= cx <= x1 and top <= cy <= bottom


def _table_words(page, table) -> list[dict]:
    """Слова страницы, чей центр лежит внутри bbox таблицы."""
    bbox = getattr(table, "bbox", None)
    if bbox is None:
        return []
    words = page.extract_words(x_tolerance=1, y_tolerance=1)
    return [w for w in words if _word_in_bbox_center(w, bbox)]


def _line_groups(words: list[dict], y_tol: float = 3.0) -> list[list[dict]]:
    """Группирует слова в строки по близости top (y_tol)."""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = [[ordered[0]]]
    for w in ordered[1:]:
        if abs(w["top"] - lines[-1][-1]["top"]) <= y_tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    return lines


def _line_starts_capital(line: list[dict]) -> bool:
    """True, если первое слово строки начинается с заглавной/кавычки."""
    if not line:
        return False
    text = line[0].get("text", "").strip()
    if not text:
        return False
    ch = text[0]
    return ch.isupper() or ch in "«\"'"


def _line_looks_numeric(line: list[dict]) -> bool:
    """True, если текст строки — число (после очистки пробелов)."""
    text = " ".join(w["text"] for w in line).strip()
    if not text:
        return False
    compact = re.sub(r"[\s\u00a0()]", "", text)
    return bool(compact) and compact.replace(",", ".").replace(".", "", 1).isdigit()


def _line_x_clusters(line: list[dict], gap: float = 18.0) -> int:
    """Число x-кластеров слов в строке (разрыв > gap)."""
    if not line:
        return 0
    centers = sorted((w["x0"] + w["x1"]) / 2 for w in line)
    clusters = 1
    for i in range(1, len(centers)):
        if centers[i] - centers[i - 1] > gap:
            clusters += 1
    return clusters


def _words_digit_ratio(words: list[dict]) -> float:
    """Доля слов, выглядящих как числа."""
    if not words:
        return 0.0
    numeric = 0
    for w in words:
        t = w.get("text", "").strip()
        if not t:
            continue
        compact = re.sub(r"[\s\u00a0()]", "", t)
        if compact and compact.replace(",", ".").replace(".", "", 1).isdigit():
            numeric += 1
    return numeric / len(words)


def _table_has_tabular_rows(page, table, min_rows: int = 3) -> bool:
    """Есть ли строки с разделением «текст слева — числа справа»."""
    words = _table_words(page, table)
    lines = _line_groups(words)
    tabular = 0
    split_x = page.width * 0.5
    for line in lines:
        if len(line) < 2:
            continue
        left = [w for w in line if (w["x0"] + w["x1"]) / 2 < split_x]
        right = [w for w in line if (w["x0"] + w["x1"]) / 2 >= split_x]
        if not left or not right:
            continue
        left_t = " ".join(w["text"] for w in left)
        right_t = " ".join(w["text"] for w in right)
        if re.search(r"[A-Za-zА-Яа-яЁё]", left_t) and re.search(r"\d", right_t):
            tabular += 1
        elif _line_x_clusters(line) >= 2 and (
            _line_looks_numeric(line) or sum(1 for w in line if re.search(r"\d", w["text"])) >= 2
        ):
            tabular += 1
    return tabular >= min_rows


def _filter_prose_tables(page, tables: list) -> list:
    """Убирает псевдо-таблицы, похожие на prose (table_looks_like_prose)."""
    return [t for t in tables if not table_looks_like_prose(page, t)]


def _table_grid_one_cell_ratio(table) -> float:
    """Доля строк pdfplumber/Camelot-сетки с не более чем одной ячейкой."""
    rows = getattr(table, "rows", None) or []
    if not rows:
        return 0.0
    single = sum(
        1 for row in rows
        if sum(1 for c in row.cells if c is not None) <= 1
    )
    return single / len(rows)


def _table_has_form_line_item_headers(page, table) -> bool:
    """Шапка товарной/сметной таблицы СФ, УПД, ТОРГ-12, КС-2/КС-3."""
    words = _table_words(page, table)
    if len(words) < 10:
        return False
    text = " ".join(w.get("text") or "" for w in words).lower()
    patterns = (
        r"наименован\w*\s+товар",
        r"код\s+товар|код\s+вида",
        r"единиц\w*\s+измер",
        r"сумма\s+налог",
        r"стоимост\w*\s+товар",
        r"налогов\w*\s+ставк|нало-?\s*говая\s+ставка",
        r"номер\s+по\s+поряд",
        r"наименован\w*\s+работ",
        r"выполнено\s+работ",
        r"масса\s+брутто|масса\s+нетто",
    )
    hits = sum(1 for p in patterns if re.search(p, text))
    return hits >= 3


def _table_has_payment_or_invoice_signals(page, table) -> bool:
    """Короткие «ломаные» таблицы оплаты/счетов: даты и суммы при слабой lattice-сетке."""
    words = _table_words(page, table)
    if len(words) < 12:
        return False
    text = " ".join(w.get("text") or "" for w in words)
    dates = len(re.findall(r"\d{2}\.\d{2}\.\d{4}", text))
    money = len(re.findall(r"\d[\d\s]*,\d{2}", text))
    headers = bool(
        re.search(
            r"(?i)(?:п/?п|предмет\s+договора|сумма|дата\s+оплаты|наименование|"
            r"единица\s+измерения|количество|примечание)",
            text,
        )
    )
    grid_rows = len(getattr(table, "rows", None) or [])
    # типичный кейс: lines дал 1–4 строки сетки, а word-lines уже «табличные»
    if grid_rows and grid_rows <= 4 and (dates >= 2 or (headers and (dates >= 1 or money >= 1))):
        return True
    if headers and dates >= 2 and money >= 1:
        return True
    # СФ/УПД/ТОРГ/КС: многоуровневая шапка без дат/сумм (пустая или header-only форма)
    if _table_has_form_line_item_headers(page, table):
        return True
    return False


def table_looks_like_prose(page, table) -> bool:
    """
    Псевдо-таблица из text strategy на prose-странице.
    Признаки: много строк, одна «колонка» текста, мало чисел, строки с заглавной буквы.
    """
    words = _table_words(page, table)
    if len(words) < 24:
        return False

    lines = _line_groups(words)
    if len(lines) < 6:
        return False

    one_cluster = 0
    multi_cluster = 0
    numeric_lines = 0
    capital_lines = 0
    sentence_lines = 0
    total_words = 0

    for line in lines:
        clusters = _line_x_clusters(line)
        if clusters <= 1:
            one_cluster += 1
        if clusters >= 2:
            multi_cluster += 1
        if _line_looks_numeric(line):
            numeric_lines += 1
        if _line_starts_capital(line):
            capital_lines += 1
        total_words += len(line)
        line_text = " ".join(w["text"] for w in line).strip()
        if line_text.endswith((".", ";", ":", ")", "»")):
            sentence_lines += 1

    n = len(lines)
    one_ratio = one_cluster / n
    multi_ratio = multi_cluster / n
    numeric_ratio = numeric_lines / n
    capital_ratio = capital_lines / n
    sentence_ratio = sentence_lines / n
    avg_words = total_words / n
    grid_one_ratio = _table_grid_one_cell_ratio(table)
    word_digit_ratio = _words_digit_ratio(words)

    # Явная таблица: часто >=2 x-кластера в строке и заметная доля числовых строк
    if multi_ratio >= 0.28 and numeric_ratio >= 0.12 and word_digit_ratio >= 0.08:
        if _table_has_tabular_rows(page, table, min_rows=4):
            return False

    # Оплата/счёт с датами и суммами — не prose, даже если lattice «схлопнул» строки
    if _table_has_payment_or_invoice_signals(page, table):
        return False

    # Крупный текстовый блок с редкими числами (уставы, органы управления) —
    # не таблица, даже если _table_has_tabular_rows ложно сработал на «п.13.1».
    if (
        word_digit_ratio <= 0.08
        and n >= 12
        and sentence_ratio >= 0.12
        and not _table_has_payment_or_invoice_signals(page, table)
    ):
        return True

    bullet_lines = sum(
        1 for line in lines
        if any((w.get("text") or "").strip().startswith("•") for w in line)
    )
    if bullet_lines >= 2 and word_digit_ratio <= 0.18:
        return True

    # Нет типичной tabular-структуры, но текст разбит на несколько колонок (переносы)
    if (
        not _table_has_tabular_rows(page, table, min_rows=2)
        and multi_ratio >= 0.22
        and word_digit_ratio <= 0.14
        and n >= 8
    ):
        return True

    if (
        not _table_has_tabular_rows(page, table, min_rows=2)
        and multi_ratio >= 0.15
        and word_digit_ratio <= 0.20
        and n >= 5
    ):
        return True

    # Нет типичной табличной структуры — скорее prose (учётная политика и т.п.)
    if not _table_has_tabular_rows(page, table, min_rows=3) and n >= 6 and word_digit_ratio <= 0.16:
        return True

    if word_digit_ratio <= 0.10 and one_ratio >= 0.42 and n >= 6:
        return True

    bbox = getattr(table, "bbox", None)
    if bbox is not None:
        page_area = max(page.width * page.height, 1.0)
        if _bbox_area(bbox) / page_area >= 0.32 and one_ratio >= 0.52 and numeric_ratio <= 0.22:
            return True

    # Сетка «одна ячейка на строку» + мало чисел — типичный text strategy на абзацах
    if grid_one_ratio >= 0.82 and n >= 10 and numeric_ratio <= 0.18:
        return True

    # Явный prose: почти все строки — один blob, мало чисел, много заглавных начал
    if one_ratio >= 0.62 and numeric_ratio <= 0.22 and capital_ratio >= 0.28:
        return True

    if one_ratio >= 0.55 and numeric_ratio <= 0.25 and avg_words >= 7 and n >= 12:
        return True

    if sentence_ratio >= 0.22 and one_ratio >= 0.52 and numeric_ratio <= 0.25 and n >= 8:
        return True

    if len(lines) >= 35 and one_ratio >= 0.72 and numeric_ratio <= 0.14:
        return True

    return False


def _words_look_tabular(words: list[dict], min_rows: int = 4) -> bool:
    """Borderless таблица ниже lined-блоков: выравнивание чисел по колонкам."""
    if len(words) < min_rows * 3:
        return False

    lines = _line_groups(words)
    numeric_rows = 0
    multi_cols = 0
    for line in lines:
        if _line_looks_numeric(line) or sum(1 for w in line if re.search(r"\d", w["text"])) >= 2:
            numeric_rows += 1
        if _line_x_clusters(line) >= 2:
            multi_cols += 1

    return numeric_rows >= min_rows and multi_cols >= min_rows


def _line_looks_prose(words: list[dict]) -> bool:
    """Строка похожа на абзац, а не на строку таблицы."""
    text = " ".join(w.get("text", "") for w in words).strip()
    if len(text) >= 55:
        return True
    if "тыс." in text and re.search(r"[а-яё]{5,}", text, re.I):
        return True
    if text.count(",") >= 2 and re.search(r"[а-яё]{4,}", text, re.I):
        return True
    return False


def _line_looks_company_row(words: list[dict]) -> bool:
    """True, если строка — компания (ООО/…) с числами или колонками."""
    text = " ".join(w.get("text", "") for w in words).strip()
    if not re.search(r"(?:ООО|ЗАО|ПАО|АО|ИП)\s", text):
        return False
    return _line_looks_numeric(words) or _line_x_clusters(words) >= 2


def _tabular_region_start_y(words: list[dict], min_rows: int = 3) -> float | None:
    """Y начала borderless-таблицы, пропуская prose-абзацы сверху."""
    if not words:
        return None

    lines = _line_groups(words)

    def _include_header_block(idx: int) -> float:
        """Y верха, включая возможные строки-заголовки над tabular-блоком."""
        start_top = min(w["top"] for w in lines[idx])
        for prev in reversed(lines[:idx]):
            if _line_looks_prose(prev):
                break
            prev_top = min(w["top"] for w in prev)
            if start_top - prev_top > 95:
                break
            start_top = prev_top
        return start_top

    for i, line in enumerate(lines):
        if _line_looks_prose(line):
            continue
        if _line_looks_company_row(line):
            return _include_header_block(i)
        window = lines[i : i + min_rows]
        if len(window) < min_rows:
            continue
        tabular_hits = sum(
            1 for ln in window
            if _line_x_clusters(ln) >= 2 or _line_looks_numeric(ln)
        )
        if tabular_hits >= min_rows - 1:
            return _include_header_block(i)
    return None


def find_supplementary_text_tables(page, existing_tables: list) -> list:
    """
    Borderless-таблица строго НИЖЕ уже найденных lined-таблиц.
    Без захвата всей страницы через text strategy.
    """
    if not existing_tables:
        return []

    bottom_y = max(t.bbox[3] for t in existing_tables if getattr(t, "bbox", None))
    words = page.extract_words(x_tolerance=1, y_tolerance=1)
    below_words = [w for w in words if w["top"] >= bottom_y - 5]
    if not below_words:
        return []

    start_y = _tabular_region_start_y(below_words)
    if start_y is None:
        return []

    tabular_words = [w for w in below_words if w["top"] >= start_y - 2]
    if not _words_look_tabular(tabular_words):
        return []

    x0 = min(w["x0"] for w in tabular_words)
    x1 = max(w["x1"] for w in tabular_words)
    y0 = max(bottom_y - 2, start_y - 2)
    y1 = max(w["bottom"] for w in tabular_words)
    region = (x0, y0, x1, y1)
    if _bbox_area(region) <= 0:
        return []

    cropped = page.within_bbox(region, strict=False)
    found = _find_tables_text(cropped)
    out: list = []
    for table in found:
        if len(table.rows) < 4:
            continue
        if table_looks_like_prose(page, table):
            continue
        if _uncovered_area_fraction(table.bbox, existing_tables) >= 0.45:
            out.append(table)
    out.sort(key=lambda t: (t.bbox[1], t.bbox[0]))
    return out


def _uncovered_area_fraction(
    candidate: tuple[float, float, float, float],
    existing: list,
) -> float:
    """Доля площади candidate, не покрытая existing-таблицами."""
    area = _bbox_area(candidate)
    if area <= 0:
        return 0.0
    covered = sum(
        _bbox_intersection_area(candidate, t.bbox)
        for t in existing
        if getattr(t, "bbox", None) is not None
    )
    return max(0.0, area - covered) / area


def _merge_table_lists(primary: list, extra: list) -> list:
    """Объединяет списки таблиц и сортирует по (top, left)."""
    if not extra:
        return primary
    combined = list(primary) + extra
    combined.sort(key=lambda t: (t.bbox[1], t.bbox[0]))
    return combined


def find_tables_with_source(
    page,
    pdf_path: str | Path | None = None,
    page_num: int | None = None,
) -> PageTableResult:
    """
    1. Camelot — первичная детекция.
    2. Camelot пусто -> pdfplumber lines; если и lines пусто -> [].
    3. Camelot есть + lines есть -> lines + borderless ниже (если есть).
    4. Camelot есть + lines пусто -> camelot auto, иначе text (без prose-псевдотаблиц).
    """
    tables_lines = _find_tables_lines(page)

    if pdf_path is None or page_num is None:
        tables_lines = _filter_prose_tables(page, tables_lines)
        source: TableSource = "lines" if tables_lines else "none"
        return PageTableResult(tables_lines, source)

    if not camelot_page_has_tables(pdf_path, page_num):
        tables_lines = _filter_prose_tables(page, tables_lines)
        source = "lines" if tables_lines else "none"
        return PageTableResult(tables_lines, source)

    if tables_lines:
        filtered_lines = _filter_prose_tables(page, tables_lines)
        if filtered_lines:
            extra = find_supplementary_text_tables(page, filtered_lines)
            return PageTableResult(_merge_table_lists(filtered_lines, extra), "lines")
        # lines нашлись, но все отфильтрованы как prose → camelot/text fallback

    tables_camelot = find_tables_camelot(pdf_path, page_num, page.height)
    tables_camelot = _filter_prose_tables(page, tables_camelot)
    if tables_camelot:
        return PageTableResult(tables_camelot, "camelot")

    tables_text = _filter_prose_tables(page, _find_tables_text(page))
    source = "text" if tables_text else "none"
    return PageTableResult(tables_text, source)


def find_tables_smart(page, pdf_path: str | Path | None = None, page_num: int | None = None) -> list:
    """Список таблиц страницы (обёртка над find_tables_with_source)."""
    return find_tables_with_source(page, pdf_path, page_num).tables


def suppress_scan_noise() -> None:
    """Глушит warnings и шумные логгеры Camelot/pdfminer/PIL."""
    warnings.filterwarnings("ignore")
    for logger_name in ("camelot", "pdfminer", "pdfminer.pdfpage", "PIL", "pypdf"):
        logging.getLogger(logger_name).setLevel(logging.ERROR)


# transitional alias
_suppress_scan_noise = suppress_scan_noise


def scan_samples_table_stats(
    samples_dir: str | Path = "samples",
    *,
    verbose: bool = True,
) -> SamplesTableStats:
    """Прогон всех PDF в samples/: сводка по источникам детекции таблиц."""
    import pdfplumber

    suppress_scan_noise()

    root = Path(samples_dir)
    pdfs = sorted(root.glob("*.pdf"))

    total_pages = 0
    pages_with_tables = 0
    pages_lines = 0
    pages_text = 0
    pages_camelot = 0
    total_seconds = 0.0
    seconds_by_source: dict[str, list[float]] = {
        "lines": [],
        "text": [],
        "camelot": [],
        "none": [],
    }
    processed_files: list[str] = []
    details: list[dict] = []

    for pdf_path in pdfs:
        file_t0 = time.perf_counter()
        with pdfplumber.open(pdf_path) as pdf:
            for pnum, page in enumerate(pdf.pages, start=1):
                total_pages += 1
                t0 = time.perf_counter()
                result = find_tables_with_source(page, pdf_path=pdf_path, page_num=pnum)
                elapsed = time.perf_counter() - t0
                total_seconds += elapsed
                seconds_by_source[result.source].append(elapsed)

                n_tables = len(result.tables)

                if n_tables:
                    pages_with_tables += 1
                if result.source == "lines":
                    pages_lines += 1
                elif result.source == "text":
                    pages_text += 1
                elif result.source == "camelot":
                    pages_camelot += 1

                details.append(
                    {
                        "pdf": pdf_path.name,
                        "page": pnum,
                        "source": result.source,
                        "tables": n_tables,
                        "seconds": round(elapsed, 4),
                    }
                )

        processed_files.append(pdf_path.name)
        if verbose:
            file_elapsed = time.perf_counter() - file_t0
            print(
                f"{pdf_path.name} — готово "
                f"({len(pdf.pages)} стр., {file_elapsed:.2f} с)",
                flush=True,
            )

    avg_seconds_per_page = total_seconds / total_pages if total_pages else 0.0
    avg_seconds_by_source = {
        source: (sum(times) / len(times) if times else 0.0)
        for source, times in seconds_by_source.items()
    }

    return SamplesTableStats(
        total_pages=total_pages,
        pages_with_tables=pages_with_tables,
        pages_lines=pages_lines,
        pages_text=pages_text,
        pages_camelot=pages_camelot,
        total_seconds=round(total_seconds, 4),
        avg_seconds_per_page=round(avg_seconds_per_page, 4),
        avg_seconds_by_source={
            source: round(avg, 4) for source, avg in avg_seconds_by_source.items()
        },
        processed_files=processed_files,
        details=details,
    )
