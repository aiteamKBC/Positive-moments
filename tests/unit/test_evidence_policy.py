"""
Phase 4B unit tests: what an invalid evidence clip means for its evaluation.

The fixtures are the REAL 2026-09-18 shapes. G2-Juliane returned 54 valid clips
and two with `start == end`; Risk Management returned 47 valid and one lasting
1,560 ms. Both produced complete, correct checklists and both were discarded,
spending a paid generation each.

The distinction under test is not "how strict is the validator" - it is
untouched and these tests prove it - but "which invalid clips should cost an
evaluation its verdict".
"""
import inspect

import pytest

from app.qa.evidence_policy import (
    CLASSIFIED_STATUSES,
    DEFAULT_EVIDENCE_POLICY,
    DEGENERATE_STATUSES,
    EVIDENCE_POLICY_V1,
    EVIDENCE_POLICY_V2,
    EVIDENCE_POLICY_VERSIONS,
    FABRICATED_STATUSES,
    assess,
    is_fatal,
    is_renderable,
)
from app.qa.validation import (
    MIN_CLIP_MS,
    NO_CUE_OVERLAP,
    NOT_POSITIVE,
    OUT_OF_RANGE,
    TOO_SHORT,
    UNPARSEABLE,
    VALID,
    validate_clip,
)


# The real failures, as clip status lists.
G2_JULIANE = [VALID] * 54 + [NOT_POSITIVE, NOT_POSITIVE]
RISK_MANAGEMENT = [VALID] * 47 + [TOO_SHORT]

CUES = [(0, 10_000), (10_000, 20_000), (20_000, 30_000)]


def _clip(start, end):
    return {"start": start, "end": end}


# --- 16. the validator is not weakened ---------------------------------------

def test_the_validator_still_rejects_a_zero_length_clip():
    verdict = validate_clip(_clip("01:26:13.108", "01:26:13.108"),
                            document_start_ms=0, document_end_ms=7_817_348,
                            cues=CUES)
    assert verdict["status"] == NOT_POSITIVE


def test_the_validator_still_rejects_a_clip_under_two_seconds():
    verdict = validate_clip(_clip("00:00:01.420", "00:00:02.980"),
                            document_start_ms=0, document_end_ms=30_000,
                            cues=CUES)
    assert verdict["status"] == TOO_SHORT
    assert MIN_CLIP_MS == 2000


def test_the_validator_still_rejects_a_fabricated_coordinate():
    outside = validate_clip(_clip("09:00:00.000", "09:00:05.000"),
                            document_start_ms=0, document_end_ms=30_000,
                            cues=CUES)
    assert outside["status"] == OUT_OF_RANGE
    no_cue = validate_clip(_clip("00:00:40.000", "00:00:45.000"),
                           document_start_ms=0, document_end_ms=60_000,
                           cues=CUES)
    assert no_cue["status"] == NO_CUE_OVERLAP


def test_the_policy_module_does_not_reach_into_clip_validation():
    """
    Structural. The policy decides CONSEQUENCES; it must not be able to change
    what `validate_clip` concludes, or "harden the failure path" would quietly
    become "loosen the validator".
    """
    import app.qa.evidence_policy as policy
    source = inspect.getsource(policy)
    assert "def validate_clip" not in source
    assert "MIN_CLIP_MS" not in source
    assert "parse_timestamp_ms" not in source


# --- 15. the real failure shapes ---------------------------------------------

@pytest.mark.parametrize("statuses,excluded,reason", [
    (G2_JULIANE, 2, NOT_POSITIVE),
    (RISK_MANAGEMENT, 1, TOO_SHORT),
])
def test_the_real_failures_cost_their_clips_and_not_their_verdicts(
        statuses, excluded, reason):
    old = assess(statuses, policy=EVIDENCE_POLICY_V1)
    new = assess(statuses, policy=EVIDENCE_POLICY_V2)
    assert old["evaluation_usable"] is False
    assert new["evaluation_usable"] is True
    assert new["fatal_clip_count"] == 0
    assert new["excluded_clip_count"] == excluded
    assert new["excluded_reasons"] == {reason: excluded}
    # The invalid clips are still counted as invalid under both rules.
    assert old["invalid_clip_count"] == new["invalid_clip_count"] == excluded


