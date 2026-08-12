"""
Входные форматы Office.

Работаем только с OOXML (.docx / .xlsx / .xlsm).
Старые .doc и .xls не конвертируем — их отсекает роутинг выше.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


OOXML_WORD = {".docx"}
OOXML_EXCEL = {".xlsx", ".xlsm"}
LEGACY_WORD = {".doc"}
LEGACY_EXCEL = {".xls"}
SUPPORTED_WORD = OOXML_WORD | LEGACY_WORD
SUPPORTED_EXCEL = OOXML_EXCEL | LEGACY_EXCEL
SUPPORTED_OFFICE = SUPPORTED_WORD | SUPPORTED_EXCEL

# то, что реально парсим
PARSEABLE_OFFICE = OOXML_WORD | OOXML_EXCEL


@dataclass(frozen=True)
class NormalizeResult:
    path: Path
    original_path: Path
    format: str  # docx | xlsx | xlsm
    converted: bool = False
    converter: str | None = None
    structure_lossy: bool = False
    warnings: tuple[str, ...] = ()
    cleanup_dir: Path | None = None


class NormalizeError(RuntimeError):
    def __init__(self, message: str, *, reason: str = "legacy_format"):
        super().__init__(message)
        self.reason = reason


def normalize_office_file(path: str | Path, *, work_dir: Path | None = None) -> NormalizeResult:
    """
    Проверяет формат и возвращает путь as-is для OOXML.

    .doc / .xls → NormalizeError(reason='legacy_format').
    work_dir оставлен для совместимости вызовов, не используется.
    """
    del work_dir
    src = Path(path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Файл не найден: {src}")

    ext = src.suffix.lower()
    if ext in LEGACY_WORD or ext in LEGACY_EXCEL:
        raise NormalizeError(
            f"Формат {ext} не поддерживается (нужен .docx / .xlsx). "
            f"Файл отсеян роутингом legacy_format.",
            reason="legacy_format",
        )
    if ext not in PARSEABLE_OFFICE:
        raise NormalizeError(
            f"Неподдерживаемый формат: {ext}. Ожидается docx/xlsx/xlsm.",
            reason="legacy_format",
        )

    if ext in OOXML_WORD:
        return NormalizeResult(path=src, original_path=src, format="docx")
    return NormalizeResult(
        path=src,
        original_path=src,
        format=ext.lstrip("."),
    )


def cleanup_normalize(result: NormalizeResult) -> None:
    """Раньше чистил temp после конвертации; сейчас no-op."""
    del result
