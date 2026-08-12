"""TableHeuristic ABC + HeuristicRegistry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

from doc_type.core import DocType, as_doc_type
from doc_type.heuristics.default import apply_default
from doc_type.heuristics.invoice import apply_invoice
from doc_type.heuristics.ks2 import apply_ks2
from doc_type.heuristics.ks3 import apply_ks3
from doc_type.heuristics.rsbu import fix_rsbu_section_value_shift, _rsbu_find_code_col
from doc_type.heuristics.torg12 import apply_torg12

ApplyFn = Callable[
    [list[list[Any]], list[str] | None],
    tuple[list[list[Any]], list[str] | None],
]


class TableHeuristic(ABC):
    """Стратегия постправок одной таблицы после process_table."""

    @abstractmethod
    def apply(
        self,
        grid: list[list[Any]],
        kinds: list[str] | None,
    ) -> tuple[list[list[Any]], list[str] | None]:
        raise NotImplementedError


class _FnHeuristic(TableHeuristic):
    def __init__(self, fn: ApplyFn) -> None:
        self._fn = fn

    def apply(
        self,
        grid: list[list[Any]],
        kinds: list[str] | None,
    ) -> tuple[list[list[Any]], list[str] | None]:
        return self._fn(grid, kinds)


def _apply_rsbu(
    grid: list[list[Any]],
    kinds: list[str] | None,
) -> tuple[list[list[Any]], list[str] | None]:
    if _rsbu_find_code_col(grid) is not None:
        return fix_rsbu_section_value_shift(grid, kinds)
    return grid, kinds


class HeuristicRegistry:
    """Реестр type → TableHeuristic."""

    def __init__(self) -> None:
        self._by_type: dict[DocType, TableHeuristic] = {
            DocType.RSBU: _FnHeuristic(_apply_rsbu),
            DocType.UNKNOWN: _FnHeuristic(apply_default),
            DocType.KS2: _FnHeuristic(apply_ks2),
            DocType.KS3: _FnHeuristic(apply_ks3),
            DocType.TORG12: _FnHeuristic(apply_torg12),
            DocType.INVOICE_SF: _FnHeuristic(apply_invoice),
            DocType.UPD: _FnHeuristic(apply_invoice),
        }

    def get(self, doc_type: DocType | str | None) -> TableHeuristic:
        dt = as_doc_type(doc_type)
        return self._by_type.get(dt, self._by_type[DocType.UNKNOWN])

    def register(self, doc_type: DocType, heuristic: TableHeuristic) -> None:
        self._by_type[doc_type] = heuristic


DEFAULT_REGISTRY = HeuristicRegistry()


def apply_type_heuristics(
    doc_type: DocType | str | None,
    page,
    grid: list[list[Any]],
    kinds: list[str] | None,
) -> tuple[list[list[Any]], list[str] | None]:
    """
    Type-specific постправки к одной таблице после process_table.

    UNKNOWN → только безопасный RSBU-fix на финансовых grid.
    ``page`` сохранён в сигнатуре для совместимости API (не используется).
    """
    del page
    return DEFAULT_REGISTRY.get(doc_type).apply(grid, kinds)
