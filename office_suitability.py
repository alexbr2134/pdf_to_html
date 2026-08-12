"""
Пригодность Office-документов (DOC/DOCX/XLS/XLSX) для smart-сборки HTML.

Коды причин отличаются от PDF: нет broken_fonts / unmarked_table_lines.
"""

from __future__ import annotations

import html
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


REASON_LEGACY_FORMAT = "legacy_format"
# старые коды — на случай уже сохранённых HTML/логов
REASON_LEGACY_UNCONVERTED = "legacy_unconverted"
REASON_LEGACY_STRUCTURE_LOSS = "legacy_structure_loss"
REASON_EMBEDDED_ONLY = "embedded_only"
REASON_ENCRYPTED = "encrypted"
REASON_EMPTY_DOCUMENT = "empty_document"
REASON_LAYOUT_TABLE_ABUSE = "layout_table_abuse"
REASON_SHEET_TOO_SPARSE = "sheet_too_sparse"

REASON_LABELS_RU: dict[str, str] = {
    REASON_LEGACY_FORMAT: "формат .doc/.xls не обрабатывается (нужен OOXML)",
    REASON_LEGACY_UNCONVERTED: "формат .doc/.xls не обрабатывается (нужен OOXML)",
    REASON_LEGACY_STRUCTURE_LOSS: "формат .doc/.xls не обрабатывается (нужен OOXML)",
    REASON_EMBEDDED_ONLY: "почти нет текста — контент в OLE/картинках",
    REASON_ENCRYPTED: "файл защищён паролем / зашифрован",
    REASON_EMPTY_DOCUMENT: "пустой документ (нет текста и таблиц)",
    REASON_LAYOUT_TABLE_ABUSE: "документ свёрстан декоративными таблицами, сетки данных нет",
    REASON_SHEET_TOO_SPARSE: "лист Excel почти пустой при огромном диапазоне",
}


@dataclass
class OfficeUnitSuitability:
    """Результат проверки одной логической единицы (страница Word / лист Excel)."""

    suitable: bool
    reasons: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    unit_num: int = 1
    unit_kind: str = "page"  # page | sheet
    unit_name: str | None = None
    doc_type: str | None = None

    @property
    def page_num(self) -> int:
        """Алиас для совместимости с PDF ConversionResult."""
        return self.unit_num

    @property
    def reason_codes(self) -> str:
        return ",".join(self.reasons)


@dataclass
class OfficeSuitabilityStats:
    """Сводка отсева по прогону Office-документа."""

    total_units: int = 0
    accepted_units: int = 0
    rejected_units: int = 0
    reason_counts: Counter = field(default_factory=Counter)
    rejected_details: list[tuple[str, int, list[str]]] = field(default_factory=list)

    def record(self, source_name: str, result: OfficeUnitSuitability) -> None:
        self.total_units += 1
        if result.suitable:
            self.accepted_units += 1
        else:
            self.rejected_units += 1
            self.reason_counts.update(result.reasons)
            self.rejected_details.append(
                (source_name, result.unit_num, list(result.reasons))
            )


def rejected_unit_notice_html(result: OfficeUnitSuitability) -> str:
    """HTML-заглушка для отсеянной единицы."""
    reasons = result.messages or [
        REASON_LABELS_RU.get(r, r) for r in result.reasons
    ]
    items = "".join(f"<li>{html.escape(m)}</li>" for m in reasons)
    codes = html.escape(result.reason_codes)
    kind = "Лист" if result.unit_kind == "sheet" else "Страница"
    label = result.unit_name or str(result.unit_num)
    return (
        f'<div class="page-rejected" role="status" data-rejected="true" '
        f'data-reasons="{codes}">'
        f"<p><strong>{kind} {html.escape(str(label))} отсеян</strong> — "
        f"не пригоден для smart-конвертации (будет передан другой модели).</p>"
        f"<ul>{items}</ul>"
        f"</div>"
    )


