"""Пороги детекции типа документа."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DetectionThresholds:
    """Пороги для определения типа документа."""

    min_confidence_for_type: float = 0.8
    min_confidence_known: float = 0.45
    path_bonus_confidence: float = 0.55
    fallback_confidence: float = 0.55
    tiebreak_confidence: float = 0.5
    path_only_min_score: float = 0.5
    path_bonus_score: float = 0.55
    path_upd_over_sf_bonus: float = 0.8
    path_invoice_sf_dampen: float = 0.4
    upd_bonus: float = 1.2
    invoice_sf_dampen: float = 0.35
    upd_dampen_when_both: float = 0.5
    ks_disambiguate_bonus: float = 0.5
    ks_disambiguate_dampen: float = 0.5
    close_scores_gap: float = 0.3
    conf_second_smooth: float = 0.5
    min_page_text_for_extract: int = 40
    invoice_field_min_markers: int = 4
    invoice_field_min_rows: int = 4
    invoice_field_max_value_len: int = 300
    torg_totals_min_tokens: int = 3
    line_item_min_index_nums: int = 6
    line_item_min_dataish_rows: int = 3
    rsbu_min_code_hits: int = 3


DEFAULT_THRESHOLDS = DetectionThresholds()
