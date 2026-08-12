"""Анализ сложности таблиц и роутинг unmarked_table_lines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pdf_suitability.config import (
    DEFAULT_CONFIG,
    DEFAULT_UNMARKED_ROUTING_STRICTNESS,
    SuitabilityConfig,
)


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


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else float(value)


def _strictness_span_thresholds(
    strictness: float, config: SuitabilityConfig
) -> tuple[int, int, int, int]:
    """Пороги span в зависимости от жёсткости."""
    s = _clamp01(strictness)
    base = (
        config.span_cells_route,
        config.span_absorbed_route,
        config.span_cells_large_route,
        config.span_max_large_route,
    )
    default = config.default_unmarked_strictness
    if abs(s - default) < 1e-9:
        return base
    if s < default:
        t = s / default
        mult = 1.0 + (1.0 - t) * 20.0
        return tuple(max(1, int(round(v * mult))) for v in base)  # type: ignore[return-value]
    t = (s - default) / (1.0 - default)
    lows = (1, 1, 1, 2)
    return tuple(  # type: ignore[return-value]
        max(lows[i], int(round(base[i] + (lows[i] - base[i]) * t)))
        for i in range(4)
    )


def _strictness_size_thresholds(
    strictness: float, config: SuitabilityConfig
) -> tuple[int, int, int, int]:
    """Пороги размера grid: (min_cols, min_rows, min_cells, min_cells_hard)."""
    s = _clamp01(strictness)
    base = (
        config.size_min_cols,
        config.size_min_rows,
        config.size_min_cells,
        config.size_min_cells_hard,
    )
    default = config.default_unmarked_strictness
    if abs(s - default) < 1e-9:
        return base
    if s < default:
        t = s / default
        mult = 1.0 + (1.0 - t) * 20.0
        return tuple(max(1, int(round(v * mult))) for v in base)  # type: ignore[return-value]
    t = (s - default) / (1.0 - default)
    lows = (2, 3, 20, 40)
    return tuple(  # type: ignore[return-value]
        max(lows[i], int(round(base[i] + (lows[i] - base[i]) * t)))
        for i in range(4)
    )


def grid_has_complex_span_merges(
    grid: list[list[Any]] | None,
    *,
    strictness: float = DEFAULT_UNMARKED_ROUTING_STRICTNESS,
    config: SuitabilityConfig | None = None,
) -> bool:
    """True, если после обработки таблицы много colspan/rowspan."""
    cfg = config or DEFAULT_CONFIG
    s = grid_span_merge_stats(grid)
    span_cells, absorbed, span_large, max_large = _strictness_span_thresholds(
        strictness, cfg
    )
    if s.span_cells >= span_cells:
        return True
    if s.absorbed >= absorbed:
        return True
    if s.span_cells >= span_large and s.max_span >= max_large:
        return True
    return False


def grid_is_large_table(
    grid: list[list[Any]] | None,
    *,
    strictness: float = DEFAULT_UNMARKED_ROUTING_STRICTNESS,
    config: SuitabilityConfig | None = None,
) -> bool:
    """True, если таблица крупная по размеру grid (не мелкая базовая)."""
    cfg = config or DEFAULT_CONFIG
    s = grid_size_stats(grid)
    min_cols, min_rows, min_cells, min_hard = _strictness_size_thresholds(
        strictness, cfg
    )
    if s.cols < min_cols:
        return False
    if s.cells >= min_hard:
        return True
    return s.rows >= min_rows and s.cells >= min_cells


def grid_needs_unmarked_routing(
    grid: list[list[Any]] | None,
    *,
    route_large_tables: bool = True,
    route_complex_spans: bool = True,
    strictness: float = DEFAULT_UNMARKED_ROUTING_STRICTNESS,
    config: SuitabilityConfig | None = None,
) -> bool:
    """Роутить таблицу: сложные span и/или крупный размер (по политике типа)."""
    cfg = config or DEFAULT_CONFIG
    if route_complex_spans and grid_has_complex_span_merges(
        grid, strictness=strictness, config=cfg
    ):
        return True
    if route_large_tables and grid_is_large_table(
        grid, strictness=strictness, config=cfg
    ):
        return True
    return False


def should_route_unmarked_complex_spans(
    *,
    raster_lines_vectorized: bool,
    grids: list[list[list[Any]]] | None,
    doc_type: str | None = None,
    strictness: float = DEFAULT_UNMARKED_ROUTING_STRICTNESS,
    config: SuitabilityConfig | None = None,
) -> bool:
    """
    Роутинг unmarked_table_lines.

    Только если линии брались с растра (векторизация сработала).

    ``strictness`` ∈ [0, 1]:
      • 0 — не роутить никогда;
      • 1 — роутить любую страницу с векторизованными (растровыми) линиями
        и таблицами;
      • DEFAULT (0.5) — прежние пороги + type-policy.
    """
    cfg = config or DEFAULT_CONFIG
    s = _clamp01(strictness)
    if s <= 0.0 or not grids:
        return False
    if not raster_lines_vectorized and s < cfg.high_strictness_without_raster:
        return False
    if raster_lines_vectorized and s >= 1.0:
        return True

    try:
        # Узкий импорт: routing_policy без heuristics (избегаем циклов).
        from pdf_doc_types import (
            grid_should_bypass_unmarked_routing,
            routing_policy_for,
        )

        policy = routing_policy_for(doc_type)
        route_large = policy.route_large_tables
        route_spans = policy.route_complex_spans
        allow_bypass = True
    except Exception:
        route_large = True
        route_spans = True
        allow_bypass = True

        def grid_should_bypass_unmarked_routing(grid, doc_type):  # type: ignore
            return False

    default = cfg.default_unmarked_strictness
    if s > default:
        t = (s - default) / (1.0 - default)
        if t >= cfg.force_route_large_t:
            route_large = True
        if t >= cfg.force_route_spans_t:
            route_spans = True
        if t >= cfg.bypass_disable_t:
            allow_bypass = False

    for g in grids:
        if allow_bypass and grid_should_bypass_unmarked_routing(g, doc_type):
            continue
        if grid_needs_unmarked_routing(
            g,
            route_large_tables=route_large,
            route_complex_spans=route_spans,
            strictness=s,
            config=cfg,
        ):
            return True
    return False


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
