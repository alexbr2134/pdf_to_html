"""HTML-эвристики полей счёта-фактуры / УПД."""

from __future__ import annotations

import re

from doc_type.config import DEFAULT_THRESHOLDS
from doc_type.constants import INVOICE_FIELD_LABELS

def _plain_text_from_html(html_body: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html_body)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>", "\n", text)
    text = re.sub(r"(?is)</tr\s*>", "\n", text)
    text = re.sub(r"(?is)</(div|li|h\d)\s*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def build_invoice_fields_table_html(text: str) -> str | None:
    """
    Из prose с маркерами (1)/(2а)/… собирает 2-колоночную таблицу полей.
    Возвращает None, если маркеров мало (не пустая форма СФ/УПД).
    """
    if not text or text.count("(") < DEFAULT_THRESHOLDS.invoice_field_min_markers:
        return None
    # маркеры вида (1), (1а), (2б)
    parts = re.split(r"(?=\(\s*\d+[аaбb]?\s*\))", text)
    rows: list[tuple[str, str, str]] = []
    for part in parts:
        m = re.match(r"\(\s*(\d+[аaбb]?)\s*\)\s*(.*)$", part, re.S)
        if not m:
            continue
        code = m.group(1).lower().replace("a", "а").replace("b", "б")
        val = " ".join(m.group(2).split())
        # обрезать следующий заголовок-мусор
        val = re.split(
            r"\b(?:Продавец|Покупатель|Адрес|ИНН/КПП|Грузоотправитель|"
            r"Грузополучатель|Валюта|Исправление|Счет-фактура|Статус)\s*:\s*$",
            val,
        )[0].strip(" :;—-")
        # не затягивать шапку товарной таблицы в поле (5)/(5а)
        val = re.split(
            r"(?i)(?:Количественная\s+единица|Наименование\s+товара|"
            r"Код\s+товара\s*/|Единица\s+измерен)",
            val,
            maxsplit=1,
        )[0].strip(" :;—-")
        label = INVOICE_FIELD_LABELS.get(code, f"Поле ({code})")
        if len(val) > DEFAULT_THRESHOLDS.invoice_field_max_value_len:
            val = val[: DEFAULT_THRESHOLDS.invoice_field_max_value_len] + "…"
        rows.append((code, label, val))
    if len(rows) < DEFAULT_THRESHOLDS.invoice_field_min_rows:
        return None

    def esc(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    out = [
        '<table data-heuristic="invoice-fields">',
        "<thead><tr><th>Код</th><th>Поле</th><th>Значение</th></tr></thead>",
        "<tbody>",
    ]
    for code, label, val in rows:
        out.append(
            "<tr>"
            f"<td>{esc(code)}</td>"
            f"<td>{esc(label)}</td>"
            f"<td>{esc(val)}</td>"
            "</tr>"
        )
    out.append("</tbody></table>")
    return "\n".join(out)
