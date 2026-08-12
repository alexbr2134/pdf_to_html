"""DocTypeDetector — скоринг типа документа по тексту и пути."""

from __future__ import annotations

from doc_type.config import DEFAULT_THRESHOLDS, DetectionThresholds
from doc_type.core import DocType, DocTypeResult
from doc_type.detection.disambiguate import (
    disambiguate_ks2_vs_ks3,
    disambiguate_upd_vs_sf,
)
from doc_type.detection.signals import PATH_HINTS, TYPE_SIGNALS


def page_text(page) -> str:
    """Текст страницы для эвристик (короткий / полный)."""
    thr = DEFAULT_THRESHOLDS
    try:
        text = page.extract_text() or ""
    except Exception:
        text = ""
    if len(text) < thr.min_page_text_for_extract:
        try:
            words = page.extract_words() or []
            text = " ".join(w.get("text", "") for w in words)
        except Exception:
            pass
    return text


# backward-compatible alias
_page_text = page_text


class DocTypeDetector:
    """Определяет тип документа по тексту страницы и слабо — по пути файла."""

    def __init__(self, thresholds: DetectionThresholds | None = None) -> None:
        self.thresholds = thresholds or DEFAULT_THRESHOLDS

    def detect(
        self,
        page=None,
        *,
        text: str | None = None,
        pdf_path: str | None = None,
        fallback: DocType | str | None = None,
    ) -> DocTypeResult:
        thr = self.thresholds
        raw = text if text is not None else (page_text(page) if page is not None else "")
        scores: dict[DocType, float] = {dt: 0.0 for dt in DocType if dt != DocType.UNKNOWN}
        hit_signals: list[str] = []

        for dt, weight, pat in TYPE_SIGNALS:
            if pat.search(raw):
                scores[dt] += weight
                hit_signals.append(f"{dt.value}:{pat.pattern[:40]}")

        disambiguate_upd_vs_sf(scores, raw, thr)
        disambiguate_ks2_vs_ks3(scores, raw, thr)

        path_bonus = 0.0
        path_type: DocType | None = None
        if pdf_path:
            for dt, pat in PATH_HINTS:
                if pat.search(str(pdf_path)):
                    path_type = dt
                    path_bonus = thr.path_bonus_score
                    scores[dt] += path_bonus
                    hit_signals.append(f"path:{dt.value}")
                    break
            if path_type == DocType.UPD and scores[DocType.INVOICE_SF] > 0:
                scores[DocType.UPD] += thr.path_upd_over_sf_bonus
                scores[DocType.INVOICE_SF] *= thr.path_invoice_sf_dampen
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

        if best_score < thr.min_confidence_for_type:
            if (
                path_type is not None
                and best_type == path_type
                and best_score >= thr.path_only_min_score
            ):
                return DocTypeResult(
                    path_type,
                    thr.path_bonus_confidence,
                    tuple(hit_signals) + ("path_only",),
                )
            if fallback is not None:
                fb = DocType(fallback) if isinstance(fallback, str) else fallback
                if fb != DocType.UNKNOWN:
                    return DocTypeResult(
                        fb,
                        thr.fallback_confidence,
                        tuple(hit_signals) + ("fallback",),
                    )
            return DocTypeResult(DocType.UNKNOWN, 0.0, tuple(hit_signals))

        conf = min(
            1.0, best_score / (best_score + second + thr.conf_second_smooth)
        )
        if (
            best_score - second < thr.close_scores_gap
            and path_type
            and path_type == best_type
        ):
            conf = max(conf, thr.path_bonus_confidence)
        if conf < thr.min_confidence_known:
            if path_type is not None and best_type == path_type:
                return DocTypeResult(
                    path_type,
                    thr.tiebreak_confidence,
                    tuple(hit_signals) + ("path_tiebreak",),
                )
            if fallback is not None:
                fb = DocType(fallback) if isinstance(fallback, str) else fallback
                if fb != DocType.UNKNOWN:
                    return DocTypeResult(
                        fb,
                        thr.fallback_confidence,
                        tuple(hit_signals) + ("fallback",),
                    )
            return DocTypeResult(DocType.UNKNOWN, conf, tuple(hit_signals))

        return DocTypeResult(best_type, conf, tuple(hit_signals))


_DEFAULT_DETECTOR = DocTypeDetector()


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
    return _DEFAULT_DETECTOR.detect(
        page, text=text, pdf_path=pdf_path, fallback=fallback
    )
