"""Основные типы и коды причин отсева страниц."""

from __future__ import annotations

from dataclasses import dataclass, field


REASON_BROKEN_FONTS = "broken_fonts"
REASON_UNMARKED_TABLE_LINES = "unmarked_table_lines"
REASON_IMAGE_ONLY_SCAN = "image_only_scan"

REASON_LABELS_RU: dict[str, str] = {
    REASON_BROKEN_FONTS: "битые шрифты / плохо размеченный текст (OCR-garble)",
    REASON_UNMARKED_TABLE_LINES: (
        "сложная таблица без векторных линий "
        "(много colspan/rowspan; для non-rsbu также крупный размер)"
    ),
    REASON_IMAGE_ONLY_SCAN: "страница почти без текстового слоя (скан/картинка)",
    # Office (см. office_suitability.py) — общие labels для RejectedPage
    "legacy_format": "формат .doc/.xls не обрабатывается (нужен OOXML)",
    "legacy_unconverted": "формат .doc/.xls не обрабатывается (нужен OOXML)",
    "legacy_structure_loss": "формат .doc/.xls не обрабатывается (нужен OOXML)",
    "embedded_only": "почти нет текста — контент в OLE/картинках",
    "encrypted": "файл защищён паролем / зашифрован",
    "empty_document": "пустой документ (нет текста и таблиц)",
    "layout_table_abuse": "документ свёрстан декоративными таблицами",
    "sheet_too_sparse": "лист Excel почти пустой при огромном диапазоне",
}


@dataclass
class PageSuitability:
    """Результат проверки одной страницы."""

    suitable: bool
    reasons: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    page_num: int = 1

    @property
    def reason_codes(self) -> str:
        """Причины через запятую для HTML data-атрибута."""
        return ",".join(self.reasons)


def merge_page_suitability(
    base: PageSuitability,
    extra: PageSuitability | None,
) -> PageSuitability:
    """Объединяет два результата проверки (порядок причин сохраняется)."""
    if extra is None or extra.suitable:
        return base
    reasons = list(base.reasons)
    messages = list(base.messages)
    seen = set(reasons)
    for r, m in zip(extra.reasons, extra.messages):
        if r in seen:
            continue
        seen.add(r)
        reasons.append(r)
        messages.append(m)
    return PageSuitability(
        suitable=not reasons,
        reasons=reasons,
        messages=messages,
        page_num=base.page_num,
    )


def suitability_unmarked_complex_spans(page_num: int = 1) -> PageSuitability:
    """PageSuitability для отсева по сложным/крупным таблицам без векторных линий."""
    return PageSuitability(
        suitable=False,
        reasons=[REASON_UNMARKED_TABLE_LINES],
        messages=[REASON_LABELS_RU[REASON_UNMARKED_TABLE_LINES]],
        page_num=page_num,
    )