def assess_text_and_structure(
    *,
    text: str,
    n_tables: int,
    n_images: int = 0,
    n_embedded: int = 0,
    unit_num: int = 1,
    unit_kind: str = "page",
    unit_name: str | None = None,
    legacy_structure_loss: bool = False,
    expect_tables: bool = False,
) -> OfficeUnitSuitability:
    """Базовая пригодность по тексту/таблицам/вложениям."""
    del legacy_structure_loss, expect_tables  # больше не конвертим legacy
    reasons: list[str] = []
    messages: list[str] = []
    compact = " ".join((text or "").split())
    n_chars = len(compact)

    if n_chars < 8 and n_tables == 0:
        if n_images + n_embedded > 0:
            reasons.append(REASON_EMBEDDED_ONLY)
            messages.append(REASON_LABELS_RU[REASON_EMBEDDED_ONLY])
        else:
            reasons.append(REASON_EMPTY_DOCUMENT)
            messages.append(REASON_LABELS_RU[REASON_EMPTY_DOCUMENT])

    if n_chars < 40 and n_images >= 2 and n_tables == 0:
        if REASON_EMBEDDED_ONLY not in reasons:
            reasons.append(REASON_EMBEDDED_ONLY)
            messages.append(REASON_LABELS_RU[REASON_EMBEDDED_ONLY])

    suitable = not reasons
    return OfficeUnitSuitability(
        suitable=suitable,
        reasons=reasons,
        messages=messages,
        unit_num=unit_num,
        unit_kind=unit_kind,
        unit_name=unit_name,
    )


def assess_sheet_density(
    *,
    n_rows: int,
    n_cols: int,
    n_nonempty: int,
    unit_num: int = 1,
    unit_name: str | None = None,
) -> OfficeUnitSuitability | None:
    """
    Отсев гигантских пустых диапазонов Excel.
    Возвращает suitability только если нужно отсеять; иначе None.
    """
    cells = max(1, n_rows * n_cols)
    density = n_nonempty / cells
    if n_rows >= 200 and n_cols >= 20 and density < 0.02 and n_nonempty < 80:
        return OfficeUnitSuitability(
            suitable=False,
            reasons=[REASON_SHEET_TOO_SPARSE],
            messages=[REASON_LABELS_RU[REASON_SHEET_TOO_SPARSE]],
            unit_num=unit_num,
            unit_kind="sheet",
            unit_name=unit_name,
        )
    return None


def assess_layout_table_abuse(
    *,
    n_tables: int,
    total_cells: int,
    nonempty_cells: int,
    text_len: int,
    unit_num: int = 1,
    unit_kind: str = "page",
    unit_name: str | None = None,
    form_like: bool = False,
) -> OfficeUnitSuitability | None:
    """
    Word: много крошечных layout-таблиц при малом полезном содержимом.

    Гигантские form-сетки (ТОРГ/КС) сюда не попадают — их режет
    ``office_table_regions``; reject только реально пустой декоративный мусор.
    """
    if form_like:
        # унифицированные формы: всегда пытаемся собрать HTML (после visual split)
        return None
    if n_tables < 8:
        return None
    if total_cells <= 0:
        return None
    density = nonempty_cells / max(1, total_cells)
    avg_cells = total_cells / n_tables
    if avg_cells <= 6 and density < 0.25 and text_len < 200:
        return OfficeUnitSuitability(
            suitable=False,
            reasons=[REASON_LAYOUT_TABLE_ABUSE],
            messages=[REASON_LABELS_RU[REASON_LAYOUT_TABLE_ABUSE]],
            unit_num=unit_num,
            unit_kind=unit_kind,
            unit_name=unit_name,
        )
    return None


def looks_like_form_document(text: str) -> bool:
    """Грубый признак унифицированной формы (КС/ТОРГ/СФ/УПД/баланс)."""
    import re

    return bool(
        re.search(
            r"форма\s*№|унифицированн|КС-?[23]|ТОРГ-?12|УПД|"
            r"сч[её]т\s*[-–]?\s*фактура|бухгалтерский\s+баланс|"
            r"ОКУД|032200[15]|0330212|071000",
            text or "",
            re.I,
        )
    )
