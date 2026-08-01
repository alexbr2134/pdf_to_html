

from __future__ import annotations

import html
import logging
import re
import time
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pdfplumber

from page_suitability import (
    PageSuitability,
    SuitabilityStats,
    assess_page_suitability,
    document_has_broken_fonts,
    merge_page_suitability,
    rejected_page_notice_html,
    should_route_unmarked_complex_spans,
    suitability_unmarked_complex_spans,
)
from pdf_doc_types import DocType, detect_doc_type
from pdf_table_engine import (
    _suppress_scan_noise,
    find_tables_smart,
    table_looks_like_prose,
)

HEADER = "HEADER"
DATA = "DATA"


# --- notebook cell 0 ---


@dataclass
class Cell:
    row: int
    col: int
    bbox: tuple[float, float, float, float] | None
    text: str = ""
    words: list[dict] = field(default_factory=list)
    rowspan: int = 1
    colspan: int = 1
    covered: bool = False
    is_placeholder: bool = False  

    @property
    def cell_center_x(self) -> float | None:
        """Центр ячейки по горизонтали (по bbox)."""
        if self.bbox is None:
            return None
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def text_center_x(self) -> float | None:
        """Центр текста по горизонтали (по крайним словам)."""
        if not self.words:
            return None
        left = min(w["x0"] for w in self.words)
        right = max(w["x1"] for w in self.words)
        return (left + right) / 2

    @property
    def is_empty(self) -> bool:
        """True, если ячейка без текста (после strip)."""
        return not self.text.strip()


# --- notebook cell 1 ---
def _word_in_bbox(word: dict, bbox: tuple[float, float, float, float]) -> bool:
    """True, если ЦЕНТР слова попадает внутрь bbox ячейки."""
    x0, top, x1, bottom = bbox
    cx = (word["x0"] + word["x1"]) / 2
    cy = (word["top"] + word["bottom"]) / 2
    return x0 <= cx <= x1 and top <= cy <= bottom


def _word_in_table_bbox(
    word: dict,
    bbox: tuple[float, float, float, float],
    margin: float = 1.0,
) -> bool:
    """True, если центр слова внутри bbox таблицы (с небольшим margin)."""
    x0, top, x1, bottom = bbox
    cx = (word["x0"] + word["x1"]) / 2
    cy = (word["top"] + word["bottom"]) / 2
    return (x0 - margin) <= cx <= (x1 + margin) and (top - margin) <= cy <= (bottom + margin)


# --- notebook cell 2 ---
def _merge_split_digit_words(words: list[dict], y_tol: float = 3.0) -> list[dict]:
    """Склеивает посимвольно разнесённые цифры PDF (1 9 8 8 0 0 -> 198800)."""

    if not words:
        return words

    ordered = sorted(words, key=lambda w: (round(w["top"]), w["x0"]))
    merged: list[dict] = []
    i = 0
    while i < len(ordered):
        w = ordered[i]
        if re.fullmatch(r"\d", w.get("text", "")):
            run = [w]
            j = i + 1
            while j < len(ordered):
                nxt = ordered[j]
                if not re.fullmatch(r"\d", nxt.get("text", "")):
                    break
                if abs(nxt["top"] - run[-1]["top"]) > y_tol:
                    break
                size = float(run[-1].get("size", 0)) or 10.0
                if nxt["x0"] - run[-1]["x1"] > size * 1.8:
                    break
                run.append(nxt)
                j += 1
            if len(run) >= 4:
                digits = "".join(r["text"] for r in run)
                merged.append({
                    **run[0],
                    "text": digits,
                    "x1": run[-1]["x1"],
                })
                i = j
                continue
        merged.append(w)
        i += 1
    return merged


def _format_thousands(num_str: str) -> str:
    """198800 -> 198 800; сохраняет ведущие скобки отрицательных."""
    sign = ""
    body = num_str
    if body.startswith("(") and body.endswith(")"):
        sign = "("
        body = body[1:-1]
    if not body.isdigit() or len(body) <= 3:
        return num_str
    parts: list[str] = []
    while body:
        parts.append(body[-3:])
        body = body[:-3]
    formatted = " ".join(reversed(parts))
    return f"{sign}{formatted}{')' if sign else ''}"


def _normalize_numeric_tokens(text: str) -> str:
    """Нормализует числовые токены в тексте (пробелы/запятые)."""

    def repl(m: re.Match[str]) -> str:
        """Callback re.sub: собирает разнесённые цифры в одно число."""
        raw = m.group(0)
        compact = raw.replace(" ", "")
        if compact.isdigit() and len(compact) >= 4:
            return _format_thousands(compact)
        return raw

    return re.sub(r"\(?\d(?:\s+\d){3,}\)?", repl, text)


def words_to_cell_text(words: list[dict], y_tol: float = 3.0) -> str:
    """Склеивает слова ячейки: строки через \\n, слова в строке через пробел."""
    if not words:
        return ""
    words = _merge_split_digit_words(words, y_tol=y_tol)
    ordered = sorted(words, key=lambda w: (round(w["top"]), w["x0"]))
    lines: list[list[dict]] = [[ordered[0]]]
    for w in ordered[1:]:
        if abs(w["top"] - lines[-1][-1]["top"]) <= y_tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    text = "\n".join(" ".join(w["text"] for w in line) for line in lines).strip()
    return _normalize_numeric_tokens(text)


# --- notebook cell 3 ---
def build_cells(page, table) -> list[list[Cell]]:
    """Строит grid ячеек Cell из pdfplumber/Camelot-таблицы и слов страницы."""
    words = page.extract_words(
        x_tolerance=1,
        y_tolerance=1,
        extra_attrs=["fontname", "size"],
    )

    table_bbox = getattr(table, "bbox", None)
    if table_bbox is not None:
        words = [w for w in words if _word_in_table_bbox(w, table_bbox)]

    grid: list[list[Cell]] = []

    for r_idx, row in enumerate(table.rows):
        row_cells: list[Cell] = []

        for c_idx, cell_bbox in enumerate(row.cells):
            if cell_bbox is None:
                row_cells.append(Cell(row=r_idx, col=c_idx, bbox=None, is_placeholder=True))
                continue

            cell_words = [w for w in words if _word_in_bbox(w, cell_bbox)]
            cell_words.sort(key=lambda w: (round(w["top"]), w["x0"]))
            text = words_to_cell_text(cell_words)

            row_cells.append(Cell(
                row=r_idx,
                col=c_idx,
                bbox=cell_bbox,
                text=text,
                words=cell_words,
                is_placeholder=not text.strip(),
            ))

        grid.append(row_cells)

    return grid


# --- notebook cell 4 ---
def _join_labels(parts: list[str]) -> str:
    """Склеивает фрагменты подписи; сохраняет дефис при переносе через '-'."""
    result = parts[0].strip()
    for p in parts[1:]:
        p = p.strip()
        if not p:
            continue
        if result.endswith("-"):
            result = result + p
        else:
            result = result + " " + p
    return result

def _font_sig(w: dict) -> tuple:
    """Сигнатура шрифта слова: (имя, размер, округлённый до 0.5 pt)."""
    return (w.get("fontname"), round(float(w.get("size", 0)) * 2) / 2)


# --- notebook cell 7 ---
def _looks_numeric(text: str) -> bool:
    """True, если текст ячейки — это число (с учётом РСБУ-форматирования)."""
    t = text.strip()
    if not t:
        return False
    # явные маркеры «не число»
    if t in {"-", "—", "Х", "X", "x", "х"}:
        return False
    # убираем разделители тысяч и скобки отрицательных чисел
    for ch in (" ", "\u00a0", "\u202f", "(", ")"):
        t = t.replace(ch, "")
    t = t.replace(",", ".")   # десятичная запятая -> точка
    try:
        float(t)
        return True
    except ValueError:
        return False


def _looks_like_table_data_value(text: str) -> bool:
    """
    Строгая проверка «значение в колонке таблицы».
    Не считать границей подписи одиночные цифры, дни, годы (1, 31, 2024).
    """

    t = (text or "").strip()
    if not t or t in {"-", "—", "Х", "X", "x", "х"}:
        return False
    if re.search(r"[A-Za-zА-Яа-яЁё«»]", t):
        return False
    digits = re.sub(r"\D", "", t)
    if len(digits) < 3:
        return False
    if len(digits) == 4 and digits.isdigit() and 1900 <= int(digits) <= 2100:
        return False
    if re.search(r"\d{1,3}(?:[\s\u00a0\u202f]\d{3})+", t):
        return True
    compact = re.sub(r"[\s\u00a0\u202f(),-]", "", t).replace(",", ".")
    try:
        float(compact.strip("()"))
        return len(digits) >= 3
    except ValueError:
        return len(digits) >= 4


def _text_starts_capital(text: str) -> bool:
    """True, если текст начинается с заглавной буквы или кавычки."""
    t = (text or "").strip()
    if not t:
        return False
    ch = t[0]
    return ch.isupper() or ch in "«\"'"


# --- notebook cell 8 ---
from typing import Any, Callable

_NON_NUMERIC_MARKERS = frozenset({"-", "—", "Х", "X", "x", "х"})


def cell_text_align(text: str, col_idx: int, n_cols: int) -> str:
    """Выравнивание ячейки: подписи слева, числа справа."""
    t = (text or "").strip()
    if not t:
        return "left"
    if col_idx == 0 and n_cols > 2:
        return "left"
    if _looks_numeric(t):
        return "right"
    if col_idx >= max(1, n_cols - 2) and re.search(r"\d", t):
        return "right"
    return "left"


def table_cell_style(text: str, col_idx: int, n_cols: int) -> str:
    """CSS-стиль ячейки таблицы (выравнивание, вертикаль)."""
    align = cell_text_align(text, col_idx, n_cols)
    return f' style="text-align: {align}; vertical-align: middle;"'


def is_prose_table(grid: list[list[Any]]) -> bool:
    """
    Таблица на самом деле prose/оглавление: одна текстовая колонка, мало чисел.
    Не применять к плотным числовым сеткам (формы) — там много колонок и чисел.
    """
    if not grid:
        return False

    n_cols = max(len(r) for r in grid)
    n_rows = len(grid)
    rows_one_blob = 0
    filled = 0
    numeric = 0

    for row in grid:
        visible = [c for c in row if not getattr(c, "covered", False) and (c.text or "").strip()]
        if len(visible) == 1:
            rows_one_blob += 1
        for cell in visible:
            filled += 1
            if _looks_numeric(cell.text):
                numeric += 1

    if filled == 0:
        return False

    tabular_rows = sum(
        1 for row in grid
        if any(
            not getattr(c, "covered", False) and (c.text or "").strip()
            and c.col == 0 and re.search(r"[A-Za-zА-Яа-яЁё]", c.text)
            for c in row
        )
        and any(
            not getattr(c, "covered", False) and _looks_numeric(c.text)
            for c in row if c.col > 0
        )
    )

    numeric_ratio = numeric / filled
    one_blob_ratio = rows_one_blob / n_rows

    if tabular_rows >= 4:
        return False

    bullet_rows = sum(
        1 for row in grid
        if any("•" in (getattr(c, "text", "") or "") for c in row)
    )
    if bullet_rows >= 2 and numeric_ratio < 0.22:
        return True

    multi_col_text_rows = sum(
        1 for row in grid
        if len([c for c in row if not getattr(c, "covered", False) and (c.text or "").strip()]) >= 2
        and not any(
            not getattr(c, "covered", False) and c.col > 0 and _looks_numeric(c.text)
            for c in row
        )
    )
    if tabular_rows < 2 and multi_col_text_rows >= max(4, n_rows // 3) and numeric_ratio < 0.22:
        return True

    if n_cols >= 4 and tabular_rows < 3 and numeric_ratio < 0.18 and n_rows >= 8:
        avg_cells = filled / max(n_rows, 1)
        if avg_cells >= 2.2:
            return True

    if tabular_rows < 2 and one_blob_ratio >= 0.48 and numeric_ratio < 0.3 and n_rows >= 6:
        return True

    if n_cols <= 3 and one_blob_ratio >= 0.55 and numeric_ratio < 0.28:
        return True

    if one_blob_ratio >= 0.8 and numeric_ratio < 0.12 and n_rows >= 3:
        return True

    if one_blob_ratio >= 0.7 and numeric_ratio < 0.18 and n_rows >= 4:
        return True

    long_rows = sum(
        1 for row in grid
        if len([c for c in row if not getattr(c, "covered", False) and (c.text or "").strip()]) == 1
        and len((row[0].text or "").strip()) >= 55
    )
    if long_rows >= 4 and numeric_ratio < 0.24:
        return True

    if n_cols >= 4 and one_blob_ratio >= 0.48 and numeric_ratio < 0.22 and n_rows >= 8:
        avg_len = sum(
            len((c.text or "").strip())
            for row in grid
            for c in row
            if not getattr(c, "covered", False) and (c.text or "").strip()
        ) / filled
        if avg_len >= 32:
            return True

    # Схлопнутая финансовая таблица (label + числа в одной колонке) — не prose
    financial_rows = sum(
        1 for row in grid
        for c in row
        if not getattr(c, "covered", False)
        and re.search(r"\d{2,}", (c.text or ""))
        and re.search(r"[A-Za-zА-Яа-яЁё«]", (c.text or ""))
    )
    if financial_rows >= 3:
        return False

    return False


def prose_grid_to_sections(
    grid: list[list[Any]],
    render_block: Callable[[Any, float], str],
    page_width: float,
) -> list[str]:
    """Строки pseudo-table -> HTML-секции с абзацами."""
    sections: list[str] = []
    for row in grid:
        visible = [c for c in row if not getattr(c, "covered", False)]
        words: list[dict] = []
        texts: list[str] = []
        for cell in visible:
            if cell.words:
                words.extend(cell.words)
            if (cell.text or "").strip():
                texts.append(cell.text.strip())
        if not texts and not words:
            continue

        words.sort(key=lambda w: (round(w["top"]), w["x0"]))
        combined = " ".join(texts) if texts else ""
        bboxes = [c.bbox for c in visible if c.bbox is not None]
        bbox = None
        if bboxes:
            bbox = (
                min(b[0] for b in bboxes),
                min(b[1] for b in bboxes),
                max(b[2] for b in bboxes),
                max(b[3] for b in bboxes),
            )

        block = type("_B", (), {})()
        block.text = combined
        block.words = words
        block.bbox = bbox

        inner = render_block(block, page_width)
        if inner:
            sections.append(f'<div class="doc-section">{inner}</div>')
    return sections


def trim_spurious_empty_rowspan(grid: list[list[Any]]) -> list[list[Any]]:
    """
    Убирает rowspan у пустых ячеек, если в тех же строках соседние колонки
    содержат текст (типичный артеfact: пустой rowspan закрывает «Район промысла»).
    """
    n_rows = len(grid)
    for r in range(n_rows):
        for c, cell in enumerate(grid[r]):
            if getattr(cell, "covered", False):
                continue
            if cell.rowspan <= 1 or (cell.text or "").strip():
                continue

            limit = r + cell.rowspan
            conflict_row = None
            for r2 in range(r + 1, min(limit, n_rows)):
                for c2, other in enumerate(grid[r2]):
                    if c2 == c or getattr(other, "covered", False):
                        continue
                    if (other.text or "").strip():
                        conflict_row = r2
                        break
                if conflict_row is not None:
                    break

            if conflict_row is None:
                continue

            new_span = max(1, conflict_row - r)
            old_span = cell.rowspan
            cell.rowspan = new_span
            for r2 in range(r + new_span, r + old_span):
                if r2 < n_rows and c < len(grid[r2]):
                    below = grid[r2][c]
                    below.covered = False
                    below.is_placeholder = not (below.text or "").strip()

    return grid


def plain_text_from_html(markup: str) -> str:
    """Текст из HTML без тегов — для сравнения с extract_text()."""
    text = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", markup, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def page_body_needs_prose_fallback(
    page,
    body: str,
    *,
    min_raw_chars: int = 120,
    min_ratio: float = 0.35,
) -> bool:
    """
    Prose-страница: pdfplumber не нашёл таблиц или псевдо-таблица «съела» слова,
    а HTML содержит лишь малую долю текста страницы.
    """
    raw = (page.extract_text() or "").strip()
    if len(raw) < min_raw_chars:
        return False
    plain = plain_text_from_html(body)
    if not plain:
        return True
    ratio = len(plain) / len(raw)
    # не уничтожать уже собранные таблицы ради plain-fallback
    if "<table" in body and len(plain) >= 80 and ratio >= 0.12:
        return False
    return ratio < min_ratio


def page_text_fallback_html(page, render_block: Callable[[Any, float], str]) -> str:
    """Если пайплайн ничего не нашёл — plain text страницы."""
    raw = (page.extract_text() or "").strip()
    if not raw:
        return ""

    words = page.extract_words(
        x_tolerance=1,
        y_tolerance=1,
        extra_attrs=["fontname", "size"],
    )
    block = type("_B", (), {})()
    block.text = raw
    block.words = words
    block.bbox = (0.0, 0.0, float(page.width), float(page.height))
    inner = render_block(block, page.width)
    if not inner:
        inner = f'<p>{html.escape(raw).replace(chr(10), "<br>")}</p>'
    return f'<div class="doc-section">{inner}</div>'


DOCUMENT_CSS = """
body { font-family: "Times New Roman", Times, serif; font-size: 11pt; line-height: 1.35; margin: 2em; }
table { border-collapse: collapse; margin: 0.6em 0 1em; width: auto; max-width: 100%; }
th, td { border: 1px solid #888; padding: 4px 10px; vertical-align: middle; text-align: left; }
td.num, th.num { text-align: right; }
h1, h2, h3, p { margin: 0.25em 0 0.5em; font-weight: inherit; font-size: inherit; }
.doc-section { margin-bottom: 0.75em; }
.broken-font-warning {
  border: 1px solid #b00;
  background: #fff5f5;
  color: #700;
  padding: 0.6em 0.9em;
  margin: 0 0 1em;
  font-weight: bold;
}
.page-rejected {
  border: 1px solid #a60;
  background: #fff8f0;
  color: #530;
  padding: 0.75em 1em;
  margin: 0 0 1em;
}
.page-rejected ul { margin: 0.4em 0 0 1.2em; }
.page[data-rejected="true"] { opacity: 0.95; }
"""


# --- notebook cell 9 ---
def _row_signals(row: list[Cell]) -> tuple[float, int]:
    """Возвращает (доля_числовых_ячеек, число_заполненных_ячеек)."""
    non_empty = [c for c in row if not c.is_empty]
    if not non_empty:
        return 0.0, 0
    numeric = sum(1 for c in non_empty if _looks_numeric(c.text))
    return numeric / len(non_empty), len(non_empty)


# --- notebook cell 10 ---
HEADER = "HEADER"
DATA = "DATA"


