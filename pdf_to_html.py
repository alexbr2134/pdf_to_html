"""
Библиотека: векторный PDF → семантический HTML (smart-пайплайн).

Использование::

    from pdf_to_html import pdf_to_html

    result = pdf_to_html("docs/balans.pdf", "out/balans.html")
    print(result.accepted_pages, result.rejected_pages)
    for page in result.rejected_pages:
        print(page.page_num, page.reasons, page.messages)

Что делает ``pdf_to_html``
    1. Открывает PDF (pdfplumber).
    2. Постранично: детект типа документа → suitability → сборка HTML
       (таблицы + текст) либо заглушка отсева.
    3. Пишет один HTML-файл.
    4. Возвращает ``ConversionResult`` с принятыми/отсеянными страницами.

Отсеянные страницы (роутинг)
    Страница остаётся в HTML как предупреждение, но smart-сборка не делается.
    Типичные причины (коды в ``RejectedPage.reasons``):

    - ``broken_fonts`` — битая кодировка / OCR-garble / перевёрнутая кириллица
    - ``image_only_scan`` — почти нет текстового слоя
    - ``unmarked_table_lines`` — линии с растра + тяжёлая сетка после обработки

Реализация
    Сборка страниц — ``pdf_html_pipeline.py`` (обычный Python-модуль).
    Детект таблиц / роутинг / типы — ``pdf_table_engine.py``,
    ``page_suitability.py``, ``pdf_doc_types.py``.
    Ноутбук ``pdf_to_html_smart.ipynb`` в runtime не используется.

Пересборка пайплайна из ноутбука (только если правили стенд)::

    python scripts/extract_pipeline.py
"""

from __future__ import annotations

import contextlib
import io
import json
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pdfplumber

from page_suitability import (
    REASON_BROKEN_FONTS,
    REASON_IMAGE_ONLY_SCAN,
    REASON_LABELS_RU,
    REASON_UNMARKED_TABLE_LINES,
)
from pdf_html_pipeline import (
    SuitabilityStats,
    build_page_section,
    document_has_broken_fonts,
    finalize_document_html,
)
from pdf_table_engine import _suppress_scan_noise

__all__ = [
    "RejectedPage",
    "ConversionResult",
    "pdf_to_html",
    "REASON_BROKEN_FONTS",
    "REASON_IMAGE_ONLY_SCAN",
    "REASON_UNMARKED_TABLE_LINES",
    "REASON_LABELS_RU",
]


# ---------------------------------------------------------------------------
# Результат конвертации
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RejectedPage:
    """Страница, отсеянная роутингом (не пригодна для smart-сборки)."""

    page_num: int
    """1-based номер страницы в PDF."""

    reasons: tuple[str, ...]
    """Стабильные коды причин (см. REASON_*)."""

    messages: tuple[str, ...] = ()
    """Человекочитаемые пояснения (если пайплайн их вернул)."""

    doc_type: str | None = None
    """Определённый тип документа на странице, если есть."""

    @property
    def reason_labels(self) -> list[str]:
        """Причины по-русски (для логов / UI)."""
        return [REASON_LABELS_RU.get(r, r) for r in self.reasons]


