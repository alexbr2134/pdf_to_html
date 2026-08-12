"""Базовый интерфейс детекторов проблем на странице."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pdf_suitability.config import SuitabilityConfig


class PageDetector(ABC):
    """Базовый интерфейс для детекторов проблем на странице."""

    @property
    @abstractmethod
    def reason_code(self) -> str:
        """Код причины для этой проверки."""

    @abstractmethod
    def detect(
        self, page: Any, config: SuitabilityConfig
    ) -> tuple[bool, str, str]:
        """
        Проверяет страницу.

        Returns
        -------
        (has_issue, reason_code, message)
        """