def _row_has_tabular_data_pattern(row: list["Cell"]) -> bool:
    """Строка с подписью и числами/кодом — DATA, не prose/thead."""
    visible = [
        c for c in row
        if not getattr(c, "covered", False) and not c.is_empty
    ]
    if len(visible) < 2:
        return False

    texts = [(c.text or "").strip() for c in visible]

    # счёт бухгалтерского баланса + значения
    if any(re.fullmatch(r"\d{4}", t) for t in texts):
        if any(
            _looks_like_table_data_value(t) or t in {"Х", "X", "х", "x", "-", "—"}
            for t in texts
        ):
            return True

    # «Х» + суммы
    if any(t in {"Х", "X", "х", "x"} for t in texts):
        if any(_looks_like_table_data_value(t) for t in texts):
            return True

    has_left_label = any(
        c.col == 0 and re.search(r"[A-Za-zА-Яа-яЁё]", c.text or "")
        for c in visible
    )
    has_trailing_numeric = any(
        c.col > 0 and (_looks_numeric(c.text) or _looks_like_table_data_value(c.text))
        for c in visible
    )
    if has_left_label and has_trailing_numeric:
        return True

    # подпись в любой колонке + числа правее
    for i, c in enumerate(visible):
        if re.search(r"[A-Za-zА-Яа-яЁё]{2,}", c.text or ""):
            if any(
                _looks_like_table_data_value(v.text) or _looks_numeric(v.text)
                for v in visible[i + 1:]
            ):
                return True

    return any(
        re.search(r"\d", c.text or "") and re.search(r"[A-Za-zА-Яа-я-]", c.text or "")
        for c in visible
    )


def classify_rows(
    grid: list[list["Cell"]],
    numeric_threshold: float = 0.3,
    min_header_filled: int = 1,
    max_header_rows: int = 6,
) -> list[str]:
    """
    Помечает каждую строку grid как HEADER или DATA.

    Строка считается шапкой, пока выполняются ОБА условия:
      - чисел мало (доля числовых ячеек < numeric_threshold)
      - заполнено хотя бы min_header_filled ячеек (это широкая строка-заголовок,
        а не узкая строка-раздел с одной ячейкой)
    Как только строка не прошла проверку — шапка закрывается, и все
    последующие строки становятся DATA.
    """
    kinds: list[str] = []
    header_open = True
    header_count = 0

    for row in grid:
        ratio, filled = _row_signals(row)

        looks_like_header = ratio < numeric_threshold and filled >= min_header_filled

        if header_open and _row_has_tabular_data_pattern(row):
            header_open = False
            kinds.append(DATA)
            continue

        # Широкая текстовая строка закрывает шапку только если это prose/абзацы,
        # а не band заголовков колонок (счёт-фактура, оплата и т.п.).
        if header_open and filled >= 4 and ratio < 0.15:
            text_cells = [
                c for c in row
                if not getattr(c, "covered", False) and (c.text or "").strip()
                and not _looks_numeric(c.text)
            ]
            if len(text_cells) >= 4:
                sentenceish = sum(
                    1
                    for c in text_cells
                    if len((c.text or "").strip()) > 50
                    and (c.text or "").rstrip().endswith((".", ";", "»"))
                )
                if sentenceish >= 2:
                    header_open = False
                    kinds.append(DATA)
                    continue

        if header_open and looks_like_header and header_count < max_header_rows:
            kinds.append(HEADER)
            header_count += 1
        else:
            header_open = False
            kinds.append(DATA)

    return kinds


# --- notebook cell 12 ---
def _column_bounds(grid: list[list["Cell"]]) -> list[tuple[float, float] | None]:
    """Для каждой колонки — её (x0, x1). Берём самую узкую ячейку колонки."""
    if not grid:
        return []
    n_cols = max(len(r) for r in grid)
    if n_cols == 0:
        return []
    per_col: list[list[tuple[float, float]]] = [[] for _ in range(n_cols)]
    for row in grid:
        for c, cell in enumerate(row):
            if c < n_cols and cell.bbox is not None:
                per_col[c].append((cell.bbox[0], cell.bbox[2]))
    bounds: list[tuple[float, float] | None] = []
    for ranges in per_col:
        bounds.append(min(ranges, key=lambda r: r[1] - r[0]) if ranges else None)
    return bounds


# --- notebook cell 13 ---
def _fragments_in_row(row: list["Cell"], gap_ratio: float = 1.5) -> list[list[dict]]:
    """Собирает все слова строки и режет их на фрагменты по крупным x-зазорам."""
    words: list[dict] = []
    for cell in row:
        words.extend(cell.words)
    if not words:
        return []

    words.sort(key=lambda w: w["x0"])

    frags: list[list[dict]] = [[words[0]]]
    for prev, cur in zip(words, words[1:]):
        size = float(prev.get("size", 0)) or 1.0
        gap = cur["x0"] - prev["x1"]
        if gap <= gap_ratio * size:
            frags[-1].append(cur)
        else:
            frags.append([cur])
    return frags


# --- notebook cell 14 ---
def _columns_for_extent(
    left: float,
    right: float,
    bounds: list[tuple[float, float] | None],
) -> list[int]:
    """Индексы колонок, чей центр попадает в горизонтальный размах [left, right]."""
    cols: list[int] = []
    for c, b in enumerate(bounds):
        if b is None:
            continue
        col_center = (b[0] + b[1]) / 2
        if left <= col_center <= right:
            cols.append(c)
    return cols


# --- notebook cell 15 ---
def restore_colspan_by_bbox(grid, kinds=None, tol=1.0) -> list[list["Cell"]]:
    """
    Ставит colspan ячейкам, чей bbox накрывает несколько колонок,
    и убирает поглощённые пустые ячейки-соседи. Работает по геометрии,
    поэтому ловит короткие подписи в широких ячейках (например «ИНН»
    над «число | месяц | год»).
    """
    if not grid:
        return grid
    bounds = _column_bounds(grid)
    new_grid: list[list["Cell"]] = []

    for row in grid:
        new_row: list["Cell"] = []
        skip_until = -1
        for c, cell in enumerate(row):
            if c <= skip_until:
                continue
            if cell.bbox is None:
                new_row.append(cell)
                continue

            text = (cell.text or "").strip()
            # числа/суммы не размазывать по фантомным колонкам
            if text and _looks_numeric(text):
                new_row.append(cell)
                continue

            covered = []
            for j, b in enumerate(bounds):
                if b is None:
                    continue
                mid = (b[0] + b[1]) / 2
                if not (cell.bbox[0] - tol <= mid <= cell.bbox[2] + tol):
                    continue
                col_w = max(b[1] - b[0], 1e-6)
                overlap = min(cell.bbox[2], b[1]) - max(cell.bbox[0], b[0])
                if overlap / col_w >= 0.35:
                    covered.append(j)

            if len(covered) > 1:
                span_end = c
                for j in range(c + 1, max(covered) + 1):
                    if j >= len(row):
                        break
                    neighbor = row[j]
                    if (neighbor.text or "").strip() and not getattr(neighbor, "is_placeholder", False):
                        break
                    span_end = j
                span = span_end - c + 1
                if span > 1:
                    cell.colspan = span
                    skip_until = span_end

            new_row.append(cell)
        new_grid.append(new_row)

    return new_grid


# --- notebook cell 17 ---


def _label_font(row: list["Cell"]) -> tuple | None:
    """Доминирующая сигнатура шрифта (имя+размер) в подписи строки."""
    words = row[0].words if row else []
    if not words:
        return None
    return Counter(_font_sig(w) for w in words).most_common(1)[0][0]

def _row_label(row: list["Cell"]) -> str:
    """Текст левой (нулевой) колонки — это подпись строки."""
    return row[0].text.strip() if row else ""


def _is_label_only(row: list["Cell"]) -> bool:
    """True, если заполнена только левая колонка (нет значений справа)."""
    non_empty = [c for c in row if not c.is_empty]
    if not non_empty:
        return False
    return not any(c.col > 0 for c in non_empty)


# --- notebook cell 18 ---
def _label_continuation_ok(a_label: str, b_label: str) -> bool:
    """Вторая строка — продолжение переноса подписи, а не новая строка таблицы."""

    if not b_label or not a_label:
        return False

    if _looks_like_column_header(a_label) or _looks_like_column_header(b_label):
        return False

    _FIELD_START = re.compile(
        r"^(?:по\s|Дата|Форма|ИНН|ОГРН|Единица|Организация|Наименование|Код|"
        r"Идентификационный|Основной|Бухгалтерская|Адрес|Место|Поступило|Выбыло|"
        r"Договор|Доля)",
        re.I,
    )
    if _FIELD_START.search(b_label.strip()):
        return False
    if re.fullmatch(r"\d{4}\s*г(?:ода?)?\.?", b_label.strip(), re.I):
        return False
    if re.fullmatch(r"\d{4}\s*г(?:ода?)?\.?", a_label.strip(), re.I):
        return False

    if a_label.endswith("-"):
        return True
    if b_label[0].islower():
        return True
    tail = a_label.rstrip()
    if tail.endswith(",") or tail.endswith(";"):
        return True
    if len(a_label) <= 42 and not tail.endswith((".", ":", "?", "!")):
        if len(b_label.split()) <= 8 and (
            a_label.endswith("-")
            or (len(a_label.split()) <= 3 and b_label[0].islower())
        ):
            return True
    return False


def _merge_label_cell(a: list["Cell"], b: list["Cell"], label_col: int = 0) -> "Cell":
    """Склеивает текст/слова двух label-ячеек при merge строк."""
    a_label = _row_label(a)
    b_label = _row_label(b)
    merged_label = _join_labels([a_label, b_label])
    label_words = list(a[label_col].words) + list(b[label_col].words)
    label_words.sort(key=lambda w: (round(w["top"]), w["x0"]))
    bboxes = [c.bbox for c in (a[label_col], b[label_col]) if c.bbox is not None]
    bbox = None
    if bboxes:
        bbox = (
            min(b[0] for b in bboxes),
            min(b[1] for b in bboxes),
            max(b[2] for b in bboxes),
            max(b[3] for b in bboxes),
        )
    base = a[label_col]
    return Cell(
        row=base.row, col=base.col, bbox=bbox,
        text=merged_label, words=label_words,
        rowspan=base.rowspan, colspan=base.colspan,
    )


def _try_merge_label_only_stack(a: list["Cell"], b: list["Cell"], label_col: int = 0):
    """Две строки только с подписью в col0 — склеить в одну подпись."""
    if not _row_is_leading_text_only(a, label_col) or not _row_is_leading_text_only(b, label_col):
        return None
    a_label = _row_label(a)
    b_label = _row_label(b)
    if not a_label or not b_label:
        return None
    if a_label.endswith(":"):
        return None
    if not _label_continuation_ok(a_label, b_label):
        return None
    fa, fb = _label_font(a), _label_font(b)
    if fa is None or fa != fb:
        return None
    new_row = list(b)
    new_row[label_col] = _merge_label_cell(a, b, label_col)
    return new_row


def _try_merge_wrap(a: list["Cell"], b: list["Cell"], label_col: int = 0):
    """Пытается слить строку-продолжение (wrap) с предыдущей."""
    a_label = _row_label(a)
    b_label = _row_label(b)
    if not a_label or not b_label:
        return None
    if a_label.endswith(":"):
        return None

    # две самостоятельные строки «подпись | значение» — не склеивать
    if _row_has_values(a, label_col) and _row_has_values(b, label_col):
        return None

    if not _label_continuation_ok(a_label, b_label):
        return None

    if not _label_continuation_ok(a_label, b_label):
        return None

    fa, fb = _label_font(a), _label_font(b)
    if fa is None or fa != fb:
        return None

    a_only = _is_label_only(a)
    b_only = _is_label_only(b)
    if a_only == b_only:
        return None

    merged_label = _join_labels([a_label, b_label])
    value_row = b if a_only else a

    label_words = list(a[0].words) + list(b[0].words)
    label_words.sort(key=lambda w: (round(w["top"]), w["x0"]))

    new_row: list["Cell"] = []
    for cell in value_row:
        if cell.col == label_col:
            new_row.append(Cell(
                row=cell.row, col=cell.col, bbox=cell.bbox,
                text=merged_label, words=label_words,
                rowspan=cell.rowspan, colspan=cell.colspan,
            ))
        else:
            new_row.append(cell)
    return new_row


# --- notebook cell 19 ---
def _row_bounds(grid: list[list["Cell"]]) -> list[tuple[float, float] | None]:
    """Для каждой строки — (top, bottom) по объединению всех ячеек строки."""
    bounds: list[tuple[float, float] | None] = []
    for row in grid:
        ranges = [(c.bbox[1], c.bbox[3]) for c in row if c.bbox is not None]
        if ranges:
            bounds.append((min(t for t, _ in ranges), max(b for _, b in ranges)))
        else:
            bounds.append(None)
    return bounds


LABEL_BAND_GAP = 14.0  # pt: допуск по вертикали для склейки подписи со строкой чисел


def _cell_y_center(cell: "Cell") -> float | None:
    """Вертикальный центр ячейки по bbox."""
    if cell.bbox is not None:
        return (cell.bbox[1] + cell.bbox[3]) / 2
    if cell.words:
        return sum((w["top"] + w["bottom"]) / 2 for w in cell.words) / len(cell.words)
    return None


def _label_overlaps_value_band(
    cell: "Cell",
    vband: tuple[float, float],
    band_gap: float,
) -> bool:
    """Подпись пересекается с интервалом строки значений ± band_gap (не только центр)."""
    ext_top = vband[0] - band_gap
    ext_bottom = vband[1] + band_gap
    if cell.bbox is not None:
        overlap = min(cell.bbox[3], ext_bottom) - max(cell.bbox[1], ext_top)
        if overlap > 0.5:
            return True
    cy = _cell_y_center(cell)
    return cy is not None and ext_top <= cy <= ext_bottom


def _row_has_values(row: list["Cell"], label_col: int = 0) -> bool:
    """True, если в строке есть непустые value-ячейки."""
    return any(
        c.col > label_col and not c.covered and not c.is_empty
        for c in row
    )


def _row_value_band(row: list["Cell"], label_col: int = 0) -> tuple[float, float] | None:
    """Вертикальный интервал строки по ячейкам со значениями (col > label_col)."""
    boxes = [
        c.bbox for c in row
        if c.col > label_col and c.bbox is not None and not c.covered and not c.is_empty
    ]
    if not boxes:
        return None
    return min(b[1] for b in boxes), max(b[3] for b in boxes)


def _first_value_col(row: list["Cell"], label_col: int = 0) -> int | None:
    """Первая колонка справа от подписи с числовым значением."""
    for cell in row:
        if cell.covered or cell.is_empty or cell.col <= label_col:
            continue
        t = (cell.text or "").strip()
        if _looks_numeric(t):
            return cell.col
    return None


def _row_is_leading_text_only(row: list["Cell"], label_col: int = 0) -> bool:
    """Заполнены только ведущие текстовые колонки до первого числа."""
    value_col = _first_value_col(row, label_col)
    visible = [
        c for c in row
        if not c.covered and not c.is_empty
    ]
    if not visible:
        return False
    if value_col is None:
        return not any(c.col > label_col for c in visible)
    return all(c.col < value_col for c in visible)


def _row_leading_label(row: list["Cell"], label_col: int = 0) -> str:
    """Текст всех ведущих колонок до первого числового значения."""
    value_col = _first_value_col(row, label_col)
    end = value_col if value_col is not None else len(row)
    parts: list[str] = []
    for c in range(label_col, end):
        if c < len(row) and (row[c].text or "").strip():
            parts.append(row[c].text.strip())
    return " ".join(parts)


def _can_merge_label_chain(labels: list[str]) -> bool:
    """Можно ли слить цепочку label-строк по шрифту/тексту."""
    for a, b in zip(labels, labels[1:]):
        if not _label_continuation_ok(a, b):
            return False
    return True


def merge_label_rows_by_band(
    grid: list[list["Cell"]],
    kinds: list[str],
    band_gap: float = LABEL_BAND_GAP,
    label_col: int = 0,
    search_radius: int = 1,
) -> tuple[list[list["Cell"]], list[str]]:
    """
    Приклеивает фрагменты подписи из соседних строк к строке с числами,
    если центр подписи попадает в вертикальный интервал строки значений ± band_gap.
    Решает случай: подпись в две строки, числа — в одной между ними.
    """
    if not grid:
        return grid, kinds

    n_rows = len(grid)
    remove: set[int] = set()

    for i, row in enumerate(grid):
        if i in remove or kinds[i] != DATA or not _row_has_values(row, label_col):
            continue

        vband = _row_value_band(row, label_col)
        if vband is None:
            continue
        ext_top, ext_bottom = vband[0] - band_gap, vband[1] + band_gap

        orphan_idxs: list[int] = []
        for j in range(max(0, i - search_radius), min(n_rows, i + search_radius + 1)):
            if j == i or j in remove or kinds[j] != DATA:
                continue
            if not _row_is_leading_text_only(grid[j], label_col):
                continue
            if _label_overlaps_value_band(grid[j][label_col], vband, band_gap):
                orphan_idxs.append(j)

        if not orphan_idxs:
            continue

        part_idxs = sorted(
            orphan_idxs + ([i] if _row_leading_label(row, label_col) else []),
            key=lambda k: _cell_y_center(grid[k][label_col]) or 0.0,
        )
        labels = [_row_leading_label(grid[k], label_col) for k in part_idxs if _row_leading_label(grid[k], label_col)]
        fonts = [_label_font(grid[k]) for k in part_idxs if _row_label(grid[k])]
        if len(labels) >= 2 and not _can_merge_label_chain(labels):
            continue
        unique_fonts = {f for f in fonts if f is not None}
        if len(unique_fonts) > 1:
            continue

        merged_label = _join_labels(labels)
        label_words: list[dict] = []
        label_bboxes: list[tuple[float, float, float, float]] = []
        for k in part_idxs:
            value_col = _first_value_col(grid[k], label_col)
            end = value_col if value_col is not None else len(grid[k])
            for c in range(label_col, end):
                if c >= len(grid[k]):
                    continue
                cell = grid[k][c]
                label_words.extend(cell.words)
                if cell.bbox is not None:
                    label_bboxes.append(cell.bbox)
        label_words.sort(key=lambda w: (round(w["top"]), w["x0"]))
        bbox = None
        if label_bboxes:
            bbox = (
                min(b[0] for b in label_bboxes),
                min(b[1] for b in label_bboxes),
                max(b[2] for b in label_bboxes),
                max(b[3] for b in label_bboxes),
            )

        base = row[label_col]
        row[label_col] = Cell(
            row=base.row, col=base.col, bbox=bbox,
            text=merged_label, words=label_words,
            rowspan=base.rowspan, colspan=base.colspan,
        )
        remove.update(orphan_idxs)

    if not remove:
        return grid, kinds

    new_grid = [r for idx, r in enumerate(grid) if idx not in remove]
    new_kinds = [k for idx, k in enumerate(kinds) if idx not in remove]
    return new_grid, new_kinds


def _row_overlap_ratio(
    cell_bbox: tuple[float, float, float, float],
    row_band: tuple[float, float],
) -> float:
    """Доля вертикального перекрытия двух строк (по bbox)."""
    row_top, row_bottom = row_band
    overlap = min(cell_bbox[3], row_bottom) - max(cell_bbox[1], row_top)
    row_h = max(row_bottom - row_top, 1.0)
    return max(0.0, overlap) / row_h




