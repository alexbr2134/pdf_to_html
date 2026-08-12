"""
Shim: публичный API перенесён в пакет ``doc_type``.

Сохраняет совместимость ``from pdf_doc_types import …``.
"""

from doc_type import *  # noqa: F403
from doc_type import __all__ as __all__  # noqa: F401
from doc_type.core import _as_doc_type  # noqa: F401
from doc_type.detection.detector import _page_text  # noqa: F401
