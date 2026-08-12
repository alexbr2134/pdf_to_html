"""Сборка pre-check пригодности страницы из детекторов."""

from __future__ import annotations

from typing import Any

from pdf_suitability.config import DEFAULT_CONFIG, SuitabilityConfig
from pdf_suitability.core import PageSuitability
from pdf_suitability.detectors.broken_fonts import BrokenFontsDetector
from pdf_suitability.detectors.image_scan import ImageOnlyScanDetector


_PRE_CHECK_DETECTORS = (
    BrokenFontsDetector(),
    ImageOnlyScanDetector(),
)


def assess_page_suitability(
    page: Any,
    page_num: int = 1,
    pdf_path: str | None = None,
    config: SuitabilityConfig | None = None,
) -> PageSuitability:
    """
    Быстрая pre-check пригодности (до конвертации).

    Здесь только broken_fonts и image_only_scan.
    unmarked_table_lines — после process_table через
    should_route_unmarked_complex_spans (см. build_page_section).
    """
    del pdf_path  # reserved; unmarked-lines gate is post-process
    cfg = config or DEFAULT_CONFIG
    reasons: list[str] = []
    messages: list[str] = []

    for detector in _PRE_CHECK_DETECTORS:
        has_issue, reason, message = detector.detect(page, cfg)
        if has_issue:
            reasons.append(reason)
            messages.append(message)

    seen: set[str] = set()
    uniq_reasons: list[str] = []
    uniq_messages: list[str] = []
    for r, m in zip(reasons, messages):
        if r in seen:
            continue
        seen.add(r)
        uniq_reasons.append(r)
        uniq_messages.append(m)

    return PageSuitability(
        suitable=not uniq_reasons,
        reasons=uniq_reasons,
        messages=uniq_messages,
        page_num=page_num,
    )