def _rowspan_value_forbidden(text: str) -> bool:
    """Числа, коды, годы, «-»/«х» — не объединять по rowspan (фин. таблицы)."""

    t = (text or "").strip()
    if not t:
        return False
    if t in {"-", "—", "–", "х", "Х", "x", "X"}:
        return True
    if _looks_numeric(t):
        return True
    # обычный и OCR-spaced год: «2024г.», «2 0 2 4 г»
    if re.fullmatch(r"\d{4}\s*г(?:ода?)?\.?", t, re.I):
        return True
    compact = re.sub(r"\s+", "", t)
    if re.fullmatch(r"\d{4}г(?:ода?)?\.?", compact, re.I):
        return True
    if re.fullmatch(r"(?:\d\s*){4}\s*г(?:ода?)?\.?", t, re.I):
        return True
    return False


def restore_rowspan_by_bbox(grid: list[list["Cell"]], tol: float = 1.0) -> list[list["Cell"]]:
    """
    Rowspan по геометрии bbox только вниз по пустым слотам той же колонки.

    - Не поглощает ячейки с текстом (иначе заголовки/числа вытесняют строки вправо).
    - Не ставит rowspan на числовые/кодовые/годовые значения.
    - Останавливается на первой непустой ячейке ниже.
    """
    del tol  # reserved
    bounds = _row_bounds(grid)
    n_rows = len(grid)

    for r, row in enumerate(grid):
        for cell in row:
            if cell.bbox is None or cell.covered:
                continue
            text = (cell.text or "").strip()
            if cell.rowspan > 1 and text:
                # не раздувать дальше; ошибочный label-rowspan на годах/шапках сбрасываем
                if text and (
                    _rowspan_value_forbidden(text)
                    or (
                        "_looks_like_column_header" in globals()
                        and _looks_like_column_header(text)
                    )
                ):
                    old_span = cell.rowspan
                    cell.rowspan = 1
                    for i in range(1, old_span):
                        rr = r + i
                        if rr >= n_rows or cell.col >= len(grid[rr]):
                            break
                        below = grid[rr][cell.col]
                        if getattr(below, "is_placeholder", False):
                            below.covered = False
                            below.is_placeholder = False
                    # fall through — геометрию на шапке/годе всё равно не ставим
                else:
                    continue
            if text and _rowspan_value_forbidden(text):
                continue
            if text and "_looks_like_column_header" in globals() and _looks_like_column_header(text):
                continue

            span = 1
            for i in range(r + 1, n_rows):
                band = bounds[i]
                if band is None:
                    break
                if cell.col >= len(grid[i]):
                    break
                below = grid[i][cell.col]
                if below.covered and not getattr(below, "is_placeholder", False):
                    break
                below_text = (below.text or "").strip()
                if below_text:
                    break
                if _row_overlap_ratio(cell.bbox, band) < 0.55:
                    break
                span += 1
                below.covered = True
                below.is_placeholder = True

            if span > 1:
                cell.rowspan = span

    return grid


def _looks_like_row_label(text: str) -> bool:
    """True, если текст похож на подпись строки таблицы."""
    t = text.strip()
    if len(t) < 2:
        return False
    if _looks_numeric(t):
        return False
    if _rowspan_value_forbidden(t):
        return False
    # заголовки колонок («Наименование…», «2022г.») — не якоря иерархии
    if "_looks_like_column_header" in globals() and _looks_like_column_header(t):
        return False
    return any(ch.isalpha() for ch in t)


def _row_has_child_label(row: list["Cell"], label_col: int) -> bool:
    """
    True, если справа от пустого слота подписи есть текстовая подпись
    дочернего уровня (например «Минтай» под «Чукотское море»).
    """
    for c in range(label_col + 1, len(row)):
        cell = row[c]
        if cell.covered:
            continue
        if cell.is_empty:
            continue
        if _looks_like_row_label(cell.text):
            return True
        # Число сразу справа (доля %) — не конец иерархии, смотрим дальше
        if _looks_numeric(cell.text) and c == label_col + 1:
            continue
        if _looks_numeric(cell.text):
            return False
    return False


def _column_has_label_rowspan_pattern(
    grid: list[list["Cell"]],
    kinds: list[str],
    col: int,
) -> bool:
    """Колонка годится для label-rowspan: якорная подпись и пустые/дочерние слоты ниже."""
    anchors = empty_slots = child_rows = 0
    for row, kind in zip(grid, kinds):
        if kind != DATA or col >= len(row):
            continue
        if not any(not c.is_empty for c in row[col + 1:]):
            continue
        cell = row[col]
        if cell.covered:
            continue
        if cell.is_empty:
            empty_slots += 1
            if _row_has_child_label(row, col):
                child_rows += 1
        elif _looks_like_row_label(cell.text):
            anchors += 1
    return anchors >= 1 and (empty_slots >= 1 or child_rows >= 1)


def _can_absorb_label_slot(row: list["Cell"], label_col: int) -> bool:
    """
    Можно ли поглотить пустой слот label_col в rowspan.
    True для иерархии «Договор | (пустой район) | Доля %» под якорной подписью.
    """
    if label_col >= len(row):
        return False
    slot = row[label_col]
    if slot.text.strip():
        return False
    if slot.covered and not getattr(slot, "is_placeholder", False):
        return False
    if slot.words and any(_looks_numeric(w["text"]) for w in slot.words):
        return False

    if label_col >= 1:
        left = row[label_col - 1]
        found = _leftmost_data_cell(row, label_col)
        if (
            found is not None
            and not left.covered
            and not left.is_empty
            and _looks_numeric(found[1].text)
            and (_looks_like_row_label(left.text) or re.search(r"\d", left.text))
        ):
            return True

    if _row_has_child_label(row, label_col):
        return True

    found = _leftmost_data_cell(row, label_col)
    if found is None:
        return False

    data_col, data_cell = found
    if data_col == label_col + 1 and _looks_numeric(data_cell.text):
        if label_col >= 1:
            left = row[label_col - 1]
            if not left.covered and not left.is_empty and _looks_like_row_label(left.text):
                return True
        return False
    return True


def _trim_numeric_leaks_from_label(cell: "Cell", grid: list[list["Cell"]], label_col: int) -> None:
    """Убирает из подписи числа, геометрически попавшие из соседней колонки."""
    if not cell.words:
        return
    bounds = _column_bounds(grid)
    if label_col >= len(bounds) or bounds[label_col] is None:
        return
    _x0, x1 = bounds[label_col]
    kept = [
        w for w in cell.words
        if not (_looks_numeric(w["text"]) and (w["x0"] + w["x1"]) / 2 > x1 - 1.5)
    ]
    if len(kept) != len(cell.words):
        cell.words = kept
        cell.text = words_to_cell_text(kept)


def _leftmost_data_cell(row: list["Cell"], from_col: int) -> tuple[int, "Cell"] | None:
    """Первая непустая не-covered ячейка справа от from_col."""
    for c in range(from_col + 1, len(row)):
        cell = row[c]
        if cell.covered or cell.is_empty:
            continue
        return c, cell
    return None


def _apply_label_rowspan_column(
    grid: list[list["Cell"]],
    kinds: list[str],
    label_col: int,
    max_span: int = 20,
) -> list[list["Cell"]]:
    """Проставляет rowspan по колонке label-ячеек."""
    n_rows = len(grid)
    r = 0
    while r < n_rows:
        if kinds[r] != DATA or label_col >= len(grid[r]):
            r += 1
            continue

        cell = grid[r][label_col]
        if cell.covered or not _looks_like_row_label(cell.text):
            r += 1
            continue
        # не растягивать шапку/годы на строки данных
        if _rowspan_value_forbidden(cell.text):
            r += 1
            continue
        if "_looks_like_column_header" in globals() and _looks_like_column_header(cell.text):
            r += 1
            continue
        # если в строке много peer-ячеек (полоса заголовков лет) — не label-rowspan
        peers = sum(
            1 for c in grid[r]
            if not c.covered and (c.text or "").strip()
        )
        if peers >= 4:
            r += 1
            continue

        span = 1
        for r2 in range(r + 1, min(n_rows, r + max_span)):
            if kinds[r2] != DATA or label_col >= len(grid[r2]):
                break
            if not _can_absorb_label_slot(grid[r2], label_col):
                break
            span += 1

        if span > 1:
            cell.rowspan = max(cell.rowspan, span)
            _trim_numeric_leaks_from_label(cell, grid, label_col)
            for r2 in range(r + 1, r + span):
                slot = grid[r2][label_col]
                slot.covered = True
                slot.is_placeholder = True
            r += span
        else:
            r += 1
    return grid


def restore_label_rowspan_soft(
    grid: list[list["Cell"]],
    kinds: list[str],
    max_span: int = 20,
) -> list[list["Cell"]]:
    """
    Rowspan для «Чукотское море», «Минтай» и аналогов.
    Колонки обрабатываются справа налево (вложенные подписи первыми).
    """
    n_cols = max(len(r) for r in grid) if grid else 0
    for col in range(n_cols - 1, -1, -1):
        if _column_has_label_rowspan_pattern(grid, kinds, col):
            grid = _apply_label_rowspan_column(grid, kinds, col, max_span)
    return grid


# --- notebook cell 20 ---
def merge_wrapped_rows(
    grid: list[list["Cell"]],
    kinds: list[str],
) -> tuple[list[list["Cell"]], list[str]]:
    """
    Сливает строки, разбитые переносом длинного текста.
    Работает в зоне DATA и для переносов заголовков в HEADER.
    """
    stacked: list[list["Cell"]] = []
    stacked_kinds: list[str] = []

    for i, row in enumerate(grid):
        if stacked and kinds[i] == stacked_kinds[-1]:
            if kinds[i] == DATA:
                merged = _try_merge_label_only_stack(stacked[-1], row)
                if merged is not None:
                    stacked[-1] = merged
                    continue
            elif kinds[i] == HEADER:
                merged = _try_merge_label_only_stack(stacked[-1], row)
                if merged is not None:
                    stacked[-1] = merged
                    continue
        stacked.append(row)
        stacked_kinds.append(kinds[i])

    result: list[list["Cell"]] = []
    result_kinds: list[str] = []

    for i, row in enumerate(stacked):
        if result and stacked_kinds[i] == DATA and result_kinds[-1] == DATA:
            merged = _try_merge_wrap(result[-1], row)
            if merged is not None:
                result[-1] = merged
                continue
        result.append(row)
        result_kinds.append(stacked_kinds[i])

    return result, result_kinds


# --- notebook cell 21 ---
"""
Расширение таблицы pdfplumber боковыми подписями.
"""


_KNOWN_ABBREVIATIONS = frozenset({
    "г", "гг",
    "руб", "коп",
    "тыс", "млн", "млрд",
    "долл", "дол",
    "евр",
    "ед", "шт", "чел",
    "стр", "рис", "табл",
    "т.д", "т.п",
    "др", "пр",
})


def _ends_with_known_abbreviation(text: str) -> bool:
    """Проверяет, заканчивается ли текст известным сокращением."""
    if not text.endswith("."):
        return False

    stripped = text.rstrip(".").strip()
    return (
        bool(stripped)
        and stripped.split()[-1].lower() in _KNOWN_ABBREVIATIONS
    )


def _font_sig(word: dict) -> tuple:
    """(Имя шрифта, размер с округлением до 0.5 pt)."""
    return (
        word.get("fontname"),
        round(float(word.get("size", 0)) * 2) / 2,
    )


def _y_aligned(w1: dict, w2: dict, tol: float) -> bool:
    """Совпадение top/bottom/center."""

    c1 = (w1["top"] + w1["bottom"]) / 2
    c2 = (w2["top"] + w2["bottom"]) / 2

    return any((
        abs(w1["top"] - w2["top"]) <= tol,
        abs(w1["bottom"] - w2["bottom"]) <= tol,
        abs(c1 - c2) <= tol,
    ))


def _cohesive_fragment(
    words: list[dict],
    side: str,
    gap_ratio: float = 1.5,
) -> list[dict]:
    """
    Выделяет непрерывный фрагмент одинакового шрифта,
    примыкающий к таблице.
    """

    if not words:
        return []

    words = sorted(words, key=lambda w: w["x0"])

    groups = [[words[0]]]

    for prev, cur in zip(words, words[1:]):
        same_font = _font_sig(prev) == _font_sig(cur)
        size = (
            float(prev.get("size", 0))
            or float(cur.get("size", 0))
            or 1.0
        )

        if same_font and (cur["x0"] - prev["x1"]) <= gap_ratio * size:
            groups[-1].append(cur)
        else:
            groups.append([cur])

    return groups[-1] if side == "left" else groups[0]


# --- notebook cell 22 ---
def _looks_like_stray_table_text(text: str) -> bool:
    """Заголовок документа / период / абзац — не подпись колонки и не поле формы."""

    t = (text or "").strip()
    if not t:
        return False
    tl = t.lower().replace("\n", " ")
    if re.search(
        r"(?:бухгалтерск|отчет об|пояснен|актив|пассив|январь|феврал|март|"
        r"апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)",
        tl,
    ) and (len(t) >= 12 or re.search(r"\d{4}", t)):
        if re.match(r"дата\s*\(", tl):
            return False
        if re.match(r"(?:форма\s+по|по\s+ок|инн|окпо|океи)", tl):
            return False
        return True
    if t.rstrip().endswith((".", "»")) and len(t) > 45:
        return True
    letters = [ch for ch in t if ch.isalpha()]
    if len(letters) >= 18:
        upper = sum(1 for ch in letters if ch.isupper()) / len(letters)
        if upper >= 0.85:
            return True
    return False


def enrich_grid_with_side_labels(
    page,
    grid: list[list["Cell"]],
    max_distance_ratio: float = 0.3,
    y_tolerance: float = 2.0,
    gap_ratio: float = 1.5,
) -> list[list["Cell"]]:
    """
    Добавляет к структуре таблицы подписи слева/справа как новые колонки Cell.
    Границы таблицы и строк берутся из самой сетки (grid), а не из table.*.
    """
    if not grid:
        return grid

    # bbox таблицы — из ячеек сетки
    boxes = [c.bbox for row in grid for c in row if c.bbox is not None]
    if not boxes:
        return grid
    t_x0 = min(b[0] for b in boxes)
    t_top = min(b[1] for b in boxes)
    t_x1 = max(b[2] for b in boxes)
    t_bottom = max(b[3] for b in boxes)

    max_distance = page.width * max_distance_ratio

    def y_center(w):
        """Вертикальный центр списка слов."""
        return (w["top"] + w["bottom"]) / 2

    words = page.extract_words(
        x_tolerance=1, y_tolerance=1, extra_attrs=["fontname", "size"]
    )

    inside_words, outside_words = [], []
    for w in words:
        if w["bottom"] < t_top - y_tolerance or w["top"] > t_bottom + y_tolerance:
            continue
        if t_x0 <= w["x0"] and w["x1"] <= t_x1:
            inside_words.append(w)
        elif w["x1"] < t_x0 and t_x0 - w["x1"] <= max_distance:
            outside_words.append(w)
        elif w["x0"] > t_x1 and w["x0"] - t_x1 <= max_distance:
            outside_words.append(w)

    table_fonts = {_font_sig(w) for w in inside_words}

    def is_valid_label(fragment: list[dict]) -> bool:
        """True, если фрагмент годится как боковая подпись."""

        if not fragment:
            return False
        if len({_font_sig(w) for w in fragment}) > 1:
            return False
        text = " ".join(w["text"] for w in fragment).strip()
        if not text:
            return False
        if text.endswith(".") and not _ends_with_known_abbreviation(text):
            return False
        # случайный заголовок/период сверху формы — не подпись кода
        if _looks_like_stray_table_text(text):
            return False
        _FORM_LABEL = re.compile(
            r"^(?:по\s+)?(?:ОКПО|ОКУД|ОКОПФ|ОКФС|ОКЕИ|ОКВЭД2?|ИНН|ОГРН)|"
            r"^Форма\s+по|^Дата\s*\(|^Единица\s+измерения|"
            r"^по\s+ОКПО|^по\s+ОКОПФ|^по\s+ОКФС|^по\s+ОКЕИ|^по\s+ОКУД",
            re.I,
        )
        if _FORM_LABEL.search(text):
            return True
        return text[0].isupper() or any(_font_sig(w) in table_fonts for w in fragment)

    # для каждой строки сетки — её y-полоса и подписи по бокам
    left_frags: list[list[dict] | None] = []
    right_frags: list[list[dict] | None] = []

    for row in grid:
        ys = [(c.bbox[1], c.bbox[3]) for c in row if c.bbox is not None]
        if not ys:
            left_frags.append(None)
            right_frags.append(None)
            continue
        r_top = min(t for t, _ in ys)
        r_bottom = max(b for _, b in ys)

        anchors = [
            w for w in inside_words
            if r_top - y_tolerance <= y_center(w) <= r_bottom + y_tolerance
        ]

        left, right = [], []
        for w in outside_words:
            if w["bottom"] < r_top - y_tolerance or w["top"] > r_bottom + y_tolerance:
                continue
            if not any(_y_aligned(w, a, y_tolerance) for a in anchors):
                continue
            (left if w["x1"] < t_x0 else right).append(w)

        lf = _cohesive_fragment(left, "left", gap_ratio)
        rf = _cohesive_fragment(right, "right", gap_ratio)
        left_frags.append(lf if is_valid_label(lf) else None)
        right_frags.append(rf if is_valid_label(rf) else None)

    has_left = any(left_frags)
    has_right = any(right_frags)
    if not has_left and not has_right:
        return grid

    def make_cell(r_idx: int, frag: list[dict] | None) -> "Cell":
        """Создаёт Cell из bbox и списка слов."""
        if not frag:
            return Cell(row=r_idx, col=0, bbox=None, text="", words=[])
        return Cell(
            row=r_idx, col=0,
            bbox=(min(w["x0"] for w in frag), min(w["top"] for w in frag),
                  max(w["x1"] for w in frag), max(w["bottom"] for w in frag)),
            text=words_to_cell_text(frag),
            words=list(frag),
        )

    new_grid = []
    for r_idx, row in enumerate(grid):
        new_row = []
        if has_left:
            new_row.append(make_cell(r_idx, left_frags[r_idx]))
        new_row.extend(row)
        if has_right:
            new_row.append(make_cell(r_idx, right_frags[r_idx]))
        for new_c, cell in enumerate(new_row):   # переиндексация колонок
            cell.col = new_c
        new_grid.append(new_row)

    return new_grid


# --- notebook cell 23 ---
def grid_to_matrix(grid: list[list["Cell"]]) -> list[list[str]]:
    """
    Превращает структуру таблицы в плоскую матрицу текста.

    colspan разворачивается в пустые ячейки, чтобы все строки имели
    одинаковое число колонок и markdown/CSV не «поехал».
    (Для HTML это не нужно — там colspan рендерится атрибутом прямо из grid.)
    """
    if not grid:
        return []

    # ширина = максимум суммы colspan по строкам
    width = max(sum(getattr(c, "colspan", 1) for c in row) for row in grid)

    matrix: list[list[str]] = []
    for row in grid:
        line: list[str] = []
        for cell in row:
            span = getattr(cell, "colspan", 1)
            line.append((cell.text or "").strip())
            line.extend([""] * (span - 1))       # места, поглощённые colspan
        line.extend([""] * (width - len(line)))  # добить строку до общей ширины
        matrix.append(line)
    return matrix


