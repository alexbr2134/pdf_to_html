"""
Единая точка входа: PDF / Word / Excel → семантический HTML.

    from to_html import to_html

    result = to_html("docs/torg12.docx", "out/torg12.html")
    # или .pdf / .xlsx / .xlsm

Роутит по расширению в ``pdf_to_html`` или ``office_to_html``.
Результат один и тот же — ``ConversionResult``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from office_normalize import SUPPORTED_OFFICE
from office_to_html import office_to_html
from pdf_to_html import ConversionResult, RejectedPage, pdf_to_html

__all__ = [
    "to_html",
    "ConversionResult",
    "RejectedPage",
    "SUPPORTED_INPUT",
]

PDF_EXTS = {".pdf"}
SUPPORTED_INPUT = PDF_EXTS | {e.lower() for e in SUPPORTED_OFFICE}


def to_html(
    src_path: str | Path,
    html_path: str | Path,
    *,
    skip_unsuitable: bool = True,
    quiet: bool = True,
    unmarked_routing_strictness: float | None = None,
) -> ConversionResult:
    """
    Конвертирует PDF или Office-файл в HTML.

    - ``.pdf`` → ``pdf_to_html``
    - ``.docx`` / ``.xlsx`` / ``.xlsm`` → ``office_to_html``
    - ``.doc`` / ``.xls`` → тоже через office (сразу роут ``legacy_format``)

    ``unmarked_routing_strictness`` имеет смысл только для PDF;
    для Office игнорируется.
    """
    src = Path(src_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Файл не найден: {src}")

    ext = src.suffix.lower()
    if ext not in SUPPORTED_INPUT:
        raise ValueError(
            f"Неподдерживаемый формат: {ext}. "
            f"Ожидается: {', '.join(sorted(SUPPORTED_INPUT))}"
        )

    if ext in PDF_EXTS:
        kwargs: dict = {
            "skip_unsuitable": skip_unsuitable,
            "quiet": quiet,
        }
        if unmarked_routing_strictness is not None:
            kwargs["unmarked_routing_strictness"] = unmarked_routing_strictness
        return pdf_to_html(src, html_path, **kwargs)

    return office_to_html(
        src,
        html_path,
        skip_unsuitable=skip_unsuitable,
        quiet=quiet,
    )


def _main(argv: Iterable[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="PDF / DOCX / XLSX → HTML"
    )
    parser.add_argument("src", type=Path, help="Входной файл")
    parser.add_argument("html", type=Path, help="Куда сохранить HTML")
    parser.add_argument(
        "--no-skip",
        action="store_true",
        help="Не отсеивать unsuitable-единицы",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--unmarked-strictness",
        type=float,
        default=None,
        help="Только PDF: жёсткость unmarked_table_lines ∈ [0, 1]",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    result = to_html(
        args.src,
        args.html,
        skip_unsuitable=not args.no_skip,
        quiet=not args.verbose,
        unmarked_routing_strictness=args.unmarked_strictness,
    )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"{result.html_path}: {result.accepted_pages}/{result.total_pages} ok, "
            f"{result.rejected_count} route, {result.seconds:.2f}s"
        )
        for page in result.rejected_pages:
            print(f"  #{page.page_num}: {'; '.join(page.reason_labels)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
