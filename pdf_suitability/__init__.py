"""
Проверка пригодности страницы PDF для smart-пайплайна (векторный текст/линии).

Страницы, не прошедшие проверку, помечаются как отсеянные — их предполагается
отдавать другой модели (скан/OCR). Сама передача другой модели здесь не делается.
"""

from pdf_suitability.assess import assess_page_suitability
from pdf_suitability.config import (
    DEFAULT_CONFIG,
    DEFAULT_UNMARKED_ROUTING_STRICTNESS,
    SuitabilityConfig,
)
from pdf_suitability.core import (
    REASON_BROKEN_FONTS,
    REASON_IMAGE_ONLY_SCAN,
    REASON_LABELS_RU,
    REASON_UNMARKED_TABLE_LINES,
    PageSuitability,
    merge_page_suitability,
    suitability_unmarked_complex_spans,
)
from pdf_suitability.detectors import (
    BrokenFontsDetector,
    GridSizeStats,
    ImageOnlyScanDetector,
    PageDetector,
    SpanMergeStats,
    document_has_broken_fonts,
    grid_has_complex_span_merges,
    grid_is_large_table,
    grid_needs_unmarked_routing,
    grid_size_stats,
    grid_span_merge_stats,
    page_has_broken_fonts,
    page_has_unmarked_table_lines,
    page_is_image_only_scan,
    should_route_unmarked_complex_spans,
)
from pdf_suitability.html import PAGE_REJECTED_CSS, rejected_page_notice_html
from pdf_suitability.stats import SuitabilityStats, format_suitability_report

__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_UNMARKED_ROUTING_STRICTNESS",
    "SuitabilityConfig",
    "REASON_BROKEN_FONTS",
    "REASON_IMAGE_ONLY_SCAN",
    "REASON_LABELS_RU",
    "REASON_UNMARKED_TABLE_LINES",
    "PageSuitability",
    "SuitabilityStats",
    "PageDetector",
    "BrokenFontsDetector",
    "ImageOnlyScanDetector",
    "SpanMergeStats",
    "GridSizeStats",
    "assess_page_suitability",
    "merge_page_suitability",
    "suitability_unmarked_complex_spans",
    "document_has_broken_fonts",
    "page_has_broken_fonts",
    "page_has_unmarked_table_lines",
    "page_is_image_only_scan",
    "grid_span_merge_stats",
    "grid_size_stats",
    "grid_has_complex_span_merges",
    "grid_is_large_table",
    "grid_needs_unmarked_routing",
    "should_route_unmarked_complex_spans",
    "rejected_page_notice_html",
    "format_suitability_report",
    "PAGE_REJECTED_CSS",
]