# --- notebook cell 24 ---
def matrix_to_markdown(matrix: list[list[str]]) -> str:
    """Матрица текста -> GFM-таблица. Первая строка — заголовок."""
    if not matrix:
        return ""

    n_cols = max(len(r) for r in matrix)
    matrix = [r + [""] * (n_cols - len(r)) for r in matrix]

    def esc(s: str) -> str:
        """Экранирует текст для Markdown/HTML-таблицы."""
        return (s or "").replace("\n", " ").replace("|", "\\|").strip()

    lines = []
    lines.append("| " + " | ".join(esc(c) for c in matrix[0]) + " |")
    lines.append("| " + " | ".join(["---"] * n_cols) + " |")
    for row in matrix[1:]:
        lines.append("| " + " | ".join(esc(c) for c in row) + " |")
    return "\n".join(lines)


# --- notebook cell 25 ---
def _cross_gap(left_words: list[dict], right_words: list[dict]) -> float | None:
    """Зазор между ближайшими словами слева и справа от границы. None, если с одной стороны пусто."""
    if not left_words or not right_words:
        return None
    max_left = max(w["x1"] for w in left_words)
    min_right = min(w["x0"] for w in right_words)
    return min_right - max_left


def _boundary_must_keep(grid: list[list["Cell"]], col: int) -> bool:
    """Граница col|col+1 — настоящая колонка (числа/подпись+число). Не сливать."""

    for row in grid:
        if col + 1 >= len(row):
            continue
        left, right = row[col], row[col + 1]
        if left.covered or right.covered:
            continue
        lt = (left.text or "").strip()
        rt = (right.text or "").strip()
        if not lt and not rt:
            continue
        if _looks_numeric(lt) and (_looks_numeric(rt) or bool(re.search(r"\d", rt))):
            return True
        if _looks_numeric(rt) and (_looks_numeric(lt) or bool(re.search(r"\d", lt))):
            return True
        if lt and rt and _looks_like_row_label(lt) and (_looks_numeric(rt) or bool(re.search(r"\d", rt))):
            return True
        if lt and rt and re.search(r"[A-Za-zА-Яа-яЁё]", lt) and re.search(r"[A-Za-zА-Яа-яЁё]", rt):
            if not (_looks_numeric(lt) or _looks_numeric(rt)):
                continue
    return False


def find_phantom_boundaries(
    grid: list[list["Cell"]],
    gap_ratio: float = 2.0,
    min_ratio: float = 0.35,
) -> list[int]:
    """
    Возвращает индексы фантомных границ. Индекс c означает: граница между
    колонками c и c+1 ложная (в большинстве строк режет близкий контент).
    """
    n_cols = max(len(r) for r in grid)
    phantom: list[int] = []

    for c in range(n_cols - 1):
        if _boundary_must_keep(grid, c):
            continue
        supports = 0   # строк, где граница режет близкое (за фантом)
        total = 0      # строк, где по обе стороны есть контент

        for row in grid:
            left = row[c].words if c < len(row) else []
            right = row[c + 1].words if c + 1 < len(row) else []
            gap = _cross_gap(left, right)
            if gap is None:
                continue
            total += 1
            size = float((left + right)[0].get("size", 0)) or 1.0
            if gap <= gap_ratio * size:
                supports += 1

        if total > 0 and supports / total >= min_ratio:
            phantom.append(c)

    return phantom


# --- notebook cell 26 ---
def _cluster_words_by_x(words: list[dict], gap_tol: float = 14.0) -> list[list[dict]]:
    """Кластеризует слова строки по x-зазорам (gap_tol)."""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: w["x0"])
    clusters: list[list[dict]] = [[ordered[0]]]
    for w in ordered[1:]:
        prev = clusters[-1][-1]
        size = float(prev.get("size", 0)) or 10.0
        if w["x0"] - prev["x1"] <= max(gap_tol, size * 1.2):
            clusters[-1].append(w)
        else:
            clusters.append([w])
    return clusters


def _cluster_is_numeric(cluster: list[dict]) -> bool:
    """True, если кластер слов выглядит числовым."""
    text = " ".join(w["text"] for w in cluster).strip()
    return bool(text) and (_looks_numeric(text) or bool(__import__("re").search(r"\d{2,}", text)))


def _realign_financial_row(row: list["Cell"], row_idx: int) -> list["Cell"] | None:
    """Строка с подписью + несколькими числовыми колонками -> label + N value cols."""
    words: list[dict] = []
    for cell in row:
        if not cell.covered:
            words.extend(cell.words)
    if not words:
        return None

    words = _merge_split_digit_words(words)
    ordered = sorted(words, key=lambda w: (round(w["top"]), w["x0"]))
    lines: list[list[dict]] = [[ordered[0]]] if ordered else []
    for w in ordered[1:]:
        if abs(w["top"] - lines[-1][-1]["top"]) <= 3:
            lines[-1].append(w)
        else:
            lines.append([w])
    if not lines:
        return None

    # основная строка данных
    main_line = max(lines, key=len)
    clusters = _cluster_words_by_x(main_line)
    if len(clusters) < 3:
        return None

    first_num = next((i for i, cl in enumerate(clusters) if _cluster_is_numeric(cl)), None)
    if first_num is None:
        return None

    first_num = next((i for i, cl in enumerate(clusters) if _cluster_is_numeric(cl)), None)
    if first_num is None:
        return None

    label_words: list[dict] = []
    num_clusters: list[list[dict]] = []

    if first_num > 0:
        label_words = [w for cl in clusters[:first_num] for w in cl]
        num_clusters = [cl for cl in clusters[first_num:] if _cluster_is_numeric(cl)]
    else:
        cl0 = clusters[0]
        split_at = next(
            (j for j, w in enumerate(cl0) if __import__("re").search(r"\d", w.get("text", ""))),
            None,
        )
        if split_at is not None and split_at > 0:
            label_words = cl0[:split_at]
            num_clusters = [
                cl for cl in _cluster_words_by_x(cl0[split_at:])
                if _cluster_is_numeric(cl)
            ]
        else:
            num_clusters = [cl for cl in clusters if _cluster_is_numeric(cl)]

    if len(num_clusters) < 2:
        return None

    new_row: list["Cell"] = []

    label_words.sort(key=lambda w: (round(w["top"]), w["x0"]))
    lbboxes = [c.bbox for c in row if c.bbox and label_words]
    label_bbox = row[0].bbox if row and row[0].bbox else (None if not lbboxes else (
        min(b[0] for b in lbboxes), min(b[1] for b in lbboxes),
        max(b[2] for b in lbboxes), max(b[3] for b in lbboxes),
    ))
    new_row.append(Cell(
        row=row_idx, col=0, bbox=label_bbox,
        text=words_to_cell_text(label_words), words=label_words,
    ))

    for ci, cl in enumerate(num_clusters, start=1):
        cl.sort(key=lambda w: (round(w["top"]), w["x0"]))
        x0 = min(w["x0"] for w in cl)
        x1 = max(w["x1"] for w in cl)
        top = min(w["top"] for w in cl)
        bottom = max(w["bottom"] for w in cl)
        new_row.append(Cell(
            row=row_idx, col=ci, bbox=(x0, top, x1, bottom),
            text=words_to_cell_text(cl), words=list(cl),
        ))
    return new_row


def _grid_needs_financial_realign(grid: list[list["Cell"]]) -> bool:
    """True, если финансовую сетку стоит перевыровнять по колонкам."""

    n_cols = max(len(r) for r in grid) if grid else 0
    if n_cols < 3:
        return False

    hits = 0
    for row in grid:
        rebuilt = _realign_financial_row(row, 0)
        if rebuilt and len(rebuilt) >= 3:
            hits += 1
            continue
        for cell in row:
            if not cell.covered and _looks_like_row_label(cell.text):
                if len(re.findall(r"\d{2,}", cell.text)) >= 2:
                    hits += 1
                    break
    return hits >= 2


def realign_financial_grid(grid: list[list["Cell"]]) -> tuple[list[list["Cell"]], bool]:
    """Перестраивает borderless финансовые таблицы по x-кластерам чисел."""
    if not _grid_needs_financial_realign(grid):
        return grid, False

    rebuilt_rows: list[list["Cell"] | None] = []
    col_counts: list[int] = []
    for r_idx, row in enumerate(grid):
        rebuilt = _realign_financial_row(row, r_idx)
        rebuilt_rows.append(rebuilt)
        if rebuilt:
            col_counts.append(len(rebuilt))

    if not col_counts:
        return grid, False

    target_cols = Counter(col_counts).most_common(1)[0][0]
    if target_cols < 3:
        return grid, False

    new_grid: list[list["Cell"]] = []
    for r_idx, row in enumerate(grid):
        rebuilt = rebuilt_rows[r_idx]
        if rebuilt is None:
            new_grid.append(row)
            continue
        # выравниваем до target_cols
        while len(rebuilt) < target_cols:
            rebuilt.append(Cell(row=r_idx, col=len(rebuilt), bbox=None, is_placeholder=True))
        for c, cell in enumerate(rebuilt[:target_cols]):
            cell.row = r_idx
            cell.col = c
        new_grid.append(rebuilt[:target_cols])
    return new_grid, True


# --- notebook cell 27 ---

def _cell_font_sig(cell: "Cell") -> tuple | None:
    """(fontname, size) по словам ячейки."""
    if not cell.words:
        return None
    sigs = [_font_sig(w) for w in cell.words]
    return Counter(sigs).most_common(1)[0][0]


def _looks_like_column_header(text: str) -> bool:
    """Отдельный заголовок колонки — не склеивать с соседями."""

    t = (text or "").strip()
    if not t:
        return False
    tl = t.lower().replace("\n", " ")

    _EXACT = {
        "пояснения", "код", "договор", "поступило", "выбыло", "списано",
        "начислено", "итого", "актив", "пассив", "баланс",
    }
    if tl in _EXACT:
        return True
    if re.fullmatch(r"код", tl, re.I):
        return True
    if re.fullmatch(r"пояснения", tl, re.I):
        return True
    if re.fullmatch(r"договор", tl, re.I):
        return True
    if re.fullmatch(r"поступило|выбыло|списано|начислено|переоцен", tl, re.I):
        return True
    if re.fullmatch(r"на\s+31\s+декабря", tl, re.I):
        return True
    if re.fullmatch(r"\d{4}\s*г(?:ода?)?\.?", tl, re.I):
        return True
    compact = re.sub(r"\s+", "", tl)
    if re.fullmatch(r"\d{4}г(?:ода?)?\.?", compact, re.I):
        return True
    if re.fullmatch(r"(?:\d\s*){4}\s*г(?:ода?)?\.?", tl, re.I):
        return True
    if re.fullmatch(r"доля,?\s*%?", tl, re.I):
        return True
    if re.search(r"^наименование\b", tl, re.I) and len(t) <= 40:
        return True
    if "район" in tl and "промысл" in tl:
        return True
    if re.search(r"^наименование\s+вбр", tl, re.I):
        return True
    # короткий заголовок из заглавных / жирных слов
    words = t.split()
    if len(words) <= 4 and len(t) <= 28:
        letters = [ch for ch in t if ch.isalpha()]
        if letters and sum(1 for ch in letters if ch.isupper()) / len(letters) >= 0.65:
            return True
    return False


def _should_merge_adjacent_labels(
    a: "Cell",
    b: "Cell",
    row: list["Cell"],
    col_a: int,
    col_b: int,
) -> bool:
    """Склеивать только явные переносы подписи, не соседние колонки."""

    ta = (a.text or "").strip()
    tb = (b.text or "").strip()
    if not ta or not tb:
        return False

    if _looks_like_column_header(ta) or _looks_like_column_header(tb):
        return False

    if re.search(r"\d{4}\s*г(?:ода?)?", ta, re.I) or re.search(r"\d{4}\s*г(?:ода?)?", tb, re.I):
        return False
    if re.search(r"на\s+31\s+декабря", ta, re.I) or re.search(r"на\s+31\s+декабря", tb, re.I):
        return False

    fa, fb = _cell_font_sig(a), _cell_font_sig(b)
    if fa and fb:
        if fa[0] != fb[0] and ("Bold" in fa[0]) != ("Bold" in fb[0]):
            return False
        if abs(fa[1] - fb[1]) > 0.6:
            return False

    if ta.endswith("-"):
        return True
    if tb[0].islower():
        return True
    tail = ta.rstrip()
    if tail.endswith(",") or tail.endswith(";"):
        return True

    # номер строки + наименование — только вне платёжных/датовых строк
    if col_a == 0 and re.fullmatch(r"\d{1,2}", ta) and re.search(r"[A-Za-zА-Яа-яЁё]", tb):
        row_blob = " ".join((c.text or "") for c in row)
        if re.search(r"\d{2}\.\d{2}\.\d{4}", row_blob):
            return False
        if re.search(r"\d[\d\s]*[.,]\d{2}\b", row_blob):
            return False
        return True

    if len(ta.split()) <= 3 and tb[0].islower():
        return True

    return False



def _cell_is_label_text(cell: "Cell", row: list["Cell"] | None = None, col_idx: int | None = None) -> bool:
    """True, если текст ячейки — подпись, а не значение."""

    t = (cell.text or "").strip()
    if not t:
        return True
    if _looks_like_table_data_value(t):
        return False
    if re.search(r"[A-Za-zА-Яа-яЁё«]", t):
        return True
    # номер строки / год — только в первой колонке
    if col_idx == 0:
        if re.fullmatch(r"\d{1,2}", t):
            return True
        if re.fullmatch(r"20\d{2}", t):
            return True
    if re.fullmatch(r"\(?\s*тыс\.\s*руб\.?\s*\)?", t, re.I):
        return True
    if row is not None and col_idx is not None and col_idx > 0 and re.fullmatch(r"\d+", t):
        prev_text = " ".join(
            (row[i].text or "").strip()
            for i in range(col_idx)
            if not getattr(row[i], "covered", False)
        )
        if re.search(r"(?:ООО|ЗАО|ПАО|АО|ИП)[\s«\"]", prev_text):
            return False
    return False


def merge_leading_text_columns(grid: list[list["Cell"]]) -> list[list["Cell"]]:
    """
    Склеивает ведущие текстовые колонки строки до первого числового значения.
    Только явные переносы подписи — не соседние заголовки колонок.
    """

    if not grid:
        return grid

    def _row_has_company_label(row: list["Cell"]) -> bool:
        """True, если в строке есть название компании (ООО/…)."""
        return any(
            re.search(r'(?:ООО|ЗАО|ПАО|АО|ИП)[\s«"]', c.text or "")
            for c in row
            if not getattr(c, "covered", False)
        )

    new_grid: list[list["Cell"]] = []
    for r_idx, row in enumerate(grid):
        merged_row: list["Cell"] = []
        c = 0
        while c < len(row):
            cell = row[c]
            if getattr(cell, "covered", False):
                merged_row.append(cell)
                c += 1
                continue

            t = (cell.text or "").strip()
            if _looks_like_column_header(t):
                merged_row.append(cell)
                c += 1
                continue

            if not _cell_is_label_text(cell, row, c) or (_looks_like_table_data_value(t) and c > 0):
                merged_row.append(cell)
                c += 1
                continue

            run = [cell]
            j = c + 1
            while j < len(row):
                nxt = row[j]
                if getattr(nxt, "covered", False):
                    j += 1
                    continue
                nt = (nxt.text or "").strip()
                if not nt:
                    break

                if re.search(r"\d{4}\s*г(?:ода?)?", nt, re.I):
                    break
                if _row_has_company_label(row) and re.fullmatch(r"\d+", nt):
                    break
                if _should_merge_adjacent_labels(run[-1], nxt, row, j - 1, j):
                    run.append(nxt)
                    j += 1
                    continue
                break

            filled = [x for x in run if (x.text or "").strip()]
            if len(filled) <= 1:
                merged_row.append(cell)
                c += 1
                continue

            words: list[dict] = []
            bboxes: list[tuple[float, float, float, float]] = []
            texts: list[str] = []
            for x in run:
                words.extend(x.words)
                if x.bbox is not None:
                    bboxes.append(x.bbox)
                if (x.text or "").strip():
                    texts.append(x.text.strip())
            words.sort(key=lambda w: (round(w["top"]), w["x0"]))
            bbox = None
            if bboxes:
                bbox = (
                    min(b[0] for b in bboxes),
                    min(b[1] for b in bboxes),
                    max(b[2] for b in bboxes),
                    max(b[3] for b in bboxes),
                )
            base = run[0]
            merged_row.append(Cell(
                row=r_idx, col=base.col, bbox=bbox,
                text=" ".join(texts), words=words,
                colspan=len(run), rowspan=base.rowspan,
            ))
            c = j

        for nc, cell in enumerate(merged_row):
            cell.col = nc
        new_grid.append(merged_row)

    return new_grid


def merge_phantom_columns(
    grid: list[list["Cell"]],
    phantom: list[int],
) -> list[list["Cell"]]:
    """Сливает колонки, разделённые фантомными границами, обратно в одну."""
    phantom_set = set(phantom)
    n_cols = max(len(r) for r in grid)

    # группы колонок для слияния: [[0], [1], [2,3,4], [5], ...]
    groups: list[list[int]] = []
    c = 0
    while c < n_cols:
        group = [c]
        while c in phantom_set:      # граница после c фантомная -> тянем c+1 в группу
            c += 1
            group.append(c)
        groups.append(group)
        c += 1

    new_grid: list[list["Cell"]] = []
    for r_idx, row in enumerate(grid):
        new_row: list["Cell"] = []
        for new_c, group in enumerate(groups):
            cells = [row[cc] for cc in group if cc < len(row)]

            words: list[dict] = []
            for cell in cells:
                words.extend(cell.words)
            words.sort(key=lambda w: (round(w["top"]), w["x0"]))

            text = words_to_cell_text(words)
            bboxes = [cell.bbox for cell in cells if cell.bbox]
            bbox = (
                min(b[0] for b in bboxes), min(b[1] for b in bboxes),
                max(b[2] for b in bboxes), max(b[3] for b in bboxes),
            ) if bboxes else None
            new_row.append(Cell(
                row=r_idx,
                col=new_c,
                bbox=bbox,
                text=text,
                words=words,
                is_placeholder=not text.strip(),
            ))
        new_grid.append(new_row)

    return new_grid


# --- notebook cell 28 ---
def drop_empty_columns_grid(grid: list[list["Cell"]]) -> list[list["Cell"]]:
    """Убирает полностью пустые колонки (фантомные), переиндексирует col."""
    if not grid:
        return grid

    n = max(len(r) for r in grid)
    keep = [j for j in range(n) if any(j < len(r) and r[j].text.strip() for r in grid)]

    new_grid: list[list["Cell"]] = []
    for row in grid:
        new_row = []
        for new_c, j in enumerate(keep):
            if j < len(row):
                cell = row[j]
                cell.col = new_c          # переиндексация колонок
                new_row.append(cell)
        new_grid.append(new_row)
    return new_grid

def drop_empty_rows_grid(
    grid: list[list["Cell"]],
    kinds: list[str],
) -> tuple[list[list["Cell"]], list[str]]:
    """Убирает полностью пустые строки (визуальные разделители), синхронно с kinds."""
    new_grid: list[list["Cell"]] = []
    new_kinds: list[str] = []
    for row, k in zip(grid, kinds):
        if any(c.text.strip() for c in row):
            new_grid.append(row)
            new_kinds.append(k)
    return new_grid, new_kinds


# --- notebook cell 29 ---

