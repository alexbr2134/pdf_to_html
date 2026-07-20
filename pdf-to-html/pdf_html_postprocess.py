"""General HTML post-processing for PDF page reconstruction."""

from __future__ import annotations

import html
import re
from typing import Any, Callable

_NON_NUMERIC_MARKERS = frozenset({"-", "—", "Х", "X", "x", "х"})


def looks_numeric(text: str) -> bool:
    t = text.strip()
    if not t or t in _NON_NUMERIC_MARKERS:
        return False
    for ch in (" ", "\u00a0", "\u202f", "(", ")"):
        t = t.replace(ch, "")
    t = t.replace(",", ".")
    try:
        float(t)
        return True
    except ValueError:
        return False


def words_to_cell_text(words: list[dict], y_tol: float = 3.0) -> str:
    """Склеивает слова ячейки: строки через \\n, слова в строке через пробел."""
    if not words:
        return ""
    ordered = sorted(words, key=lambda w: (round(w["top"]), w["x0"]))
    lines: list[list[dict]] = [[ordered[0]]]
    for w in ordered[1:]:
        if abs(w["top"] - lines[-1][-1]["top"]) <= y_tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    return "\n".join(" ".join(w["text"] for w in line) for line in lines).strip()


def cell_text_align(text: str, col_idx: int, n_cols: int) -> str:
    """Выравнивание ячейки: подписи слева, числа справа."""
    t = (text or "").strip()
    if not t:
        return "left"
    if col_idx == 0 and n_cols > 2:
        return "left"
    if looks_numeric(t):
        return "right"
    if col_idx >= max(1, n_cols - 2) and re.search(r"\d", t):
        return "right"
    return "left"


def table_cell_style(text: str, col_idx: int, n_cols: int) -> str:
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
            if looks_numeric(cell.text):
                numeric += 1

    if filled == 0:
        return False

    numeric_ratio = numeric / filled
    one_blob_ratio = rows_one_blob / n_rows

    if n_cols <= 2 and one_blob_ratio >= 0.65 and numeric_ratio < 0.25:
        return True

    if one_blob_ratio >= 0.8 and numeric_ratio < 0.12 and n_rows >= 3:
        return True

    # Оглавление / списки: много колонок в сетке, но в каждой строке один смысловой блок
    if one_blob_ratio >= 0.7 and numeric_ratio < 0.18 and n_rows >= 4:
        return True

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
    Prose-страница: таблиц нет или псевдо-таблица поглотила слова,
    а HTML содержит лишь малую долю текста страницы.
    """
    raw = (page.extract_text() or "").strip()
    if len(raw) < min_raw_chars:
        return False
    plain = plain_text_from_html(body)
    if not plain:
        return True
    return len(plain) / len(raw) < min_ratio


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
"""
