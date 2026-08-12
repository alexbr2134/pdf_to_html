"""Vectorize raster table grid lines onto a PDF page for pdfplumber lines strategy.

Preserves the original text layer: copies the page via show_pdf_page, then draws
vector lines on top. Text glyph regions are masked only on the CV working image.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import cv2
import fitz
import numpy as np

from pdf_paddle_detection import render_page_bgr

_TABLE_LINES = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
}

_MIN_TABLE_ROWS = 2
_MIN_TABLE_AREA_RATIO = 0.02
_RASTER_IMAGE_COVER_RATIO = 0.45
_LINE_WIDTH_PT = 0.6
_DEFAULT_ZOOM = 2.0
_TEXT_MASK_PAD_PX = 3
_MAX_LINE_THICKNESS_PX = 6
_PARALLEL_TOL_PX = 3.0
_GAP_FRAC = 0.03
_MIN_H_FRAC = 1 / 12  # erode kernel / min length as fraction of page width
_MIN_V_FRAC = 1 / 14


@dataclass
class VectorizedPageSession:
    page: object
    pdf_path: str
    page_num: int
    vectorized: bool


def _page_area(page) -> float:
    return float(page.width) * float(page.height)


def _table_bbox_area(table) -> float:
    x0, top, x1, bottom = table.bbox
    return max(0.0, x1 - x0) * max(0.0, bottom - top)


def _lines_table_usable(page) -> bool:
    tables = page.find_tables(_TABLE_LINES)
    if not tables:
        return False
    best = max(tables, key=_table_bbox_area)
    if len(best.rows) < _MIN_TABLE_ROWS:
        return False
    return _table_bbox_area(best) >= _page_area(page) * _MIN_TABLE_AREA_RATIO


def _raster_image_cover_ratio(page) -> float:
    area = _page_area(page)
    if area <= 0 or not page.images:
        return 0.0
    covered = 0.0
    for img in page.images:
        w = float(img.get("width") or img.get("x1", 0) - img.get("x0", 0))
        h = float(img.get("height") or img.get("bottom", 0) - img.get("top", 0))
        covered += max(0.0, w) * max(0.0, h)
    return min(1.0, covered / area)


def page_needs_line_vectorization(page, pdf_path: str | Path | None = None, page_num: int | None = None) -> bool:
    """True if pdfplumber lines strategy is weak and raster lines should be redrawn."""
    del pdf_path, page_num

    if _lines_table_usable(page):
        return False

    raster_cover = _raster_image_cover_ratio(page)
    if raster_cover >= _RASTER_IMAGE_COVER_RATIO:
        return True

    if raster_cover >= 0.05 and len(page.lines) < 3 and page.chars:
        text_tables = page.find_tables(
            {"vertical_strategy": "text", "horizontal_strategy": "text"}
        )
        if text_tables:
            return True

    return False


def iter_text_span_bboxes_pt(page: fitz.Page) -> list[tuple[float, float, float, float]]:
    """Glyph span bboxes in PDF points (for CV masking only)."""
    bboxes: list[tuple[float, float, float, float]] = []
    try:
        data = page.get_text("dict")
    except Exception:
        return bboxes
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bbox = span.get("bbox")
                if not bbox or len(bbox) < 4:
                    continue
                x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
                if x1 <= x0 or y1 <= y0:
                    continue
                bboxes.append((x0, y0, x1, y1))
    return bboxes


_iter_text_span_bboxes_pt = iter_text_span_bboxes_pt


def mask_text_regions(
    img_bgr: np.ndarray,
    text_bboxes_pt: list[tuple[float, float, float, float]],
    *,
    scale: float,
    pad_px: int = _TEXT_MASK_PAD_PX,
) -> np.ndarray:
    """White-out text glyph regions on a working copy (CV only; PDF unchanged)."""
    if not text_bboxes_pt:
        return img_bgr
    out = img_bgr.copy()
    h, w = out.shape[:2]
    inv = 1.0 / scale if scale else 1.0
    pad_y = pad_px + 2
    for x0, y0, x1, y1 in text_bboxes_pt:
        px0 = max(0, int(x0 * inv) - pad_px)
        py0 = max(0, int(y0 * inv) - pad_y)
        px1 = min(w, int(x1 * inv) + pad_px)
        py1 = min(h, int(y1 * inv) + pad_y)
        if px1 > px0 and py1 > py0:
            cv2.rectangle(out, (px0, py0), (px1, py1), (255, 255, 255), thickness=-1)
    return out


def _dark_ratio(img_bgr: np.ndarray, thr: int = 140) -> float:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    return float((gray < thr).mean())


def _prepare_work_image(
    img_bgr: np.ndarray,
    *,
    text_bboxes_pt: list[tuple[float, float, float, float]] | None,
    scale: float,
    mask_text: bool,
) -> np.ndarray:
    """Mask text for CV, but fall back if masking wiped almost all ink (lines under OCR boxes)."""
    if not mask_text or not text_bboxes_pt:
        return img_bgr
    work = mask_text_regions(img_bgr, text_bboxes_pt, scale=scale)
    if _dark_ratio(work) < 0.12 * max(_dark_ratio(img_bgr), 1e-6):
        return img_bgr
    return work


def _binary_for_lines(gray: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    adaptive = cv2.adaptiveThreshold(
        blur,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        15,
        9,
    )
    # Keep only reasonably dark strokes (drop light gray noise / anti-alias).
    dark = (gray < 150).astype(np.uint8) * 255
    return cv2.bitwise_and(adaptive, dark)


def _extract_axis_mask(binary: np.ndarray, *, horizontal: bool) -> tuple[np.ndarray, int]:
    """Classic erode→dilate line extract: only long continuous strokes survive."""
    h, w = binary.shape[:2]
    if horizontal:
        length = max(30, int(w * _MIN_H_FRAC))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (length, 1))
    else:
        length = max(30, int(h * _MIN_V_FRAC))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, length))
    mask = cv2.erode(binary, kernel)
    mask = cv2.dilate(mask, kernel)
    return mask, length


def _mask_to_horizontal_segments(
    mask: np.ndarray,
    min_width: int,
    max_thickness: int = _MAX_LINE_THICKNESS_PX,
) -> list[tuple[float, float, float, float]]:
    segments: list[tuple[float, float, float, float]] = []
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if w < min_width or h > max_thickness:
            continue
        cy = y + h / 2.0
        segments.append((float(x), cy, float(x + w), cy))
    return segments


def _mask_to_vertical_segments(
    mask: np.ndarray,
    min_height: int,
    max_thickness: int = _MAX_LINE_THICKNESS_PX,
) -> list[tuple[float, float, float, float]]:
    segments: list[tuple[float, float, float, float]] = []
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if h < min_height or w > max_thickness:
            continue
        cx = x + w / 2.0
        segments.append((cx, float(y), cx, float(y + h)))
    return segments


def _snap_horizontal_to_gutter(
    gray: np.ndarray,
    x0: float,
    y: float,
    x1: float,
    *,
    search: int = 4,
) -> float:
    """Move H-line to the whitest nearby row (between text lines)."""
    h, w = gray.shape[:2]
    xa, xb = max(0, int(x0)), min(w, int(x1) + 1)
    if xb <= xa:
        return y
    best_y, best_score = int(round(y)), -1.0
    for dy in range(-search, search + 1):
        yy = int(round(y)) + dy
        if 0 <= yy < h:
            score = float(gray[yy, xa:xb].mean())
            if score > best_score:
                best_score = score
                best_y = yy
    return float(best_y)


def _snap_vertical_to_gutter(
    gray: np.ndarray,
    x: float,
    y0: float,
    y1: float,
    *,
    search: int = 4,
) -> float:
    h, w = gray.shape[:2]
    ya, yb = max(0, int(y0)), min(h, int(y1) + 1)
    if yb <= ya:
        return x
    best_x, best_score = int(round(x)), -1.0
    for dx in range(-search, search + 1):
        xx = int(round(x)) + dx
        if 0 <= xx < w:
            score = float(gray[ya:yb, xx].mean())
            if score > best_score:
                best_score = score
                best_x = xx
    return float(best_x)


def _cluster_coords(values: list[float], tol: float) -> list[float]:
    if not values:
        return []
    values = sorted(values)
    clusters: list[list[float]] = [[values[0]]]
    for val in values[1:]:
        if abs(val - clusters[-1][-1]) <= tol:
            clusters[-1].append(val)
        else:
            clusters.append([val])
    return [sum(cl) / len(cl) for cl in clusters]


def _merge_intervals(intervals: list[tuple[float, float]], max_gap: float) -> list[tuple[float, float]]:
    if not intervals:
        return []
    ordered = sorted((min(a, b), max(a, b)) for a, b in intervals)
    merged: list[list[float]] = [[ordered[0][0], ordered[0][1]]]
    for a, b in ordered[1:]:
        if a <= merged[-1][1] + max_gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _dedupe_parallel_lines(
    lines: list[tuple[float, float, float, float]],
    *,
    horizontal: bool,
    tol: float = _PARALLEL_TOL_PX,
) -> list[tuple[float, float, float, float]]:
    if not lines:
        return []
    key_idx = 1 if horizontal else 0
    ordered = sorted(lines, key=lambda s: s[key_idx])
    groups: list[list[tuple[float, float, float, float]]] = [[ordered[0]]]
    for seg in ordered[1:]:
        if abs(seg[key_idx] - groups[-1][-1][key_idx]) <= tol:
            groups[-1].append(seg)
        else:
            groups.append([seg])
    out: list[tuple[float, float, float, float]] = []
    for group in groups:
        if horizontal:
            y = sum(s[1] for s in group) / len(group)
            out.append((min(s[0] for s in group), y, max(s[2] for s in group), y))
        else:
            x = sum(s[0] for s in group) / len(group)
            out.append((x, min(s[1] for s in group), x, max(s[3] for s in group)))
    return out


def _rule_support_ratio(
    line_mask: np.ndarray,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    step: int = 3,
    band: int = 1,
) -> float:
    h, w = line_mask.shape[:2]
    dx, dy = x1 - x0, y1 - y0
    length = max(abs(dx), abs(dy), 1.0)
    n = max(2, int(length // step) + 1)
    hits = 0
    for i in range(n):
        t = i / (n - 1)
        cx = int(round(x0 + dx * t))
        cy = int(round(y0 + dy * t))
        found = False
        for oy in range(-band, band + 1):
            for ox in range(-band, band + 1):
                xx, yy = cx + ox, cy + oy
                if 0 <= xx < w and 0 <= yy < h and line_mask[yy, xx]:
                    found = True
                    break
            if found:
                break
        if found:
            hits += 1
    return hits / n


def detect_table_line_segments(
    img_bgr: np.ndarray,
    *,
    min_h_len: int | None = None,
    min_v_len: int | None = None,
    return_line_mask: bool = False,
):
    """
    Detect H/V table rules via erode→dilate (long continuous strokes only).

    If return_line_mask=True, returns (segments, line_mask).
    """
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    binary = _binary_for_lines(gray)
    h_mask, h_len = _extract_axis_mask(binary, horizontal=True)
    v_mask, v_len = _extract_axis_mask(binary, horizontal=False)
    if min_h_len is not None:
        h_len = min_h_len
    if min_v_len is not None:
        v_len = min_v_len

    segments = _mask_to_horizontal_segments(h_mask, min_width=h_len)
    segments += _mask_to_vertical_segments(v_mask, min_height=v_len)

    # Snap to whitespace gutters so rules sit between rows/cols, not through glyphs.
    snapped: list[tuple[float, float, float, float]] = []
    for x0, y0, x1, y1 in segments:
        if abs(y0 - y1) <= 1e-3:
            y = _snap_horizontal_to_gutter(gray, x0, y0, x1)
            snapped.append((x0, y, x1, y))
        else:
            x = _snap_vertical_to_gutter(gray, x0, y0, y1)
            snapped.append((x, y0, x, y1))

    line_mask = cv2.bitwise_or(h_mask, v_mask)
    if return_line_mask:
        return snapped, line_mask
    return snapped


def merge_and_snap_segments(
    segments: list[tuple[float, float, float, float]],
    page_w: float,
    page_h: float,
    *,
    snap_tol: float = 3.0,
    line_mask: np.ndarray | None = None,
) -> list[tuple[float, float, float, float]]:
    """
    Merge collinear segments, bridge small gaps, collapse double edges.

    Does not stretch lines to the full page — keeps real extents.
    """
    if not segments:
        return []

    max_gap = max(8.0, page_w * _GAP_FRAC)
    min_h = max(28.0, page_w * _MIN_H_FRAC * 0.85)
    min_v = max(28.0, page_h * _MIN_V_FRAC * 0.85)

    horiz: list[tuple[float, float, float, float]] = []
    vert: list[tuple[float, float, float, float]] = []
    for x0, y0, x1, y1 in segments:
        if abs(y0 - y1) <= snap_tol and abs(x1 - x0) >= abs(y1 - y0):
            horiz.append((min(x0, x1), (y0 + y1) / 2, max(x0, x1), (y0 + y1) / 2))
        elif abs(x0 - x1) <= snap_tol and abs(y1 - y0) >= abs(x1 - x0):
            vert.append(((x0 + x1) / 2, min(y0, y1), (x0 + x1) / 2, max(y0, y1)))

    merged: list[tuple[float, float, float, float]] = []

    if horiz:
        for y in _cluster_coords([s[1] for s in horiz], snap_tol * 2):
            row = [s for s in horiz if abs(s[1] - y) <= snap_tol * 2]
            intervals = _merge_intervals([(s[0], s[2]) for s in row], max_gap)
            # Only bridge a larger span if the morphology mask supports it.
            if line_mask is not None and len(intervals) > 1:
                bridged: list[tuple[float, float]] = [intervals[0]]
                for a, b in intervals[1:]:
                    prev_a, prev_b = bridged[-1]
                    gap = a - prev_b
                    if 0 < gap <= max_gap * 2.5 and _rule_support_ratio(
                        line_mask, prev_b, y, a, y
                    ) >= 0.35:
                        bridged[-1] = (prev_a, b)
                    else:
                        bridged.append((a, b))
                intervals = bridged
            for x0, x1 in intervals:
                if x1 - x0 >= min_h:
                    merged.append((x0, y, x1, y))

    if vert:
        for x in _cluster_coords([s[0] for s in vert], snap_tol * 2):
            col = [s for s in vert if abs(s[0] - x) <= snap_tol * 2]
            intervals = _merge_intervals([(s[1], s[3]) for s in col], max_gap)
            if line_mask is not None and len(intervals) > 1:
                bridged = [intervals[0]]
                for a, b in intervals[1:]:
                    prev_a, prev_b = bridged[-1]
                    gap = a - prev_b
                    if 0 < gap <= max_gap * 2.5 and _rule_support_ratio(
                        line_mask, x, prev_b, x, a
                    ) >= 0.35:
                        bridged[-1] = (prev_a, b)
                    else:
                        bridged.append((a, b))
                intervals = bridged
            for y0, y1 in intervals:
                if y1 - y0 >= min_v:
                    merged.append((x, y0, x, y1))

    horiz_out = [s for s in merged if abs(s[1] - s[3]) <= snap_tol]
    vert_out = [s for s in merged if abs(s[0] - s[2]) <= snap_tol]
    return (
        _dedupe_parallel_lines(horiz_out, horizontal=True)
        + _dedupe_parallel_lines(vert_out, horizontal=False)
    )


def _segments_px_to_pt(
    segments: list[tuple[float, float, float, float]],
    scale: float,
) -> list[tuple[float, float, float, float]]:
    return [
        (x0 * scale, y0 * scale, x1 * scale, y1 * scale)
        for x0, y0, x1, y1 in segments
    ]


def _draw_vector_lines(
    page: fitz.Page,
    segments_pt: list[tuple[float, float, float, float]],
) -> None:
    if not segments_pt:
        return
    shape = page.new_shape()
    for x0, y0, x1, y1 in segments_pt:
        shape.draw_line(fitz.Point(x0, y0), fitz.Point(x1, y1))
    shape.finish(color=(0, 0, 0), width=_LINE_WIDTH_PT)
    shape.commit()


def vectorize_page_to_pdf(
    pdf_path: str | Path,
    page_num: int,
    out_path: str | Path,
    *,
    zoom: float = _DEFAULT_ZOOM,
    mask_text: bool = True,
) -> int:
    """
    Detect raster table lines and overlay vector lines on a copy of the page.

    The original text layer and content stream are preserved via show_pdf_page.
    Text is masked only on the temporary CV image when mask_text=True.
    Returns the number of vector lines drawn.
    """
    pdf_path = Path(pdf_path)
    out_path = Path(out_path)

    img_bgr, scale = render_page_bgr(pdf_path, page_num, zoom=zoom)

    src = fitz.open(str(pdf_path))
    try:
        src_page = src[page_num - 1]
        rect = src_page.rect

        text_bboxes = iter_text_span_bboxes_pt(src_page) if mask_text else []
        work = _prepare_work_image(
            img_bgr,
            text_bboxes_pt=text_bboxes,
            scale=scale,
            mask_text=mask_text,
        )

        segments_px, line_mask = detect_table_line_segments(work, return_line_mask=True)
        page_h_px, page_w_px = work.shape[:2]
        segments_pt = _segments_px_to_pt(
            merge_and_snap_segments(
                segments_px,
                float(page_w_px),
                float(page_h_px),
                snap_tol=2.0,
                line_mask=line_mask,
            ),
            scale,
        )

        out = fitz.open()
        try:
            out_page = out.new_page(width=rect.width, height=rect.height)
            out_page.show_pdf_page(rect, src, page_num - 1)
            _draw_vector_lines(out_page, segments_pt)
            out.save(str(out_path))
        finally:
            out.close()
    finally:
        src.close()

    return len(segments_pt)


@contextmanager
def vectorized_page_session(
    pdf_path: str | Path,
    page_num: int,
    *,
    zoom: float = _DEFAULT_ZOOM,
) -> Iterator[VectorizedPageSession]:
    """
    Yield a pdfplumber page, vectorizing raster table lines when needed.

    When vectorized, the session uses a temporary single-page PDF that keeps
    the original text layer and adds vector grid lines on top.
    """
    import pdfplumber

    pdf_path = Path(pdf_path)
    with pdfplumber.open(str(pdf_path)) as pdf:
        orig_page = pdf.pages[page_num - 1]
        needs = page_needs_line_vectorization(orig_page, pdf_path, page_num)
        chars_before = len(orig_page.chars)

    if not needs:
        with pdfplumber.open(str(pdf_path)) as pdf:
            yield VectorizedPageSession(
                page=pdf.pages[page_num - 1],
                pdf_path=str(pdf_path),
                page_num=page_num,
                vectorized=False,
            )
        return

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_path = tmp.name
    tmp.close()
    line_count = 0
    try:
        line_count = vectorize_page_to_pdf(pdf_path, page_num, tmp_path, zoom=zoom)
        pdf = pdfplumber.open(tmp_path)
        try:
            page = pdf.pages[0]
            if chars_before > 0 and len(page.chars) == 0:
                pdf.close()
                pdf = None
                with pdfplumber.open(str(pdf_path)) as orig:
                    yield VectorizedPageSession(
                        page=orig.pages[page_num - 1],
                        pdf_path=str(pdf_path),
                        page_num=page_num,
                        vectorized=False,
                    )
                return
            yield VectorizedPageSession(
                page=page,
                pdf_path=tmp_path,
                page_num=1,
                vectorized=line_count > 0,
            )
        finally:
            if pdf is not None:
                pdf.close()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