def _cell_font_sig(cell: "Cell") -> tuple | None:
    """(fontname, size) по словам ячейки."""
    if not cell.words:
        return None
    sigs = [_font_sig(w) for w in cell.words]
    return Counter(sigs).most_common(1)[0][0]


def _looks_like_column_header(text: str) -> bool:
    """Отдельный заголовок колонки — не склеивать с соседями."""

    t = (text or "").strip()
    if not t:
        return False
    tl = t.lower().replace("\n", " ")

    _EXACT = {
        "пояснения", "код", "договор", "поступило", "выбыло", "списано",
        "начислено", "итого", "актив", "пассив", "баланс",
    }
    if tl in _EXACT:
        return True
    if re.fullmatch(r"код", tl, re.I):
        return True
    if re.fullmatch(r"пояснения", tl, re.I):
        return True
    if re.fullmatch(r"договор", tl, re.I):
        return True
    if re.fullmatch(r"поступило|выбыло|списано|начислено|переоцен", tl, re.I):
        return True
    if re.fullmatch(r"на\s+31\s+декабря", tl, re.I):
        return True
    if re.fullmatch(r"\d{4}\s*г(?:ода?)?\.?", tl, re.I):
        return True
    compact = re.sub(r"\s+", "", tl)
    if re.fullmatch(r"\d{4}г(?:ода?)?\.?", compact, re.I):
        return True
    if re.fullmatch(r"(?:\d\s*){4}\s*г(?:ода?)?\.?", tl, re.I):
        return True
    if re.fullmatch(r"доля,?\s*%?", tl, re.I):
        return True
    if re.search(r"^наименование\b", tl, re.I) and len(t) <= 48:
        return True
    if "район" in tl and "промысл" in tl:
        return True
    if re.search(r"^наименование\s+вбр", tl, re.I):
        return True
    # заголовки счёта-фактуры / оплаты
    if re.search(
        r"(?i)(?:п/?п|предмет\s+договора|дата\s+оплаты|сумма\s+оплаты|"
        r"единица\s+измерения|количество|код\s+вида|цена\s*\(|тариф|"
        r"стоимость|налоговая\s+ставка|примечание)",
        tl,
    ) and len(t) <= 64:
        return True
    if re.fullmatch(r"№|N|№\s*п/?п", tl):
        return True

    # подзаголовки СФ/УПД/ТОРГ/КС (2-й уровень шапки)
    if re.search(
        r"(?i)условн\w*\s*обознач|национальн|цифро[-\s]*в\w*\s*код|"
        r"кратк\w*\s*наименов|код\s*по\s*океи|"
        r"наименов\w*\s*,?\s*характерист|масса\s*брутто|масса\s*нетто|"
        r"вид\s*упаков|по\s*поряд|выполнено\s*работ|"
        r"цена\s*за\s*единиц|стоимост\w*,?\s*руб",
        tl,
    ) and len(t) <= 96:
        return True

    # короткий заголовок из заглавных / жирных слов
    words = t.split()
    if len(words) <= 4 and len(t) <= 28:
        letters = [ch for ch in t if ch.isalpha()]
        if letters and sum(1 for ch in letters if ch.isupper()) / len(letters) >= 0.65:
            return True
    return False


def _should_merge_adjacent_labels(
    a: "Cell",
    b: "Cell",
    row: list["Cell"],
    col_a: int,
    col_b: int,
) -> bool:
    """Склеивать только явные переносы подписи, не соседние колонки."""

    ta = (a.text or "").strip()
    tb = (b.text or "").strip()
    if not ta or not tb:
        return False

    if _looks_like_column_header(ta) or _looks_like_column_header(tb):
        return False

    if re.search(r"\d{4}\s*г(?:ода?)?", ta, re.I) or re.search(r"\d{4}\s*г(?:ода?)?", tb, re.I):
        return False
    if re.search(r"на\s+31\s+декабря", ta, re.I) or re.search(r"на\s+31\s+декабря", tb, re.I):
        return False

    fa, fb = _cell_font_sig(a), _cell_font_sig(b)
    if fa and fb:
        if fa[0] != fb[0] and ("Bold" in fa[0]) != ("Bold" in fb[0]):
            return False
        if abs(fa[1] - fb[1]) > 0.6:
            return False

    if ta.endswith("-"):
        return True
    if tb[0].islower():
        return True
    tail = ta.rstrip()
    if tail.endswith(",") or tail.endswith(";"):
        return True

    # номер строки + наименование — только вне платёжных/датовых строк
    if col_a == 0 and re.fullmatch(r"\d{1,2}", ta) and re.search(r"[A-Za-zА-Яа-яЁё]", tb):
        row_blob = " ".join((c.text or "") for c in row)
        if re.search(r"\d{2}\.\d{2}\.\d{4}", row_blob):
            return False
        if re.search(r"\d[\d\s]*[.,]\d{2}\b", row_blob):
            return False
        return True

    if len(ta.split()) <= 3 and tb[0].islower():
        return True

    return False



def _grid_is_vertical_code_stack(grid: list[list["Cell"]]) -> bool:
    """Таблица кодов (ОКУД/ОКПО): короткие строки «подпись | код»."""

    if len(grid) < 3:
        return False
    n_cols = max(len(r) for r in grid) if grid else 0
    if n_cols > 4:
        return False

    form_markers = sum(
        1
        for row in grid
        for c in row
        if re.search(r"ОКУД|ОКПО|ОКОПФ|ОКФС|ОКЕИ|Форма по", c.text or "", re.I)
    )

    multi_value_rows = 0
    pairs = 0
    for row in grid:
        visible = [c for c in row if not getattr(c, "covered", False) and not c.is_empty]
        if len(visible) < 2:
            continue
        value_cols = [c for c in visible if c.col > 0]
        if len(value_cols) >= 2:
            multi_value_rows += 1
        label = (visible[0].text or "").strip()
        if visible[0].col != 0 or not re.search(r"[A-Za-zА-Яа-яЁё]", label):
            continue
        if any(
            _looks_like_table_data_value((c.text or "").strip())
            or re.fullmatch(r"\d{5,}", (c.text or "").strip())
            for c in value_cols
        ):
            pairs += 1

    # широкие таблицы с несколькими колонками значений — не стек кодов
    if multi_value_rows >= 2 and form_markers == 0:
        return False
    if n_cols >= 3 and form_markers == 0 and multi_value_rows >= 1:
        return False

    return pairs >= 3 and form_markers >= 1 or (pairs >= 3 and n_cols <= 3 and multi_value_rows == 0)


def _grid_is_financial_statement(grid: list[list["Cell"]]) -> bool:
    """Бухгалтерский баланс / отчёт: много 4-значных счетов."""

    codes = sum(
        1
        for row in grid
        for c in row
        if re.fullmatch(r"\d{4}", (c.text or "").strip())
    )
    return codes >= 4

from pdf_table_engine import find_tables_smart, table_looks_like_prose


def _augment_vertical_code_stack(page, grid: list[list["Cell"]]) -> list[list["Cell"]]:
    """
    Достраивает стек «Коды» (ОКУД/ОКПО): недостающие верхние строки и левые
    подписи формы, не затягивая заголовок документа и поля «Организация».
    """

    if not grid:
        return grid

    boxes = [c.bbox for row in grid for c in row if c.bbox is not None]
    if not boxes:
        return grid
    t_top = min(b[1] for b in boxes)
    t_x1 = max(b[2] for b in boxes)
    t_bottom = max(b[3] for b in boxes)
    # якорь — колонка значений кодов (непустые ячейки col>=1), не пустой хвост
    n_cols_now = max(len(r) for r in grid)
    value_boxes = [
        c.bbox
        for row in grid
        for c in row
        if c.bbox is not None and c.col >= 1 and (c.text or "").strip()
    ]
    if not value_boxes:
        value_boxes = [
            c.bbox
            for row in grid
            for c in row
            if c.bbox is not None and c.col >= max(0, n_cols_now - 1)
        ]
    if not value_boxes:
        value_boxes = boxes
    v_x0 = min(b[0] for b in value_boxes)
    t_x0 = v_x0

    # только узкий правый блок кодов
    if v_x0 < page.width * 0.45 or (t_x1 - v_x0) > page.width * 0.35:
        return grid
    flat = " ".join((c.text or "") for row in grid for c in row)
    code_hits = len(re.findall(r"\b\d{5,}\b", flat))
    if code_hits < 2 and not re.search(r"ОКУД|ОКПО|Коды", flat, re.I):
        return grid

    words = page.extract_words(
        x_tolerance=1, y_tolerance=1, extra_attrs=["fontname", "size"]
    )
    if not words:
        return grid

    def _frag_text(frag: list[dict]) -> str:
        """Текст фрагмента слов через пробел."""
        return " ".join(w["text"] for w in frag).strip()

    def _cell_from_frag(r_idx: int, col: int, frag: list[dict] | None) -> "Cell":
        """Cell из фрагмента слов и целевых колонок."""
        if not frag:
            return Cell(row=r_idx, col=col, bbox=None, text="", words=[])
        return Cell(
            row=r_idx,
            col=col,
            bbox=(
                min(w["x0"] for w in frag),
                min(w["top"] for w in frag),
                max(w["x1"] for w in frag),
                max(w["bottom"] for w in frag),
            ),
            text=words_to_cell_text(frag),
            words=list(frag),
        )

    _LABEL_RE = re.compile(
        r"^(?:Форма\s+по\s+ОКУД|Дата\s*\(|(?:по\s+)?(?:ОКПО|ОКУД|ОКОПФ|ОКФС|ОКЕИ|ОКВЭД2?|ИНН)|"
        r"по\s+ОКОПФ/\s*ОКФС|по\s+ОКПО|по\s+ОКЕИ|по\s+ОКУД)",
        re.I,
    )

    # слова над таблицей в полосе кодов (и чуть левее — подписи)
    above = [
        w
        for w in words
        if w["bottom"] <= t_top + 1.5
        and w["top"] >= t_top - 70
        and w["x0"] >= t_x0 - 140
        and w["x1"] <= t_x1 + 8
    ]
    has_kody = any(re.search(r"Коды", c.text or "", re.I) for row in grid for c in row)
    has_okud = any(re.search(r"ОКУД|0710", c.text or "", re.I) for row in grid for c in row)

    new_top_rows: list[list["Cell"]] = []
    if above and (not has_kody or not has_okud):
        # кластеризация по y
        above_sorted = sorted(above, key=lambda w: (round(w["top"] / 3) * 3, w["x0"]))
        bands: list[list[dict]] = []
        for w in above_sorted:
            if not bands or abs(w["top"] - bands[-1][0]["top"]) > 6:
                bands.append([w])
            else:
                bands[-1].append(w)

        for band in bands:
            left = [w for w in band if w["x1"] < t_x0 - 1]
            right = [w for w in band if w["x0"] >= t_x0 - 1]
            lt = _frag_text(left)
            rt = _frag_text(right)
            if _looks_like_stray_table_text(lt) or _looks_like_stray_table_text(rt):
                continue
            if not has_kody and (
                re.fullmatch(r"Коды", rt, re.I)
                or (re.fullmatch(r"Коды", lt, re.I) and not rt)
            ):
                kody_frag = right if re.fullmatch(r"Коды", rt, re.I) else left
                new_top_rows.append(
                    [
                        _cell_from_frag(0, 0, None),
                        _cell_from_frag(0, 1, kody_frag),
                    ]
                )
                has_kody = True
                continue
            if not has_okud and (
                _LABEL_RE.search(lt) or re.search(r"ОКУД", lt, re.I)
            ) and re.search(r"\d{5,}", rt):
                new_top_rows.append(
                    [
                        _cell_from_frag(0, 0, left),
                        _cell_from_frag(0, 1, right),
                    ]
                )
                has_okud = True
            elif not has_okud and not lt and re.search(r"0710\d{3}", rt):
                # значение ОКУД без подписи в этом же band — подпись ищем левее
                lab = [
                    w
                    for w in words
                    if w["x1"] < t_x0 - 1
                    and w["x0"] >= t_x0 - 140
                    and abs(w["top"] - band[0]["top"]) <= 8
                ]
                lab_text = _frag_text(lab)
                if lab and (
                    _LABEL_RE.search(lab_text) or re.search(r"ОКУД", lab_text, re.I)
                ):
                    new_top_rows.append(
                        [
                            _cell_from_frag(0, 0, lab),
                            _cell_from_frag(0, 1, right),
                        ]
                    )
                    has_okud = True

    # гарантируем колонку подписей слева
    n_cols = max(len(r) for r in grid)
    if n_cols == 1:
        grid = [
            [Cell(row=i, col=0, bbox=None, text="", words=[]), *row]
            for i, row in enumerate(grid)
        ]
        for row in grid:
            for j, c in enumerate(row):
                c.col = j
        n_cols = 2

    # дописать пустые левые подписи из ближайших form-label слов
    for r_idx, row in enumerate(grid):
        label = row[0] if row else None
        if label is not None and (label.text or "").strip():
            continue
        ys = [(c.bbox[1], c.bbox[3]) for c in row if c.bbox is not None]
        if not ys:
            continue
        r_top = min(t for t, _ in ys)
        r_bottom = max(b for _, b in ys)
        value_text = " ".join(
            (c.text or "").strip()
            for c in row[1:]
            if (c.text or "").strip()
        ).strip()
        # строка-заголовок «Коды» — без левой подписи
        if re.fullmatch(r"Коды", value_text, re.I):
            continue
        cand = [
            w
            for w in words
            if w["x1"] < t_x0 - 1
            and w["x0"] >= t_x0 - 130
            and w["bottom"] >= r_top - 3
            and w["top"] <= r_bottom + 3
        ]
        if not cand:
            continue
        cand.sort(key=lambda w: w["x0"])
        # склеить связный фрагмент справа-налево у края кодов
        frag = _cohesive_fragment(cand, "left", 1.5)
        text = _frag_text(frag)
        if not text or _looks_like_stray_table_text(text):
            continue
        if re.fullmatch(r"Коды", text, re.I):
            continue
        if value_text and text.strip().lower() == value_text.lower():
            continue
        # подпись должна пересекаться по Y с value-ячейкой, иначе это соседняя строка
        val_box = next((c.bbox for c in row[1:] if c.bbox is not None), None)
        if val_box is not None and frag:
            frag_yc = (min(w["top"] for w in frag) + max(w["bottom"] for w in frag)) / 2
            if not (val_box[1] - 2 <= frag_yc <= val_box[3] + 2):
                continue
        if not (_LABEL_RE.search(text) or text.lower().startswith("по ")):
            # заглавная сама по себе слишком широка (затягивает «Коды»)
            continue
        row[0] = _cell_from_frag(r_idx, 0, frag)

    if not new_top_rows:
        return grid

    # выровнять ширину дописанных строк под текущий grid
    width = max(len(r) for r in grid)
    aligned_top: list[list["Cell"]] = []
    for row in new_top_rows:
        if len(row) < width:
            row = row + [
                Cell(row=0, col=j, bbox=None, text="", words=[])
                for j in range(len(row), width)
            ]
        elif len(row) > width:
            # схлопнуть лишние value-ячейки в первую value
            head, vals = row[:1], row[1:]
            merged_val = vals[0]
            for extra in vals[1:]:
                if (extra.text or "").strip():
                    merged_val.text = (merged_val.text + " " + extra.text).strip()
                    merged_val.words.extend(extra.words)
            row = head + [merged_val] + [
                Cell(row=0, col=j, bbox=None, text="", words=[])
                for j in range(2, width)
            ]
        for j, c in enumerate(row):
            c.col = j
        aligned_top.append(row[:width])

    out = aligned_top + grid
    for i, row in enumerate(out):
        for c in row:
            c.row = i
    return out


def _apply_code_stack_value_colspans(grid: list[list["Cell"]]) -> list[list["Cell"]]:
    """
    В таблице кодов РСБУ (label | значение): одна ячейка кода накрывает
    все value-колонки (colspan), кроме строки даты (число | месяц | год).
    """
    if not grid or not _grid_is_vertical_code_stack(grid):
        return grid

    n_cols = max(len(row) for row in grid)
    if n_cols < 3:
        return grid
    value_width = n_cols - 1  # колонки справа от подписи

    new_grid: list[list["Cell"]] = []
    for row in grid:
        by_col = {c.col: c for c in row}
        value_cells = [
            by_col[c]
            for c in range(1, n_cols)
            if c in by_col and not getattr(by_col[c], "covered", False)
        ]
        filled = [c for c in value_cells if (c.text or "").strip()]
        # дата и прочие строки с несколькими значениями — без изменений
        if len(filled) != 1:
            new_grid.append(row)
            continue

        val = filled[0]
        val.colspan = value_width
        new_row = [by_col[0]] if 0 in by_col else []
        new_row.append(val)
        # перенумеровать col у оставшихся на всякий случай не трогаем —
        # render идёт по порядку ячеек в row
        new_grid.append(new_row)

    return new_grid


def process_table(page, table, *, doc_type=None):
    """Полная структурная обработка одной таблицы -> (grid, kinds).

    doc_type — опционально pdf_doc_types.DocType / str; после сборки
    применяются type-specific эвристики (для unknown — безопасный RSBU-fix
    только на финансовых grid).
    """
    grid = build_cells(page, table)
    grid = merge_phantom_columns(grid, find_phantom_boundaries(grid))
    fin_stmt = _grid_is_financial_statement(grid)
    if not fin_stmt:
        grid = merge_leading_text_columns(grid)
    grid = drop_empty_columns_grid(grid)
    kinds = classify_rows(grid)

    code_stack = _grid_is_vertical_code_stack(grid)
    fin_stmt = _grid_is_financial_statement(grid)

    if not code_stack:
        grid, kinds = merge_wrapped_rows(grid, kinds)
        grid, kinds = merge_label_rows_by_band(grid, kinds)
    if not fin_stmt:
        grid = merge_leading_text_columns(grid)
    grid, kinds = drop_empty_rows_grid(grid, kinds)
    grid = restore_colspan_by_bbox(grid)
    if not fin_stmt:
        grid = restore_label_rowspan_soft(grid, kinds)
    grid = restore_rowspan_by_bbox(grid)
    if not code_stack and not fin_stmt:
        grid = merge_leading_text_columns(grid)
    grid = drop_empty_columns_grid(grid)
    n_cols = max(len(r) for r in grid) if grid else 0
    if n_cols <= 5:
        grid = enrich_grid_with_side_labels(page, grid)
    n_before_aug = len(grid)
    grid = _augment_vertical_code_stack(page, grid)
    added = len(grid) - n_before_aug
    if added > 0:
        kinds = [HEADER] * added + list(kinds)
    elif added < 0:
        kinds = list(kinds)[: len(grid)]
    grid = _apply_code_stack_value_colspans(grid)
    grid = trim_spurious_empty_rowspan(grid)
    if len(kinds) != len(grid):
        kinds = classify_rows(grid)
    from pdf_doc_types import apply_type_heuristics
    grid, kinds = apply_type_heuristics(doc_type, page, grid, kinds)
    if kinds is not None and len(kinds) != len(grid):
        kinds = classify_rows(grid)
    return grid, kinds


# --- notebook cell 30 ---

_TABLE_CELL_STYLE = ' style="text-align: left; vertical-align: middle;"'

