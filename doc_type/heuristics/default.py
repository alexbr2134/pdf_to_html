"""Дефолтные эвристики для UNKNOWN / безопасный RSBU-fix."""

from __future__ import annotations

from typing import Any

from doc_type.heuristics.rsbu import _rsbu_find_code_col, fix_rsbu_section_value_shift


def apply_default(
    grid: list[list[Any]],
    kinds: list[str] | None,
) -> tuple[list[list[Any]], list[str] | None]:
    code_col = _rsbu_find_code_col(grid)
    if code_col is not None:
        return fix_rsbu_section_value_shift(grid, kinds)
    return grid, kinds
