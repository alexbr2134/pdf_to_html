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
_TEXT_MASK_PAD_PX = 2
# Horizontal-only: bridge gaps cut by text before open (px at zoom≈2).
_H_GAP_CLOSE_PX = 21
# Shorter H open than default V open — printed rules are often broken by glyphs.
_H_OPEN_DIV = 40
_H_OPEN_MIN = 16
# If an H row already covers this fraction of the V span, extend to full V width.
_H_EXTEND_V_FRAC = 0.25


@dataclass
class VectorizedPageSession:
    """Сессия: pdfplumber page + путь/номер + флаг vectorized."""
    page: object
    pdf_path: str
    page_num: int
    vectorized: bool


def _page_area(page) -> float:
    """Площадь страницы pdfplumber (width * height)."""
    return float(page.width) * float(page.height)


def _table_bbox_area(table) -> float:
    """Площадь bbox таблицы."""
    x0, top, x1, bottom = table.bbox
    return max(0.0, x1 - x0) * max(0.0, bottom - top)


def _lines_table_usable(page) -> bool:
    """True, если lines-strategy уже даёт достаточно крупную таблицу."""
    tables = page.find_tables(_TABLE_LINES)
    if not tables:
        return False
    best = max(tables, key=_table_bbox_area)
    if len(best.rows) < _MIN_TABLE_ROWS:
        return False
    return _table_bbox_area(best) >= _page_area(page) * _MIN_TABLE_AREA_RATIO


def _raster_image_cover_ratio(page) -> float:
    """Доля площади страницы, покрытая embedded-изображениями."""
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
    del pdf_path, page_num  # reserved for future fitz drawing checks

    if _lines_table_usable(page):
        return False

    raster_cover = _raster_image_cover_ratio(page)
    if raster_cover >= _RASTER_IMAGE_COVER_RATIO:
        return True

    # Смешанный PDF: текстовый слой поверх частичного растра — только если есть растр.
    if raster_cover >= 0.05 and len(page.lines) < 3 and page.chars:
        text_tables = page.find_tables(
            {"vertical_strategy": "text", "horizontal_strategy": "text"}
        )
        if text_tables:
            return True

    return False


def _mask_to_horizontal_segments(mask: np.ndarray, min_width: int) -> list[tuple[float, float, float, float]]:
    """Контуры маски → горизонтальные сегменты (px), min_width."""
    segments: list[tuple[float, float, float, float]] = []
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if w < min_width:
            continue
        cy = y + h / 2.0
        segments.append((float(x), cy, float(x + w), cy))
    return segments


def _mask_to_vertical_segments(mask: np.ndarray, min_height: int) -> list[tuple[float, float, float, float]]:
    """Контуры маски → вертикальные сегменты (px), min_height."""
    segments: list[tuple[float, float, float, float]] = []
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if h < min_height:
            continue
        cx = x + w / 2.0
        segments.append((cx, float(y), cx, float(y + h)))
    return segments


def _iter_text_span_bboxes_pt(page: fitz.Page) -> list[tuple[float, float, float, float]]:
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


# Public alias used by notebook debug overlays.
iter_text_span_bboxes_pt = _iter_text_span_bboxes_pt


def mask_text_regions(
    img_bgr: np.ndarray,
    text_bboxes_pt: list[tuple[float, float, float, float]],
    *,
    scale: float,
    pad_px: int = _TEXT_MASK_PAD_PX,
) -> np.ndarray:
    """
    White-out text glyph regions on a working copy of the page image.

    Does not modify the PDF — only improves CV line detection.
    scale = pdf_points / pixel (same as render_page_bgr).
    """
    if not text_bboxes_pt:
        return img_bgr
    out = img_bgr.copy()
    h, w = out.shape[:2]
    inv = 1.0 / scale if scale else 1.0
    for x0, y0, x1, y1 in text_bboxes_pt:
        px0 = max(0, int(x0 * inv) - pad_px)
        py0 = max(0, int(y0 * inv) - pad_px)
        px1 = min(w, int(x1 * inv) + pad_px)
        py1 = min(h, int(y1 * inv) + pad_px)
        if px1 > px0 and py1 > py0:
            cv2.rectangle(out, (px0, py0), (px1, py1), (255, 255, 255), thickness=-1)
    return out


