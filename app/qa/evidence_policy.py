"""
Phase 4B: what an invalid evidence clip means for the evaluation that holds it.

THE PROBLEM THIS SOLVES
-----------------------
On 2026-09-18 two real lectures were rejected outright:

  * G2-Juliane: 54 valid clips, 2 with `start == end`
  * Risk Management: 47 valid clips, 1 lasting 1,560 ms instead of 2,000 ms

Both produced a complete, correct eleven-row checklist and zero structured
output errors. Both were discarded, and both spent a paid generation, because
ANY invalid clip failed the whole evaluation.

The model was wrong - the system message says "Each evidence clip MUST have a
duration of at least 2 seconds" and "Never return identical start and end
timestamps", so these are `MODEL_OUTPUT_INVALID`, not a validator defect. But
regenerating does not fix a model that occasionally emits a degenerate clip
among fifty-six; it re-rolls the dice, and three re-rolls later the lecture is
in manual review with a checklist that was right the first time.

THE DISTINCTION THIS MODULE MAKES
---------------------------------
Not every invalid clip means the same thing.

  FABRICATED - `OUT_OF_TRANSCRIPT_RANGE`, `NO_CUE_OVERLAP` (and, under v1 and
  v2, `UNPARSEABLE_TIMESTAMP` - see V3 below). The model named a place that
  does not exist in the transcript. That is hallucination, it says the answer was not grounded in
  the evidence it claims, and it must keep failing the entire evaluation.

  DEGENERATE - `END_NOT_AFTER_START`, `SHORTER_THAN_MINIMUM`. The model named
  a REAL place in the transcript and gave it an unusable window. A zero-length
  or 1.5-second clip is EMPTY evidence, not FALSE evidence: it points nowhere
  wrong, it simply points at nothing usable.

Excluding an empty pointer costs the evaluation one supporting citation.
Discarding the evaluation costs its entire verdict and a paid generation. The
second is the larger loss, and it is the one the old policy always took.

V3: AN UNREADABLE TIMESTAMP NAMES NO PLACE AT ALL
-------------------------------------------------
On 2026-10-16 the Ray MSP lecture was rejected twice, 29 of 37 clips
`UNPARSEABLE_TIMESTAMP`. Twenty-eight were `49:05.214`-style MM:SS.mmm - real
cue times with the zero hour left off - and one was `04:18:480`. Calling them
fabricated was wrong: a string the validator cannot read is not a claim about
the transcript, so it cannot be a false one.

  * v3 READS MM:SS.mmm as hour zero (`validate_clip(allow_minutes_seconds=)`).
    A clip read that way then faces every check a full timestamp faces, so
    one that lands outside the transcript or between cues is still FABRICATED
    and still fails the evaluation.
  * v3 EXCLUDES what still cannot be read, as empty evidence. It never
    renders; it costs one citation, not the verdict.

v1 and v2 read and judge exactly as they always did.

WHAT THIS IS NOT
----------------
It is NOT a relaxation of `app/qa/validation.py`. Its checks are untouched
(v3 only lets it READ one more spelling of the same instant):
a 1,560 ms clip is still `SHORTER_THAN_MINIMUM` and a zero-length clip is still
`END_NOT_AFTER_START`, every clip is still persisted with its real status, and
no timestamp is ever rewritten. What changes is only the CONSEQUENCE for the
evaluation, and excluded clips never reach the render, so a degenerate clip
cannot become evidence by the back door.

VERSIONED
---------
The policy joins the QA source fingerprint, so an evaluation always says which
rule judged it and no historical answer is silently reinterpreted. It does NOT
join the MODEL INPUT fingerprint, because it changes nothing about the question
the provider was asked - which is what makes re-judging a stored answer free.
"""
from app.qa.validation import (
    NO_CUE_OVERLAP,
    NOT_POSITIVE,
    OUT_OF_RANGE,
    TOO_SHORT,
    UNPARSEABLE,
    VALID,
)


