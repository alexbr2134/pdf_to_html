"""HTML-эвристики итогов ТОРГ-12."""

from __future__ import annotations

import re

from doc_type.config import DEFAULT_THRESHOLDS

def build_torg_totals_table_html(text: str) -> str | None:
    """
    Страница ТОРГ-12 только с итогами: «Итого / 34 / Х / 94422 / Х / 16996 / 111419».
    """
    if not re.search(r"\bИтого\b", text, re.I):
        return None
    if "<table" in text.lower():
        return None
    # вытащим числа после слова Итого
    m = re.search(r"Итого\s*(.+)$", text, re.I | re.S)
    if not m:
        return None
    tokens = re.findall(
        r"Х|X|х|x|\d[\d\s\xa0]*[.,]\d{2}|\d{2,}",
        m.group(1),
    )
    tokens = [" ".join(t.split()) for t in tokens]
    if len(tokens) < DEFAULT_THRESHOLDS.torg_totals_min_tokens:
        return None
    # канон: qty, Х?, sum_wo_vat, Х?, vat, sum_with_vat
    labels = [
        "Количество мест / кол-во",
        "Масса (служебн.)",
        "Сумма без НДС",
        "НДС (служебн.)",
        "Сумма НДС",
        "Сумма с НДС",
    ]
    # если ровно 5 токенов без второго Х — сдвинем
    pairs: list[tuple[str, str]] = [("Показатель", "Итого")]
    for lab, tok in zip(labels, tokens):
        pairs.append((lab, tok))

    def esc(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    out = ['<table data-heuristic="torg12-totals">', "<tbody>"]
    for lab, val in pairs:
        out.append(f"<tr><th>{esc(lab)}</th><td>{esc(val)}</td></tr>")
    out.append("</tbody></table>")
    return "\n".join(out)
