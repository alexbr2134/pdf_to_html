"""Детектор битых шрифтов / OCR-garble."""

from __future__ import annotations

import re
from typing import Any

from pdf_suitability.config import DEFAULT_CONFIG, SuitabilityConfig
from pdf_suitability.core import REASON_BROKEN_FONTS, REASON_LABELS_RU
from pdf_suitability.detectors.base import PageDetector


def _ru_bigram_score(word: str, config: SuitabilityConfig) -> int:
    """Число частых русских биграмм в слове."""
    w = word.lower().replace("ё", "е")
    return sum(1 for i in range(len(w) - 1) if w[i : i + 2] in config.ru_bigrams)


def _reversed_cyrillic_garble(text: str, config: SuitabilityConfig) -> bool:
    """
    True, если кириллица читается «задом наперёд» (частый баг ToUnicode/порядка
    глифов): extract_text даёт «еинежолирП», а в chars — «Приложение».
    """
    words = re.findall(r"[А-Яа-яЁё]{5,}", text)
    if len(words) < config.rev_garble_min_words:
        return False

    rev_better = 0
    lex_hits = 0
    for w in words:
        fwd = _ru_bigram_score(w, config)
        rev = _ru_bigram_score(w[::-1], config)
        if rev >= fwd + config.rev_bigram_margin:
            rev_better += 1
        if w[::-1].lower().replace("ё", "е") in config.ru_fin_lex:
            lex_hits += 1

    n = len(words)
    if (
        rev_better >= config.rev_better_min_count
        and rev_better / n >= config.rev_better_min_ratio
    ):
        return True
    if (
        lex_hits >= config.rev_lex_min_count
        and lex_hits / n >= config.rev_lex_min_ratio
    ):
        return True
    return False


def page_has_broken_fonts(
    page: Any, config: SuitabilityConfig | None = None
) -> bool:
    """
    True, если на странице битая кодировка шрифтов / OCR-garble
    (латиница вместо кириллицы, HiddenHorzOCR, перевёрнутая кириллица и т.п.).
    """
    cfg = config or DEFAULT_CONFIG
    chars = page.chars or []
    n_chars = len(chars)
    if n_chars < cfg.min_chars_for_font_check:
        return False

    ocr_font_hits = 0
    for ch in chars:
        fname = ch.get("fontname") or ""
        if re.search(r"OCR|HiddenHorz|HiddenVert|GlyphLess", fname, re.I):
            ocr_font_hits += 1

    if ocr_font_hits >= cfg.ocr_font_threshold:
        return True

    text = page.extract_text() or ""
    if not text.strip():
        return False

    if _reversed_cyrillic_garble(text, cfg):
        return True

    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < cfg.min_letters_for_cyrillic_check:
        return False

    cyr = sum(1 for ch in letters if "\u0400" <= ch <= "\u04FF")
    lat = sum(1 for ch in letters if ("A" <= ch <= "Z") or ("a" <= ch <= "z"))
    cyr_ratio = cyr / len(letters)
    lat_ratio = lat / len(letters)

    if lat_ratio >= cfg.lat_ratio_threshold and cyr_ratio <= cfg.cyr_ratio_threshold:
        return True

    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9~<>}{\]\[;:]+", text)
    garble = 0
    for w in words:
        if len(w) < 4:
            continue
        has_lat = bool(re.search(r"[A-Za-z]", w))
        has_cyr = bool(re.search(r"[А-Яа-яЁё]", w))
        if has_lat and has_cyr:
            garble += 1
        elif has_lat and not has_cyr:
            if re.search(r"[~<>}{\]\[]", w) or (
                len(w) >= 6 and sum(ch.isupper() for ch in w) / len(w) > 0.4
            ):
                garble += 1
    return garble >= cfg.garble_threshold and cyr_ratio < cfg.cyr_ratio_for_garble


class BrokenFontsDetector(PageDetector):
    """Детектор битых шрифтов."""

    @property
    def reason_code(self) -> str:
        return REASON_BROKEN_FONTS

    def detect(
        self, page: Any, config: SuitabilityConfig
    ) -> tuple[bool, str, str]:
        if page_has_broken_fonts(page, config):
            return True, self.reason_code, REASON_LABELS_RU[self.reason_code]
        return False, self.reason_code, ""


def document_has_broken_fonts(pdf, config: SuitabilityConfig | None = None) -> bool:
    """True, если хотя бы на одной странице PDF битые шрифты (совместимость)."""
    cfg = config or DEFAULT_CONFIG
    return any(page_has_broken_fonts(page, cfg) for page in pdf.pages)
