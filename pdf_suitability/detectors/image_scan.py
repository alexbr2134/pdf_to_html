"""Детектор image-only / скан без текстового слоя."""

from __future__ import annotations

from typing import Any

from pdf_suitability.config import DEFAULT_CONFIG, SuitabilityConfig
from pdf_suitability.core import REASON_IMAGE_ONLY_SCAN, REASON_LABELS_RU
from pdf_suitability.detectors.base import PageDetector


def page_is_image_only_scan(
    page: Any,
    *,
    min_chars: int | None = None,
    min_image_cover: float | None = None,
    config: SuitabilityConfig | None = None,
) -> bool:
    """
    True, если страница почти без текстового слоя при заметном растре
    (скан без OCR / картинка на всю полосу).
    """
    from pdf_line_vectorize import _raster_image_cover_ratio

    cfg = config or DEFAULT_CONFIG
    min_chars = cfg.min_chars_for_text if min_chars is None else min_chars
    min_image_cover = (
        cfg.min_image_cover if min_image_cover is None else min_image_cover
    )

    n_chars = len(page.chars or [])
    text = (page.extract_text() or "").strip()
    if n_chars >= min_chars or len(text) >= min_chars:
        return False
    try:
        cover = float(_raster_image_cover_ratio(page))
    except Exception:
        cover = 0.0
    return cover >= min_image_cover


class ImageOnlyScanDetector(PageDetector):
    """Детектор сканов без текстового слоя."""

    @property
    def reason_code(self) -> str:
        return REASON_IMAGE_ONLY_SCAN

    def detect(
        self, page: Any, config: SuitabilityConfig
    ) -> tuple[bool, str, str]:
        if page_is_image_only_scan(page, config=config):
            return True, self.reason_code, REASON_LABELS_RU[self.reason_code]
        return False, self.reason_code, ""
