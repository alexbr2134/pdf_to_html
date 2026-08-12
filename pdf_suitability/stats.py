"""Статистика и отчёты по отсеву страниц."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from pdf_suitability.core import REASON_LABELS_RU, PageSuitability


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


def format_suitability_report(
    stats: SuitabilityStats, *, title: str = "Отсев страниц"
) -> str:
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
            file_reasons: Counter = Counter()
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
