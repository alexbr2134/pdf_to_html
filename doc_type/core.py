"""Типы документов и политика роутинга."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DocType(str, Enum):
    """Поддерживаемые классы документов (v2 + типичные samples)."""

    RSBU = "rsbu"
    KS2 = "ks2"
    KS3 = "ks3"
    INVOICE_SF = "invoice_sf"
    TORG12 = "torg12"
    UPD = "upd"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DocTypeResult:
    """Результат детекции типа."""

    doc_type: DocType
    confidence: float  # 0..1
    signals: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        """True, если тип определён не как unknown."""
        from doc_type.config import DEFAULT_THRESHOLDS

        return (
            self.doc_type != DocType.UNKNOWN
            and self.confidence >= DEFAULT_THRESHOLDS.min_confidence_known
        )


@dataclass(frozen=True)
class RoutingPolicy:
    """Политика post-check роутинга unmarked_table_lines."""

    route_large_tables: bool = True
    route_complex_spans: bool = True
    keep_line_item_grids: bool = False


_DEFAULT_ROUTING = RoutingPolicy()

_ROUTING_BY_TYPE: dict[DocType, RoutingPolicy] = {
    DocType.RSBU: RoutingPolicy(route_large_tables=False, route_complex_spans=True),
    DocType.KS2: RoutingPolicy(
        route_large_tables=False, route_complex_spans=True, keep_line_item_grids=True
    ),
    DocType.KS3: RoutingPolicy(
        route_large_tables=False, route_complex_spans=True, keep_line_item_grids=True
    ),
    DocType.INVOICE_SF: RoutingPolicy(
        route_large_tables=False, route_complex_spans=False
    ),
    DocType.TORG12: RoutingPolicy(
        route_large_tables=False, route_complex_spans=False
    ),
    DocType.UPD: RoutingPolicy(
        route_large_tables=False, route_complex_spans=False
    ),
    DocType.UNKNOWN: _DEFAULT_ROUTING,
}


def routing_policy_for(doc_type: DocType | str | None) -> RoutingPolicy:
    """Политика роутинга для типа (unknown → дефолт)."""
    if doc_type is None:
        return _DEFAULT_ROUTING
    if isinstance(doc_type, str):
        try:
            doc_type = DocType(doc_type)
        except ValueError:
            return _DEFAULT_ROUTING
    return _ROUTING_BY_TYPE.get(doc_type, _DEFAULT_ROUTING)


def as_doc_type(doc_type: DocType | str | None) -> DocType:
    if isinstance(doc_type, DocType):
        return doc_type
    if isinstance(doc_type, str):
        try:
            return DocType(doc_type)
        except ValueError:
            return DocType.UNKNOWN
    return DocType.UNKNOWN


# backward-compatible private alias
_as_doc_type = as_doc_type
