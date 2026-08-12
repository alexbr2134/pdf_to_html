"""
Shim: публичный API перенесён в пакет ``pdf_suitability``.

Сохраняет совместимость ``from page_suitability import …``.
"""

from pdf_suitability import *  # noqa: F403
from pdf_suitability import __all__ as __all__  # noqa: F401
