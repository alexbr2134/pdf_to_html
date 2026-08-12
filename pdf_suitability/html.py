"""HTML/CSS для отсеянных страниц."""

from __future__ import annotations

import html

from pdf_suitability.core import REASON_LABELS_RU, PageSuitability


PAGE_REJECTED_CSS = """
.page-rejected {
  border: 1px solid #a60;
  background: #fff8f0;
  color: #530;
  padding: 0.75em 1em;
  margin: 0 0 1em;
}
.page-rejected ul { margin: 0.4em 0 0 1.2em; }
.page[data-rejected="true"] { opacity: 0.95; }
"""


def rejected_page_notice_html(result: PageSuitability) -> str:
    """HTML-заглушка для отсеянной страницы (вместо полной конвертации)."""
    reasons = result.messages or [
        REASON_LABELS_RU.get(r, r) for r in result.reasons
    ]
    items = "".join(f"<li>{html.escape(m)}</li>" for m in reasons)
    codes = html.escape(result.reason_codes)
    return (
        f'<div class="page-rejected" role="status" data-rejected="true" '
        f'data-reasons="{codes}">'
        f"<p><strong>Страница {result.page_num} отсеяна</strong> — "
        f"не пригодна для smart-конвертации (будет передана другой модели).</p>"
        f"<ul>{items}</ul>"
        f"</div>"
    )
