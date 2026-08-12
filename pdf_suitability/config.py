"""Конфигурация и пороги проверки пригодности страниц."""

from __future__ import annotations

from dataclasses import dataclass, field


def _default_ru_bigrams() -> frozenset[str]:
    return frozenset(
        {
            "ст", "но", "ен", "ов", "ни", "на", "ра", "то", "ер", "ро", "ан", "ко",
            "ал", "по", "ол", "ор", "ес", "ос", "те", "ле", "ск", "ин", "ль", "во",
            "ка", "пр", "ре", "не", "ли", "та", "от", "ат", "ии", "ие", "ой", "ый",
            "ть", "ся", "ци", "че", "ва", "де", "го", "ем", "ам", "ом", "ум", "им",
            "ел", "ла", "ри", "ти", "тр", "за", "из", "ых", "их", "ее",
        }
    )


def _default_ru_fin_lex() -> frozenset[str]:
    return frozenset(
        {
            "приложение", "приказу", "министерства", "финансов", "российской",
            "федерации", "форма", "бухгалтерского", "баланса", "декабря",
            "наименование", "показателя", "показатель", "капитал", "уставный",
            "добавочный", "резервный", "нераспределенная", "прибыль",
            "корректировки", "изменением", "учетной", "политики", "исправлением",
            "ошибок", "чистые", "активы", "дивиденды", "реорганизация",
            "уменьшение", "количество", "акций", "отчет", "изменениях",
            "организация", "юридического", "лица", "пояснения", "актив", "пассив",
        }
    )


# Жёсткость роутинга unmarked_table_lines ∈ [0, 1]:
#   0 — не роутить такие таблицы никогда;
#   1 — роутить любую страницу, где линии брались с растра и есть таблицы;
#   DEFAULT — текущие пороги + type-policy (как до параметра).
DEFAULT_UNMARKED_ROUTING_STRICTNESS = 0.5


@dataclass(frozen=True)
class SuitabilityConfig:
    """Конфигурация проверки пригодности страниц."""

    # Пороги для broken_fonts
    min_chars_for_font_check: int = 50
    ocr_font_threshold: int = 20
    min_letters_for_cyrillic_check: int = 80
    lat_ratio_threshold: float = 0.55
    cyr_ratio_threshold: float = 0.20
    garble_threshold: int = 15
    cyr_ratio_for_garble: float = 0.35
    # перевёрнутая кириллица
    rev_garble_min_words: int = 10
    rev_better_min_count: int = 6
    rev_better_min_ratio: float = 0.28
    rev_lex_min_count: int = 6
    rev_lex_min_ratio: float = 0.12
    rev_bigram_margin: int = 2

    # Пороги для image_only_scan
    min_chars_for_text: int = 40
    min_image_cover: float = 0.35

    # Пороги для таблиц (base @ DEFAULT strictness)
    span_cells_route: int = 4
    span_absorbed_route: int = 7
    span_cells_large_route: int = 3
    span_max_large_route: int = 5
    size_min_cols: int = 5
    size_min_rows: int = 15
    size_min_cells: int = 120
    size_min_cells_hard: int = 200

    # unmarked strictness policy
    default_unmarked_strictness: float = DEFAULT_UNMARKED_ROUTING_STRICTNESS
    high_strictness_without_raster: float = 0.85
    bypass_disable_t: float = 0.7
    force_route_large_t: float = 0.4
    force_route_spans_t: float = 0.55

    # Лексиконы
    ru_bigrams: frozenset[str] = field(default_factory=_default_ru_bigrams)
    ru_fin_lex: frozenset[str] = field(default_factory=_default_ru_fin_lex)


DEFAULT_CONFIG = SuitabilityConfig()
