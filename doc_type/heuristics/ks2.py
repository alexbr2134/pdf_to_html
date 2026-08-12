"""КС-2: заголовки и итоги."""

from __future__ import annotations

import re
from typing import Any

from doc_type.constants import KS2_HEADERS_15, KS2_HEADERS_8
from doc_type.heuristics.cells import cell_text as _cell_text
from doc_type.heuristics.ks_totals import fix_ks_footer_total_labels

def _row_is_numeric_index_header(row: list[Any]) -> bool:
    """Строка вида 1|2|3|…|N без текстовых заголовков."""
    texts = [_cell_text(c) for c in row if not getattr(c, "covered", False)]
    texts = [t for t in texts if t]
    if len(texts) < 6:
        return False
    nums: list[int] = []
    for t in texts:
        if not re.fullmatch(r"\d{1,2}", t):
            return False
        nums.append(int(t))
    return nums[0] == 1 and nums == list(range(1, len(nums) + 1))


def _row_has_text_header_labels(row: list[Any]) -> bool:
    joined = " ".join(_cell_text(c) for c in row)
    return bool(
        re.search(
            r"Наименование|Выполнено|Стоимость|Количество|Единица|Товар|НДС",
            joined,
            re.I,
        )
    )


def fix_ks2_numeric_headers(
    grid: list[list[Any]],
    kinds: list[str] | None,
) -> tuple[list[list[Any]], list[str] | None]:
    """Подставляет канонические заголовки КС-2 вместо голой строки 1…N."""
    if not grid:
        return grid, kinds
    kind_list = list(kinds) if kinds is not None else ["DATA"] * len(grid)
    for i, row in enumerate(grid):
        if not _row_is_numeric_index_header(row):
            continue
        # если выше уже есть нормальные подписи — не трогаем
        prev_ok = any(
            _row_has_text_header_labels(grid[j]) for j in range(max(0, i - 3), i)
        )
        if prev_ok:
            continue
        n = len([c for c in row if not getattr(c, "covered", False)])
        headers = None
        if n == 8:
            headers = KS2_HEADERS_8
        elif n in (15, 16):
            headers = KS2_HEADERS_15[:n]
        if not headers:
            continue
        visible = [c for c in row if not getattr(c, "covered", False)]
        for cell, title in zip(visible, headers):
            cell.text = title
            if hasattr(cell, "words"):
                cell.words = []
        if i < len(kind_list):
            kind_list[i] = "HEADER"
        break
    if kinds is None:
        return grid, None
    return grid, kind_list


def apply_ks2(
    grid: list[list[Any]],
    kinds: list[str] | None,
) -> tuple[list[list[Any]], list[str] | None]:
    grid, kinds = fix_ks2_numeric_headers(grid, kinds)
    return fix_ks_footer_total_labels(grid, kinds, ["Итого", "Всего по акту"])
