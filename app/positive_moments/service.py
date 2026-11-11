"""
Analyse one lecture's canonical transcript for Positive Moments.

    current analysis of this exact transcript under this policy?  -> reuse
    provable legacy V5 result?                                  -> import
    otherwise (and only when a model is allowed)                -> selector
                                                                   -> validation
                                                                   -> verifier

Every outcome is persisted with its lineage: lecture, document, document
source fingerprint, transcript content fingerprint, policy version, source.
A provider failure is persisted as FAILED with a safe code, so it shows in the
UI and is retried explicitly - never silently re-bought every cycle.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.positive_moments import ai
from app.positive_moments import policy as p
from app.positive_moments.legacy_import import IMPORTED, try_import
from app.positive_moments.validation import validate
from app.qa.provider import ProviderError

MOMENTS_FOUND = "MOMENTS_FOUND"
NO_POSITIVE_MOMENTS = "NO_POSITIVE_MOMENTS"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
FAILED = "FAILED"

# Lecture-level analysis states (the dashboard vocabulary).
NOT_ANALYZED = "NOT_ANALYZED"
ANALYZING = "ANALYZING"
STALE = "STALE"


@dataclass
class AnalysisOutcome:
    status: str
    analysis: dict | None
    reused: bool = False
    source: str | None = None
    detail: str | None = None
    provider_calls: int = 0


def analysis_state(latest: dict | None, current: dict | None) -> str:
    """NOT_ANALYZED / STALE / the current analysis's own status."""
    if current is not None:
        return current["status"]
    if latest is None:
        return NOT_ANALYZED
    if latest["status"] == FAILED:
        return FAILED
    return STALE


class PositiveMomentAnalyzer:

    def __init__(self, *, repository, model=None):
        self.repository = repository
        self.model = model

    def analyze(self, connection, *, lecture_id, document_id, legacy_session_id,
                allow_model: bool) -> AnalysisOutcome:
        transcript = self.repository.transcript(connection, document_id)
        if transcript is None or not transcript.cues:
            return AnalysisOutcome(REVIEW_REQUIRED, None, detail="NO_CANONICAL_CUES")
        current = self.repository.current_analysis(
            connection, lecture_id, document_id=transcript.document_id,
            transcript_fingerprint=transcript.fingerprint)
        if current is not None:
            return AnalysisOutcome(current["status"], current, reused=True,
                                   source=current["source"])

        completeness, clips = self.repository.legacy_result(connection, legacy_session_id)
        legacy = try_import(transcript, completeness=completeness, positive_clips=clips)
        if legacy.status == IMPORTED:
            analysis = self.repository.save_analysis(
                connection, transcript=transcript, source=p.SOURCE_LEGACY_V5,
                status=MOMENTS_FOUND, moments=legacy.moments,
                candidate_count=legacy.clip_count,
                structurally_valid_count=len(legacy.moments), rejection_summary={},
                model_metadata={"legacy_import_version": p.LEGACY_IMPORT_VERSION,
                                "legacy_reason": legacy.reason},
                legacy_fingerprint=legacy.legacy_fingerprint)
            return AnalysisOutcome(MOMENTS_FOUND, analysis, source=p.SOURCE_LEGACY_V5,
                                   detail=legacy.reason)

        if not allow_model or self.model is None:
            return AnalysisOutcome(NOT_ANALYZED, None,
                                   detail=f"MODEL_NOT_ALLOWED; legacy {legacy.reason}")
        return self._analyze_with_model(connection, transcript, legacy_reason=legacy.reason)

    def _analyze_with_model(self, connection, transcript, *, legacy_reason) -> AnalysisOutcome:
        metadata = {"legacy_import_outcome": legacy_reason}
        calls = 0
        try:
            candidates, selector_meta = ai.select(self.model, transcript)
            calls += 1
            metadata["selector"] = selector_meta
            moments, rejections = validate(transcript, candidates)
            accepted, verifier_rejections = [], {}
            if moments:
                verdicts, verifier_meta = ai.verify(self.model, moments)
                calls += 1
                metadata["verifier"] = verifier_meta
                accepted, verifier_rejections = ai.apply_verdicts(moments, verdicts)
        except ProviderError as error:
            analysis = self.repository.save_analysis(
                connection, transcript=transcript, source=p.SOURCE_CODED_AI, status=FAILED,
                moments=[], candidate_count=0, structurally_valid_count=0,
                rejection_summary={}, model_metadata={**metadata,
                                                      "provider_failure": error.diagnostics()},
                error_code=error.code, error_message=str(error))
            return AnalysisOutcome(FAILED, analysis, source=p.SOURCE_CODED_AI,
                                   detail=error.code, provider_calls=calls or 1)
        summary = dict(rejections)
        for code, count in verifier_rejections.items():
            summary[code] = summary.get(code, 0) + count
        no_verdicts = moments and verifier_rejections.get(p.VERIFIER_NO_VERDICT) == len(moments)
        status = (REVIEW_REQUIRED if no_verdicts else
                  MOMENTS_FOUND if accepted else NO_POSITIVE_MOMENTS)
        analysis = self.repository.save_analysis(
            connection, transcript=transcript, source=p.SOURCE_CODED_AI, status=status,
            moments=accepted, candidate_count=len(candidates),
            structurally_valid_count=len(moments), rejection_summary=summary,
            model_metadata=metadata)
        return AnalysisOutcome(status, analysis, source=p.SOURCE_CODED_AI,
                               provider_calls=calls)
