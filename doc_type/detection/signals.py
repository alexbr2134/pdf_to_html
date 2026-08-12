"""Сигналы и path-hints для детекции типа."""

from __future__ import annotations

import re

from doc_type.core import DocType

TYPE_SIGNALS: list[tuple[DocType, float, re.Pattern[str]]] = [
    (DocType.RSBU, 1.2, re.compile(r"Бухгалтерский\s+баланс", re.I)),
    (DocType.RSBU, 1.2, re.compile(r"Отч[её]т\s+о\s+финансовых\s+результатах", re.I)),
    (DocType.RSBU, 1.0, re.compile(r"Отч[её]т\s+о\s+движении\s+денежных\s+средств", re.I)),
    (DocType.RSBU, 1.0, re.compile(r"Форма\s+по\s+ОКУД\s*0710\d{3}", re.I)),
    (DocType.RSBU, 0.8, re.compile(r"\b071000[1-5]\b")),
    (DocType.RSBU, 0.6, re.compile(r"Итого\s+по\s+разделу\s+[IVX]+", re.I)),
    (DocType.KS2, 1.3, re.compile(r"форма\s*№\s*КС-?2\b", re.I)),
    (DocType.KS2, 1.1, re.compile(r"\bКС-?2\b")),
    (DocType.KS2, 1.0, re.compile(r"0322005")),
    (DocType.KS2, 0.9, re.compile(r"ПРИЕМК[ЕА]\s+ВЫПОЛНЕННЫХ\s+РАБОТ", re.I)),
    (DocType.KS3, 1.3, re.compile(r"форма\s*№\s*КС-?3\b", re.I)),
    (DocType.KS3, 1.1, re.compile(r"\bКС-?3\b")),
    (DocType.KS3, 1.0, re.compile(r"0322001")),
    (DocType.KS3, 0.9, re.compile(r"СТОИМОСТИ\s+ВЫПОЛНЕННЫХ\s+РАБОТ", re.I)),
    (DocType.INVOICE_SF, 1.0, re.compile(r"Сч[её]т\s*[-–]?\s*фактура", re.I)),
    (DocType.INVOICE_SF, 0.8, re.compile(r"постановлени[юя]\s+Правительства[\s\S]{0,40}1137", re.I)),
    (DocType.TORG12, 1.3, re.compile(r"форма\s*№\s*ТОРГ-?12\b", re.I)),
    (DocType.TORG12, 1.1, re.compile(r"\bТОРГ-?12\b", re.I)),
    (DocType.TORG12, 1.0, re.compile(r"\b330212\b")),
    (DocType.TORG12, 0.7, re.compile(r"ТОВАРНАЯ\s+НАКЛАДНАЯ", re.I)),
    (DocType.UPD, 1.3, re.compile(r"Универсальный\s+передаточный\s+документ", re.I)),
    (DocType.UPD, 1.1, re.compile(r"\bУПД\b")),
    (DocType.UPD, 0.9, re.compile(r"Статус:\s*[12]\s*[–-]\s*(?:счет|передаточн)", re.I)),
    (DocType.UPD, 0.7, re.compile(r"ММВ-20-3/96", re.I)),
]

PATH_HINTS: list[tuple[DocType, re.Pattern[str]]] = [
    (DocType.RSBU, re.compile(r"(?:^|[/\\])РСБУ(?:[/\\]|$)", re.I)),
    (DocType.KS2, re.compile(r"(?:^|[/\\])кс-?2(?:[/\\]|$)", re.I)),
    (DocType.KS3, re.compile(r"(?:^|[/\\])кс-?3(?:[/\\]|$)", re.I)),
    (DocType.INVOICE_SF, re.compile(r"(?:^|[/\\])счет-?фактура(?:[/\\]|$)", re.I)),
    (DocType.TORG12, re.compile(r"(?:^|[/\\])торг\s*12(?:[/\\]|$)", re.I)),
    (DocType.UPD, re.compile(r"(?:^|[/\\])упд(?:[/\\]|$)", re.I)),
]

_TYPE_SIGNALS = TYPE_SIGNALS
_PATH_HINTS = PATH_HINTS
