"""
Сборка HTML из Office-моделей + type heuristics (переиспользование pdf_doc_types).
"""

from __future__ import annotations

import html
import re

from office_docx import DocxDocumentModel
from office_suitability import rejected_unit_notice_html, OfficeUnitSuitability
from office_xlsx import ExcelSheetModel
from pdf_doc_types import (
    DocType,
    apply_type_heuristics,
    detect_doc_type,
    enrich_page_html_for_doc_type,
)
from pdf_html_pipeline import (
    DOCUMENT_CSS,
    Cell,
    classify_rows,
    render_table_html,
    wrap_html_document,
)


def _render_paragraph(text: str, *, heading_level: int | None = None) -> str:
    if heading_level and 1 <= heading_level <= 6:
        return f"<h{heading_level}>{html.escape(text)}</h{heading_level}>"
    if "\n" in text:
        esc = "<br>\n".join(html.escape(line) for line in text.split("\n"))
    else:
        esc = html.escape(text)
    return f"<p>{esc}</p>"


def _process_grid(
    grid: list[list[Cell]],
    doc_type: DocType | str | None,
) -> str:
    if not grid:
        return ""
    kinds = classify_rows(grid)
    grid, kinds = apply_type_heuristics(doc_type, None, grid, kinds)
    assert kinds is not None
    return render_table_html(grid, kinds)


def render_docx_html(
    model: DocxDocumentModel,
    *,
    doc_type: DocType | str | None = None,
    source_path: str | None = None,
) -> tuple[str, DocType]:
    """HTML body одного DOCX + определённый тип."""
    if doc_type is None:
        detected = detect_doc_type(text=model.text, pdf_path=source_path)
        doc_type = detected.doc_type
    else:
        doc_type = doc_type if isinstance(doc_type, DocType) else DocType(str(doc_type))

    parts: list[str] = []
    for block in model.blocks:
        if block.kind in {"paragraph", "heading"}:
            parts.append(
                _render_paragraph(block.text, heading_level=block.heading_level)
            )
        elif block.kind == "table" and block.grid is not None:
            parts.append(_process_grid(block.grid, doc_type))

    body = "\n".join(parts)
    body = enrich_page_html_for_doc_type(doc_type, body)
    return body, doc_type if isinstance(doc_type, DocType) else DocType.UNKNOWN


def render_sheet_html(
    sheet: ExcelSheetModel,
    *,
    doc_type: DocType | str | None = None,
    source_path: str | None = None,
    workbook_text: str = "",
) -> tuple[str, DocType]:
    """HTML одного листа Excel (несколько таблиц + prose)."""
    probe = sheet.text or workbook_text
    if doc_type is None:
        detected = detect_doc_type(text=probe, pdf_path=source_path)
        doc_type = detected.doc_type

    parts: list[str] = []
    if sheet.name and sheet.name not in {"Sheet", "Sheet1", "TDSheet", "Лист1"}:
        parts.append(f"<h2>{html.escape(sheet.name)}</h2>")

    blocks = sheet.blocks
    if not blocks and sheet.grid:
        # backward-compat
        parts.append(_process_grid(sheet.grid, doc_type))
    else:
        for block in blocks:
            if block.kind == "paragraph" and block.text:
                parts.append(_render_paragraph(block.text))
            elif block.kind == "table" and block.grid:
                parts.append(_process_grid(block.grid, doc_type))

    body = "\n".join(parts)
    body = enrich_page_html_for_doc_type(doc_type, body)
    return body, doc_type if isinstance(doc_type, DocType) else DocType.UNKNOWN


def wrap_unit_section(
    body: str,
    *,
    unit_num: int,
    unit_kind: str = "page",
    unit_name: str | None = None,
    doc_type: DocType | str | None = None,
    suitability: OfficeUnitSuitability | None = None,
) -> str:
    """Обёртка единицы документа (как page-секция PDF)."""
    rejected = suitability is not None and not suitability.suitable
    dt = ""
    if doc_type is not None:
        dt = doc_type.value if isinstance(doc_type, DocType) else str(doc_type)
    attrs = [
        f'data-unit="{unit_num}"',
        f'data-unit-kind="{html.escape(unit_kind)}"',
    ]
    if unit_name:
        attrs.append(f'data-unit-name="{html.escape(unit_name)}"')
    if dt:
        attrs.append(f'data-doc-type="{html.escape(dt)}"')
    if rejected:
        attrs.append('data-rejected="true"')
        if suitability and suitability.reasons:
            attrs.append(
                f'data-reasons="{html.escape(",".join(suitability.reasons))}"'
            )
        inner = rejected_unit_notice_html(suitability) if suitability else body
    else:
        inner = body

    label = unit_name or str(unit_num)
    kind_label = "Лист" if unit_kind == "sheet" else "Раздел"
    header = (
        f'<div class="doc-section-label">{html.escape(kind_label)} '
        f"{html.escape(str(label))}</div>"
    )
    return (
        f'<section class="page" {" ".join(attrs)}>\n'
        f"{header}\n{inner}\n</section>"
    )


def finalize_office_html(
    sections: list[str],
    *,
    title: str,
    source_name: str | None = None,
    extra_notices: list[str] | None = None,
) -> str:
    """Полный HTML-документ."""
    notices = []
    for msg in extra_notices or []:
        notices.append(
            f'<div class="broken-font-warning" role="status">{html.escape(msg)}</div>'
        )
    if source_name:
        notices.insert(
            0,
            f'<p class="doc-section">Источник: {html.escape(source_name)}</p>',
        )
    body = "\n".join(notices + sections)
    _ = DOCUMENT_CSS
    return wrap_html_document(body, title=title)
