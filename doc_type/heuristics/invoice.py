"""СФ / УПД: сетка обычно уже ок; page-level — html.enrich."""

from __future__ import annotations

from typing import Any


def apply_invoice(
    grid: list[list[Any]],
    kinds: list[str] | None,
) -> tuple[list[list[Any]], list[str] | None]:
    return grid, kinds
