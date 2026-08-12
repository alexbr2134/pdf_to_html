"""
Режем огромные Word-таблицы форм (ТОРГ, КС, СФ) по границам ячеек.

Идея простая: шапка и товарная сетка часто сидят в одной w:tbl —
смотрим tcBorders/tblBorders и режем на горизонтальные куски.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from docx.oxml.ns import qn
from docx.table import Table

from pdf_html_pipeline import Cell

_INVISIBLE = frozenset({"nil", "none"})

_FORM_RE = re.compile(
    r"форма\s*№|унифицированн|КС-?[23]|ТОРГ-?12|УПД|"
    r"сч[её]т\s*[-–]?\s*фактура|бухгалтерский\s+баланс|"
    r"ОКУД|ОКПО|0330212|032200[15]|071000",
    re.I,
)

_INDEX_HEADER_MIN = 6


@dataclass(frozen=True)
class TableDefaults:
    top: bool = False
    left: bool = False
    bottom: bool = False
    right: bool = False
    inside_h: bool = False
    inside_v: bool = False


@dataclass
class RegionSlice:
    """Горизонтальный пояс исходной таблицы."""

    r0: int
    r1: int  # inclusive
    kind: Literal["header", "data", "other"] = "other"


def _edge_visible(elem, edge: str, default: bool = False) -> bool:
    """True, если у tcBorders/tblBorders ребро видимо."""
    if elem is None:
        return default
    child = elem.find(qn(f"w:{edge}"))
    if child is None:
        return default
    val = (child.get(qn("w:val")) or "").lower()
    if not val:
        # у части экспортёров видимость только через sz
        sz = child.get(qn("w:sz"))
        if sz is not None:
            try:
                return int(sz) > 0
            except ValueError:
                return default
        return default
    if val in _INVISIBLE:
        return False
    return True


def read_tbl_border_defaults(table: Table) -> TableDefaults:
    tbl = table._tbl  # noqa: SLF001
    tblPr = tbl.tblPr
    if tblPr is None:
        return TableDefaults()
    borders = tblPr.find(qn("w:tblBorders"))
    if borders is None:
        return TableDefaults()
    return TableDefaults(
        top=_edge_visible(borders, "top"),
        left=_edge_visible(borders, "left"),
        bottom=_edge_visible(borders, "bottom"),
        right=_edge_visible(borders, "right"),
        inside_h=_edge_visible(borders, "insideH"),
        inside_v=_edge_visible(borders, "insideV"),
    )


def _tc_borders(tc) -> Any:
    tcPr = tc.tcPr
    if tcPr is None:
        return None
    return tcPr.find(qn("w:tcBorders"))


def cell_border_flags(
    tc,
    defaults: TableDefaults,
) -> tuple[bool, bool, bool, bool]:
    """(top, left, bottom, right) visibility for a ``w:tc``."""
    borders = _tc_borders(tc)
    return (
        _edge_visible(borders, "top", defaults.top or defaults.inside_h),
        _edge_visible(borders, "left", defaults.left or defaults.inside_v),
        _edge_visible(borders, "bottom", defaults.bottom or defaults.inside_h),
        _edge_visible(borders, "right", defaults.right or defaults.inside_v),
    )


def build_row_border_scores(
    table: Table,
    n_rows: int,
    n_cols: int,
) -> list[int]:
    """
    Сколько видимых бордеров у ячеек строки (по фактическим ``w:tc``).
    Индекс = номер строки таблицы Word (0-based), до compact.
    """
    defaults = read_tbl_border_defaults(table)
    scores = [0] * n_rows
    rows_xml = table._tbl.tr_lst  # noqa: SLF001
    for r_idx, tr in enumerate(rows_xml):
        if r_idx >= n_rows:
            break
        count = 0
        for tc in tr.tc_lst:
            t, l, b, r = cell_border_flags(tc, defaults)
            count += sum((t, l, b, r))
        scores[r_idx] = count
    # if table has insideH/insideV defaults, empty scores still get a floor
    if defaults.inside_h or defaults.inside_v:
        floor = (2 if defaults.inside_h else 0) + (2 if defaults.inside_v else 0)
        for r in range(n_rows):
            if scores[r] == 0:
                scores[r] = floor * max(1, n_cols // 4)
    return scores


def grid_text_blob(grid: list[list[Cell]]) -> str:
    parts: list[str] = []
    for row in grid:
        for c in row:
            if c.covered:
                continue
            t = (c.text or "").strip()
            if t:
                parts.append(t)
    return "\n".join(parts)


def looks_like_layout_form_table(grid: list[list[Cell]]) -> bool:
    """Крупная декоративная сетка формы (ТОРГ/КС/СФ) vs обычная data-table."""
    if not grid:
        return False
    n_rows = len(grid)
    n_cols = len(grid[0])
    text = grid_text_blob(grid)
    if _FORM_RE.search(text):
        if n_rows >= 8 and n_cols >= 4:
            return True
        if n_rows >= 12 and n_cols >= 3:
            return True

    if n_rows < 15 or n_cols < 6:
        return False

    visible = 0
    nonempty = 0
    mergeish = 0
    for row in grid:
        for c in row:
            if c.covered:
                continue
            visible += 1
            if (c.text or "").strip():
                nonempty += 1
            if c.colspan > 1 or c.rowspan > 1:
                mergeish += 1
    if visible <= 0:
        return False
    density = nonempty / visible
    merge_ratio = mergeish / visible
    return density < 0.55 and (merge_ratio >= 0.12 or n_cols >= 8)


def _row_is_index_header(row: list[Cell]) -> bool:
    """ТОРГ 1..N или СФ/УПД «А 1 1а 1б 2 …»."""
    nums: list[int] = []
    parts: list[str] = []
    for c in row:
        if c.covered:
            continue
        t = (c.text or "").strip()
        if not t:
            continue
        parts.append(t.lower().replace("\n", "").replace(" ", ""))
        if re.fullmatch(r"\d{1,2}", t):
            nums.append(int(t))
        elif nums:
            break
    if (
        len(nums) >= _INDEX_HEADER_MIN
        and nums[0] == 1
        and nums == list(range(1, len(nums) + 1))
    ):
        return True
    if len(parts) < 6:
        return False
    short = [p for p in parts if len(p) <= 3]
    if len(short) < 6:
        return False
    has_letter_codes = any(re.fullmatch(r"\d{1,2}[аaбb]", p) for p in parts)
    starts_with_a = parts[0] in {"а", "a"}
    return "1" in parts and "2" in parts and (has_letter_codes or starts_with_a)


def _row_nonempty_count(row: list[Cell]) -> int:
    return sum(
        1 for c in row if not c.covered and (c.text or "").strip()
    )


def _row_max_colspan(row: list[Cell]) -> int:
    m = 1
    for c in row:
        if c.covered:
            continue
        m = max(m, int(c.colspan or 1))
    return m


def _row_looks_headerish(row: list[Cell]) -> bool:
    """Строка больше похожа на шапку формы, чем на данные."""
    ne = _row_nonempty_count(row)
    if ne == 0:
        return False
    if _row_max_colspan(row) >= 3:
        return True
    texts = [
        (c.text or "").strip()
        for c in row
        if not c.covered and (c.text or "").strip()
    ]
    joined = " ".join(texts)
    if re.search(
        r"грузоотправител|грузополучател|поставщик|плательщик|"
        r"форма\s+по\s+окуд|унифицированн|товарная\s+накладн",
        joined,
        re.I,
    ):
        return True
    return False


def _row_looks_dataish(row: list[Cell]) -> bool:
    if _row_is_index_header(row):
        return True
    ne = _row_nonempty_count(row)
    if ne >= 4 and _row_max_colspan(row) <= 2:
        return True
    texts = [
        (c.text or "").strip()
        for c in row
        if not c.covered and (c.text or "").strip()
    ]
    if not texts:
        return False
    joined = " ".join(texts).lower()
    if re.search(
        r"наименование|единица|количество|цена|сумма|ндс|"
        r"товар|номенклатур|кол-во|масса",
        joined,
        re.I,
    ):
        return True
    # numeric-heavy line items
    digitish = sum(1 for t in texts if re.search(r"\d", t))
    return ne >= 3 and digitish >= 2 and _row_max_colspan(row) <= 2


def find_horizontal_bands(
    grid: list[list[Cell]],
    row_border_scores: list[int] | None = None,
) -> list[RegionSlice]:
    """Ищем, где шапка кончается и начинаются позиции (1..N / пустой зазор)."""
    n = len(grid)
    if n == 0:
        return []
    if n < 6:
        return [RegionSlice(0, n - 1, "other")]

    scores = row_border_scores
    if scores is None or len(scores) < n:
        scores = [0] * n

    index_rows = [i for i in range(n) if _row_is_index_header(grid[i])]
    split_at: list[int] = []

    if index_rows:
        idx = index_rows[0]
        data_start = idx
        for back in range(1, 4):
            r = idx - back
            if r < 0:
                break
            if _row_looks_headerish(grid[r]):
                break  # ОКУД / стороны — это ещё шапка
            ne = _row_nonempty_count(grid[r])
            max_cs = _row_max_colspan(grid[r])
            if ne >= 3 and max_cs <= 2:
                data_start = r  # заголовки колонок
            else:
                break
        if data_start > 0:
            split_at.append(data_start)

    if not split_at:
        for i in range(2, n - 2):
            if not _row_looks_dataish(grid[i]):
                continue
            prev_header = sum(
                1 for r in range(max(0, i - 4), i) if _row_looks_headerish(grid[r])
            )
            if prev_header >= 2:
                split_at.append(i)
                break

    if not split_at:
        for i in range(1, n - 1):
            if _row_nonempty_count(grid[i]) > 0:
                continue
            above = sum(scores[r] for r in range(max(0, i - 3), i))
            below = sum(scores[r] for r in range(i + 1, min(n, i + 4)))
            if above >= 4 and below >= 4:
                split_at.append(i + 1)
                break

    if not split_at:
        return [RegionSlice(0, n - 1, "other")]

    cuts = sorted(set(split_at))
    bands: list[RegionSlice] = []
    cursor = 0
    for cut in cuts:
        if cut <= cursor or cut >= n:
            continue
        bands.append(RegionSlice(cursor, cut - 1, "header"))
        cursor = cut
    bands.append(RegionSlice(cursor, n - 1, "data"))
    return bands


def slice_grid(grid: list[list[Cell]], r0: int, r1: int) -> list[list[Cell]]:
    """Вырезает строки [r0, r1] и перенумеровывает Cell.row/col."""
    if not grid or r0 > r1 or r0 < 0:
        return []
    r1 = min(r1, len(grid) - 1)
    out: list[list[Cell]] = []
    for r in range(r0, r1 + 1):
        new_row: list[Cell] = []
        for c, cell in enumerate(grid[r]):
            if cell.covered:
                new_row.append(
                    Cell(
                        row=len(out),
                        col=c,
                        bbox=None,
                        text="",
                        covered=True,
                        is_placeholder=True,
                    )
                )
                continue
            # clamp rowspan to remaining rows in slice
            rs = min(cell.rowspan, r1 - r + 1)
            new_row.append(
                Cell(
                    row=len(out),
                    col=c,
                    bbox=None,
                    text=cell.text,
                    rowspan=rs,
                    colspan=cell.colspan,
                    covered=False,
                    is_placeholder=not bool((cell.text or "").strip()),
                )
            )
        out.append(new_row)
    return out


def _right_code_col(grid: list[list[Cell]]) -> int | None:
    """Правая колонка с короткими кодами ОКУД/ОКПО (0330212 и т.п.)."""
    if not grid:
        return None
    n_cols = len(grid[0])
    if n_cols < 3:
        return None
    best_col = None
    best_hits = 0
    for c in range(n_cols - 1, max(-1, n_cols - 4), -1):
        hits = 0
        texts = 0
        for row in grid:
            if c >= len(row):
                continue
            cell = row[c]
            if cell.covered:
                continue
            t = (cell.text or "").strip()
            if not t:
                continue
            texts += 1
            if re.fullmatch(r"\d{4,10}", t.replace(" ", "")):
                hits += 1
            elif re.search(r"коды|окуд|окпо", t, re.I):
                hits += 1
        if hits >= 2 and hits >= texts * 0.4:
            if hits > best_hits:
                best_hits = hits
                best_col = c
    return best_col


def peel_code_column_kv(grid: list[list[Cell]]) -> list[list[Cell]] | None:
    """
    Шапка формы: label | … | code → двухколоночная KV.
    Возвращает None, если колонка кодов не найдена.
    """
    code_col = _right_code_col(grid)
    if code_col is None:
        return None

    rows_out: list[list[Cell]] = []
    for r, row in enumerate(grid):
        label_parts: list[str] = []
        code = ""
        for c, cell in enumerate(row):
            if cell.covered:
                continue
            t = (cell.text or "").strip()
            if not t:
                continue
            if c == code_col:
                code = t
            else:
                label_parts.append(t)
        label = " ".join(label_parts).strip()
        if not label and not code:
            continue
        rows_out.append(
            [
                Cell(
                    row=len(rows_out),
                    col=0,
                    bbox=None,
                    text=label,
                    colspan=1,
                    rowspan=1,
                ),
                Cell(
                    row=len(rows_out),
                    col=1,
                    bbox=None,
                    text=code,
                    colspan=1,
                    rowspan=1,
                    is_placeholder=not bool(code),
                ),
            ]
        )
    if len(rows_out) < 2:
        return None
    return rows_out


def compact_region_grid(grid: list[list[Cell]]) -> list[list[Cell]]:
    """Локальный compact без зависимости от office_docx (избежать циклов)."""
    if not grid:
        return grid
    n_rows = len(grid)
    n_cols = len(grid[0])
    keep_cols: list[int] = []
    for c in range(n_cols):
        useful = False
        for r in range(n_rows):
            cell = grid[r][c]
            if cell.covered:
                continue
            if (cell.text or "").strip() or cell.colspan > 1 or cell.rowspan > 1:
                useful = True
                break
        if useful:
            keep_cols.append(c)
    if not keep_cols:
        return []

    col_map = {old: i for i, old in enumerate(keep_cols)}
    keep_set = set(keep_cols)
    out: list[list[Cell]] = []
    for row in grid:
        has = any(
            (not row[c].covered and ((row[c].text or "").strip() or row[c].colspan > 1))
            for c in keep_cols
            if c < len(row)
        )
        if not has:
            continue
        new_row: list[Cell] = []
        for c in keep_cols:
            cell = row[c]
            if cell.covered:
                new_row.append(
                    Cell(
                        row=len(out),
                        col=col_map[c],
                        bbox=None,
                        text="",
                        covered=True,
                        is_placeholder=True,
                    )
                )
                continue
            cs = cell.colspan
            if cs > 1:
                spanned = [c + k for k in range(cs) if c + k < n_cols]
                kept = [x for x in spanned if x in keep_set]
                cs = max(1, len(kept))
            new_row.append(
                Cell(
                    row=len(out),
                    col=col_map[c],
                    bbox=None,
                    text=(cell.text or "").strip(),
                    rowspan=min(cell.rowspan, max(1, n_rows - len(out))),
                    colspan=cs,
                    covered=False,
                    is_placeholder=not bool((cell.text or "").strip()),
                )
            )
        out.append(new_row)
    for r, row in enumerate(out):
        for cell in row:
            if not cell.covered and cell.rowspan > 1:
                cell.rowspan = min(cell.rowspan, len(out) - r)
    return out


_COL_HEADER_RE = re.compile(
    r"наименование\s+товар|наименован\w*\s+работ|количеств|кол-?во|"
    r"цена\s*\(|цена,|стоимост|"
    r"ед\.?\s*изм|единица\s+измерен|номенклатур|"
    r"масса\s+(?:брутто|нетто)|сумма\s*(?:без|с\s*ндс|ндс)?|"
    r"\bндс\b|обоснование|выполнено|"
    r"номер\s+по\s+порядку|№\s*п/?п",
    re.I,
)

_FOOTER_CHROME_RE = re.compile(
    r"по\s+доверенности|отпуск\s+груза\s+разрешил|груз\s+получил|"
    r"груз\s+принял|главный\s*\(?старший\)?\s*бухгалтер|"
    r"м\.?\s*п\.?|расшифровка\s+подписи|"
    r"товарная\s+накладная\s+имеет\s+приложение|"
    r"всего\s+отпущено\s+на\s+сумму",
    re.I,
)

_LABEL_ROW_RE = re.compile(
    r"^(должность|подпись|расшифровка\s+подписи|прописью|"
    r"организация.*реквизит|грузополучатель)$",
    re.I,
)


def _row_looks_product_line(row: list[Cell]) -> bool:
    """Похоже на строку позиции (название + цифры), а не на шапку/подписи."""
    if _row_max_colspan(row) > 2:
        return False
    texts = [
        (c.text or "").strip()
        for c in row
        if not c.covered and (c.text or "").strip()
    ]
    if len(texts) < 4:
        return False
    joined = " ".join(texts)
    if _LABEL_ROW_RE.search(joined) or re.search(
        r"должность|подпись|расшифровка|прописью", joined, re.I
    ):
        return False
    # поля шапки СФ/УПД — (2), Продавец:, ИНН...
    if re.search(
        r"\(\s*\d+[аaбb]?\s*\)|продавец\s*:|покупатель\s*:|"
        r"инн\s*/?\s*кпп|грузоотправител|грузополучател|"
        r"статус\s*:|плательщик|поставщик\s*:",
        joined,
        re.I,
    ):
        return False
    digitish = sum(1 for t in texts if re.search(r"\d", t))
    if digitish < 2:
        return False
    return any(
        len(t) >= 10 and re.search(r"[А-Яа-яA-Za-z]", t) and not re.fullmatch(r"[\d\s.,\-/%]+", t)
        for t in texts
    )


def is_line_item_data_table(grid: list[list[Cell]]) -> bool:
    """Товарная/сметная таблица — то, что оставляем как <table>."""
    if not grid or len(grid) < 2:
        return False
    n_cols = len(grid[0])
    if n_cols < 3:
        return False

    blob = grid_text_blob(grid)
    has_index = any(_row_is_index_header(r) for r in grid[:8])
    if _FOOTER_CHROME_RE.search(blob) and not has_index:
        return False

    if has_index:
        return True

    col_header = False
    for row in grid[:6]:
        joined = " ".join(
            (c.text or "").strip()
            for c in row
            if not c.covered and (c.text or "").strip()
        )
        if _COL_HEADER_RE.search(joined):
            col_header = True
            break

    product_rows = sum(1 for row in grid if _row_looks_product_line(row))
    dense = 0
    for row in grid:
        if _row_looks_product_line(row):
            dense += 1
            continue
        ne = _row_nonempty_count(row)
        if ne >= 4 and _row_max_colspan(row) <= 2:
            texts = [
                (c.text or "").strip()
                for c in row
                if not c.covered and (c.text or "").strip()
            ]
            joined = " ".join(texts)
            if re.search(r"должность|подпись|расшифровка|прописью", joined, re.I):
                continue
            dense += 1

    if col_header and product_rows >= 1 and dense >= 2:
        return True
    return product_rows >= 2 and dense >= 3 and len(grid) >= 5


def is_form_chrome_grid(grid: list[list[Cell]]) -> bool:
    """Шапка/футер формы или узкая KV-сетка — в HTML это prose."""
    if not grid:
        return False
    if is_line_item_data_table(grid):
        return False

    text = grid_text_blob(grid)
    n_rows = len(grid)
    n_cols = len(grid[0])

    if re.search(
        r"м\.?\s*п\.?|по\s+доверенности|прописью|"
        r"приложение\s+на|имеет\s+приложение|"
        r"унифицированная\s+форма|форма\s+по\s+окуд|"
        r"товарная\s+накладная\s+имеет|"
        r"товар\s*\(груз\)\s*передал|результаты\s+работ|"
        r"ответственн\w+\s+за\s+правильност|"
        r"номер\s+документа|дата\s+составл",
        text,
        re.I,
    ):
        return True

    if n_cols <= 2:
        return True

    merge_rows = sum(1 for row in grid if _row_max_colspan(row) >= 3)
    if n_rows >= 2 and merge_rows >= max(2, int(n_rows * 0.35)):
        return True

    if re.search(
        r"грузоотправител|грузополучател|поставщик|плательщик|"
        r"номер\s+документа|дата\s+составл|по\s+окпо|по\s+окуд",
        text,
        re.I,
    ) and not is_line_item_data_table(grid):
        return True

    return False


_TAIL_CHROME_RE = re.compile(
    r"товарная\s+накладная\s+имеет\s+приложение|"
    r"порядковых\s+номеров\s+записей|"
    r"масса\s+груза\s*\(\s*(?:нетто|брутто)|"
    r"всего\s+мест|"
    r"по\s+доверенности|"
    r"отпуск\s+груза\s+разрешил|"
    r"приложение\s*\(\s*паспорта|"
    r"всего\s+отпущено\s+на\s+сумму|"
    r"груз\s+получил|груз\s+принял",
    re.I,
)

_TOTALS_ROW_RE = re.compile(r"^(итого|всего\s+по\s+накладной)\b", re.I)


def _row_joined_text(row: list[Cell]) -> str:
    return " ".join(
        (c.text or "").strip()
        for c in row
        if not c.covered and (c.text or "").strip()
    )


def find_trailing_chrome_start(grid: list[list[Cell]]) -> int | None:
    """Где заканчиваются позиции/итоги и начинается хвост формы."""
    if not grid or len(grid) < 4:
        return None
    anchor = 0
    for i, row in enumerate(grid[:8]):
        t = _row_joined_text(row)
        if _row_is_index_header(row) or _COL_HEADER_RE.search(t):
            anchor = i
            break
    for i in range(max(anchor + 1, 2), len(grid)):
        t = _row_joined_text(grid[i])
        if not t:
            continue
        if _TOTALS_ROW_RE.search(t):
            continue
        if _TAIL_CHROME_RE.search(t):
            return i
    return None


def trim_trailing_form_chrome(grid: list[list[Cell]]) -> list[list[Cell]]:
    """Убирает футер, если он прилип к товарной таблице снизу."""
    cut = find_trailing_chrome_start(grid)
    if cut is None or cut < 3:
        return grid
    trimmed = grid[:cut]
    return trimmed if trimmed else grid


def should_emit_as_html_table(grid: list[list[Cell]]) -> bool:
    """Оставляем <table> только для товарной/сметной сетки."""
    if not grid:
        return False
    if is_form_chrome_grid(grid):
        return False
    return is_line_item_data_table(grid)


def grid_rows_as_prose_lines(grid: list[list[Cell]]) -> list[str]:
    """Плоские строки из сетки (для шапок/футеров)."""
    lines: list[str] = []
    n_cols = len(grid[0]) if grid else 0
    for row in grid:
        parts = [
            (c.text or "").strip()
            for c in row
            if not c.covered and (c.text or "").strip()
        ]
        if not parts:
            continue
        if n_cols <= 2 and len(parts) == 2:
            a, b = parts[0], parts[1]
            if len(b) <= 40 and not re.search(r"[.!?]$", a):
                lines.append(f"{a}: {b}" if a else b)
            else:
                lines.append(f"{a} {b}".strip())
        elif len(parts) == 1:
            lines.append(parts[0])
        else:
            if all(len(p) < 48 for p in parts):
                lines.append(" — ".join(parts))
            else:
                lines.append(" ".join(parts))
    return lines


def split_layout_table_grids(
    grid: list[list[Cell]],
    *,
    table: Table | None = None,
    raw_row_count: int | None = None,
) -> list[list[list[Cell]]]:
    """Режет форму на куски; если не вышло — возвращает исходник как есть."""
    if not looks_like_layout_form_table(grid):
        return [grid]

    border_scores: list[int] | None = None
    if table is not None and raw_row_count:
        border_scores = build_row_border_scores(table, raw_row_count, len(grid[0]))
        if len(border_scores) != len(grid):
            border_scores = None

    bands = find_horizontal_bands(grid, border_scores)
    if len(bands) <= 1:
        return [grid]

    result: list[list[list[Cell]]] = []
    for band in bands:
        sub = slice_grid(grid, band.r0, band.r1)
        sub = compact_region_grid(sub)
        if not sub:
            continue
        result.append(sub)

    if len(result) <= 1:
        return [grid]
    return result
