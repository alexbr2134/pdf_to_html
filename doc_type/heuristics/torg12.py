"""ТОРГ-12: подписи итогов в сетке."""

from __future__ import annotations

from typing import Any

from doc_type.heuristics.ks_totals import fix_ks_footer_total_labels

def fix_torg12_total_row_labels(
    grid: list[list[Any]],
    kinds: list[str] | None,
) -> tuple[list[list[Any]], list[str] | None]:
    """Подписывает Итого / Всего по накладной на хвостовых строках без товара."""
    return fix_ks_footer_total_labels(
        grid,
        kinds,
        ["Итого", "Всего по накладной"],
    )


def apply_torg12(
    grid: list[list[Any]],
    kinds: list[str] | None,
) -> tuple[list[list[Any]], list[str] | None]:
    return fix_torg12_total_row_labels(grid, kinds)
