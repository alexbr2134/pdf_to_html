"""Допилки HTML под тип документа (СФ/УПД/ТОРГ)."""

from __future__ import annotations

import re

from doc_type.config import DEFAULT_THRESHOLDS
from doc_type.core import DocType, as_doc_type
from doc_type.html.invoice import (
    _plain_text_from_html,
    build_invoice_fields_table_html,
)
from doc_type.html.torg12 import build_torg_totals_table_html


def enrich_page_html_for_doc_type(
    doc_type: DocType | str | None,
    html_body: str,
) -> str:
    """Мелкие дописки: поля СФ если нет таблицы, итоги ТОРГ если сетки нет."""
    dt = as_doc_type(doc_type)
    if not html_body or not html_body.strip():
        return html_body
    text = _plain_text_from_html(html_body)
    n_tables = html_body.lower().count("<table")
    thr = DEFAULT_THRESHOLDS

    if dt in (DocType.INVOICE_SF, DocType.UPD):
        # шапку в KV-таблицу только когда товарной сетки ещё нет
        if "invoice-fields" not in html_body.lower() and n_tables == 0:
            field_markers = len(re.findall(r"\(\s*\d+[аaбb]?\s*\)", text))
            if field_markers >= thr.invoice_field_min_markers:
                fields = build_invoice_fields_table_html(text)
                if fields:
                    return html_body.rstrip() + "\n" + fields

    if dt == DocType.TORG12 and n_tables == 0:
        totals = build_torg_totals_table_html(text)
        if totals:
            return html_body.rstrip() + "\n" + totals

    return html_body