_BOLD_RE = re.compile(r"bold|black|heavy|semibold|demi|-bd[^a-z]|,bold", re.I)
_ITALIC_RE = re.compile(r"italic|oblique|-it[^a-z]|,italic", re.I)
_FAMILY_HINTS = (
    (re.compile(r"times|tnr|nimbusrom", re.I), '"Times New Roman", Times, serif'),
    (re.compile(r"arial|helvetica|swiss|liberationsans|dejavusans", re.I), "Arial, Helvetica, sans-serif"),
    (re.compile(r"courier|mono|consolas|liberationmono|dejavusansmono", re.I), '"Courier New", Courier, monospace'),
    (re.compile(r"calibri", re.I), "Calibri, Arial, sans-serif"),
    (re.compile(r"georgia", re.I), "Georgia, 'Times New Roman', serif"),
)


def _strip_subset_prefix(fontname: str) -> str:
    """Убирает subset-префикс шрифта (ABCDEF+Arial → Arial)."""
    return fontname.split("+", 1)[-1]


def _is_bold_font(fontname: str) -> bool:
    """True, если имя шрифта указывает на bold."""
    return bool(_BOLD_RE.search(fontname))


def _is_italic_font(fontname: str) -> bool:
    """True, если имя шрифта указывает на italic."""
    return bool(_ITALIC_RE.search(fontname))


def _font_family_css(fontname: str | None) -> str:
    """CSS font-family из имени PDF-шрифта."""
    raw = _strip_subset_prefix(fontname or "")
    for pattern, css_family in _FAMILY_HINTS:
        if pattern.search(raw):
            return css_family
    return '"Times New Roman", Times, serif'


def _word_run_key(w: dict) -> tuple:
    """Ключ группировки слов в run: шрифт + стиль."""
    fn = w.get("fontname") or ""
    size = round(float(w.get("size", 0)) * 2) / 2
    return (_font_family_css(fn), _is_bold_font(fn), _is_italic_font(fn), size)


def _run_style(w: dict) -> str:
    """Inline CSS для run слов (font-size, weight, style)."""
    fn = w.get("fontname") or ""
    size = float(w.get("size", 11) or 11)
    parts = [
        f"font-family: {_font_family_css(fn)}",
        f"font-size: {size:.1f}pt",
    ]
    if _is_bold_font(fn):
        parts.append("font-weight: 700")
    if _is_italic_font(fn):
        parts.append("font-style: italic")
    return "; ".join(parts)


def _group_word_runs(line: list[dict], gap_ratio: float = 1.5) -> list[list[dict]]:
    """Группирует слова строки в runs с одинаковым шрифтом."""
    if not line:
        return []
    runs: list[list[dict]] = [[line[0]]]
    for prev, cur in zip(line, line[1:]):
        same_font = _word_run_key(prev) == _word_run_key(cur)
        size = float(prev.get("size", 0)) or 10.0
        gap = cur["x0"] - prev["x1"]
        if same_font and gap <= gap_ratio * size:
            runs[-1].append(cur)
        else:
            runs.append([cur])
    return runs


def _group_words_into_lines_for_html(words: list[dict], y_tol: float = 3.0) -> list[list[dict]]:
    """Слова → строки по близости top для HTML."""
    if not words:
        return []
    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = [[words[0]]]
    for w in words[1:]:
        if abs(w["top"] - lines[-1][-1]["top"]) <= y_tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    return lines


def words_to_styled_html(
    words: list[dict],
    gap_ratio: float = 1.5,
    line_breaks: bool = False,
) -> str:
    """Слова pdfplumber -> HTML со span-ами: семейство, размер, жирность, курсив."""
    if not words:
        return ""

    lines = _group_words_into_lines_for_html(words)
    html_lines: list[str] = []

    for line in lines:
        parts: list[str] = []
        for run in _group_word_runs(line, gap_ratio):
            text = " ".join(w["text"] for w in run).strip()
            if not text:
                continue
            style = _run_style(run[0])
            parts.append(f'<span style="{style}">{html.escape(text)}</span>')
        if parts:
            html_lines.append(" ".join(parts))

    if not html_lines:
        return ""
    if line_breaks and len(html_lines) > 1:
        return "<br>\n".join(html_lines)
    return " ".join(html_lines)


def render_table_html(grid: list[list["Cell"]], kinds: list[str]) -> str:
    """Одна таблица -> семантический HTML с thead/tbody, colspan/rowspan, шрифты из PDF."""

    def render_row(row, tag: str) -> str:
        """Рендерит одну строку таблицы в HTML <tr>…</tr>."""
        n_cols = sum(
            getattr(c, "colspan", 1) or 1
            for c in row
            if not getattr(c, "covered", False)
        ) or len(row)
        cells = []
        col_idx = 0
        for c in row:
            if getattr(c, "covered", False):
                continue
            attrs = ""
            if getattr(c, "colspan", 1) > 1:
                attrs += f' colspan="{c.colspan}"'
            if getattr(c, "rowspan", 1) > 1:
                attrs += f' rowspan="{c.rowspan}"'
            cell_text = (c.text or "").strip()
            attrs += table_cell_style(cell_text, col_idx, n_cols)
            col_idx += getattr(c, "colspan", 1) or 1
            inner = words_to_styled_html(
                c.words,
                line_breaks="\n" in (c.text or ""),
            ) if c.words else html.escape(cell_text)
            cells.append(f"      <{tag}{attrs}>{inner}</{tag}>")
        return "    <tr>\n" + "\n".join(cells) + "\n    </tr>"

    rendered = [(k, render_row(row, "th" if k == HEADER else "td"))
                for row, k in zip(grid, kinds)]

    out = ["<table>"]
    i, n = 0, len(rendered)

    if rendered and rendered[0][0] == HEADER:
        out.append("  <thead>")
        while i < n and rendered[i][0] == HEADER:
            out.append(rendered[i][1])
            i += 1
        out.append("  </thead>")

    out.append("  <tbody>")
    while i < n:
        out.append(rendered[i][1])
        i += 1
    out.append("  </tbody>")

    out.append("</table>")
    return "\n".join(out)


# --- notebook cell 32 ---
# --- Восстановление структуры страницы: текст + таблицы ---

def word_key(w: dict) -> tuple:
    """Уникальный ключ слова на странице."""
    return (round(w["x0"], 1), round(w["top"], 1), round(w["x1"], 1), w["text"])


def _word_center_in_bbox(w: dict, bbox: tuple[float, float, float, float], tol: float = 2.0) -> bool:
    """True, если центр слова внутри bbox."""
    x0, top, x1, bottom = bbox
    cx = (w["x0"] + w["x1"]) / 2
    cy = (w["top"] + w["bottom"]) / 2
    return x0 - tol <= cx <= x1 + tol and top - tol <= cy <= bottom + tol


def mark_table_words_used(
    page,
    processed: list[tuple[list[list[Cell]], list[str], object]],
) -> set[tuple]:
    """
    Помечает слова из ячеек таблиц и все слова внутри bbox таблицы pdfplumber.
    """
    used: set[tuple] = set()

    for grid, _kinds, table in processed:
        for row in grid:
            for cell in row:
                for w in cell.words:
                    used.add(word_key(w))
        bbox = getattr(table, "bbox", None)
        if bbox is not None:
            for w in page.extract_words(x_tolerance=1, y_tolerance=1):
                if _word_center_in_bbox(w, bbox):
                    used.add(word_key(w))

    return used


# --- notebook cell 33 ---
def _group_words_into_lines(words: list[dict], y_tol: float = 3.0) -> list[list[dict]]:
    """Группирует слова в строки по близости top."""
    if not words:
        return []
    words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = [[words[0]]]
    for w in words[1:]:
        prev = lines[-1][-1]
        if abs(w["top"] - prev["top"]) <= y_tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    return lines


def _lines_to_cell(lines: list[list[dict]], block_idx: int) -> Cell:
    """Склеивает строки в один текстовый блок формата Cell."""
    words: list[dict] = []
    for line in lines:
        words.extend(line)
    words.sort(key=lambda w: (round(w["top"]), w["x0"]))
    text = words_to_cell_text(words)
    bbox = (
        min(w["x0"] for w in words),
        min(w["top"] for w in words),
        max(w["x1"] for w in words),
        max(w["bottom"] for w in words),
    )
    return Cell(row=block_idx, col=0, bbox=bbox, text=text, words=words)


def extract_free_text_blocks(
    page,
    used_keys: set[tuple],
    line_gap_ratio: float = 1.4,
    min_chars: int = 1,
) -> list[Cell]:
    """
    (б) Извлекает неиспользованные текстовые блоки в формате Cell с bbox.
    Сначала строки, затем абзацы по вертикальному зазору.
    """
    words = page.extract_words(x_tolerance=1, y_tolerance=1, extra_attrs=["fontname", "size"])
    free = [w for w in words if word_key(w) not in used_keys and w["text"].strip()]
    if not free:
        return []

    lines = _group_words_into_lines(free)
    blocks: list[Cell] = []
    current_lines: list[list[dict]] = [lines[0]]

    def _starts_new_block(prev_line: list[dict], next_line: list[dict], gap: float, size: float) -> bool:
        """True, если строка слов начинает новый текстовый блок."""
        if not next_line:
            return False
        next_text = next_line[0].get("text", "").strip()
        if not _text_starts_capital(next_text):
            return False
        prev_text = " ".join(w["text"] for w in prev_line).strip()
        if prev_text.endswith((".", ":", ";", "?", "!")):
            return True
        return gap > line_gap_ratio * size * 1.2

    for prev_line, next_line in zip(lines, lines[1:]):
        prev_h = max(w["bottom"] for w in prev_line) - min(w["top"] for w in prev_line)
        gap = min(w["top"] for w in next_line) - max(w["bottom"] for w in prev_line)
        size = float(prev_line[0].get("size", 0)) or prev_h or 10.0
        same_paragraph = gap <= line_gap_ratio * size and not _starts_new_block(prev_line, next_line, gap, size)

        if same_paragraph:
            current_lines.append(next_line)
        else:
            cell = _lines_to_cell(current_lines, len(blocks))
            if len(cell.text.strip()) >= min_chars:
                blocks.append(cell)
            current_lines = [next_line]

    cell = _lines_to_cell(current_lines, len(blocks))
    if len(cell.text.strip()) >= min_chars:
        blocks.append(cell)

    return blocks


# --- notebook cell 34 ---


@dataclass
class DocElement:
    kind: str          # "text" | "table"
    bbox: tuple[float, float, float, float]
    html: str
    text: str          # plain text (для text-блоков)
    words: list[dict] = field(default_factory=list)
    sort_top: float = 0.0
    sort_left: float = 0.0


def _dominant_font_size(words: list[dict]) -> float:
    """Доминирующий размер шрифта в блоке слов."""
    if not words:
        return 10.0
    sizes = [round(float(w.get("size", 10)), 1) for w in words]
    return Counter(sizes).most_common(1)[0][0]


def _text_alignment(block: Cell, page_width: float) -> str:
    """Выравнивание текстового блока по позиции на странице."""
    if block.bbox is None:
        return "left"
    x0, _top, x1, _bottom = block.bbox
    block_w = x1 - x0
    center = (x0 + x1) / 2
    page_center = page_width / 2
    if abs(center - page_center) < page_width * 0.08 and block_w > page_width * 0.4:
        return "center"
    if x0 > page_width * 0.55:
        return "right"
    return "left"


def render_text_block_html(block: Cell, page_width: float) -> str:
    """Рендерит TextBlock в HTML (p/h с inline-стилями)."""
    text = (block.text or "").strip()
    if not text and not block.words:
        return ""

    align = _text_alignment(block, page_width)
    has_lines = "\n" in text
    inner = words_to_styled_html(block.words, line_breaks=has_lines)
    if not inner:
        inner = html.escape(text).replace("\n", "<br>")

    size = _dominant_font_size(block.words)
    if size >= 16:
        tag = "h1"
    elif size >= 13:
        tag = "h2"
    elif size >= 11.5:
        tag = "h3"
    else:
        tag = "p"

    return f'<{tag} style="text-align: {align};">{inner}</{tag}>'


def _reading_order_key(el: DocElement) -> tuple:
    """Сортировка: сверху вниз, затем слева направо."""
    band = round(el.sort_top / 4) * 4
    return (band, round(el.sort_left))


def _merge_inline_text_elements(
    elements: list[DocElement],
    page_width: float,
    y_tol: float = 6.0,
) -> list[DocElement]:
    """Сливает текстовые блоки на одной строке, сохраняя шрифты из words."""

    if not elements:
        return elements

    def _is_codes_form_chip(text: str) -> bool:
        """True, если блок — «чипы» кодов формы (ОКУД и т.п.)."""
        t = (text or "").strip().lower().replace("\n", " ")
        return bool(
            re.fullmatch(r"коды", t)
            or re.match(r"форма\s+по\s+окуд", t)
            or re.fullmatch(r"\d{5,8}", t)
        )

    def _is_doc_titleish(text: str) -> bool:
        """True, если текст похож на заголовок документа."""
        return _looks_like_stray_table_text(text) or (
            len((text or "").strip()) >= 18
            and bool(re.search(r"(?i)отчет|баланс|пояснен", text or ""))
        )

    merged: list[DocElement] = []
    i = 0
    while i < len(elements):
        el = elements[i]
        if el.kind != "text":
            merged.append(el)
            i += 1
            continue
        # уже собранный HTML без words (prose_sections) — не пересобирать/не сливать
        if not el.words and el.html:
            merged.append(el)
            i += 1
            continue

        group = [el]
        j = i + 1
        while j < len(elements):
            nxt = elements[j]
            if nxt.kind != "text":
                break
            if not nxt.words and nxt.html:
                break
            if abs(nxt.sort_top - el.sort_top) > y_tol:
                break
            # не сливать заголовок документа с блоком «Коды» / ОКУД справа
            gap = nxt.sort_left - (el.bbox[2] if el.bbox else el.sort_left)
            if gap > page_width * 0.22:
                break
            if _is_codes_form_chip(nxt.text) and _is_doc_titleish(el.text):
                break
            if _is_codes_form_chip(el.text) and _is_doc_titleish(nxt.text):
                break
            group.append(nxt)
            j += 1

        if len(group) == 1:
            merged.append(el)
        else:
            group.sort(key=lambda e: e.sort_left)
            combined_words: list[dict] = []
            for g in group:
                combined_words.extend(g.words)
            combined_words.sort(key=lambda w: (round(w["top"]), w["x0"]))
            combined = " ".join(g.text.strip() for g in group if g.text.strip())
            x0 = min(g.bbox[0] for g in group)
            top = min(g.bbox[1] for g in group)
            x1 = max(g.bbox[2] for g in group)
            bottom = max(g.bbox[3] for g in group)
            block = Cell(row=0, col=0, bbox=(x0, top, x1, bottom), text=combined, words=combined_words)
            merged.append(DocElement(
                kind="text",
                bbox=(x0, top, x1, bottom),
                text=combined,
                words=combined_words,
                html=f'<div class="doc-section">{render_text_block_html(block, page_width)}</div>',
                sort_top=top,
                sort_left=x0,
            ))
        i = j if len(group) > 1 else i + 1

    return merged





def _grid_has_embedded_prose(grid: list[list["Cell"]]) -> bool:
    """True только если внутри таблицы есть настоящие абзацы (стр. 12 залог)."""
    return any(_row_is_prose(row) for row in grid)


def _grid_is_financial_statement(grid: list[list["Cell"]]) -> bool:
    """True, если grid похож на фин. отчётность (коды/суммы)."""

    codes = sum(
        1
        for row in grid
        for c in row
        if re.fullmatch(r"\d{4}", (c.text or "").strip())
    )
    return codes >= 4
def _row_combined_text(row: list["Cell"]) -> str:
    """Склеенный текст всех ячеек строки."""
    parts = [
        (c.text or "").strip()
        for c in row
        if not getattr(c, "covered", False) and (c.text or "").strip()
    ]
    return " ".join(parts)


def _row_is_company_data_row(row: list["Cell"]) -> bool:
    """True, если строка — данные по компании в фин. форме."""
    combined = _row_combined_text(row)
    if not re.search(r'(?:ООО|ЗАО|ПАО|АО|ИП)[\s«"]',  combined):
        return False
    return any(_looks_like_table_data_value(c.text) for c in row if not getattr(c, "covered", False))


def _row_is_section_title(row: list["Cell"]) -> bool:
    """Заголовок раздела вне табличной строки — только для явного prose split."""
    if _row_has_tabular_data_pattern(row):
        return False
    visible = [
        c for c in row
        if not getattr(c, "covered", False) and (c.text or "").strip()
    ]
    if any(_looks_like_table_data_value(c.text) for c in visible):
        return False
    if any(re.fullmatch(r"\d{4}", (c.text or "").strip()) for c in visible):
        return False
    combined = _row_combined_text(row)
    if len(combined) < 8 or len(combined) > 120:
        return False
    letters = [ch for ch in combined if ch.isalpha()]
    if len(letters) < 4:
        return False
    upper_ratio = sum(1 for ch in letters if ch.isupper()) / len(letters)
    if upper_ratio < 0.85:
        return False
    # «Х 123456» — не заголовок
    if re.fullmatch(r"[XХxх]\s+[\d\s().,-]+", combined):
        return False
    return True


def _looks_like_stray_table_text(text: str) -> bool:
    """Заголовок документа / период / абзац — не подпись колонки и не поле формы."""

    t = (text or "").strip()
    if not t:
        return False
    tl = t.lower().replace("\n", " ")
    if re.search(
        r"(?:бухгалтерск|отчет об|пояснен|актив|пассив|январь|феврал|март|"
        r"апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)",
        tl,
    ) and (len(t) >= 12 or re.search(r"\d{4}", t)):
        # короткие поля формы («Дата …») не режем
        if re.match(r"дата\s*\(", tl):
            return False
        if re.match(r"(?:форма\s+по|по\s+ок|инн|окпо|океи)", tl):
            return False
        return True
    if t.rstrip().endswith((".", "»")) and len(t) > 45:
        return True
    letters = [ch for ch in t if ch.isalpha()]
    if len(letters) >= 18:
        upper = sum(1 for ch in letters if ch.isupper()) / len(letters)
        if upper >= 0.85:
            return True
    return False


def _row_is_table_header_row(row: list["Cell"]) -> bool:
    """Строка заголовков колонок — не prose."""

    visible = [
        c for c in row
        if not getattr(c, "covered", False) and (c.text or "").strip()
    ]
    if len(visible) < 2:
        return False
    if any(_looks_like_table_data_value(c.text) for c in visible):
        return False
    if any(_looks_like_stray_table_text(c.text or "") for c in visible):
        # целиком «Отчет…» / период — не header band
        if sum(_looks_like_stray_table_text(c.text or "") for c in visible) >= max(1, len(visible) - 1):
            return False
    headers = sum(1 for c in visible if _looks_like_column_header(c.text))
    if headers >= 2:
        return True
    if headers >= 1 and len(visible) >= 3:
        return True

    # 2-ячейная подшапка СФ/УПД: «код» + «условное обозначение (национальное)»
    if headers >= 1 and len(visible) == 2:
        companion = next(
            (c for c in visible if not _looks_like_column_header(c.text)),
            None,
        )
        if companion is not None:
            ot = (companion.text or "").strip().lower().replace("\n", " ")
            if re.search(
                r"условн|обознач|национал|цифро|кратк|наименов|океи|код|"
                r"измер|количеств|ставк|акциз",
                ot,
            ):
                return True

    # широкая шапка счёта/оплаты: много коротких подписей колонок без чисел
    if len(visible) >= 4:
        headerish = 0
        for c in visible:
            t = (c.text or "").strip()
            if not t:
                continue
            if _looks_like_column_header(t):
                headerish += 1
                continue
            if t[0] in "№NПп" or (t[0].isupper() and len(t) <= 48):
                headerish += 1
                continue
            if len(t) <= 28 and not t.rstrip().endswith((".", "»")):
                headerish += 0.5
        if headerish >= max(3.0, len(visible) * 0.55):
            return True
    return False


