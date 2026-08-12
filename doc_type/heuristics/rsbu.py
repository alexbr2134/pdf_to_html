"""РСБУ: починка сползания раздела / строки показателя."""

from __future__ import annotations

import re
from copy import copy
from typing import Any

from doc_type.config import DEFAULT_THRESHOLDS
from doc_type.constants import _CODE_RE, _SECTION_ONLY_RE, _SECTION_SPLIT_RE
from doc_type.heuristics.cells import cell_text as _cell_text



def _rsbu_find_code_col(grid: list[list[Any]]) -> int | None:
    """Колонка с 4-значными кодами строк баланса."""
    if not grid:
        return None
    width = max(len(r) for r in grid)
    best_col, best_n = None, 0
    for col in range(width):
        n = 0
        for row in grid:
            if col >= len(row):
                continue
            if _CODE_RE.fullmatch(_cell_text(row[col])):
                n += 1
        if n > best_n:
            best_n, best_col = n, col
    return best_col if best_n >= DEFAULT_THRESHOLDS.rsbu_min_code_hits else None


def _rsbu_label_col(code_col: int) -> int:
    """Колонка наименования — обычно сразу слева от кода."""
    return max(0, code_col - 1)


def _is_section_only_label(text: str) -> bool:
    t = " ".join((text or "").split())
    if not t or _CODE_RE.fullmatch(t):
        return False
    if re.match(r"^Итого\s+по\s+разделу\b", t, re.I):
        return False
    if re.match(r"^БАЛАНС\b", t, re.I):
        return False
    return bool(_SECTION_ONLY_RE.match(t))


def _split_section_prefix(text: str) -> tuple[str, str] | None:
    """Делит «I. РАЗДЕЛ Название показателя» → (раздел, показатель)."""
    t = " ".join((text or "").split())
    if not t or re.match(r"^Итого\s+по\s+разделу\b", t, re.I):
        return None
    m = _SECTION_SPLIT_RE.match(t)
    if not m:
        return None
    section = " ".join(m.group("section").split())
    item = " ".join(m.group("item").split())
    if len(item) < 3 or _is_section_only_label(item):
        return None
    # не отрезать слишком короткий «раздел»
    if len(section) < 3:
        return None
    return section, item


def _row_has_line_code(row: list[Any], code_col: int) -> bool:
    if code_col >= len(row):
        return False
    return bool(_CODE_RE.fullmatch(_cell_text(row[code_col])))


def _row_has_numeric_values(row: list[Any], after_col: int) -> bool:
    """Есть ли числа справа от code_col (суммы периода)."""
    for c in row[after_col + 1 :]:
        t = _cell_text(c).replace(" ", "").replace("\xa0", "")
        if not t or t in {"-", "—", "Х", "X", "x", "х"}:
            continue
        if re.search(r"\d", t):
            return True
    return False


def _clear_row_values(row: list[Any], from_col: int) -> None:
    """Очищает код и значения в строке (оставляет подпись слева)."""
    for c in range(from_col, len(row)):
        cell = row[c]
        cell.text = ""
        if hasattr(cell, "words"):
            cell.words = []


def _move_row_values(src: list[Any], dst: list[Any], from_col: int) -> None:
    """Переносит код/значения из src в dst (с from_col)."""
    for c in range(from_col, max(len(src), len(dst))):
        if c >= len(src) or c >= len(dst):
            continue
        sc, dc = src[c], dst[c]
        if _cell_text(dc) and not _cell_text(sc):
            continue
        dc.text = sc.text
        if hasattr(sc, "words") and hasattr(dc, "words"):
            dc.words = list(sc.words or [])
        if getattr(sc, "bbox", None) is not None:
            dc.bbox = sc.bbox
        for attr in ("colspan", "rowspan", "covered"):
            if hasattr(sc, attr) and hasattr(dc, attr):
                setattr(dc, attr, getattr(sc, attr))
        sc.text = ""
        if hasattr(sc, "words"):
            sc.words = []


def _clone_empty_row_like(row: list[Any], label_text: str, label_col: int) -> list[Any]:
    """Новая строка-клон структуры с подписью раздела."""
    new_row: list[Any] = []
    for i, cell in enumerate(row):
        nc = copy(cell)
        if i == label_col:
            nc.text = label_text
            if hasattr(nc, "words"):
                nc.words = []
        else:
            nc.text = ""
            if hasattr(nc, "words"):
                nc.words = []
            if hasattr(nc, "covered"):
                nc.covered = False
            if hasattr(nc, "colspan"):
                nc.colspan = 1
            if hasattr(nc, "rowspan"):
                nc.rowspan = 1
        if hasattr(nc, "row"):
            pass  # индексы пересчитает caller при необходимости
        new_row.append(nc)
    return new_row


def fix_rsbu_section_value_shift(
    grid: list[list[Any]],
    kinds: list[str] | None = None,
) -> tuple[list[list[Any]], list[str] | None]:
    """
    Чинит сползание строк РСБУ-баланса:

    1) «I. РАЗДЕЛ Название» + код/суммы → строка раздела + строка показателя.
    2) Строка-раздел с кодом/суммами + следующая строка-показатель без кода
       → перенос кода/сумм на показатель.

    Не трогает «Итого по разделу» / «БАЛАНС».
    """
    if not grid:
        return grid, kinds

    code_col = _rsbu_find_code_col(grid)
    if code_col is None:
        return grid, kinds
    label_col = _rsbu_label_col(code_col)

    new_grid: list[list[Any]] = []
    new_kinds: list[str] = []
    kind_list = list(kinds) if kinds is not None else ["DATA"] * len(grid)

    i = 0
    while i < len(grid):
        row = grid[i]
        kind = kind_list[i] if i < len(kind_list) else "DATA"
        label = _cell_text(row[label_col]) if label_col < len(row) else ""
        has_code = _row_has_line_code(row, code_col)
        has_vals = _row_has_numeric_values(row, code_col) or has_code

        # --- case 1: section prefix glued to item name ---
        split = _split_section_prefix(label) if label else None
        if split and has_code:
            section, item = split
            sec_row = _clone_empty_row_like(row, section, label_col)
            row[label_col].text = item
            if hasattr(row[label_col], "words"):
                # текст уже правильный; слова старой склейки лучше сбросить
                row[label_col].words = []
            new_grid.append(sec_row)
            new_kinds.append("HEADER" if kind == "HEADER" else "DATA")
            new_grid.append(row)
            new_kinds.append(kind)
            i += 1
            continue

        # --- case 2: section-only label holding next item's values ---
        if (
            _is_section_only_label(label)
            and has_vals
            and i + 1 < len(grid)
        ):
            nxt = grid[i + 1]
            nxt_label = _cell_text(nxt[label_col]) if label_col < len(nxt) else ""
            nxt_has_code = _row_has_line_code(nxt, code_col)
            nxt_is_section = _is_section_only_label(nxt_label) or bool(
                _split_section_prefix(nxt_label)
            )
            if (
                nxt_label
                and not nxt_has_code
                and not nxt_is_section
                and not re.match(r"^Итого\s+по\s+разделу\b", nxt_label, re.I)
            ):
                _move_row_values(row, nxt, code_col)
                new_grid.append(row)
                new_kinds.append(kind)
                new_grid.append(nxt)
                new_kinds.append(kind_list[i + 1] if i + 1 < len(kind_list) else "DATA")
                i += 2
                continue

        new_grid.append(row)
        new_kinds.append(kind)
        i += 1

    if kinds is None:
        return new_grid, None
    return new_grid, new_kinds
