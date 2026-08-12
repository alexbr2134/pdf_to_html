"""Общие хелперы ячеек таблицы."""

from __future__ import annotations

from typing import Any


def cell_text(cell: Any) -> str:
    return (getattr(cell, "text", None) or "").strip()


_cell_text = cell_text