def _row_is_prose(row: list["Cell"]) -> bool:
    """Строка grid — абзац/предложение, а не строка таблицы."""
    if _row_is_table_header_row(row):
        return False
    visible = [
        c for c in row
        if not getattr(c, "covered", False) and (c.text or "").strip()
    ]
    if len(visible) < 2:
        return False
    if _row_is_company_data_row(row):
        return False
    combined = _row_combined_text(row)
    # фрагменты многоуровневой шапки СФ/УПД/ТОРГ/КС — не prose
    if re.search(
        r"(?i)условн\w*\s*обознач|национальн|код\s*вида\s*товар|"
        r"наименован\w*\s*товар|единиц\w*\s*измер|сумма\s*налог|"
        r"стоимост\w*\s*товар|цифро[-\s]*в|кратк\w*\s*наименов|"
        r"без\s*налога|с\s*налогом|номер\s*по\s*поряд|"
        r"наименован\w*\s*работ|выполнено\s*работ",
        combined,
    ):
        return False
    text_cells = [
        c for c in visible
        if re.search(r"[A-Za-zА-Яа-яЁё]", c.text or "")
        and not _looks_like_table_data_value(c.text)
    ]
    numeric_data = [
        c for c in visible if _looks_like_table_data_value(c.text)
    ]

    def _ends_like_sentence(s: str) -> bool:
        s = s.rstrip()
        if not s:
            return False
        if s.endswith((".", ";", "»")):
            return True
        if s.endswith(")"):
            # «(национальное)» / «(объем)» — подписи колонок, не конец абзаца
            if re.search(r"\([^)]{0,48}\)$", s) and len(s) <= 90:
                return False
            return True
        return False

    if len(combined) >= 35 and len(text_cells) >= 2:
        if re.search(r"(тыс\.\s*руб|руб\.;|\)\s*,)", combined):
            if len(numeric_data) <= 4:
                return True
        if _ends_like_sentence(combined) and len(numeric_data) <= 3:
            return True
    if _row_has_tabular_data_pattern(row):
        return False
    if len(combined) < 40:
        return False
    ends_sentence = _ends_like_sentence(combined)
    if len(text_cells) >= 2 and len(numeric_data) <= 3:
        if ends_sentence or "тыс." in combined or len(combined) >= 90:
            return True
    # много текстовых ячеек без чисел — prose только при «абзацности»
    if len(text_cells) >= 3 and len(numeric_data) <= 2:
        avg_len = len(combined) / max(len(text_cells), 1)
        if ends_sentence or avg_len >= 36:
            return True
        return False
    return False



@dataclass
class GridBlock:
    kind: str          # "prose" | "table"
    rows: list[list["Cell"]]
    kinds: list[str]


