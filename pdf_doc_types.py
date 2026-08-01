"""
Распознавание типа документа и type-specific эвристики smart-пайплайна.

Если тип не определён уверенно — DocType.UNKNOWN → дефолтное поведение.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class DocType(str, Enum):
    """Поддерживаемые классы документов (v2 + типичные samples)."""

    RSBU = "rsbu"
    KS2 = "ks2"
    KS3 = "ks3"
    INVOICE_SF = "invoice_sf"
    TORG12 = "torg12"
    UPD = "upd"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DocTypeResult:
    """Результат детекции типа."""

    doc_type: DocType
    confidence: float  # 0..1
    signals: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        """True, если тип определён не как unknown."""
        return self.doc_type != DocType.UNKNOWN and self.confidence >= 0.45


@dataclass(frozen=True)
class RoutingPolicy:
    """Политика post-check роутинга unmarked_table_lines."""

    # Роутить крупные таблицы (rows/cols/cells) при raster vectorized.
    route_large_tables: bool = True
    # Роутить по сложным colspan/rowspan.
    # False для СФ/УПД/ТОРГ: многоуровневая шапка — норма, не сигнал «сломано».
    route_complex_spans: bool = True
    # Не роутить сетки, похожие на строки работ/товаров (КС-2/КС-3 сметы).
    keep_line_item_grids: bool = False


_DEFAULT_ROUTING = RoutingPolicy()

_ROUTING_BY_TYPE: dict[DocType, RoutingPolicy] = {
    # Крупный баланс ок; странные span — роут.
    DocType.RSBU: RoutingPolicy(route_large_tables=False, route_complex_spans=True),
    # Сметы часто крупные/с colspan шапки, но строки работ smart читает.
    DocType.KS2: RoutingPolicy(
        route_large_tables=False, route_complex_spans=True, keep_line_item_grids=True
    ),
    DocType.KS3: RoutingPolicy(
        route_large_tables=False, route_complex_spans=True, keep_line_item_grids=True
    ),
    # Шапка счёта-фактуры/УПД/ТОРГ всегда с rowspan/colspan.
    DocType.INVOICE_SF: RoutingPolicy(
        route_large_tables=False, route_complex_spans=False
    ),
    DocType.TORG12: RoutingPolicy(
        route_large_tables=False, route_complex_spans=False
    ),
    DocType.UPD: RoutingPolicy(
        route_large_tables=False, route_complex_spans=False
    ),
    DocType.UNKNOWN: _DEFAULT_ROUTING,
}


# (doc_type, weight, pattern) — вес сигнала для скоринга
_TYPE_SIGNALS: list[tuple[DocType, float, re.Pattern[str]]] = [
    # РСБУ
    (DocType.RSBU, 1.2, re.compile(r"Бухгалтерский\s+баланс", re.I)),
    (DocType.RSBU, 1.2, re.compile(r"Отч[её]т\s+о\s+финансовых\s+результатах", re.I)),
    (DocType.RSBU, 1.0, re.compile(r"Отч[её]т\s+о\s+движении\s+денежных\s+средств", re.I)),
    (DocType.RSBU, 1.0, re.compile(r"Форма\s+по\s+ОКУД\s*0710\d{3}", re.I)),
    (DocType.RSBU, 0.8, re.compile(r"\b071000[1-5]\b")),
    (DocType.RSBU, 0.6, re.compile(r"Итого\s+по\s+разделу\s+[IVX]+", re.I)),
    # КС-2
    (DocType.KS2, 1.3, re.compile(r"форма\s*№\s*КС-?2\b", re.I)),
    (DocType.KS2, 1.1, re.compile(r"\bКС-?2\b")),
    (DocType.KS2, 1.0, re.compile(r"0322005")),
    (DocType.KS2, 0.9, re.compile(r"ПРИЕМК[ЕА]\s+ВЫПОЛНЕННЫХ\s+РАБОТ", re.I)),
    # КС-3
    (DocType.KS3, 1.3, re.compile(r"форма\s*№\s*КС-?3\b", re.I)),
    (DocType.KS3, 1.1, re.compile(r"\bКС-?3\b")),
    (DocType.KS3, 1.0, re.compile(r"0322001")),
    (DocType.KS3, 0.9, re.compile(r"СТОИМОСТИ\s+ВЫПОЛНЕННЫХ\s+РАБОТ", re.I)),
    # Счёт-фактура (не УПД)
    (DocType.INVOICE_SF, 1.0, re.compile(r"Сч[её]т\s*[-–]?\s*фактура", re.I)),
    (DocType.INVOICE_SF, 0.8, re.compile(r"постановлени[юя]\s+Правительства[\s\S]{0,40}1137", re.I)),
    # ТОРГ-12
    (DocType.TORG12, 1.3, re.compile(r"форма\s*№\s*ТОРГ-?12\b", re.I)),
    (DocType.TORG12, 1.1, re.compile(r"\bТОРГ-?12\b", re.I)),
    (DocType.TORG12, 1.0, re.compile(r"\b330212\b")),
    (DocType.TORG12, 0.7, re.compile(r"ТОВАРНАЯ\s+НАКЛАДНАЯ", re.I)),
    # УПД
    (DocType.UPD, 1.3, re.compile(r"Универсальный\s+передаточный\s+документ", re.I)),
    (DocType.UPD, 1.1, re.compile(r"\bУПД\b")),
    (DocType.UPD, 0.9, re.compile(r"Статус:\s*[12]\s*[–-]\s*(?:счет|передаточн)", re.I)),
    (DocType.UPD, 0.7, re.compile(r"ММВ-20-3/96", re.I)),
]

# Слабые подсказки по пути (папка v2/…) — только при близких скорах
_PATH_HINTS: list[tuple[DocType, re.Pattern[str]]] = [
    (DocType.RSBU, re.compile(r"(?:^|[/\\])РСБУ(?:[/\\]|$)", re.I)),
    (DocType.KS2, re.compile(r"(?:^|[/\\])кс-?2(?:[/\\]|$)", re.I)),
    (DocType.KS3, re.compile(r"(?:^|[/\\])кс-?3(?:[/\\]|$)", re.I)),
    (DocType.INVOICE_SF, re.compile(r"(?:^|[/\\])счет-?фактура(?:[/\\]|$)", re.I)),
    (DocType.TORG12, re.compile(r"(?:^|[/\\])торг\s*12(?:[/\\]|$)", re.I)),
    (DocType.UPD, re.compile(r"(?:^|[/\\])упд(?:[/\\]|$)", re.I)),
]


def routing_policy_for(doc_type: DocType | str | None) -> RoutingPolicy:
    """Политика роутинга для типа (unknown → дефолт)."""
    if doc_type is None:
        return _DEFAULT_ROUTING
    if isinstance(doc_type, str):
        try:
            doc_type = DocType(doc_type)
        except ValueError:
            return _DEFAULT_ROUTING
    return _ROUTING_BY_TYPE.get(doc_type, _DEFAULT_ROUTING)


def _page_text(page) -> str:
    """Текст страницы для эвристик (короткий / полный)."""
    try:
        text = page.extract_text() or ""
    except Exception:
        text = ""
    if len(text) < 40:
        # fallback: склеить слова
        try:
            words = page.extract_words() or []
            text = " ".join(w.get("text", "") for w in words)
        except Exception:
            pass
    return text


def detect_doc_type(
    page=None,
    *,
    text: str | None = None,
    pdf_path: str | None = None,
    fallback: DocType | str | None = None,
) -> DocTypeResult:
    """
    Определяет тип документа по тексту страницы (и слабо — по пути файла).

    fallback — тип с предыдущей страницы того же PDF (для приложений без шапки).
    """
    raw = text if text is not None else (_page_text(page) if page is not None else "")
    # УПД часто содержит «Счет-фактура» в шапке — сначала снимем явный УПД
    scores: dict[DocType, float] = {dt: 0.0 for dt in DocType if dt != DocType.UNKNOWN}
    hit_signals: list[str] = []

    for dt, weight, pat in _TYPE_SIGNALS:
        if pat.search(raw):
            scores[dt] += weight
            hit_signals.append(f"{dt.value}:{pat.pattern[:40]}")

    # Дизамбигуация УПД vs счёт-фактура (УПД почти всегда содержит «Счет-фактура»)
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
            scores[DocType.UPD] += 1.2
            scores[DocType.INVOICE_SF] *= 0.35
        elif scores[DocType.UPD] > 0:
            scores[DocType.UPD] *= 0.5

    # КС-2 vs КС-3
    if scores[DocType.KS2] and scores[DocType.KS3]:
        if re.search(r"КС-?3|0322001|СТОИМОСТИ\s+ВЫПОЛНЕННЫХ", raw, re.I):
            scores[DocType.KS3] += 0.5
            scores[DocType.KS2] *= 0.5
        elif re.search(r"КС-?2|0322005|ПРИЕМК", raw, re.I):
            scores[DocType.KS2] += 0.5
            scores[DocType.KS3] *= 0.5

    path_bonus = 0.0
    path_type: DocType | None = None
    if pdf_path:
        for dt, pat in _PATH_HINTS:
            if pat.search(str(pdf_path)):
                path_type = dt
                path_bonus = 0.55
                scores[dt] += path_bonus
                hit_signals.append(f"path:{dt.value}")
                break
        # папка упд + текст счета-фактуры → всё же УПД
        if path_type == DocType.UPD and scores[DocType.INVOICE_SF] > 0:
            scores[DocType.UPD] += 0.8
            scores[DocType.INVOICE_SF] *= 0.4
            hit_signals.append("path_upd_over_sf")

    best_type = DocType.UNKNOWN
    best_score = 0.0
    second = 0.0
    for dt, sc in scores.items():
        if sc > best_score:
            second = best_score
            best_score = sc
            best_type = dt
        elif sc > second:
            second = sc

    if best_score < 0.8:
        # продолжение документа: хватает папки v2/кс-2 и т.п.
        if path_type is not None and best_type == path_type and best_score >= 0.5:
            return DocTypeResult(
                path_type, 0.55, tuple(hit_signals) + ("path_only",)
            )
        # слабый сигнал — fallback с прошлой страницы
        if fallback is not None:
            fb = DocType(fallback) if isinstance(fallback, str) else fallback
            if fb != DocType.UNKNOWN:
                return DocTypeResult(fb, 0.55, tuple(hit_signals) + ("fallback",))
        return DocTypeResult(DocType.UNKNOWN, 0.0, tuple(hit_signals))

    # нормализуем confidence
    conf = min(1.0, best_score / (best_score + second + 0.5))
    if best_score - second < 0.3 and path_type and path_type == best_type:
        conf = max(conf, 0.55)
    if conf < 0.45:
        if path_type is not None and best_type == path_type:
            return DocTypeResult(
                path_type, 0.5, tuple(hit_signals) + ("path_tiebreak",)
            )
        if fallback is not None:
            fb = DocType(fallback) if isinstance(fallback, str) else fallback
            if fb != DocType.UNKNOWN:
                return DocTypeResult(fb, 0.55, tuple(hit_signals) + ("fallback",))
        return DocTypeResult(DocType.UNKNOWN, conf, tuple(hit_signals))

    return DocTypeResult(best_type, conf, tuple(hit_signals))


# --- РСБУ: починка сползания раздела / строки показателя ---

_SECTION_ONLY_RE = re.compile(
    r"^(?:"
    r"АКТИВ|ПАССИВ|"
    r"(?:АКТИВ|ПАССИВ)\s+[IVXLCХ]+\.\s+.+"
    r"|[IVXLCХ]+\.\s+[А-ЯЁA-Z].+"
    r")$",
    re.I,
)

# Раздел (КАПС / римский номер) + показатель (с строчной после заглавной).
# Не отрезаем «АКТИВЫ» от «ВНЕОБОРОТНЫЕ АКТИВЫ».
_SECTION_SPLIT_RE = re.compile(
    r"^(?P<section>"
    r"(?:АКТИВ|ПАССИВ)"
    r"(?:\s+[IVXLCХ]+\.\s+[А-ЯЁ][А-ЯЁ0-9\-\s«»\"]*[А-ЯЁ])?"
    r"|[IVXLCХ]+\.\s+[А-ЯЁ][А-ЯЁ0-9\-\s«»\"]*[А-ЯЁ]"
    r")\s+(?P<item>[А-ЯЁA-Z][а-яё].+)$"
)

_CODE_RE = re.compile(r"^\d{4}$")


def _cell_text(cell: Any) -> str:
    return (getattr(cell, "text", None) or "").strip()


def _rsbu_find_code_col(grid: list[list[Any]]) -> int | None:
    """Колонка с 4-значными кодами строк баланса."""
    if not grid:
        return None
    width = max(len(r) for r in grid)
    best_col, best_n = None, 0
    for col in range(width):
        n = 0
        for row in grid:
            if col >= len(row):
                continue
            if _CODE_RE.fullmatch(_cell_text(row[col])):
                n += 1
        if n > best_n:
            best_n, best_col = n, col
    return best_col if best_n >= 3 else None


def _rsbu_label_col(code_col: int) -> int:
    """Колонка наименования — обычно сразу слева от кода."""
    return max(0, code_col - 1)


def _is_section_only_label(text: str) -> bool:
    t = " ".join((text or "").split())
    if not t or _CODE_RE.fullmatch(t):
        return False
    if re.match(r"^Итого\s+по\s+разделу\b", t, re.I):
        return False
    if re.match(r"^БАЛАНС\b", t, re.I):
        return False
    return bool(_SECTION_ONLY_RE.match(t))


def _split_section_prefix(text: str) -> tuple[str, str] | None:
    """Делит «I. РАЗДЕЛ Название показателя» → (раздел, показатель)."""
    t = " ".join((text or "").split())
    if not t or re.match(r"^Итого\s+по\s+разделу\b", t, re.I):
        return None
    m = _SECTION_SPLIT_RE.match(t)
    if not m:
        return None
    section = " ".join(m.group("section").split())
    item = " ".join(m.group("item").split())
    if len(item) < 3 or _is_section_only_label(item):
        return None
    # не отрезать слишком короткий «раздел»
    if len(section) < 3:
        return None
    return section, item


def _row_has_line_code(row: list[Any], code_col: int) -> bool:
    if code_col >= len(row):
        return False
    return bool(_CODE_RE.fullmatch(_cell_text(row[code_col])))


def _row_has_numeric_values(row: list[Any], after_col: int) -> bool:
    """Есть ли числа справа от code_col (суммы периода)."""
    for c in row[after_col + 1 :]:
        t = _cell_text(c).replace(" ", "").replace("\xa0", "")
        if not t or t in {"-", "—", "Х", "X", "x", "х"}:
            continue
        if re.search(r"\d", t):
            return True
    return False


def _clear_row_values(row: list[Any], from_col: int) -> None:
    """Очищает код и значения в строке (оставляет подпись слева)."""
    for c in range(from_col, len(row)):
        cell = row[c]
        cell.text = ""
        if hasattr(cell, "words"):
            cell.words = []


def _move_row_values(src: list[Any], dst: list[Any], from_col: int) -> None:
    """Переносит код/значения из src в dst (с from_col)."""
    for c in range(from_col, max(len(src), len(dst))):
        if c >= len(src) or c >= len(dst):
            continue
        sc, dc = src[c], dst[c]
        if _cell_text(dc) and not _cell_text(sc):
            continue
        dc.text = sc.text
        if hasattr(sc, "words") and hasattr(dc, "words"):
            dc.words = list(sc.words or [])
        if getattr(sc, "bbox", None) is not None:
            dc.bbox = sc.bbox
        for attr in ("colspan", "rowspan", "covered"):
            if hasattr(sc, attr) and hasattr(dc, attr):
                setattr(dc, attr, getattr(sc, attr))
        sc.text = ""
        if hasattr(sc, "words"):
            sc.words = []


def _clone_empty_row_like(row: list[Any], label_text: str, label_col: int) -> list[Any]:
    """Новая строка-клон структуры с подписью раздела."""
    from copy import copy

    new_row: list[Any] = []
    for i, cell in enumerate(row):
        nc = copy(cell)
        if i == label_col:
            nc.text = label_text
            if hasattr(nc, "words"):
                nc.words = []
        else:
            nc.text = ""
            if hasattr(nc, "words"):
                nc.words = []
            if hasattr(nc, "covered"):
                nc.covered = False
            if hasattr(nc, "colspan"):
                nc.colspan = 1
            if hasattr(nc, "rowspan"):
                nc.rowspan = 1
        if hasattr(nc, "row"):
            pass  # индексы пересчитает caller при необходимости
        new_row.append(nc)
    return new_row


def fix_rsbu_section_value_shift(
    grid: list[list[Any]],
    kinds: list[str] | None = None,
) -> tuple[list[list[Any]], list[str] | None]:
    """
    Чинит сползание строк РСБУ-баланса:

    1) «I. РАЗДЕЛ Название» + код/суммы → строка раздела + строка показателя.
    2) Строка-раздел с кодом/суммами + следующая строка-показатель без кода
       → перенос кода/сумм на показатель.

    Не трогает «Итого по разделу» / «БАЛАНС».
    """
    if not grid:
        return grid, kinds

    code_col = _rsbu_find_code_col(grid)
    if code_col is None:
        return grid, kinds
    label_col = _rsbu_label_col(code_col)

    new_grid: list[list[Any]] = []
    new_kinds: list[str] = []
    kind_list = list(kinds) if kinds is not None else ["DATA"] * len(grid)

    i = 0
    while i < len(grid):
        row = grid[i]
        kind = kind_list[i] if i < len(kind_list) else "DATA"
        label = _cell_text(row[label_col]) if label_col < len(row) else ""
        has_code = _row_has_line_code(row, code_col)
        has_vals = _row_has_numeric_values(row, code_col) or has_code

        # --- case 1: section prefix glued to item name ---
        split = _split_section_prefix(label) if label else None
        if split and has_code:
            section, item = split
            sec_row = _clone_empty_row_like(row, section, label_col)
            row[label_col].text = item
            if hasattr(row[label_col], "words"):
                # текст уже правильный; слова старой склейки лучше сбросить
                row[label_col].words = []
            new_grid.append(sec_row)
            new_kinds.append("HEADER" if kind == "HEADER" else "DATA")
            new_grid.append(row)
            new_kinds.append(kind)
            i += 1
            continue

        # --- case 2: section-only label holding next item's values ---
        if (
            _is_section_only_label(label)
            and has_vals
            and i + 1 < len(grid)
        ):
            nxt = grid[i + 1]
            nxt_label = _cell_text(nxt[label_col]) if label_col < len(nxt) else ""
            nxt_has_code = _row_has_line_code(nxt, code_col)
            nxt_is_section = _is_section_only_label(nxt_label) or bool(
                _split_section_prefix(nxt_label)
            )
            if (
                nxt_label
                and not nxt_has_code
                and not nxt_is_section
                and not re.match(r"^Итого\s+по\s+разделу\b", nxt_label, re.I)
            ):
                _move_row_values(row, nxt, code_col)
                new_grid.append(row)
                new_kinds.append(kind)
                new_grid.append(nxt)
                new_kinds.append(kind_list[i + 1] if i + 1 < len(kind_list) else "DATA")
                i += 2
                continue

        new_grid.append(row)
        new_kinds.append(kind)
        i += 1

    if kinds is None:
        return new_grid, None
    return new_grid, new_kinds


def _as_doc_type(doc_type: DocType | str | None) -> DocType:
    if isinstance(doc_type, DocType):
        return doc_type
    if isinstance(doc_type, str):
        try:
            return DocType(doc_type)
        except ValueError:
            return DocType.UNKNOWN
    return DocType.UNKNOWN


def grid_looks_like_line_item_table(grid: list[list[Any]] | None) -> bool:
    """
    True, если grid похож на таблицу строк работ/товаров (КС/смета):
    есть строка «1 2 3 … N» и/или несколько строк «№ | текст | … | сумма».
    """
    if not grid or len(grid) < 3:
        return False

    for row in grid[:6]:
        nums: list[int] = []
        for cell in row:
            t = _cell_text(cell)
            if re.fullmatch(r"\d{1,2}", t):
                nums.append(int(t))
            elif t:
                # допускаем склейки вроде «3 4» только если почти вся строка числовая
                break
        if len(nums) >= 6 and nums[0] == 1 and nums == list(range(1, len(nums) + 1)):
            return True

    dataish = 0
    for row in grid:
        texts = [_cell_text(c) for c in row]
        if not texts:
            continue
        head = texts[0]
        if not re.fullmatch(r"\d{1,3}", head):
            continue
        has_name = any(re.search(r"[А-Яа-яЁё]{4,}", t or "") for t in texts[1:5])
        has_money = any(
            re.search(r"\d{2,}", (t or "").replace(" ", "").replace("\xa0", ""))
            for t in texts[-5:]
        )
        if has_name and has_money:
            dataish += 1
    return dataish >= 3


def grid_should_bypass_unmarked_routing(
    grid: list[list[Any]] | None,
    doc_type: DocType | str | None,
) -> bool:
    """True — не роутить эту таблицу, даже если span/size формально сработали."""
    dt = _as_doc_type(doc_type)
    policy = routing_policy_for(dt)
    if not policy.keep_line_item_grids:
        return False
    return grid_looks_like_line_item_table(grid)


# --- КС-2 / КС-3 ---

_KS2_HEADERS_8 = [
    "№ п/п",
    "Позиция по смете",
    "Наименование работ",
    "Номер единичной расценки",
    "Единица измерения",
    "Количество",
    "Цена за единицу, руб.",
    "Стоимость, руб.",
]

_KS2_HEADERS_15 = [
    "№ п/п",
    "Обоснование",
    "Наименование работ",
    "Ед. изм.",
    "Количество",
    "Всего",
    "Осн.З",
    "ЭМ",
    "З/п мех.",
    "Всего",
    "Осн.З",
    "ЭМ",
    "З/п мех.",
    "ЗТ",
    "ЗТМ",
]


def _row_is_numeric_index_header(row: list[Any]) -> bool:
    """Строка вида 1|2|3|…|N без текстовых заголовков."""
    texts = [_cell_text(c) for c in row if not getattr(c, "covered", False)]
    texts = [t for t in texts if t]
    if len(texts) < 6:
        return False
    nums: list[int] = []
    for t in texts:
        if not re.fullmatch(r"\d{1,2}", t):
            return False
        nums.append(int(t))
    return nums[0] == 1 and nums == list(range(1, len(nums) + 1))


def _row_has_text_header_labels(row: list[Any]) -> bool:
    joined = " ".join(_cell_text(c) for c in row)
    return bool(
        re.search(
            r"Наименование|Выполнено|Стоимость|Количество|Единица|Товар|НДС",
            joined,
            re.I,
        )
    )


def fix_ks2_numeric_headers(
    grid: list[list[Any]],
    kinds: list[str] | None,
) -> tuple[list[list[Any]], list[str] | None]:
    """Подставляет канонические заголовки КС-2 вместо голой строки 1…N."""
    if not grid:
        return grid, kinds
    kind_list = list(kinds) if kinds is not None else ["DATA"] * len(grid)
    for i, row in enumerate(grid):
        if not _row_is_numeric_index_header(row):
            continue
        # если выше уже есть нормальные подписи — не трогаем
        prev_ok = any(
            _row_has_text_header_labels(grid[j]) for j in range(max(0, i - 3), i)
        )
        if prev_ok:
            continue
        n = len([c for c in row if not getattr(c, "covered", False)])
        headers = None
        if n == 8:
            headers = _KS2_HEADERS_8
        elif n in (15, 16):
            headers = _KS2_HEADERS_15[:n]
        if not headers:
            continue
        visible = [c for c in row if not getattr(c, "covered", False)]
        for cell, title in zip(visible, headers):
            cell.text = title
            if hasattr(cell, "words"):
                cell.words = []
        if i < len(kind_list):
            kind_list[i] = "HEADER"
        break
    if kinds is None:
        return grid, None
    return grid, kind_list


def _row_amount_cells(row: list[Any]) -> list[str]:
    """Непустые «суммоподобные» хвосты строки."""
    out: list[str] = []
    for cell in row:
        t = _cell_text(cell)
        if not t:
            continue
        compact = t.replace(" ", "").replace("\xa0", "").replace("'", "")
        if t in {"Х", "X", "x", "х", "-", "—"}:
            out.append(t)
        elif re.search(r"\d", compact) and not re.fullmatch(r"\d{1,3}", t):
            out.append(t)
    return out


def _row_is_bare_total_row(row: list[Any]) -> bool:
    """
    Строка итогов без подписи: слева пусто/прочерки, справа суммы.
    """
    texts = [_cell_text(c) for c in row]
    if not any(texts):
        return False
    # первая текстовая ячейка не должна быть названием работ
    first_text = next((t for t in texts if t), "")
    if re.search(r"[А-Яа-яЁё]{4,}", first_text) and not re.match(
        r"^(?:Итого|Всего|НДС)\b", first_text, re.I
    ):
        return False
    amounts = _row_amount_cells(row)
    if len(amounts) < 1:
        return False
    # название работ отсутствует или это уже итог
    name_like = [
        t
        for t in texts[:-3]
        if t and re.search(r"[А-Яа-яЁё]{4,}", t) and t not in {"Х", "X"}
    ]
    return len(name_like) == 0 and len(amounts) >= 1


def _label_col_for_totals(grid: list[list[Any]]) -> int:
    """Колонка для подписи итогов — обычно 0 или первая текстовая широкая."""
    if not grid:
        return 0
    # предпочитаем col с наибольшим числом кириллических подписей
    width = max(len(r) for r in grid)
    best, best_n = 0, -1
    for col in range(min(3, width)):
        n = 0
        for row in grid:
            if col >= len(row):
                continue
            if re.search(r"[А-Яа-яЁё]{4,}", _cell_text(row[col])):
                n += 1
        if n > best_n:
            best, best_n = col, n
    return best


def fix_ks_footer_total_labels(
    grid: list[list[Any]],
    kinds: list[str] | None,
    labels: list[str],
) -> tuple[list[list[Any]], list[str] | None]:
    """Подписывает хвостовые «голые» суммы снизу вверх заданными labels."""
    if not grid or not labels:
        return grid, kinds
    bare_idxs = [i for i, row in enumerate(grid) if _row_is_bare_total_row(row)]
    if not bare_idxs:
        return grid, kinds
    # берём непрерывный хвост
    tail: list[int] = []
    for i in reversed(bare_idxs):
        if not tail or i == tail[0] - 1:
            tail.insert(0, i)
        else:
            break
    if not tail:
        return grid, kinds
    if len(tail) == 1:
        use = [labels[0]]
    elif len(tail) <= len(labels):
        use = labels[-len(tail) :]
    else:
        use = labels
        tail = tail[-len(use) :]
    label_col = _label_col_for_totals(grid)
    for idx, lab in zip(tail, use):
        row = grid[idx]
        if label_col < len(row) and not _cell_text(row[label_col]):
            row[label_col].text = lab
            if hasattr(row[label_col], "words"):
                row[label_col].words = []
        else:
            # ищем первую пустую слева
            for cell in row:
                if not _cell_text(cell):
                    cell.text = lab
                    if hasattr(cell, "words"):
                        cell.words = []
                    break
    return grid, kinds


def fix_ks3_split_num_name(
    grid: list[list[Any]],
    kinds: list[str] | None,
) -> tuple[list[list[Any]], list[str] | None]:
    """«1 Демонтаж…» в одной ячейке с colspan → номер + название."""
    if not grid:
        return grid, kinds
    for row in grid:
        if len(row) < 2:
            continue
        for ci, cell in enumerate(row[:-1]):
            t = _cell_text(cell)
            m = re.match(r"^(\d{1,3})\s+([А-Яа-яЁёA-Za-z].+)$", t)
            if not m:
                continue
            # сосед справа пустой — типичный поглощённый слот
            right = row[ci + 1]
            if _cell_text(right):
                continue
            if int(getattr(cell, "colspan", 1) or 1) < 2 and ci > 0:
                continue
            cell.text = m.group(1)
            if hasattr(cell, "words"):
                cell.words = []
            if hasattr(cell, "colspan"):
                cell.colspan = 1
            right.text = m.group(2).strip()
            if hasattr(right, "words"):
                right.words = []
            break
    return grid, kinds


# --- ТОРГ-12 ---

def fix_torg12_total_row_labels(
    grid: list[list[Any]],
    kinds: list[str] | None,
) -> tuple[list[list[Any]], list[str] | None]:
    """Подписывает Итого / Всего по накладной на хвостовых строках без товара."""
    return fix_ks_footer_total_labels(
        grid,
        kinds,
        ["Итого", "Всего по накладной"],
    )


# --- СФ / УПД: prose с маркерами (1)(2)(6) ---

_INVOICE_FIELD_LABELS: dict[str, str] = {
    "1": "Номер и дата счёта-фактуры",
    "1а": "Исправление № / дата",
    "2": "Продавец",
    "2а": "Адрес продавца",
    "2б": "ИНН/КПП продавца",
    "3": "Грузоотправитель и его адрес",
    "4": "Грузополучатель и его адрес",
    "5": "К платёжно-расчётному документу",
    "5а": "Документ об отгрузке",
    "6": "Покупатель",
    "6а": "Адрес покупателя",
    "6б": "ИНН/КПП покупателя",
    "7": "Валюта: наименование, код",
    "8": "Идентификатор госконтракта",
}


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
    if not text or text.count("(") < 4:
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
        label = _INVOICE_FIELD_LABELS.get(code, f"Поле ({code})")
        if len(val) > 300:
            val = val[:300] + "…"
        rows.append((code, label, val))
    if len(rows) < 4:
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
    if len(tokens) < 3:
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


def enrich_page_html_for_doc_type(
    doc_type: DocType | str | None,
    page,
    html_body: str,
) -> str:
    """
    Page-level эвристики: пустые СФ/УПД (prose с маркерами), итоги ТОРГ-12 без table.
    Не трогает страницы, где уже есть нормальные <table> с данными (кроме дополнения).
    """
    del page
    dt = _as_doc_type(doc_type)
    if not html_body or not html_body.strip():
        return html_body
    text = _plain_text_from_html(html_body)
    n_tables = html_body.lower().count("<table")

    if dt in (DocType.INVOICE_SF, DocType.UPD):
        # Поля (1)/(2)/(6) — даже если товарная таблица уже найдена (пустая форма СФ).
        if "invoice-fields" not in html_body.lower():
            field_markers = len(re.findall(r"\(\s*\d+[аaбb]?\s*\)", text))
            if n_tables == 0 or field_markers >= 4:
                fields = build_invoice_fields_table_html(text)
                if fields:
                    return html_body.rstrip() + "\n" + fields

    if dt == DocType.TORG12 and n_tables == 0:
        totals = build_torg_totals_table_html(text)
        if totals:
            return html_body.rstrip() + "\n" + totals

    return html_body


def apply_type_heuristics(
    doc_type: DocType | str | None,
    page,
    grid: list[list[Any]],
    kinds: list[str] | None,
) -> tuple[list[list[Any]], list[str] | None]:
    """
    Type-specific постправки к одной таблице после process_table.

    UNKNOWN → только безопасный RSBU-fix на финансовых grid.
    """
    del page
    dt = _as_doc_type(doc_type)

    if dt in (DocType.RSBU, DocType.UNKNOWN):
        code_col = _rsbu_find_code_col(grid)
        if code_col is not None:
            grid, kinds = fix_rsbu_section_value_shift(grid, kinds)

    if dt == DocType.KS2:
        grid, kinds = fix_ks2_numeric_headers(grid, kinds)
        grid, kinds = fix_ks_footer_total_labels(
            grid, kinds, ["Итого", "Всего по акту"]
        )

    if dt == DocType.KS3:
        grid, kinds = fix_ks3_split_num_name(grid, kinds)
        grid, kinds = fix_ks_footer_total_labels(
            grid, kinds, ["Итого", "НДС", "Всего с НДС"]
        )

    if dt == DocType.TORG12:
        grid, kinds = fix_torg12_total_row_labels(grid, kinds)

    # СФ/УПД: сетка обычно уже ок; page-level — enrich_page_html_for_doc_type
    return grid, kinds