# The original rule: any invalid clip fails the evaluation. Preserved so every
# evaluation written before Phase 4B keeps the meaning it was written with.
EVIDENCE_POLICY_V1 = "evidence_policy_v1_reject_any_invalid"
# Phase 4B: fabricated coordinates still fail; degenerate windows are excluded.
EVIDENCE_POLICY_V2 = "evidence_policy_v2_exclude_degenerate_clips"
# v2, plus: MM:SS.mmm is read as hour zero, and an unreadable timestamp is
# excluded as empty evidence instead of failing the evaluation.
EVIDENCE_POLICY_V3 = "evidence_policy_v3_read_minutes_seconds"

DEFAULT_EVIDENCE_POLICY = EVIDENCE_POLICY_V3
EVIDENCE_POLICY_VERSIONS = (EVIDENCE_POLICY_V1, EVIDENCE_POLICY_V2, EVIDENCE_POLICY_V3)


# The model named somewhere that is not in the transcript. Fatal under every
# policy, forever: an ungrounded citation makes the whole answer suspect.
FABRICATED_STATUSES = frozenset({OUT_OF_RANGE, NO_CUE_OVERLAP})

# The model named a real place with an unusable window. Empty evidence.
DEGENERATE_STATUSES = frozenset({NOT_POSITIVE, TOO_SHORT})

# The model wrote a timestamp nothing can read, so it named no place at all.
# Fatal under v1 and v2, as it always was; excluded under v3.
UNREADABLE_STATUSES = frozenset({UNPARSEABLE})

# Every non-VALID status must be classified. A new validator status that is
# none of these would otherwise be silently tolerated, so the assessment
# treats anything unrecognised as fatal and says so.
CLASSIFIED_STATUSES = FABRICATED_STATUSES | DEGENERATE_STATUSES | UNREADABLE_STATUSES

# What each policy excludes rather than fails on. Everything else is fatal.
_EXCLUDED_STATUSES = {
    EVIDENCE_POLICY_V1: frozenset(),
    EVIDENCE_POLICY_V2: DEGENERATE_STATUSES,
    EVIDENCE_POLICY_V3: DEGENERATE_STATUSES | UNREADABLE_STATUSES,
}


def reads_minutes_seconds(policy: str) -> bool:
    """Does this policy read a MM:SS.mmm clip timestamp as hour zero?"""
    return policy == EVIDENCE_POLICY_V3

UNCLASSIFIED = "UNCLASSIFIED_INVALID_STATUS"


def assess(statuses, *, policy: str = DEFAULT_EVIDENCE_POLICY) -> dict:
    """
    Decide what a set of clip statuses means for the evaluation.

    `statuses` is every clip's validation status, in any order. Returns the
    counts and one boolean: may this evaluation stand?
    """
    if policy not in EVIDENCE_POLICY_VERSIONS:
        raise ValueError(f"unknown evidence policy {policy!r}")

    statuses = list(statuses)
    invalid = [status for status in statuses if status != VALID]
    unclassified = [status for status in invalid
                    if status not in CLASSIFIED_STATUSES]

    tolerated = _EXCLUDED_STATUSES[policy]
    fatal = [status for status in invalid if status not in tolerated]
    excluded = [status for status in invalid if status in tolerated]

    return {
        "evidence_policy_version": policy,
        "clip_count": len(statuses),
        "valid_clip_count": len(statuses) - len(invalid),
        "invalid_clip_count": len(invalid),
        "fatal_clip_count": len(fatal),
        "excluded_clip_count": len(excluded),
        # Deliberately surfaced: a status this module has never heard of is
        # treated as fatal AND reported, rather than quietly falling through.
        "unclassified_statuses": sorted(set(unclassified)),
        "fatal_reasons": _tally(fatal),
        "excluded_reasons": _tally(excluded),
        "evaluation_usable": not fatal,
    }


def is_fatal(status: str, *, policy: str = DEFAULT_EVIDENCE_POLICY) -> bool:
    """Does this one status, on its own, make the evaluation unusable?"""
    if status == VALID:
        return False
    return status not in _EXCLUDED_STATUSES[policy]


def is_renderable(status: str, *, policy: str = DEFAULT_EVIDENCE_POLICY) -> bool:
    """
    May this clip become rendered evidence?

    Only VALID clips ever render, under every policy. An excluded clip is
    excluded everywhere - tolerating it in the evaluation must never mean
    tolerating it in the output.
    """
    return status == VALID


def _tally(statuses) -> dict:
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))
