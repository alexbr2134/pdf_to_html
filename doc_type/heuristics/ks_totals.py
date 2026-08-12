"""Общие хелперы итогов КС/ТОРГ."""

from __future__ import annotations

import re
from typing import Any

from doc_type.heuristics.cells import cell_text as _cell_text

def _row_amount_cells(row: list[Any]) -> list[str]:
    """Непустые «суммоподобные» хвосты строки."""
    out: list[str] = []
    for cell in row:
        t = _cell_text(cell)
        if not t:
            continue
        compact = t.replace(" ", "").replace("\xa0", "").replace("'", "")
        if t in {"Х", "X", "x", "х", "-", "—"}:
            out.append(t)
        elif re.search(r"\d", compact) and not re.fullmatch(r"\d{1,3}", t):
            out.append(t)
    return out


def _row_is_bare_total_row(row: list[Any]) -> bool:
    """
    Строка итогов без подписи: слева пусто/прочерки, справа суммы.
    """
    texts = [_cell_text(c) for c in row]
    if not any(texts):
        return False
    # первая текстовая ячейка не должна быть названием работ
    first_text = next((t for t in texts if t), "")
    if re.search(r"[А-Яа-яЁё]{4,}", first_text) and not re.match(
        r"^(?:Итого|Всего|НДС)\b", first_text, re.I
    ):
        return False
    amounts = _row_amount_cells(row)
    if len(amounts) < 1:
        return False
    # название работ отсутствует или это уже итог
    name_like = [
        t
        for t in texts[:-3]
        if t and re.search(r"[А-Яа-яЁё]{4,}", t) and t not in {"Х", "X"}
    ]
    return len(name_like) == 0 and len(amounts) >= 1


def _label_col_for_totals(grid: list[list[Any]]) -> int:
    """Колонка для подписи итогов — обычно 0 или первая текстовая широкая."""
    if not grid:
        return 0
    # предпочитаем col с наибольшим числом кириллических подписей
    width = max(len(r) for r in grid)
    best, best_n = 0, -1
    for col in range(min(3, width)):
        n = 0
        for row in grid:
            if col >= len(row):
                continue
            if re.search(r"[А-Яа-яЁё]{4,}", _cell_text(row[col])):
                n += 1
        if n > best_n:
            best, best_n = col, n
    return best


def fix_ks_footer_total_labels(
    grid: list[list[Any]],
    kinds: list[str] | None,
    labels: list[str],
) -> tuple[list[list[Any]], list[str] | None]:
    """Подписывает хвостовые «голые» суммы снизу вверх заданными labels."""
    if not grid or not labels:
        return grid, kinds
    bare_idxs = [i for i, row in enumerate(grid) if _row_is_bare_total_row(row)]
    if not bare_idxs:
        return grid, kinds
    # берём непрерывный хвост
    tail: list[int] = []
    for i in reversed(bare_idxs):
        if not tail or i == tail[0] - 1:
            tail.insert(0, i)
        else:
            break
    if not tail:
        return grid, kinds
    if len(tail) == 1:
        use = [labels[0]]
    elif len(tail) <= len(labels):
        use = labels[-len(tail) :]
    else:
        use = labels
        tail = tail[-len(use) :]
    label_col = _label_col_for_totals(grid)
    for idx, lab in zip(tail, use):
        row = grid[idx]
        if label_col < len(row) and not _cell_text(row[label_col]):
            row[label_col].text = lab
            if hasattr(row[label_col], "words"):
                row[label_col].words = []
        else:
            # ищем первую пустую слева
            for cell in row:
                if not _cell_text(cell):
                    cell.text = lab
                    if hasattr(cell, "words"):
                        cell.words = []
                    break
    return grid, kinds
