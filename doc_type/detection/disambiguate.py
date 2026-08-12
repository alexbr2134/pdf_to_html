"""Дизамбигуация близких типов документов."""

from __future__ import annotations

import re

from doc_type.config import DEFAULT_THRESHOLDS, DetectionThresholds
from doc_type.core import DocType


def disambiguate_upd_vs_sf(
    scores: dict[DocType, float],
    raw: str,
    thr: DetectionThresholds = DEFAULT_THRESHOLDS,
) -> None:
    updish = bool(
        re.search(
            r"\bУПД\b|Универсальный\s+передаточн|передаточный\s+документ|"
            r"Статус\s*:\s*[12]|ММВ-20-3/96|счет-фактура\s+и\s+передаточн",
            raw,
            re.I,
        )
    )
    if scores[DocType.INVOICE_SF] > 0 and (scores[DocType.UPD] > 0 or updish):
        if updish:
            scores[DocType.UPD] += thr.upd_bonus
            scores[DocType.INVOICE_SF] *= thr.invoice_sf_dampen
        elif scores[DocType.UPD] > 0:
            scores[DocType.UPD] *= thr.upd_dampen_when_both


def disambiguate_ks2_vs_ks3(
    scores: dict[DocType, float],
    raw: str,
    thr: DetectionThresholds = DEFAULT_THRESHOLDS,
) -> None:
    if scores[DocType.KS2] and scores[DocType.KS3]:
        if re.search(r"КС-?3|0322001|СТОИМОСТИ\s+ВЫПОЛНЕННЫХ", raw, re.I):
            scores[DocType.KS3] += thr.ks_disambiguate_bonus
            scores[DocType.KS2] *= thr.ks_disambiguate_dampen
        elif re.search(r"КС-?2|0322005|ПРИЕМК", raw, re.I):
            scores[DocType.KS2] += thr.ks_disambiguate_bonus
            scores[DocType.KS3] *= thr.ks_disambiguate_dampen
