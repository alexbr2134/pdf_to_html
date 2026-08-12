"""
Распознавание типа документа и type-specific эвристики smart-пайплайна.

Если тип не определён уверенно — DocType.UNKNOWN → дефолтное поведение.
Эвристики не должны ломать чужие типы (только точечные правки под свой класс).
"""

from __future__ import annotations

from doc_type.config import DEFAULT_THRESHOLDS, DetectionThresholds
from doc_type.core import (
    DocType,
    DocTypeResult,
    RoutingPolicy,
    as_doc_type,
    routing_policy_for,
)
from doc_type.detection.detector import DocTypeDetector, detect_doc_type, page_text
from doc_type.heuristics.base import (
    DEFAULT_REGISTRY,
    HeuristicRegistry,
    TableHeuristic,
    apply_type_heuristics,
)
from doc_type.heuristics.ks2 import fix_ks2_numeric_headers
from doc_type.heuristics.ks3 import fix_ks3_split_num_name
from doc_type.heuristics.ks_totals import fix_ks_footer_total_labels
from doc_type.heuristics.line_items import (
    grid_looks_like_line_item_table,
    grid_should_bypass_unmarked_routing,
)
from doc_type.heuristics.rsbu import fix_rsbu_section_value_shift
from doc_type.heuristics.torg12 import fix_torg12_total_row_labels
from doc_type.html.enrich import enrich_page_html_for_doc_type
from doc_type.html.invoice import build_invoice_fields_table_html
from doc_type.html.torg12 import build_torg_totals_table_html

# private aliases used by callers / suitability
_as_doc_type = as_doc_type
_page_text = page_text

__all__ = [
    "DEFAULT_REGISTRY",
    "DEFAULT_THRESHOLDS",
    "DetectionThresholds",
    "DocType",
    "DocTypeDetector",
    "DocTypeResult",
    "HeuristicRegistry",
    "RoutingPolicy",
    "TableHeuristic",
    "apply_type_heuristics",
    "as_doc_type",
    "build_invoice_fields_table_html",
    "build_torg_totals_table_html",
    "detect_doc_type",
    "enrich_page_html_for_doc_type",
    "fix_ks2_numeric_headers",
    "fix_ks3_split_num_name",
    "fix_ks_footer_total_labels",
    "fix_rsbu_section_value_shift",
    "fix_torg12_total_row_labels",
    "grid_looks_like_line_item_table",
    "grid_should_bypass_unmarked_routing",
    "page_text",
    "routing_policy_for",
]
