"""
Проверка пригодности страницы PDF для smart-пайплайна (векторный текст/линии).

Страницы, не прошедшие проверку, помечаются как отсеянные — их предполагается
отдавать другой модели (скан/OCR). Сама передача другой модели здесь не делается.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


# --- коды причин отсева (стабильные для логов / data-* атрибутов) ---

REASON_BROKEN_FONTS = "broken_fonts"
REASON_UNMARKED_TABLE_LINES = "unmarked_table_lines"
REASON_IMAGE_ONLY_SCAN = "image_only_scan"

REASON_LABELS_RU: dict[str, str] = {
    REASON_BROKEN_FONTS: "битые шрифты / плохо размеченный текст (OCR-garble)",
    REASON_UNMARKED_TABLE_LINES: (
        "сложная/крупная таблица без векторных линий "
        "(много colspan/rowspan или большой размер после обработки)"
    ),
    REASON_IMAGE_ONLY_SCAN: "страница почти без текстового слоя (скан/картинка)",
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


@dataclass
class SuitabilityStats:
    """Сводка отсева по прогону (несколько PDF или один документ)."""

    total_pages: int = 0
    accepted_pages: int = 0
    rejected_pages: int = 0
    reason_counts: Counter = field(default_factory=Counter)
    # [(pdf_name, page_num, reasons)]
    rejected_details: list[tuple[str, int, list[str]]] = field(default_factory=list)
    # pdf_name -> (accepted, rejected)
    per_file: dict[str, tuple[int, int]] = field(default_factory=dict)

    def record(self, pdf_name: str, result: PageSuitability) -> None:
        """Учитывает результат проверки одной страницы."""
        self.total_pages += 1
        acc, rej = self.per_file.get(pdf_name, (0, 0))
        if result.suitable:
            self.accepted_pages += 1
            self.per_file[pdf_name] = (acc + 1, rej)
        else:
            self.rejected_pages += 1
            self.reason_counts.update(result.reasons)
            self.rejected_details.append((pdf_name, result.page_num, list(result.reasons)))
            self.per_file[pdf_name] = (acc, rej + 1)

    @property
    def rejection_rate(self) -> float:
        """Доля отсеянных страниц [0..1]."""
        if self.total_pages <= 0:
            return 0.0
        return self.rejected_pages / self.total_pages


def page_has_broken_fonts(page) -> bool:
    """
    True, если на странице битая кодировка шрифтов / OCR-garble
    (латиница вместо кириллицы, HiddenHorzOCR и т.п.).
    """
    chars = page.chars or []
    n_chars = len(chars)
    if n_chars < 50:
        return False

    ocr_font_hits = 0
    for ch in chars:
        fname = ch.get("fontname") or ""
        if re.search(r"OCR|HiddenHorz|HiddenVert|GlyphLess", fname, re.I):
            ocr_font_hits += 1

    if ocr_font_hits >= 20:
        return True

    text = page.extract_text() or ""
    if not text.strip():
        return False

    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 80:
        return False

    cyr = sum(1 for ch in letters if "\u0400" <= ch <= "\u04FF")
    lat = sum(1 for ch in letters if ("A" <= ch <= "Z") or ("a" <= ch <= "z"))
    cyr_ratio = cyr / len(letters)
    lat_ratio = lat / len(letters)

    if lat_ratio >= 0.55 and cyr_ratio <= 0.20:
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
    return garble >= 15 and cyr_ratio < 0.35


# Порог «много объединений» после process_table (colspan/rowspan).
# Мелкие/базовые (0–2 span) smart оставляет; роутим многоуровневые шапки
# вроде 2703000015-104 (4 span, max colspan 5) и тяжелее.
_SPAN_CELLS_ROUTE = 4
_SPAN_ABSORBED_ROUTE = 7
_SPAN_CELLS_LARGE_ROUTE = 3
_SPAN_MAX_LARGE_ROUTE = 5

# Крупная таблица (после process_table): мелкие базовые не роутим.
# Пример: 2703000015-6 balance 17×6 / 102 cells — ещё ок; -9 26×6 / 156 — роут.
_SIZE_MIN_COLS = 5
_SIZE_MIN_ROWS = 15
_SIZE_MIN_CELLS = 120
_SIZE_MIN_CELLS_HARD = 200


@dataclass
class SpanMergeStats:
    """Сводка colspan/rowspan по одной обработанной таблице."""

    span_cells: int = 0
    colspan_cells: int = 0
    rowspan_cells: int = 0
    absorbed: int = 0
    max_colspan: int = 1
    max_rowspan: int = 1

    @property
    def max_span(self) -> int:
        """Максимум из colspan/rowspan."""
        return max(self.max_colspan, self.max_rowspan)


@dataclass
class GridSizeStats:
    """Размер grid после process_table."""

    rows: int = 0
    cols: int = 0
    cells: int = 0


def grid_span_merge_stats(grid: list[list[Any]] | None) -> SpanMergeStats:
    """Считает объединения colspan/rowspan в grid после process_table."""
    stats = SpanMergeStats()
    if not grid:
        return stats
    for row in grid:
        for cell in row:
            if getattr(cell, "covered", False):
                continue
            cs = int(getattr(cell, "colspan", 1) or 1)
            rs = int(getattr(cell, "rowspan", 1) or 1)
            if cs > 1:
                stats.colspan_cells += 1
                stats.absorbed += cs - 1
                stats.max_colspan = max(stats.max_colspan, cs)
            if rs > 1:
                stats.rowspan_cells += 1
                stats.absorbed += rs - 1
                stats.max_rowspan = max(stats.max_rowspan, rs)
            if cs > 1 or rs > 1:
                stats.span_cells += 1
    return stats


def grid_size_stats(grid: list[list[Any]] | None) -> GridSizeStats:
    """rows × cols и число непокрытых ячеек."""
    if not grid:
        return GridSizeStats()
    cols = max((len(row) for row in grid), default=0)
    cells = sum(
        1
        for row in grid
        for cell in row
        if not getattr(cell, "covered", False)
    )
    return GridSizeStats(rows=len(grid), cols=cols, cells=cells)


def grid_has_complex_span_merges(grid: list[list[Any]] | None) -> bool:
    """
    True, если после обработки таблицы много colspan/rowspan —
    структура очевидно сложная для smart.
    """
    s = grid_span_merge_stats(grid)
    if s.span_cells >= _SPAN_CELLS_ROUTE:
        return True
    if s.absorbed >= _SPAN_ABSORBED_ROUTE:
        return True
    if s.span_cells >= _SPAN_CELLS_LARGE_ROUTE and s.max_span >= _SPAN_MAX_LARGE_ROUTE:
        return True
    return False


def grid_is_large_table(grid: list[list[Any]] | None) -> bool:
    """True, если таблица крупная по размеру grid (не мелкая базовая)."""
    s = grid_size_stats(grid)
    if s.cols < _SIZE_MIN_COLS:
        return False
    if s.cells >= _SIZE_MIN_CELLS_HARD:
        return True
    return s.rows >= _SIZE_MIN_ROWS and s.cells >= _SIZE_MIN_CELLS


def grid_needs_unmarked_routing(grid: list[list[Any]] | None) -> bool:
    """Роутить таблицу: сложные span ИЛИ крупный размер."""
    return grid_has_complex_span_merges(grid) or grid_is_large_table(grid)


def should_route_unmarked_complex_spans(
    *,
    raster_lines_vectorized: bool,
    grids: list[list[list[Any]]] | None,
) -> bool:
    """
    Роутинг unmarked_table_lines:

    Только если линии брались с растра (векторизация сработала) И при обработке
    хотя бы одна таблица:
      • получила много colspan/rowspan, ИЛИ
      • крупная по размеру (rows/cols/cells).
    Векторизацию не отключаем — решение после process_table.

    PDF с уже размеченными линиями (vectorized=False) не роутятся
    (например 2508064833-*).
    """
    if not raster_lines_vectorized or not grids:
        return False
    return any(grid_needs_unmarked_routing(g) for g in grids)


def suitability_unmarked_complex_spans(page_num: int = 1) -> PageSuitability:
    """PageSuitability для отсева по сложным/крупным таблицам без векторных линий."""
    return PageSuitability(
        suitable=False,
        reasons=[REASON_UNMARKED_TABLE_LINES],
        messages=[REASON_LABELS_RU[REASON_UNMARKED_TABLE_LINES]],
        page_num=page_num,
    )


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


def page_has_unmarked_table_lines(
    page,
    pdf_path: str | None = None,
    page_num: int | None = None,
) -> bool:
    """
    Устарело как pre-check: всегда False.

    Роутинг unmarked_table_lines делается после process_table через
    should_route_unmarked_complex_spans (векторизация + сложные span).
    """
    del page, pdf_path, page_num
    return False


def page_is_image_only_scan(page, *, min_chars: int = 40, min_image_cover: float = 0.35) -> bool:
    """
    True, если страница почти без текстового слоя при заметном растре
    (скан без OCR / картинка на всю полосу).
    """
    from pdf_line_vectorize import _raster_image_cover_ratio

    n_chars = len(page.chars or [])
    text = (page.extract_text() or "").strip()
    if n_chars >= min_chars or len(text) >= min_chars:
        return False
    try:
        cover = float(_raster_image_cover_ratio(page))
    except Exception:
        cover = 0.0
    return cover >= min_image_cover


def assess_page_suitability(
    page,
    page_num: int = 1,
    pdf_path: str | None = None,
) -> PageSuitability:
    """
    Быстрая pre-check пригодности (до конвертации).

    Здесь только broken_fonts и image_only_scan.
    unmarked_table_lines — после process_table через
    should_route_unmarked_complex_spans (см. build_page_section).
    """
    del pdf_path  # reserved; unmarked-lines gate is post-process
    reasons: list[str] = []
    messages: list[str] = []

    if page_has_broken_fonts(page):
        reasons.append(REASON_BROKEN_FONTS)
        messages.append(REASON_LABELS_RU[REASON_BROKEN_FONTS])

    if page_is_image_only_scan(page):
        reasons.append(REASON_IMAGE_ONLY_SCAN)
        messages.append(REASON_LABELS_RU[REASON_IMAGE_ONLY_SCAN])

    # уникальные, с сохранением порядка
    seen: set[str] = set()
    uniq_reasons: list[str] = []
    uniq_messages: list[str] = []
    for r, m in zip(reasons, messages):
        if r in seen:
            continue
        seen.add(r)
        uniq_reasons.append(r)
        uniq_messages.append(m)

    return PageSuitability(
        suitable=not uniq_reasons,
        reasons=uniq_reasons,
        messages=uniq_messages,
        page_num=page_num,
    )


def rejected_page_notice_html(result: PageSuitability) -> str:
    """HTML-заглушка для отсеянной страницы (вместо полной конвертации)."""
    reasons = result.messages or [
        REASON_LABELS_RU.get(r, r) for r in result.reasons
    ]
    items = "".join(f"<li>{html.escape(m)}</li>" for m in reasons)
    codes = html.escape(result.reason_codes)
    return (
        f'<div class="page-rejected" role="status" data-rejected="true" '
        f'data-reasons="{codes}">'
        f"<p><strong>Страница {result.page_num} отсеяна</strong> — "
        f"не пригодна для smart-конвертации (будет передана другой модели).</p>"
        f"<ul>{items}</ul>"
        f"</div>"
    )


def format_suitability_report(stats: SuitabilityStats, *, title: str = "Отсев страниц") -> str:
    """Человекочитаемый отчёт для консоли."""
    if stats.total_pages <= 0:
        return f"=== {title} ===\nНет страниц для проверки.\n"

    pct = 100.0 * stats.rejection_rate
    lines = [
        f"=== {title} ===",
        f"Всего страниц:  {stats.total_pages}",
        f"Принято:        {stats.accepted_pages} "
        f"({100.0 - pct:.1f}%)",
        f"Отсеяно:        {stats.rejected_pages} ({pct:.1f}%)",
    ]
    if stats.reason_counts:
        lines.append("Причины отсева:")
        for code, cnt in stats.reason_counts.most_common():
            label = REASON_LABELS_RU.get(code, code)
            lines.append(f"  • {code}: {cnt}  ({label})")

    files_with_rej = [
        (name, acc, rej)
        for name, (acc, rej) in sorted(stats.per_file.items())
        if rej > 0
    ]
    if files_with_rej:
        lines.append("По файлам (есть отсев):")
        for name, acc, rej in files_with_rej:
            total = acc + rej
            # причины по этому файлу
            file_reasons = Counter()
            for pdf_name, _pnum, reasons in stats.rejected_details:
                if pdf_name == name:
                    file_reasons.update(reasons)
            reason_s = ", ".join(
                f"{c}×{n}" for c, n in file_reasons.most_common()
            )
            lines.append(f"  • {name}: {rej}/{total} отсеяно [{reason_s}]")

    if stats.rejected_details and stats.rejected_pages <= 40:
        lines.append("Список отсеянных страниц:")
        for pdf_name, pnum, reasons in stats.rejected_details:
            lines.append(f"  • {pdf_name} стр.{pnum}: {', '.join(reasons)}")
    elif stats.rejected_pages > 40:
        lines.append(
            f"(детальный список скрыт: отсеяно {stats.rejected_pages} стр.; "
            f"см. per_file / reason_counts)"
        )

    lines.append("")
    return "\n".join(lines)


def document_has_broken_fonts(pdf) -> bool:
    """True, если хотя бы на одной странице PDF битые шрифты (совместимость)."""
    return any(page_has_broken_fonts(page) for page in pdf.pages)


# CSS-фрагмент для вставки в DOCUMENT_CSS пайплайна
PAGE_REJECTED_CSS = """
.page-rejected {
  border: 1px solid #a60;
  background: #fff8f0;
  color: #530;
  padding: 0.75em 1em;
  margin: 0 0 1em;
}
.page-rejected ul { margin: 0.4em 0 0 1.2em; }
.page[data-rejected="true"] { opacity: 0.95; }
"""