def _bbox_area(bbox: tuple[float, float, float, float]) -> float:
    """Площадь bbox."""
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _bbox_intersection_area(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    """Площадь пересечения двух bbox."""
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _table_mostly_inside(inner, outer, threshold: float = 0.85) -> bool:
    """True, если таблица почти целиком внутри другой."""
    inner_bbox = getattr(inner, "bbox", None)
    outer_bbox = getattr(outer, "bbox", None)
    if inner_bbox is None or outer_bbox is None:
        return False
    area = _bbox_area(inner_bbox)
    if area <= 0:
        return False
    return _bbox_intersection_area(inner_bbox, outer_bbox) / area >= threshold


def _filter_nested_tables(tables: list) -> list:
    """Убирает маленькие таблицы, лежащие внутри bbox более крупной."""
    main, _nested = _extract_nested_code_tables(None, tables)
    return main


def _extract_nested_code_tables(
    page,
    tables: list,
) -> tuple[list, list[tuple[object, list[list["Cell"]], list[str]]]]:
    """Отделяет вложенные таблицы кодов (ОКПО/ИНН/ОКУД) от основных."""
    if len(tables) <= 1:
        return list(tables), []
    drop: set[int] = set()
    extracted: list[tuple[object, list[list["Cell"]], list[str]]] = []
    for i, outer in enumerate(tables):
        if i in drop:
            continue
        for j, inner in enumerate(tables):
            if i == j or j in drop:
                continue
            if not _table_mostly_inside(inner, outer):
                continue
            if page is not None:
                grid, kinds = process_table(page, inner)
                if _grid_is_vertical_code_stack(grid):
                    extracted.append((inner, grid, kinds))
                    drop.add(j)
                    continue
            drop.add(j)
    main = [t for idx, t in enumerate(tables) if idx not in drop]
    return main, extracted


def _strip_codes_from_intro_row(
    row: list["Cell"],
    code_bbox: tuple[float, float, float, float] | None = None,
) -> list["Cell"]:
    """Убирает из intro-строки значения, уже попавшие в таблицу «Коды»."""
    new_row: list["Cell"] = []
    for cell in row:
        if getattr(cell, "covered", False) or not (cell.text or "").strip():
            new_row.append(cell)
            continue
        words = list(cell.words)
        if code_bbox is not None:
            x_cut = code_bbox[0] - 3.0
            words = [w for w in words if w["x0"] < x_cut]
            date_ws = [w for w in words if re.search(r"Дата|месяц, год", w.get("text", ""))]
            if date_ws:
                band = date_ws[0]["top"]
                words = [w for w in words if abs(w["top"] - band) > 4]
        text = words_to_cell_text(words) if words else ""
        kept_lines: list[str] = []
        for line in text.split("\n"):
            ls = line.strip()
            if not ls or ls == "Коды":
                continue
            if re.fullmatch(r"[\d/\.\-]+", ls):
                continue
            if re.fullmatch(r"Дата \(число, месяц, год\)", ls):
                continue
            kept_lines.append(line)
        text = "\n".join(kept_lines).strip()
        new_row.append(Cell(
            row=cell.row,
            col=cell.col,
            bbox=cell.bbox,
            text=text,
            words=words,
            colspan=cell.colspan,
            rowspan=cell.rowspan,
            covered=cell.covered,
            is_placeholder=not text,
        ))
    return new_row


def _row_filled_col_count(row: list["Cell"]) -> int:
    """Число непустых ячеек в строке."""
    return sum(
        1 for c in row
        if not getattr(c, "covered", False) and (c.text or "").strip()
    )


def _row_is_form_intro_row(row: list["Cell"]) -> bool:
    """True, если строка — вводный текст формы, не данные."""
    visible = [
        c for c in row
        if not getattr(c, "covered", False) and (c.text or "").strip()
    ]
    if len(visible) != 1:
        return False
    text = (visible[0].text or "").strip()
    if len(text) < 80:
        return False
    markers = ("Пояснения", "Организация", "ОКПО", "ИНН", "ОКВЭД", "бухгалтерск")
    return any(marker in text for marker in markers)


def _row_section_title_fragment(row: list["Cell"]) -> str | None:
    """Фрагмент заголовка секции из строки (если есть)."""
    combined = _row_combined_text(row)
    m = re.search(r"(\d+\.\d+(?:\.\d+)?\.\s+[А-ЯA-ZЁ][^\n]{5,120})", combined)
    if m:
        return m.group(1).strip()
    return None


def _strip_section_title_from_row(row: list["Cell"]) -> list["Cell"]:
    """Убирает заголовок секции из ячеек строки."""
    title = _row_section_title_fragment(row)
    if not title:
        return row
    new_row: list["Cell"] = []
    stripped = False
    for cell in row:
        if stripped or getattr(cell, "covered", False) or not (cell.text or "").strip():
            new_row.append(cell)
            continue
        text = cell.text
        cleaned = re.sub(re.escape(title), "", text, count=1).strip()
        cleaned = re.sub(r"^\d+\.\d+(?:\.\d+)?\.\s+[^\n]+", "", cleaned, count=1).strip()
        if cleaned == (text or "").strip():
            new_row.append(cell)
            continue
        new_cell = Cell(
            row=cell.row,
            col=cell.col,
            bbox=cell.bbox,
            text=cleaned,
            words=[w for w in cell.words if cleaned and w.get("text") in cleaned.split()],
            colspan=cell.colspan,
            rowspan=cell.rowspan,
            covered=cell.covered,
            is_placeholder=not cleaned,
        )
        new_row.append(new_cell)
        stripped = True
    return new_row


_MASHED_HEADER_MARKERS = (
    "код", "строки", "период", "наименование", "на начало",
    "изменения за период", "на конец", "затраты за", "обесценение",
    "принято к учету", "основных", "стоимость",
)


def _row_has_line_code(row: list["Cell"]) -> bool:
    """True, если в строке есть код строки отчёта (3–4 цифры)."""
    return any(
        not getattr(c, "covered", False)
        and re.fullmatch(r"\d{4}", (c.text or "").strip())
        for c in row
    )


def _strip_mashed_header_from_transition_row(row: list["Cell"]) -> list["Cell"]:
    """Убирает фрагменты шапки второй формы из последней строки первой."""
    if not _row_has_line_code(row):
        return row
    new_row: list["Cell"] = []
    for cell in row:
        if getattr(cell, "covered", False) or not (cell.text or "").strip():
            new_row.append(cell)
            continue
        text_l = (cell.text or "").lower()
        hits = sum(1 for m in _MASHED_HEADER_MARKERS if m in text_l)
        if hits >= 3 and cell.col == 0:
            new_row.append(Cell(
                row=cell.row,
                col=cell.col,
                bbox=cell.bbox,
                text="",
                words=[],
                colspan=1,
                rowspan=1,
                covered=cell.covered,
                is_placeholder=True,
            ))
            continue
        new_row.append(cell)
    return new_row


def _prose_row_from_text(text: str, template: "Cell") -> list["Cell"]:
    """Строит prose HTML-секцию из текста строки."""
    return [Cell(
        row=0,
        col=0,
        bbox=template.bbox,
        text=text,
        words=[],
    )]


def _find_movement_table_split_row(grid: list[list["Cell"]]) -> int | None:
    """Строка начала второй tabular-формы после широкой movement-таблицы."""
    counts = [_row_filled_col_count(row) for row in grid]
    for i in range(3, len(grid)):
        prev_max = max(counts[max(0, i - 6): i] or [0])
        cur = counts[i]
        if prev_max < 10 or cur > 7:
            continue
        row_text = _row_combined_text(grid[i]).lower()
        if any(k in row_text for k in ("код", "строки", "период", "наименование", "на начало")):
            return i
    return None


def _split_financial_mega_grid(
    grid: list[list["Cell"]],
    kinds: list[str],
) -> list[GridBlock]:
    """Режет слишком крупный financial-grid на отдельные таблицы."""
    if not grid:
        return []

    blocks: list[GridBlock] = []
    start = 0
    if _row_is_form_intro_row(grid[0]):
        blocks.append(GridBlock("prose", [grid[0]], [kinds[0]]))
        start = 1

    tail = grid[start:]
    tail_kinds = kinds[start:]
    if not tail:
        return blocks

    split_at = _find_movement_table_split_row(tail)
    if split_at is None:
        blocks.append(GridBlock("table", tail, tail_kinds))
        return blocks

    first_rows = tail[:split_at]
    first_kinds = tail_kinds[:split_at]
    second_rows = tail[split_at:]
    second_kinds = tail_kinds[split_at:]

    section_title_block: GridBlock | None = None
    if first_rows:
        title = _row_section_title_fragment(first_rows[-1])
        if title:
            template = next(
                (c for c in first_rows[-1] if not c.covered and (c.text or "").strip()),
                first_rows[-1][0],
            )
            cleaned_last = _strip_mashed_header_from_transition_row(
                _strip_section_title_from_row(first_rows[-1])
            )
            first_rows = first_rows[:-1] + [cleaned_last]
            section_title_block = GridBlock(
                "prose",
                [_prose_row_from_text(title, template)],
                [DATA],
            )
        blocks.append(GridBlock("table", first_rows, first_kinds))

    if section_title_block:
        blocks.append(section_title_block)

    if second_rows:
        second_rows = drop_empty_columns_grid(second_rows)
        blocks.append(GridBlock("table", second_rows, second_kinds))
    return blocks


def split_mixed_grid(
    grid: list[list["Cell"]],
    kinds: list[str],
) -> list[GridBlock]:
    """Делит mega-table на prose-блоки и табличные фрагменты."""
    if not grid:
        return []

    if _grid_is_financial_statement(grid):
        return _split_financial_mega_grid(grid, kinds)

    if not _grid_has_embedded_prose(grid):
        return [GridBlock("table", grid, kinds)]

    blocks: list[GridBlock] = []
    i = 0
    while i < len(grid):
        row = grid[i]
        if _row_is_prose(row):
            prose_rows = [row]
            j = i + 1
            while j < len(grid) and _row_is_prose(grid[j]):
                prose_rows.append(grid[j])
                j += 1
            blocks.append(GridBlock("prose", prose_rows, [DATA] * len(prose_rows)))
            i = j
            continue

        table_rows = [row]
        table_kinds = [kinds[i]]
        j = i + 1
        while j < len(grid) and not _row_is_prose(grid[j]):
            table_rows.append(grid[j])
            table_kinds.append(kinds[j])
            j += 1

        filled = sum(1 for r in table_rows for c in r if (c.text or "").strip())
        if filled >= 2:
            blocks.append(GridBlock("table", table_rows, table_kinds))
        i = j

    return blocks


from page_suitability import (
    PageSuitability,
    SuitabilityStats,
    assess_page_suitability,
    document_has_broken_fonts,
    format_suitability_report,
    merge_page_suitability,
    page_has_broken_fonts,
    rejected_page_notice_html,
    should_route_unmarked_complex_spans,
    suitability_unmarked_complex_spans,
)
from pdf_doc_types import DocType, detect_doc_type

POORLY_MARKED_TEXT_MESSAGE = "Документ содержит плохо размеченный текст"


def poorly_marked_text_notice_html() -> str:
    """HTML-заметка о плохо размеченном тексте / сломанных шрифтах (на весь документ)."""
    return (
        f'<div class="broken-font-warning" role="alert">'
        f"{html.escape(POORLY_MARKED_TEXT_MESSAGE)}"
        f"</div>"
    )


def build_page_section(
    page,
    page_num: int = 1,
    pdf_path: str | None = None,
    *,
    skip_unsuitable: bool = True,
    doc_type_fallback=None,
) -> tuple[str, PageSuitability]:
    """
    Собирает HTML <section> одной страницы.

    Pre-check: битые шрифты / image-only скан.
    Детект типа документа → type-specific эвристики / политика роутинга.
    Затем всегда конвертация (с векторизацией линий при необходимости).
    Post-check: если линии брались с растра и process_table сделал много
    colspan/rowspan — страница роутится (unmarked_table_lines).
    Для РСБУ крупные таблицы без сложных span не роутятся.
    """
    detected = detect_doc_type(
        page, pdf_path=pdf_path, fallback=doc_type_fallback
    )
    doc_type = detected.doc_type if detected.known else DocType.UNKNOWN
    # слабый fallback всё равно прокидываем в эвристики/роутинг
    if not detected.known and detected.doc_type != DocType.UNKNOWN:
        doc_type = detected.doc_type

    suitability = assess_page_suitability(
        page, page_num=page_num, pdf_path=pdf_path
    )
    type_attr = f' data-doc-type="{html.escape(doc_type.value)}"'
    attrs = type_attr
    if skip_unsuitable and not suitability.suitable:
        body = rejected_page_notice_html(suitability)
        attrs = (
            f'{type_attr} data-rejected="true" '
            f'data-reasons="{html.escape(suitability.reason_codes)}"'
        )
    else:
        body, vectorized, grids = build_page_body_with_meta(
            page,
            page_num=page_num,
            pdf_path=pdf_path,
            doc_type=doc_type,
        )
        if skip_unsuitable and should_route_unmarked_complex_spans(
            raster_lines_vectorized=vectorized,
            grids=grids,
            doc_type=doc_type.value,
        ):
            suitability = merge_page_suitability(
                suitability,
                suitability_unmarked_complex_spans(page_num),
            )
            body = rejected_page_notice_html(suitability)
            attrs = (
                f'{type_attr} data-rejected="true" '
                f'data-reasons="{html.escape(suitability.reason_codes)}"'
            )
    section = (
        f'<section class="page" data-page="{page_num}"{attrs}>\n'
        f"{body}\n</section>"
    )
    # сохраняем тип на объекте suitability для future (не ломает dataclass)
    try:
        suitability.doc_type = doc_type.value  # type: ignore[attr-defined]
    except Exception:
        pass
    return section, suitability


def finalize_document_html(
    pdf,
    page_sections: list[str],
    title: str,
    *,
    source_name: str | None = None,
    suitability_stats: SuitabilityStats | None = None,
) -> str:
    """
    Собирает HTML документа.

    При битых шрифтах на уровне PDF — предупреждение в консоль и баннер в HTML.
    Если передан suitability_stats с отсевом — краткая строка в консоль по файлу.
    """
    parts = list(page_sections)
    label = source_name or title
    if document_has_broken_fonts(pdf):
        print(f"{label}: {POORLY_MARKED_TEXT_MESSAGE}", flush=True)
        parts.insert(0, poorly_marked_text_notice_html())
    if suitability_stats is not None and suitability_stats.rejected_pages:
        # краткий per-file hint (полный отчёт печатает export)
        file_rej = [
            (p, reasons)
            for name, p, reasons in suitability_stats.rejected_details
            if name == label
        ]
        if file_rej:
            print(
                f"{label}: отсеяно {len(file_rej)} стр. "
                f"({', '.join(sorted({r for _, rs in file_rej for r in rs}))})",
                flush=True,
            )
    return wrap_html_document("\n".join(parts), title=title)


def wrap_html_document(body: str, title: str = "Document") -> str:
    """Оборачивает body в полный HTML-документ с CSS."""
    css = DOCUMENT_CSS
    return (
        "<!DOCTYPE html>\n<html lang=\"ru\">\n<head>\n"
        f"<meta charset=\"utf-8\"><title>{html.escape(title)}</title>\n"
        f"<style>{css}</style>\n</head>\n<body>\n{body}\n</body>\n</html>"
    )


def build_page_body(
    page,
    page_num: int = 1,
    pdf_path: str | None = None,
) -> str:
    """
    HTML body одной страницы: текстовые блоки + таблицы в порядке чтения.
    """
    html_body, _vectorized, _grids = build_page_body_with_meta(
        page, page_num=page_num, pdf_path=pdf_path
    )
    return html_body


def build_page_body_with_meta(
    page,
    page_num: int = 1,
    pdf_path: str | None = None,
    *,
    doc_type=None,
) -> tuple[str, bool, list]:
    """
    Как build_page_body, плюс meta для роутинга:
    (html, raster_lines_vectorized, non-prose table grids).
    Векторизация линий сохраняется.
    doc_type прокидывается в process_table (type-specific эвристики).
    """
    if pdf_path:
        from pdf_line_vectorize import vectorized_page_session

        with vectorized_page_session(pdf_path, page_num) as session:
            html_body, grids = _build_page_body_impl(
                session.page,
                session.page_num,
                session.pdf_path,
                doc_type=doc_type,
            )
            return html_body, bool(session.vectorized), grids
    html_body, grids = _build_page_body_impl(
        page, page_num, pdf_path, doc_type=doc_type
    )
    return html_body, False, grids


def _build_page_body_impl(
    page,
    page_num: int = 1,
    pdf_path: str | None = None,
    *,
    doc_type=None,
) -> tuple[str, list]:
    """Внутренняя сборка body (после опциональной векторизации линий)."""
    tables_raw = find_tables_smart(page, pdf_path=pdf_path, page_num=page_num)
    tables_raw, nested_code_tables = _extract_nested_code_tables(page, tables_raw)
    code_bbox = nested_code_tables[0][0].bbox if nested_code_tables else None
    if not tables_raw:
        body = page_text_fallback_html(page, render_text_block_html) or ""
        from pdf_doc_types import enrich_page_html_for_doc_type
        body = enrich_page_html_for_doc_type(doc_type, page, body)
        return body, []

    processed: list[tuple[list[list[Cell]], list[str], object, bool]] = []
    prose_sections: list[tuple[tuple, str]] = []
    for table in tables_raw:
        if table_looks_like_prose(page, table):
            grid = build_cells(page, table)
            kinds = classify_rows(grid)
            as_prose = True
        else:
            grid, kinds = process_table(page, table, doc_type=doc_type)
            if code_bbox and grid and _row_is_form_intro_row(grid[0]):
                grid[0] = _strip_codes_from_intro_row(grid[0], code_bbox)
            as_prose = is_prose_table(grid)
        if as_prose:
            bbox = table.bbox
            for section_html in prose_grid_to_sections(
                grid, render_text_block_html, page.width
            ):
                prose_sections.append(((bbox[1], bbox[0]), section_html))
        processed.append((grid, kinds, table, as_prose))

    used_keys = mark_table_words_used(
        page,
        [(g, k, t) for g, k, t, _ in processed]
        + [(g, k, t) for t, g, k in nested_code_tables],
    )
    free_blocks = extract_free_text_blocks(page, used_keys)

    elements: list[DocElement] = []

    for grid, kinds, table, as_prose in processed:
        if as_prose:
            continue
        blocks = split_mixed_grid(grid, kinds)
        if len(blocks) <= 1 and blocks and blocks[0].kind == "table":
            bbox = table.bbox
            table_html = render_table_html(grid, kinds)
            elements.append(DocElement(
                kind="table",
                bbox=bbox,
                html=f'<div class="doc-section">{table_html}</div>',
                text="",
                sort_top=bbox[1],
                sort_left=bbox[0],
            ))
            continue

        for block in blocks:
            bboxes = [c.bbox for row in block.rows for c in row if c.bbox is not None]
            if not bboxes:
                continue
            block_bbox = (
                min(b[0] for b in bboxes),
                min(b[1] for b in bboxes),
                max(b[2] for b in bboxes),
                max(b[3] for b in bboxes),
            )
            if block.kind == "prose":
                for section_html in prose_grid_to_sections(
                    block.rows, render_text_block_html, page.width
                ):
                    elements.append(DocElement(
                        kind="text",
                        bbox=block_bbox,
                        html=section_html,
                        text="",
                        sort_top=block_bbox[1],
                        sort_left=block_bbox[0],
                    ))
            else:
                table_html = render_table_html(block.rows, block.kinds)
                elements.append(DocElement(
                    kind="table",
                    bbox=block_bbox,
                    html=f'<div class="doc-section">{table_html}</div>',
                    text="",
                    sort_top=block_bbox[1],
                    sort_left=block_bbox[0],
                ))

    for table, grid, kinds in nested_code_tables:
        bbox = table.bbox
        table_html = render_table_html(grid, kinds)
        elements.append(DocElement(
            kind="table",
            bbox=bbox,
            html=f'<div class="doc-section">{table_html}</div>',
            text="",
            sort_top=bbox[1],
            sort_left=bbox[0],
        ))

    for _key, section_html in sorted(prose_sections, key=lambda x: x[0]):
        elements.append(DocElement(
            kind="text",
            bbox=(0, _key[0], page.width, _key[0]),
            html=section_html,
            text="",
            sort_top=_key[0],
            sort_left=_key[1],
        ))

    for block in free_blocks:
        if block.bbox is None:
            continue
        block_html = render_text_block_html(block, page.width)
        if not block_html:
            continue
        elements.append(DocElement(
            kind="text",
            bbox=block.bbox,
            html=f'<div class="doc-section">{block_html}</div>',
            text=block.text.strip(),
            words=list(block.words),
            sort_top=block.bbox[1],
            sort_left=block.bbox[0],
        ))

    elements.sort(key=_reading_order_key)
    elements = _merge_inline_text_elements(elements, page.width)
    body = "\n".join(el.html for el in elements)
    if not body.strip() or page_body_needs_prose_fallback(page, body):
        body = page_text_fallback_html(page, render_text_block_html)
    from pdf_doc_types import enrich_page_html_for_doc_type
    body = enrich_page_html_for_doc_type(doc_type, page, body)
    # grids для post-check роутинга: только не-prose таблицы после process_table
    route_grids = [g for g, _k, _t, as_prose in processed if not as_prose]
    return body, route_grids


def build_page_html(
    page,
    page_num: int = 1,
    title: str = "Document",
    pdf_path: str | None = None,
) -> str:
    """Полный HTML-документ одной страницы."""
    body = build_page_body(page, page_num=page_num, pdf_path=pdf_path)
    return wrap_html_document(body, title=title)


# --- notebook cell 35 ---


from pdf_table_engine import _suppress_scan_noise


def export_samples_to_html(
    samples_dir: str | Path = "samples",
    output_dir: str | Path = "samples_html",
    *,
    skip_unsuitable: bool = True,
) -> dict:
    """
    PDF из samples_dir -> HTML в output_dir (полный пайплайн на каждой странице).

    Страницы, не пригодные для smart (битые шрифты, неразмеченные линии таблиц,
    image-only скан), при skip_unsuitable=True не конвертируются — в HTML
    ставится заглушка; в конце печатается сводка отсева.
    """
    _suppress_scan_noise()

    samples_dir = Path(samples_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(samples_dir.glob("*.pdf"))
    total_pages = 0
    total_seconds = 0.0
    exported: list[str] = []
    suitability_stats = SuitabilityStats()

    for pdf_path in pdfs:
        doc_type_fallback = None
        file_t0 = time.perf_counter()
        with pdfplumber.open(pdf_path) as pdf:
            page_sections: list[str] = []
            for pnum, page in enumerate(pdf.pages, start=1):
                page_t0 = time.perf_counter()
                section, suitability = build_page_section(
                    page,
                    page_num=pnum,
                    pdf_path=str(pdf_path),
                    skip_unsuitable=skip_unsuitable,
                    doc_type_fallback=doc_type_fallback,
                )
                doc_type_fallback = getattr(suitability, "doc_type", doc_type_fallback)
                suitability_stats.record(pdf_path.name, suitability)
                page_elapsed = time.perf_counter() - page_t0
                total_pages += 1
                total_seconds += page_elapsed
                page_sections.append(section)

            doc_html = finalize_document_html(
                pdf,
                page_sections,
                title=pdf_path.stem,
                source_name=pdf_path.name,
                suitability_stats=suitability_stats,
            )
            n_pages = len(pdf.pages)

        out_path = output_dir / f"{pdf_path.stem}.html"
        out_path.write_text(doc_html, encoding="utf-8")
        exported.append(out_path.name)

        file_elapsed = time.perf_counter() - file_t0
        _acc, file_rej = suitability_stats.per_file.get(pdf_path.name, (0, 0))
        rej_note = f", отсеяно {file_rej}" if file_rej else ""
        print(
            f"{pdf_path.name} -> {out_path.name} "
            f"({n_pages} стр.{rej_note}, {file_elapsed:.2f} с)",
            flush=True,
        )

    avg_seconds_per_page = total_seconds / total_pages if total_pages else 0.0
    report = format_suitability_report(suitability_stats)
    print("\n" + report, flush=True)

    return {
        "pdf_count": len(pdfs),
        "total_pages": total_pages,
        "total_seconds": round(total_seconds, 4),
        "avg_seconds_per_page": round(avg_seconds_per_page, 4),
        "exported_files": exported,
        "output_dir": str(output_dir),
        "accepted_pages": suitability_stats.accepted_pages,
        "rejected_pages": suitability_stats.rejected_pages,
        "rejection_rate": round(suitability_stats.rejection_rate, 4),
        "rejection_reasons": dict(suitability_stats.reason_counts),
        "suitability_report": report,
    }


# --- notebook cell 39 ---
# --- Векторизация растровых линий таблиц (сканы / фото-PDF) ---
# Копирует страницу (текст сохраняется) + рисует vector lines поверх.
# Текст маскируется только на рабочей картинке для OpenCV.

from pdf_line_vectorize import (
    detect_table_line_segments,
    mask_text_regions,
    merge_and_snap_segments,
    page_needs_line_vectorization,
    vectorized_page_session,
    iter_text_span_bboxes_pt,
)
from pdf_paddle_detection import render_page_bgr
from pdf_table_engine import find_tables_smart


def debug_line_vectorization(
    pdf_path: str | Path,
    page_num: int = 1,
    *,
    save_overlay: str | Path | None = None,
) -> dict:
    """
    Сравнить find_tables / chars / lines до и после векторизации на одной странице.

    save_overlay — путь к PNG с наложенными линиями (опционально).
    """
    import fitz

    pdf_path = Path(pdf_path)
    with pdfplumber.open(str(pdf_path)) as pdf:
        page = pdf.pages[page_num - 1]
        needs = page_needs_line_vectorization(page, pdf_path, page_num)
        before_lines = len(page.lines)
        before_chars = len(page.chars)
        before_tables = len(find_tables_smart(page, pdf_path=str(pdf_path), page_num=page_num))

    with vectorized_page_session(pdf_path, page_num) as session:
        after_lines = len(session.page.lines)
        after_chars = len(session.page.chars)
        after_tables = len(
            find_tables_smart(
                session.page,
                pdf_path=session.pdf_path,
                page_num=session.page_num,
            )
        )
        vectorized = session.vectorized

    overlay_path = None
    if save_overlay is not None:
        import cv2

        img_bgr, scale = render_page_bgr(pdf_path, page_num)
        doc = fitz.open(str(pdf_path))
        try:
            text_bboxes = iter_text_span_bboxes_pt(doc[page_num - 1])
        finally:
            doc.close()
        work = mask_text_regions(img_bgr, text_bboxes, scale=scale)
        h, w = work.shape[:2]
        segs = merge_and_snap_segments(
            detect_table_line_segments(work),
            float(w),
            float(h),
            snap_tol=2.0,
        )
        overlay = img_bgr.copy()
        for x0, y0, x1, y1 in segs:
            cv2.line(overlay, (int(x0), int(y0)), (int(x1), int(y1)), (0, 0, 255), 1)
        overlay_path = str(save_overlay)
        cv2.imwrite(overlay_path, overlay)

    return {
        "pdf": pdf_path.name,
        "page": page_num,
        "needs_vectorization": needs,
        "vectorized": vectorized,
        "chars_before": before_chars,
        "chars_after": after_chars,
        "lines_before": before_lines,
        "lines_after": after_lines,
        "tables_before": before_tables,
        "tables_after": after_tables,
        "overlay": overlay_path,
    }

# Пример:
# debug_line_vectorization("парсинг pdf данные/1655388160-6.pdf", page_num=1, save_overlay="debug_lines.png")


# --- notebook cell 40 ---


from pdf_table_engine import _suppress_scan_noise


def regenerate_sample_html(
    filename: str,
    samples_dir: str | Path = "samples",
    output_dir: str | Path = "samples_html",
) -> dict:
    """
    Перегенерировать HTML для одного PDF из samples/.

    filename — имя файла с расширением или без (например «RSBU_12m_2025» или «RSBU_12m_2025.pdf»).
    Возвращает словарь с параметрами генерации.
    """
    _suppress_scan_noise()

    samples_dir = Path(samples_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    name = Path(filename).name
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"

    pdf_path = samples_dir / name
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF не найден: {pdf_path}")

    file_t0 = time.perf_counter()
    page_times: list[float] = []
    suitability_stats = SuitabilityStats()

    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        page_sections: list[str] = []

        for pnum, page in enumerate(pdf.pages, start=1):
            page_t0 = time.perf_counter()
            section, suitability = build_page_section(
                page,
                page_num=pnum,
                pdf_path=str(pdf_path),
                skip_unsuitable=True,
            )
            suitability_stats.record(pdf_path.name, suitability)
            page_times.append(time.perf_counter() - page_t0)
            page_sections.append(section)

        doc_html = finalize_document_html(
            pdf,
            page_sections,
            title=pdf_path.stem,
            source_name=pdf_path.name,
            suitability_stats=suitability_stats,
        )

    out_path = output_dir / f"{pdf_path.stem}.html"
    out_path.write_text(doc_html, encoding="utf-8")

    total_seconds = sum(page_times)
    file_seconds = time.perf_counter() - file_t0
    avg_seconds_per_page = total_seconds / page_count if page_count else 0.0

    stats = {
        "pdf_name": pdf_path.name,
        "html_name": out_path.name,
        "pdf_path": str(pdf_path.resolve()),
        "html_path": str(out_path.resolve()),
        "page_count": page_count,
        "total_seconds": round(total_seconds, 4),
        "file_seconds": round(file_seconds, 4),
        "avg_seconds_per_page": round(avg_seconds_per_page, 4),
        "page_seconds": [round(t, 4) for t in page_times],
        "accepted_pages": suitability_stats.accepted_pages,
        "rejected_pages": suitability_stats.rejected_pages,
        "rejection_reasons": dict(suitability_stats.reason_counts),
        "suitability_report": format_suitability_report(
            suitability_stats, title=f"Отсев: {pdf_path.name}"
        ),
    }

    print(f"PDF:  {stats['pdf_name']}")
    print(f"HTML: {stats['html_path']}")
    print(f"Страниц: {stats['page_count']}")
    print(
        f"Принято: {stats['accepted_pages']}, "
        f"отсеяно: {stats['rejected_pages']}"
    )
    print(f"Время (только страницы): {stats['total_seconds']:.4f} с")
    print(f"Время (файл целиком, с записью): {stats['file_seconds']:.4f} с")
    print(f"Среднее на страницу: {stats['avg_seconds_per_page']:.4f} с/стр.")
    if stats["rejected_pages"]:
        print(stats["suitability_report"])

    return stats


# Пример:
# regenerate_sample_html("RSBU_12m_2025")


# --- notebook cell 43 ---
# Пакетная конвертация: v2/ -> v2_html/ (с сохранением структуры подпапок)



from pdf_table_engine import _suppress_scan_noise


def export_tree_to_html(
    samples_dir: str | Path = "v2",
    output_dir: str | Path = "v2_html",
    *,
    skip_unsuitable: bool = True,
) -> dict:
    """
    Рекурсивно: PDF из samples_dir -> HTML в output_dir
    с тем же относительным путём (подпапки сохраняются).
    """
    _suppress_scan_noise()

    samples_dir = Path(samples_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    pdfs = sorted(p for p in samples_dir.rglob("*.pdf") if p.is_file())
    total_pages = 0
    total_seconds = 0.0
    exported: list[str] = []
    suitability_stats = SuitabilityStats()

    for pdf_path in pdfs:
        doc_type_fallback = None
        rel = pdf_path.relative_to(samples_dir)
        out_path = output_dir / rel.with_suffix(".html")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        file_t0 = time.perf_counter()
        with pdfplumber.open(pdf_path) as pdf:
            page_sections: list[str] = []
            for pnum, page in enumerate(pdf.pages, start=1):
                page_t0 = time.perf_counter()
                section, suitability = build_page_section(
                    page,
                    page_num=pnum,
                    pdf_path=str(pdf_path),
                    skip_unsuitable=skip_unsuitable,
                    doc_type_fallback=doc_type_fallback,
                )
                doc_type_fallback = getattr(suitability, "doc_type", doc_type_fallback)
                suitability_stats.record(str(rel), suitability)
                page_elapsed = time.perf_counter() - page_t0
                total_pages += 1
                total_seconds += page_elapsed
                page_sections.append(section)

            doc_html = finalize_document_html(
                pdf,
                page_sections,
                title=pdf_path.stem,
                source_name=str(rel),
                suitability_stats=suitability_stats,
            )
            n_pages = len(pdf.pages)

        out_path.write_text(doc_html, encoding="utf-8")
        exported.append(str(out_path.relative_to(output_dir)))

        file_elapsed = time.perf_counter() - file_t0
        _acc, file_rej = suitability_stats.per_file.get(str(rel), (0, 0))
        rej_note = f", отсеяно {file_rej}" if file_rej else ""
        print(
            f"{rel} -> {out_path.relative_to(output_dir)} "
            f"({n_pages} стр.{rej_note}, {file_elapsed:.2f} с)",
            flush=True,
        )

    avg_seconds_per_page = total_seconds / total_pages if total_pages else 0.0
    report = format_suitability_report(suitability_stats)
    print("\n" + report, flush=True)

    return {
        "pdf_count": len(pdfs),
        "total_pages": total_pages,
        "total_seconds": round(total_seconds, 4),
        "avg_seconds_per_page": round(avg_seconds_per_page, 4),
        "exported_files": exported,
        "output_dir": str(output_dir),
        "accepted_pages": suitability_stats.accepted_pages,
        "rejected_pages": suitability_stats.rejected_pages,
        "rejection_rate": round(suitability_stats.rejection_rate, 4),
        "rejection_reasons": dict(suitability_stats.reason_counts),
        "suitability_report": report,
    }


# --- detection helpers (shared) ---

def process_table_detected(page, table, *, side_labels_in_bbox: bool = True):
    """Как process_table; enrich_grid пропускается если подписи уже в crop."""
    grid = build_cells(page, table)
    if not grid:
        return [], []
    grid = merge_phantom_columns(grid, find_phantom_boundaries(grid))
    grid = drop_empty_columns_grid(grid)
    if not grid:
        return [], []
    kinds = classify_rows(grid)
    grid, kinds = merge_wrapped_rows(grid, kinds)
    grid, kinds = merge_label_rows_by_band(grid, kinds)
    grid, kinds = drop_empty_rows_grid(grid, kinds)
    if not grid:
        return [], []
    grid = restore_rowspan_by_bbox(grid)
    grid = restore_colspan_by_bbox(grid)
    grid = restore_label_rowspan_soft(grid, kinds)
    if not side_labels_in_bbox:
        n_cols = max(len(r) for r in grid) if grid else 0
        if n_cols <= 5:
            grid = enrich_grid_with_side_labels(page, grid)
    grid = trim_spurious_empty_rowspan(grid)
    return grid, kinds