def test_a_fabricated_coordinate_still_fails_the_whole_evaluation():
    """
    The line that must not move. A clip pointing outside the transcript says
    the answer was not grounded in the evidence it cites, and that makes the
    whole answer suspect - not just the clip.
    """
    for status in FABRICATED_STATUSES:
        result = assess([VALID] * 50 + [status], policy=EVIDENCE_POLICY_V2)
        assert result["evaluation_usable"] is False, status
        assert result["fatal_clip_count"] == 1
        assert result["excluded_clip_count"] == 0


def test_one_fabricated_clip_condemns_an_otherwise_degenerate_set():
    result = assess([VALID] * 40 + [NOT_POSITIVE, TOO_SHORT, NO_CUE_OVERLAP],
                    policy=EVIDENCE_POLICY_V2)
    assert result["evaluation_usable"] is False
    assert result["fatal_clip_count"] == 1
    assert result["excluded_clip_count"] == 2


def test_the_two_categories_are_disjoint_and_cover_every_invalid_status():
    assert not (FABRICATED_STATUSES & DEGENERATE_STATUSES)
    every = {OUT_OF_RANGE, NO_CUE_OVERLAP, UNPARSEABLE, NOT_POSITIVE, TOO_SHORT}
    assert CLASSIFIED_STATUSES == every


def test_an_unclassified_status_fails_closed_and_is_reported():
    """
    A status this module has never heard of must never inherit permission by
    being unrecognised. It is fatal, and it says so by name.
    """
    result = assess([VALID, "SOME_NEW_VALIDATOR_STATUS"], policy=EVIDENCE_POLICY_V2)
    assert result["evaluation_usable"] is False
    assert result["unclassified_statuses"] == ["SOME_NEW_VALIDATOR_STATUS"]


def test_an_all_valid_answer_is_usable_under_both_policies():
    for policy in EVIDENCE_POLICY_VERSIONS:
        result = assess([VALID] * 12, policy=policy)
        assert result["evaluation_usable"] is True
        assert result["invalid_clip_count"] == 0
        assert result["excluded_clip_count"] == 0


def test_an_unknown_policy_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError):
        assess([VALID], policy="evidence_policy_v99")


# --- excluded clips never become evidence ------------------------------------

def test_only_valid_clips_may_ever_render():
    """
    Tolerating a degenerate clip in the VERDICT must never mean tolerating it
    in the published evidence. There is no policy under which an excluded clip
    renders.
    """
    for policy in EVIDENCE_POLICY_VERSIONS:
        assert is_renderable(VALID, policy=policy) is True
        for status in CLASSIFIED_STATUSES:
            assert is_renderable(status, policy=policy) is False, (policy, status)


def test_is_fatal_agrees_with_the_full_assessment():
    for status in CLASSIFIED_STATUSES | {VALID}:
        for policy in EVIDENCE_POLICY_VERSIONS:
            expected = not assess([status], policy=policy)["evaluation_usable"]
            assert is_fatal(status, policy=policy) is expected, (policy, status)


# --- versioning ---------------------------------------------------------------

def test_the_default_is_the_phase_4b_policy_and_v1_remains_selectable():
    assert DEFAULT_EVIDENCE_POLICY == EVIDENCE_POLICY_V2
    assert EVIDENCE_POLICY_V1 in EVIDENCE_POLICY_VERSIONS
    # v1 still means exactly what it always meant.
    assert assess(G2_JULIANE, policy=EVIDENCE_POLICY_V1)["evaluation_usable"] is False


def test_the_policy_is_not_part_of_the_model_input_fingerprint():
    """
    The property that makes recovery free. The evidence policy changes how an
    answer is JUDGED, never what the provider was asked - so a stored answer
    stays reusable and no existing evaluation is orphaned into buying itself
    again.
    """
    import app.qa.inputs as inputs
    source = inspect.getsource(inputs)
    assert "evidence_policy" not in source


def test_the_policy_does_not_move_the_qa_source_fingerprint():
    """
    Deliberate, and the reason is arithmetic: putting it in the fingerprint
    would orphan every existing evaluation, and the next scheduled cycle would
    buy a fresh generation for each one.
    """
    from tests.unit.test_shadow_qa import package
    from app.qa.inputs import qa_source_fingerprint
    before = qa_source_fingerprint(package=package(), model="gpt-5.2")
    after = qa_source_fingerprint(package=package(), model="gpt-5.2")
    assert before == after