@dataclass
class ConversionResult:
    """Итог ``pdf_to_html``: куда записали HTML и что потеряли."""

    pdf_path: Path
    html_path: Path
    total_pages: int
    accepted_pages: int
    rejected_pages: list[RejectedPage] = field(default_factory=list)
    seconds: float = 0.0
    """Время полного прогона (все страницы), секунды."""

    doc_types: list[str] = field(default_factory=list)
    """Тип документа по страницам (в порядке 1..N); ``unknown`` / пусто допустимы."""

    warnings: list[str] = field(default_factory=list)
    """Доп. проблемы документа (например, битые шрифты на уровне PDF)."""

    @property
    def rejected_count(self) -> int:
        return len(self.rejected_pages)

    @property
    def all_accepted(self) -> bool:
        """True, если ни одна страница не отсеяна."""
        return self.rejected_count == 0

    @property
    def rejection_rate(self) -> float:
        if self.total_pages <= 0:
            return 0.0
        return self.rejected_count / self.total_pages

    def rejected_page_nums(self) -> list[int]:
        return [p.page_num for p in self.rejected_pages]

    def reasons_summary(self) -> dict[str, int]:
        """Счётчик кодов причин по отсеянным страницам."""
        counts: dict[str, int] = {}
        for page in self.rejected_pages:
            for reason in page.reasons:
                counts[reason] = counts.get(reason, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        """Сериализуемое представление (логи, JSON)."""
        return {
            "pdf_path": str(self.pdf_path),
            "html_path": str(self.html_path),
            "total_pages": self.total_pages,
            "accepted_pages": self.accepted_pages,
            "rejected_count": self.rejected_count,
            "rejection_rate": round(self.rejection_rate, 4),
            "seconds": round(self.seconds, 4),
            "doc_types": list(self.doc_types),
            "warnings": list(self.warnings),
            "reasons_summary": self.reasons_summary(),
            "rejected_pages": [
                {
                    "page_num": p.page_num,
                    "reasons": list(p.reasons),
                    "messages": list(p.messages),
                    "reason_labels": p.reason_labels,
                    "doc_type": p.doc_type,
                }
                for p in self.rejected_pages
            ],
        }


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------


def pdf_to_html(
    pdf_path: str | Path,
    html_path: str | Path,
    *,
    skip_unsuitable: bool = True,
    quiet: bool = True,
) -> ConversionResult:
    """
    Конвертирует PDF в HTML постранично и сохраняет результат.

    Parameters
    ----------
    pdf_path:
        Путь к исходному PDF.
    html_path:
        Куда записать HTML (родительские каталоги создаются при необходимости).
    skip_unsuitable:
        Если True (по умолчанию) — страницы с ``broken_fonts`` /
        ``image_only_scan`` / ``unmarked_table_lines`` не собираются smart'ом:
        в HTML попадает заглушка, страница попадает в ``rejected_pages``.
    quiet:
        Если True — не печатать служебные сообщения пайплайна в stdout.

    Returns
    -------
    ConversionResult
        Пути, счётчики, список отсеянных страниц с причинами, время.

    Raises
    ------
    FileNotFoundError
        Нет PDF.
    OSError
        Не удалось записать HTML.
    """
    src = Path(pdf_path).expanduser().resolve()
    dst = Path(html_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"PDF не найден: {src}")

    _suppress_scan_noise()
    warnings.filterwarnings("ignore")

    suitability_stats = SuitabilityStats()
    page_sections: list[str] = []
    rejected: list[RejectedPage] = []
    doc_types: list[str] = []
    doc_warnings: list[str] = []
    doc_type_fallback = None
    accepted = 0

    t0 = time.perf_counter()
    with pdfplumber.open(src) as pdf:
        total = len(pdf.pages)
        for page_num, page in enumerate(pdf.pages, start=1):
            section, suitability = _call_quiet(
                quiet,
                build_page_section,
                page,
                page_num=page_num,
                pdf_path=str(src),
                skip_unsuitable=skip_unsuitable,
                doc_type_fallback=doc_type_fallback,
            )

            doc_type = getattr(suitability, "doc_type", None)
            if doc_type:
                doc_type_fallback = doc_type
            doc_types.append(str(doc_type) if doc_type else "unknown")

            suitability_stats.record(src.name, suitability)
            page_sections.append(section)

            if suitability.suitable:
                accepted += 1
            else:
                rejected.append(
                    RejectedPage(
                        page_num=page_num,
                        reasons=tuple(suitability.reasons or ()),
                        messages=tuple(getattr(suitability, "messages", None) or ()),
                        doc_type=str(doc_type) if doc_type else None,
                    )
                )

        if document_has_broken_fonts(pdf):
            doc_warnings.append("Документ содержит плохо размеченный текст")

        html_doc = _call_quiet(
            quiet,
            finalize_document_html,
            pdf,
            page_sections,
            title=src.stem,
            source_name=src.name,
            suitability_stats=suitability_stats,
        )

    elapsed = time.perf_counter() - t0
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(html_doc, encoding="utf-8")

    return ConversionResult(
        pdf_path=src,
        html_path=dst,
        total_pages=total,
        accepted_pages=accepted,
        rejected_pages=rejected,
        seconds=elapsed,
        doc_types=doc_types,
        warnings=doc_warnings,
    )


def _call_quiet(quiet: bool, fn, *args, **kwargs):
    """Вызывает fn; при quiet=True глушит stdout на время вызова."""
    if not quiet:
        return fn(*args, **kwargs)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# Удобный запуск из командной строки
# ---------------------------------------------------------------------------


def _main(argv: Iterable[str] | None = None) -> int:
    """``python pdf_to_html.py input.pdf output.html``."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Smart PDF → HTML (локальная утилита, не HTTP API)."
    )
    parser.add_argument("pdf", type=Path, help="Путь к PDF")
    parser.add_argument("html", type=Path, help="Куда сохранить HTML")
    parser.add_argument(
        "--no-skip",
        action="store_true",
        help="Не отсеивать unsuitable-страницы (пытаться собрать всё)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Печатать сообщения пайплайна",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Печать ConversionResult.to_dict() в stdout",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    result = pdf_to_html(
        args.pdf,
        args.html,
        skip_unsuitable=not args.no_skip,
        quiet=not args.verbose,
    )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"{result.html_path} — {result.accepted_pages}/{result.total_pages} принято, "
            f"{result.rejected_count} отсеяно, {result.seconds:.2f} с"
        )
        for page in result.rejected_pages:
            labels = "; ".join(page.reason_labels)
            print(f"  стр.{page.page_num}: {labels}")
        for w in result.warnings:
            print(f"  предупреждение: {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
