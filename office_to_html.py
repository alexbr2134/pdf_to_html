"""
Word/Excel → семантический HTML.

    from office_to_html import office_to_html
    result = office_to_html("docs/torg12.docx", "out/torg12.html")

Парсим .docx / .xlsx / .xlsm. Старые .doc и .xls сразу роутятся
(legacy_format), без конвертации в OOXML.

Типы документов — те же, что у PDF (`pdf_doc_types`).
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from office_docx import extract_docx
from office_html import (
    finalize_office_html,
    render_docx_html,
    render_sheet_html,
    wrap_unit_section,
)
from office_normalize import (
    NormalizeError,
    SUPPORTED_OFFICE,
    cleanup_normalize,
    normalize_office_file,
)
from office_suitability import (
    REASON_ENCRYPTED,
    REASON_LABELS_RU,
    REASON_LEGACY_FORMAT,
    OfficeUnitSuitability,
    assess_layout_table_abuse,
    assess_sheet_density,
    assess_text_and_structure,
    looks_like_form_document,
)
from office_xlsx import extract_excel
from pdf_doc_types import DocType
from pdf_to_html import ConversionResult, RejectedPage

__all__ = [
    "office_to_html",
    "ConversionResult",
    "RejectedPage",
    "SUPPORTED_OFFICE",
    "REASON_LABELS_RU",
]


def office_to_html(
    office_path: str | Path,
    html_path: str | Path,
    *,
    skip_unsuitable: bool = True,
    quiet: bool = True,
) -> ConversionResult:
    """
    Конвертирует DOCX/XLSX в HTML.

    Word → один раздел на документ, Excel → раздел на лист.
    Результат совместим с ``pdf_to_html.ConversionResult``.
    """
    src = Path(office_path).expanduser().resolve()
    dst = Path(html_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Файл не найден: {src}")

    t0 = time.perf_counter()
    warnings: list[str] = []
    rejected: list[RejectedPage] = []
    doc_types: list[str] = []
    sections: list[str] = []
    accepted = 0
    total = 0
    norm = None

    try:
        with _quiet(quiet):
            try:
                norm = normalize_office_file(src)
            except NormalizeError as exc:
                reason = getattr(exc, "reason", REASON_LEGACY_FORMAT)
                total = 1
                suitability = OfficeUnitSuitability(
                    suitable=False,
                    reasons=[reason],
                    messages=[REASON_LABELS_RU.get(reason, str(exc))],
                    unit_num=1,
                    unit_kind="page",
                )
                sections.append(
                    wrap_unit_section("", unit_num=1, suitability=suitability)
                )
                rejected.append(
                    RejectedPage(
                        page_num=1,
                        reasons=tuple(suitability.reasons),
                        messages=tuple(suitability.messages),
                    )
                )
                html_doc = finalize_office_html(
                    sections, title=src.stem, source_name=src.name
                )
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(html_doc, encoding="utf-8")
                return ConversionResult(
                    pdf_path=src,
                    html_path=dst,
                    total_pages=1,
                    accepted_pages=0,
                    rejected_pages=rejected,
                    seconds=time.perf_counter() - t0,
                    doc_types=["unknown"],
                    warnings=[],
                )

            warnings.extend(norm.warnings)

            if norm.format == "docx":
                accepted, total, sections, rejected, doc_types, more_w = _convert_docx(
                    norm.path,
                    source_path=str(src),
                    skip_unsuitable=skip_unsuitable,
                )
                warnings.extend(more_w)
            else:
                accepted, total, sections, rejected, doc_types, more_w = _convert_excel(
                    norm.path,
                    source_path=str(src),
                    fmt=norm.format,
                    skip_unsuitable=skip_unsuitable,
                )
                warnings.extend(more_w)

            html_doc = finalize_office_html(
                sections,
                title=src.stem,
                source_name=src.name,
                extra_notices=list(warnings),
            )
    finally:
        if norm is not None:
            cleanup_normalize(norm)

    elapsed = time.perf_counter() - t0
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(html_doc, encoding="utf-8")

    return ConversionResult(
        pdf_path=src,
        html_path=dst,
        total_pages=total,
        accepted_pages=accepted,
        rejected_pages=rejected,
        seconds=elapsed,
        doc_types=doc_types,
        warnings=warnings,
    )


def _convert_docx(
    path: Path,
    *,
    source_path: str,
    skip_unsuitable: bool,
) -> tuple[int, int, list[str], list[RejectedPage], list[str], list[str]]:
    warnings: list[str] = []
    try:
        model = extract_docx(path)
    except PermissionError:
        suitability = OfficeUnitSuitability(
            suitable=False,
            reasons=[REASON_ENCRYPTED],
            messages=[REASON_LABELS_RU[REASON_ENCRYPTED]],
            unit_num=1,
        )
        section = wrap_unit_section("", unit_num=1, suitability=suitability)
        return (
            0,
            1,
            [section],
            [
                RejectedPage(
                    page_num=1,
                    reasons=tuple(suitability.reasons),
                    messages=tuple(suitability.messages),
                )
            ],
            ["unknown"],
            warnings,
        )

    suitability = assess_text_and_structure(
        text=model.text,
        n_tables=model.n_tables,
        n_images=model.n_images,
        n_embedded=model.n_embedded,
        unit_num=1,
        unit_kind="page",
    )
    abuse = assess_layout_table_abuse(
        n_tables=model.n_tables,
        total_cells=model.total_cells,
        nonempty_cells=model.nonempty_cells,
        text_len=len(model.text),
        unit_num=1,
        form_like=looks_like_form_document(model.text),
    )
    if abuse is not None:
        suitability = abuse

    body = ""
    doc_type: DocType | None = None
    if suitability.suitable or not skip_unsuitable:
        body, doc_type = render_docx_html(model, source_path=source_path)
        suitability.doc_type = doc_type.value
    else:
        from pdf_doc_types import detect_doc_type

        doc_type = detect_doc_type(text=model.text, pdf_path=source_path).doc_type
        suitability.doc_type = doc_type.value

    section = wrap_unit_section(
        body,
        unit_num=1,
        unit_kind="page",
        doc_type=doc_type,
        suitability=suitability if (skip_unsuitable and not suitability.suitable) else None,
    )

    rejected: list[RejectedPage] = []
    accepted = 1
    if skip_unsuitable and not suitability.suitable:
        accepted = 0
        rejected.append(
            RejectedPage(
                page_num=1,
                reasons=tuple(suitability.reasons),
                messages=tuple(suitability.messages),
                doc_type=suitability.doc_type,
            )
        )

    return (
        accepted,
        1,
        [section],
        rejected,
        [doc_type.value if doc_type else "unknown"],
        warnings,
    )


def _convert_excel(
    path: Path,
    *,
    source_path: str,
    fmt: str,
    skip_unsuitable: bool,
) -> tuple[int, int, list[str], list[RejectedPage], list[str], list[str]]:
    warnings: list[str] = []
    try:
        book = extract_excel(path, fmt=fmt)
    except PermissionError:
        suitability = OfficeUnitSuitability(
            suitable=False,
            reasons=[REASON_ENCRYPTED],
            messages=[REASON_LABELS_RU[REASON_ENCRYPTED]],
            unit_num=1,
            unit_kind="sheet",
        )
        section = wrap_unit_section(
            "", unit_num=1, unit_kind="sheet", suitability=suitability
        )
        return (
            0,
            1,
            [section],
            [
                RejectedPage(
                    page_num=1,
                    reasons=(REASON_ENCRYPTED,),
                    messages=(REASON_LABELS_RU[REASON_ENCRYPTED],),
                )
            ],
            ["unknown"],
            warnings,
        )

    warnings.extend(book.warnings)
    sections: list[str] = []
    rejected: list[RejectedPage] = []
    doc_types: list[str] = []
    accepted = 0
    fallback_type: DocType | None = None

    visible_sheets = [s for s in book.sheets if not s.hidden] or list(book.sheets)
    for idx, sheet in enumerate(visible_sheets, start=1):
        density = assess_sheet_density(
            n_rows=sheet.n_rows,
            n_cols=sheet.n_cols,
            n_nonempty=sheet.n_nonempty,
            unit_num=idx,
            unit_name=sheet.name,
        )
        suitability = density or assess_text_and_structure(
            text=sheet.text,
            n_tables=1 if sheet.grid else 0,
            unit_num=idx,
            unit_kind="sheet",
            unit_name=sheet.name,
        )

        body = ""
        doc_type: DocType | None = fallback_type
        if suitability.suitable or not skip_unsuitable:
            body, doc_type = render_sheet_html(
                sheet,
                doc_type=fallback_type,
                source_path=source_path,
                workbook_text=book.text,
            )
            fallback_type = doc_type
            suitability.doc_type = doc_type.value
        else:
            from pdf_doc_types import detect_doc_type

            doc_type = detect_doc_type(
                text=sheet.text or book.text,
                pdf_path=source_path,
                fallback=fallback_type,
            ).doc_type
            suitability.doc_type = doc_type.value

        doc_types.append(doc_type.value if doc_type else "unknown")
        show_reject = skip_unsuitable and not suitability.suitable
        sections.append(
            wrap_unit_section(
                body,
                unit_num=idx,
                unit_kind="sheet",
                unit_name=sheet.name,
                doc_type=doc_type,
                suitability=suitability if show_reject else None,
            )
        )
        if show_reject:
            rejected.append(
                RejectedPage(
                    page_num=idx,
                    reasons=tuple(suitability.reasons),
                    messages=tuple(suitability.messages),
                    doc_type=suitability.doc_type,
                )
            )
        else:
            accepted += 1

    total = len(visible_sheets) or 1
    if not visible_sheets:
        suitability = assess_text_and_structure(text="", n_tables=0, unit_num=1)
        sections.append(
            wrap_unit_section("", unit_num=1, unit_kind="sheet", suitability=suitability)
        )
        rejected.append(
            RejectedPage(
                page_num=1,
                reasons=tuple(suitability.reasons),
                messages=tuple(suitability.messages),
            )
        )
        doc_types = ["unknown"]
        total = 1

    return accepted, total, sections, rejected, doc_types, warnings


@contextlib.contextmanager
def _quiet(quiet: bool):
    if not quiet:
        yield
        return
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield


def _main(argv: Iterable[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Office (.docx/.xlsx) → HTML"
    )
    parser.add_argument("office", type=Path, help="Путь к Office-файлу")
    parser.add_argument("html", type=Path, help="Куда сохранить HTML")
    parser.add_argument(
        "--no-skip",
        action="store_true",
        help="Не отсеивать unsuitable-единицы",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    result = office_to_html(
        args.office,
        args.html,
        skip_unsuitable=not args.no_skip,
        quiet=not args.verbose,
    )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"{result.html_path}: {result.accepted_pages}/{result.total_pages} ok, "
            f"{result.rejected_count} route, {result.seconds:.2f}s"
        )
        for page in result.rejected_pages:
            print(f"  #{page.page_num}: {'; '.join(page.reason_labels)}")
        for w in result.warnings[:5]:
            print(f"  warn: {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
