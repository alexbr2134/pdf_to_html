"""Type-specific table heuristics."""

from doc_type.heuristics.base import (
    DEFAULT_REGISTRY,
    HeuristicRegistry,
    TableHeuristic,
    apply_type_heuristics,
)
from doc_type.heuristics.line_items import (
    grid_looks_like_line_item_table,
    grid_should_bypass_unmarked_routing,
)
from doc_type.heuristics.rsbu import fix_rsbu_section_value_shift

__all__ = [
    "DEFAULT_REGISTRY",
    "HeuristicRegistry",
    "TableHeuristic",
    "apply_type_heuristics",
    "fix_rsbu_section_value_shift",
    "grid_looks_like_line_item_table",
    "grid_should_bypass_unmarked_routing",
]
