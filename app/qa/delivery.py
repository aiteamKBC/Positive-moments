"""
Versioned delivery classification: was this lecture delivered, not delivered,
or delivered with a transcript too incomplete to judge?

WHY THIS EXISTS
---------------
The legacy gate (`deterministic.delivery_status`, node `Session Delivered?`)
reads ONE number: the minutes spanned by the transcript cues. Under 20 it
declares the session not delivered and writes the cancelled checklist.

That conflates two different facts. On 2026-09-04 "AI in Project Control 2026"
ran a 2h51m Teams call that covered the whole 11:00-13:00 Cairo slot, but Teams
transcription captured only 00:01:22-00:14:14 of it. The transcript was short
because the TRANSCRIPT was incomplete, not because the lecture did not happen,
and the platform recorded a real lecture as cancelled.

THE RULE (`delivery_coverage_guard_v1`)
---------------------------------------
    transcript meets the legacy delivery minimum      -> DELIVERED (unchanged)
    transcript below it, AND the provider call was live
      inside the SCHEDULED window for at least that
      same minimum                                     -> TRANSCRIPT_COVERAGE_INCOMPLETE
    otherwise                                          -> NON_DELIVERED (unchanged)

No new threshold is invented: "enough to count as delivered" is the legacy
20 minutes, applied a second time to independent evidence. The reference is
the scheduled window, never the raw call length, because a real call often
opens hours early - Andrew-Scheduling Professional on 2026-09-16 was a 287
minute call around a 119 minute lecture - and a whole-call ratio would flag
valid lectures.

The call bounds are the provider's own transcript `createdDateTime` /
`endDateTime` as persisted by Phase 2B (`actual_start` / `actual_end`), which
are independent of the cue content; for the AI Project Control call they
match the Teams recording's bounds to the millisecond.

A long call is NOT proof of useful teaching. It only stops the platform from
claiming the lecture was not delivered. The outcome is a review, never a QA
score built from thirteen minutes.
"""
from dataclasses import dataclass, field
from typing import Any

from app.qa.deterministic import (
    DELIVERED,
    DELIVERY_MINIMUM_MINUTES,
    NON_DELIVERED,
    delivery_status,
)


DELIVERY_POLICY_VERSION = "delivery_coverage_guard_v1"

# A classification, and also the review reason carried by the evaluation. It is
# not a new QA status: the evaluation is REVIEW_REQUIRED, as for every other
# answer a human has to look at.
TRANSCRIPT_COVERAGE_INCOMPLETE = "TRANSCRIPT_COVERAGE_INCOMPLETE"

# Why each classification was reached. The NON_DELIVERED spelling is the one
# the service already stamped on non-delivered evaluations.
TRANSCRIPT_MEETS_DELIVERY_MINIMUM = "TRANSCRIPT_MEETS_DELIVERY_MINIMUM"
DURATION_BELOW_DELIVERY_MINIMUM = "DURATION_BELOW_DELIVERY_MINIMUM"
CALL_COVERED_SCHEDULE_TRANSCRIPT_SHORT = "CALL_COVERED_SCHEDULE_TRANSCRIPT_SHORT"


@dataclass(frozen=True)
class DeliveryDecision:
    classification: str
    reason: str
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def departs_from_legacy(self) -> bool:
        """True exactly when the legacy gate alone would have decided otherwise."""
        return self.classification == TRANSCRIPT_COVERAGE_INCOMPLETE


def classify_delivery(*, duration_minutes, scheduled_start, scheduled_end,
                      call_start, call_end, transcript_span_seconds=None
                      ) -> DeliveryDecision:
    """
    Deterministic, offline, and shared: the QA service classifies with it and
    the pipeline state resolver asks it whether an old NON_DELIVERED answer
    would still be given today.
    """
    minimum_seconds = DELIVERY_MINIMUM_MINUTES * 60
    scheduled_seconds = _seconds(scheduled_start, scheduled_end)
    call_seconds = _seconds(call_start, call_end)
    overlap_seconds = None
    if None not in (call_start, call_end, scheduled_start, scheduled_end):
        overlap_seconds = max(0.0, (min(call_end, scheduled_end)
                                    - max(call_start, scheduled_start)).total_seconds())
    if transcript_span_seconds is None and duration_minutes is not None:
        transcript_span_seconds = float(duration_minutes) * 60
    diagnostics = {
        "delivery_policy_version": DELIVERY_POLICY_VERSION,
        "delivery_minimum_minutes": DELIVERY_MINIMUM_MINUTES,
        "duration_minutes": duration_minutes,
        "scheduled_duration_seconds": scheduled_seconds,
        "call_start": call_start.isoformat() if call_start else None,
        "call_end": call_end.isoformat() if call_end else None,
        "call_duration_seconds": call_seconds,
        "call_overlap_with_schedule_seconds": overlap_seconds,
        "call_schedule_coverage_ratio": _ratio(overlap_seconds, scheduled_seconds),
        "transcript_span_seconds": (float(transcript_span_seconds)
                                    if transcript_span_seconds is not None else None),
        "transcript_schedule_coverage_ratio": _ratio(
            transcript_span_seconds, scheduled_seconds),
    }

    if delivery_status(duration_minutes) == DELIVERED:
        return _decision(DELIVERED, TRANSCRIPT_MEETS_DELIVERY_MINIMUM, diagnostics)
    if overlap_seconds is not None and overlap_seconds >= minimum_seconds:
        return _decision(TRANSCRIPT_COVERAGE_INCOMPLETE,
                         CALL_COVERED_SCHEDULE_TRANSCRIPT_SHORT, diagnostics)
    return _decision(NON_DELIVERED, DURATION_BELOW_DELIVERY_MINIMUM, diagnostics)


def _decision(classification, reason, diagnostics) -> DeliveryDecision:
    return DeliveryDecision(classification, reason, {
        **diagnostics, "delivery_classification": classification,
        "delivery_classification_reason": reason})


def _seconds(start, end):
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def _ratio(numerator, denominator):
    if numerator is None or not denominator:
        return None
    return round(float(numerator) / float(denominator), 4)
