"""Эвристики таблиц строк работ/товаров (КС/смета)."""

from __future__ import annotations

import re
from typing import Any

from doc_type.config import DEFAULT_THRESHOLDS
from doc_type.core import as_doc_type, routing_policy_for
from doc_type.heuristics.cells import cell_text as _cell_text

def grid_looks_like_line_item_table(grid: list[list[Any]] | None) -> bool:
    """
    True, если grid похож на таблицу строк работ/товаров (КС/смета):
    есть строка «1 2 3 … N» и/или несколько строк «№ | текст | … | сумма».
    """
    if not grid or len(grid) < 3:
        return False

    for row in grid[:6]:
        nums: list[int] = []
        for cell in row:
            t = _cell_text(cell)
            if re.fullmatch(r"\d{1,2}", t):
                nums.append(int(t))
            elif t:
                # допускаем склейки вроде «3 4» только если почти вся строка числовая
                break
        if len(nums) >= DEFAULT_THRESHOLDS.line_item_min_index_nums and nums[0] == 1 and nums == list(range(1, len(nums) + 1)):
            return True

    dataish = 0
    for row in grid:
        texts = [_cell_text(c) for c in row]
        if not texts:
            continue
        head = texts[0]
        if not re.fullmatch(r"\d{1,3}", head):
            continue
        has_name = any(re.search(r"[А-Яа-яЁё]{4,}", t or "") for t in texts[1:5])
        has_money = any(
            re.search(r"\d{2,}", (t or "").replace(" ", "").replace("\xa0", ""))
            for t in texts[-5:]
        )
        if has_name and has_money:
            dataish += 1
    return dataish >= DEFAULT_THRESHOLDS.line_item_min_dataish_rows


def grid_should_bypass_unmarked_routing(
    grid: list[list[Any]] | None,
    doc_type: DocType | str | None,
) -> bool:
    """True — не роутить эту таблицу, даже если span/size формально сработали."""
    dt = as_doc_type(doc_type)
    policy = routing_policy_for(dt)
    if not policy.keep_line_item_grids:
        return False
    return grid_looks_like_line_item_table(grid)
