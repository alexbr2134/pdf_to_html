"""КС-3: split num/name и итоги."""

from __future__ import annotations

import re
from typing import Any

from doc_type.heuristics.cells import cell_text as _cell_text
from doc_type.heuristics.ks_totals import fix_ks_footer_total_labels

def fix_ks3_split_num_name(
    grid: list[list[Any]],
    kinds: list[str] | None,
) -> tuple[list[list[Any]], list[str] | None]:
    """«1 Демонтаж…» в одной ячейке с colspan → номер + название."""
    if not grid:
        return grid, kinds
    for row in grid:
        if len(row) < 2:
            continue
        for ci, cell in enumerate(row[:-1]):
            t = _cell_text(cell)
            m = re.match(r"^(\d{1,3})\s+([А-Яа-яЁёA-Za-z].+)$", t)
            if not m:
                continue
            # сосед справа пустой — типичный поглощённый слот
            right = row[ci + 1]
            if _cell_text(right):
                continue
            if int(getattr(cell, "colspan", 1) or 1) < 2 and ci > 0:
                continue
            cell.text = m.group(1)
            if hasattr(cell, "words"):
                cell.words = []
            if hasattr(cell, "colspan"):
                cell.colspan = 1
            right.text = m.group(2).strip()
            if hasattr(right, "words"):
                right.words = []
            break
    return grid, kinds


def apply_ks3(
    grid: list[list[Any]],
    kinds: list[str] | None,
) -> tuple[list[list[Any]], list[str] | None]:
    grid, kinds = fix_ks3_split_num_name(grid, kinds)
    return fix_ks_footer_total_labels(
        grid, kinds, ["Итого", "НДС", "Всего с НДС"]
    )
