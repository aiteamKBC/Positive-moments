"""
Phase 3C3B: where Item 2's "actual start" and "actual end" come from.

The controlled-day pilot (Phase 3C3A) measured the problem. Item 2 has always
been computed from the TEAMS CALL bounds - `actual_start = min(part.start)`,
the instant the call opened, persisted on `lecture_transcript_selections`. On
2026-09-16 every lecture actually began within two minutes of its scheduled
time, yet two of seven calls had opened 22 and 167 minutes early.

Nothing was wrong with those two outcomes: starting early always passes, so
Met was correct either way. The exposure is the mirror case. A call that opens
early, or is reused from an earlier session, shifts `actual_start` backwards
and can therefore **mask a genuinely late lecture** - Item 2 would report Met
when the teaching started half an hour late. At 2-in-7 incidence the shift is
common, so the masking case is not hypothetical.

This module makes the source an explicit, versioned choice rather than an
unexamined assumption. It changes no threshold: `item2_status` still means
"Met unless more than 20 minutes late, or ending more than 20 minutes early".
Only the definition of "started" moves.

IMPORTANT: this conversion exists for punctuality wall-clock semantics ONLY.
QA evidence timestamps stay call-relative and are never rebased - see Phase
3C2.3A/3B, where the 02:47 origin was proven to be the provider's own.
"""
import math
from datetime import timedelta


# The historical source: the call open/close instants. Preserved by name so an
# old evaluation stays reproducible rather than being retroactively
# reinterpreted.
LEGACY_CALL_BOUNDS = "legacy_call_bounds_v1"

# The lecture-bounds source: the first and last surviving canonical cue,
# converted to wall clock through the call-start instant. Deterministic, no
# model, no heuristics, no text inspection.
CANONICAL_CUE_BOUNDS = "canonical_cue_bounds_v1"

PUNCTUALITY_SOURCE_VERSIONS = (LEGACY_CALL_BOUNDS, CANONICAL_CUE_BOUNDS)
DEFAULT_PUNCTUALITY_SOURCE = LEGACY_CALL_BOUNDS


class PunctualitySourceError(RuntimeError):
    """An unknown punctuality source, or one its inputs cannot satisfy."""


def js_round(value: float) -> int:
    """JavaScript Math.round: half toward +Infinity, matching the legacy node."""
    return int(math.floor(value + 0.5))


def derive_bounds(*, call_start, call_end, first_cue_start_ms=None,
                  last_cue_end_ms=None, source_version=DEFAULT_PUNCTUALITY_SOURCE):
    """
    Return the (actual_start, actual_end) a given source version implies.

    Under CANONICAL_CUE_BOUNDS the cue offsets are added to the CALL START,
    because canonical cue timestamps are call-relative by construction. For a
    multi-part v2 document the offsets are already expressed against part one's
    call, which is the same instant `min(part.start)` produced, so the
    conversion holds without special-casing.

    Missing cue data is a refusal, not a silent fallback to the call bounds:
    a punctuality answer that quietly changed source would be unauditable.
    """
    if source_version not in PUNCTUALITY_SOURCE_VERSIONS:
        raise PunctualitySourceError(f"unknown punctuality source: {source_version}")
    if source_version == LEGACY_CALL_BOUNDS:
        return call_start, call_end
    if call_start is None:
        raise PunctualitySourceError("cue bounds need the call start instant")
    if first_cue_start_ms is None or last_cue_end_ms is None:
        raise PunctualitySourceError("cue bounds need a first and last canonical cue")
    if last_cue_end_ms < first_cue_start_ms:
        raise PunctualitySourceError("last cue ends before the first cue starts")
    return (call_start + timedelta(milliseconds=int(first_cue_start_ms)),
            call_start + timedelta(milliseconds=int(last_cue_end_ms)))


def difference_minutes(actual, scheduled):
    """Signed minutes, rounded exactly as the legacy node rounds."""
    if actual is None or scheduled is None:
        return None
    return js_round((actual - scheduled).total_seconds() / 60)


def punctuality_provenance(*, source_version, scheduled_start, scheduled_end,
                           actual_start, actual_end) -> dict:
    """
    Everything needed to re-derive and audit one Item 2 decision.

    Recorded alongside the evaluation so the final Met/Partial/Not Met is never
    the only surviving evidence: which source was used, what it produced, and
    what it was measured against.
    """
    start_diff = difference_minutes(actual_start, scheduled_start)
    end_diff = difference_minutes(actual_end, scheduled_end)
    return {
        "punctuality_source_version": source_version,
        "scheduled_start": _iso(scheduled_start),
        "scheduled_end": _iso(scheduled_end),
        "derived_actual_start": _iso(actual_start),
        "derived_actual_end": _iso(actual_end),
        "start_difference_minutes": start_diff,
        "end_difference_minutes": end_diff,
    }


def _iso(value):
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
