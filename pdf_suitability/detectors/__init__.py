"""Детекторы проблем на странице PDF."""

from pdf_suitability.detectors.base import PageDetector
from pdf_suitability.detectors.broken_fonts import (
    BrokenFontsDetector,
    document_has_broken_fonts,
    page_has_broken_fonts,
)
from pdf_suitability.detectors.image_scan import (
    ImageOnlyScanDetector,
    page_is_image_only_scan,
)
from pdf_suitability.detectors.table_complexity import (
    GridSizeStats,
    SpanMergeStats,
    grid_has_complex_span_merges,
    grid_is_large_table,
    grid_needs_unmarked_routing,
    grid_size_stats,
    grid_span_merge_stats,
    page_has_unmarked_table_lines,
    should_route_unmarked_complex_spans,
)

__all__ = [
    "PageDetector",
    "BrokenFontsDetector",
    "ImageOnlyScanDetector",
    "document_has_broken_fonts",
    "page_has_broken_fonts",
    "page_is_image_only_scan",
    "GridSizeStats",
    "SpanMergeStats",
    "grid_has_complex_span_merges",
    "grid_is_large_table",
    "grid_needs_unmarked_routing",
    "grid_size_stats",
    "grid_span_merge_stats",
    "page_has_unmarked_table_lines",
    "should_route_unmarked_complex_spans",
]