def detect_table_line_segments(
    img_bgr: np.ndarray,
    *,
    min_h_len: int | None = None,
    min_v_len: int | None = None,
) -> list[tuple[float, float, float, float]]:
    """Detect horizontal/vertical table grid lines in a page image (pixel coords)."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        15,
        8,
    )

    h, w = binary.shape[:2]
    # H: shorter open + gap close so rules broken by text still reconnect.
    h_len = min_h_len or max(_H_OPEN_MIN, w // _H_OPEN_DIV)
    v_len = min_v_len or max(25, h // 25)

    h_src = cv2.morphologyEx(
        binary,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (_H_GAP_CLOSE_PX, 1)),
    )
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (h_len, 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_len))

    h_mask = cv2.morphologyEx(h_src, cv2.MORPH_OPEN, h_kernel)
    # Second, shorter H pass — catch remaining broken pieces.
    h_len2 = max(10, h_len // 2)
    h_mask2 = cv2.morphologyEx(
        h_src,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (h_len2, 1)),
    )
    v_mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel)

    segments: list[tuple[float, float, float, float]] = []
    segments.extend(_mask_to_horizontal_segments(h_mask, min_width=max(8, h_len // 3)))
    segments.extend(_mask_to_horizontal_segments(h_mask2, min_width=max(8, h_len2 // 2)))
    segments.extend(_mask_to_vertical_segments(v_mask, min_height=v_len // 2))
    return segments


def _cluster_coords(values: list[float], tol: float) -> list[float]:
    """Кластеризация координат с допуском tol → средние кластеров."""
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


def merge_and_snap_segments(
    segments: list[tuple[float, float, float, float]],
    page_w: float,
    page_h: float,
    *,
    snap_tol: float = 3.0,
) -> list[tuple[float, float, float, float]]:
    """Merge nearly collinear segments and snap to a common grid."""
    if not segments:
        return []

    horiz: list[tuple[float, float, float, float]] = []
    vert: list[tuple[float, float, float, float]] = []
    for x0, y0, x1, y1 in segments:
        if abs(y0 - y1) <= snap_tol and abs(x1 - x0) >= abs(y1 - y0):
            horiz.append((min(x0, x1), (y0 + y1) / 2, max(x0, x1), (y0 + y1) / 2))
        elif abs(x0 - x1) <= snap_tol and abs(y1 - y0) >= abs(x1 - x0):
            vert.append(((x0 + x1) / 2, min(y0, y1), (x0 + x1) / 2, max(y0, y1)))

    merged: list[tuple[float, float, float, float]] = []
    vert_merged: list[tuple[float, float, float, float]] = []

    # Verticals unchanged — keep first so H can stretch to crossing V edges.
    if vert:
        x_clusters = _cluster_coords([s[0] for s in vert], snap_tol * 2)
        for x in x_clusters:
            col_segs = [s for s in vert if abs(s[0] - x) <= snap_tol * 2]
            if not col_segs:
                continue
            y0 = min(s[1] for s in col_segs)
            y1 = max(s[3] for s in col_segs)
            if (y1 - y0) >= page_h * 0.7:
                y0, y1 = 0.0, page_h
            vert_merged.append((x, y0, x, y1))
        merged.extend(vert_merged)

    if horiz:
        y_clusters = _cluster_coords([s[1] for s in horiz], snap_tol * 2)
        for y in y_clusters:
            row_segs = [s for s in horiz if abs(s[1] - y) <= snap_tol * 2]
            if not row_segs:
                continue
            x0 = min(s[0] for s in row_segs)
            x1 = max(s[2] for s in row_segs)
            # Stretch to verticals that cross this row (local table width).
            crossing = [
                s for s in vert_merged
                if (s[1] - snap_tol) <= y <= (s[3] + snap_tol)
            ]
            if crossing:
                v_left = min(s[0] for s in crossing)
                v_right = max(s[0] for s in crossing)
                v_span = v_right - v_left
                if v_span > 0 and (x1 - x0) >= v_span * _H_EXTEND_V_FRAC:
                    x0, x1 = v_left, v_right
            elif (x1 - x0) >= page_w * 0.7:
                x0, x1 = 0.0, page_w
            merged.append((x0, y, x1, y))

    return merged


def _segments_px_to_pt(
    segments: list[tuple[float, float, float, float]],
    scale: float,
) -> list[tuple[float, float, float, float]]:
    """Переводит сегменты из пикселей в PDF points (* scale)."""
    return [
        (x0 * scale, y0 * scale, x1 * scale, y1 * scale)
        for x0, y0, x1, y1 in segments
    ]


def _draw_vector_lines(
    page: fitz.Page,
    segments_pt: list[tuple[float, float, float, float]],
) -> None:
    """Рисует векторные линии на странице PyMuPDF (fitz)."""
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

        work = img_bgr
        if mask_text:
            text_bboxes = _iter_text_span_bboxes_pt(src_page)
            work = mask_text_regions(img_bgr, text_bboxes, scale=scale)

        segments_px = detect_table_line_segments(work)
        page_h_px, page_w_px = work.shape[:2]
        segments_pt = _segments_px_to_pt(
            merge_and_snap_segments(
                segments_px, float(page_w_px), float(page_h_px), snap_tol=2.0
            ),
            scale,
        )

        out = fitz.open()
        try:
            out_page = out.new_page(width=rect.width, height=rect.height)
            # Copy original page (text + images + drawings), then overlay lines.
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
            # Safety: if text was somehow lost, fall back to original page.
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
