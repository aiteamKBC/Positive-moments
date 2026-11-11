"""
Positive Moment Evidence Intelligence, offline.

The model is a scripted stand-in, the database is a stand-in, and nothing here
can reach a network (tests/conftest.py blocks it). What is pinned: the model
only ever proposes cue ranges; everything persisted as evidence is rebuilt
from canonical cues; the verifier decides on its own terms; legacy V5 results
are reused only when provable.
"""
from __future__ import annotations

import json

import pytest

from app.positive_moments import ai
from app.positive_moments import policy as p
from app.positive_moments.legacy_import import IMPORTED, NOT_AVAILABLE, STALE, try_import
from app.positive_moments.models import CanonicalTranscript, Candidate, Cue
from app.positive_moments.service import (
    FAILED,
    MOMENTS_FOUND,
    NO_POSITIVE_MOMENTS,
    NOT_ANALYZED,
    STALE as STALE_STATE,
    PositiveMomentAnalyzer,
    analysis_state,
)
from app.positive_moments.validation import Rejected, build_moment, deduplicate, validate
from app.qa.provider import ProviderError

TRAINER = "Dr Femi Adeyemi"
AMY, BEN = "Amy Learner", "Ben Learner"


def transcript(lines, *, trainer=TRAINER, document_id="doc-1"):
    """lines: (speaker, text) or (speaker, text, seconds_long). 5 s apart by default."""
    cues, clock = [], 1000 * 60 * 80      # 01:20:00
    for index, line in enumerate(lines, start=1):
        speaker, text = line[0], line[1]
        length = int((line[2] if len(line) > 2 else 4) * 1000)
        cues.append(Cue(index, clock, clock + length, speaker, text))
        clock += length + 1000
    return CanonicalTranscript(lecture_id="lecture-1", document_id=document_id,
                               document_source_fingerprint="src-fp", cues=tuple(cues),
                               trainer_speaker=trainer, subject="AI in Project Control")


LESSON = transcript([
    (TRAINER, "So the earned value framework compares planned and actual cost."),        # 1
    (TRAINER, "Does anyone have a question about the cost performance index?"),        # 2
    (AMY, "I was confused before but that makes much more sense now, thank you."),       # 3
    (TRAINER, "Great, let us move on to schedule variance."),                           # 4
    (BEN, "Honestly this framework would really help in my role on the rail project."),  # 5
    (BEN, "I can actually use this with my team next week for our monthly report."),     # 6
    (TRAINER, "Brilliant."),                                                            # 7
    (AMY, "Thanks everyone, bye."),                                                      # 8
    (BEN, "Your screen share is great, I can see it clearly now."),                      # 9
    (TRAINER, "I am really proud of this slide deck, it is my best work."),              # 10
])


def candidate(start, end, speakers, category="learning_experience", confidence=0.8,
              reason="because"):
    return Candidate(start, end, tuple(speakers), category, reason, confidence)


# ---------------------------------------------------------------------------
# 1. canonical cues are the only input
# ---------------------------------------------------------------------------

def test_the_selector_reads_stored_canonical_cues_and_marks_the_trainer():
    message = ai.selector_user_message(LESSON)
    assert f"c3 [1:20:10] {AMY} (LEARNER): I was confused before" in message
    assert f"c1 [1:20:00] {TRAINER} (TRAINER)" in message
    assert "graph" not in message.casefold()


def test_the_package_never_downloads_a_transcript():
    import ast, pathlib
    for path in pathlib.Path("app/positive_moments").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not [n for n in names if n.startswith(("app.transcripts.graph", "app.graph"))], path


# ---------------------------------------------------------------------------
# 2. structural validation
# ---------------------------------------------------------------------------

def test_an_invented_cue_id_is_rejected():
    moments, rejections = validate(LESSON, [candidate(3, 99, [AMY]), candidate(0, 3, [AMY])])
    assert moments == [] and rejections[p.UNKNOWN_CUE] == 2


def test_an_inverted_range_is_rejected():
    with pytest.raises(Rejected) as exc:
        build_moment(LESSON, candidate(5, 3, [BEN]))
    assert exc.value.code == p.INVERTED_RANGE


def test_the_trainer_can_never_be_a_positive_speaker():
    with pytest.raises(Rejected) as exc:
        build_moment(LESSON, candidate(10, 10, [TRAINER], "trainer"))
    assert exc.value.code == p.TRAINER_AS_POSITIVE_SPEAKER
    mixed = build_moment(LESSON, candidate(2, 3, [TRAINER, AMY]))
    assert mixed.positive_speakers == [AMY]


def test_a_speaker_who_does_not_speak_in_the_range_is_rejected():
    with pytest.raises(Rejected) as exc:
        build_moment(LESSON, candidate(3, 3, [BEN]))
    assert exc.value.code == p.SPEAKER_NOT_IN_RANGE


def test_generic_thanks_without_a_training_target_is_rejected():
    with pytest.raises(Rejected) as exc:
        build_moment(LESSON, candidate(8, 8, [AMY]))
    assert exc.value.code == p.GENERIC_PRAISE_NO_TARGET


def test_meeting_control_praise_is_rejected():
    with pytest.raises(Rejected) as exc:
        build_moment(LESSON, candidate(9, 9, [BEN], "support"))
    assert exc.value.code == p.MEETING_TOOL_PRAISE


def test_role_relevance_and_intended_application_are_valid_evidence():
    moment = build_moment(LESSON, candidate(5, 6, [BEN], "application"))
    assert moment.category == "learning_experience"
    assert moment.conversation_type == "learner_statement"
    assert "help in my role" in moment.positive_quote


def test_confusion_to_understanding_keeps_the_trainer_context():
    moment = build_moment(LESSON, candidate(2, 3, [AMY]))
    assert moment.trainer_included is True
    assert moment.all_speakers == [TRAINER, AMY]
    assert moment.positive_speakers == [AMY]
    assert moment.conversation_type == "learner_trainer_exchange"
    assert [line["role"] for line in moment.dialogue] == ["trainer", "learner"]
    assert [line["positive"] for line in moment.dialogue] == [False, True]


def test_a_multi_speaker_exchange_is_retained_with_only_learners_positive():
    moment = build_moment(LESSON, candidate(2, 6, [AMY, BEN]))
    assert moment.conversation_type == "multi_speaker_exchange"
    assert set(moment.positive_speakers) == {AMY, BEN}
    assert TRAINER in moment.all_speakers and TRAINER not in moment.positive_speakers


def test_the_exact_quote_is_rebuilt_from_cues_never_model_text():
    moment = build_moment(LESSON, candidate(3, 3, [AMY], reason="Amy said 'best course ever'"))
    assert moment.exact_quote == f"{AMY}: {LESSON.by_index[3].text}"
    assert moment.positive_quote == LESSON.by_index[3].text
    assert (moment.evidence_start_ms, moment.evidence_end_ms) == (
        LESSON.by_index[3].start_ms, LESSON.by_index[3].end_ms)
    assert "best course ever" not in moment.exact_quote


def test_an_invalid_category_is_rejected_and_aliases_normalise():
    with pytest.raises(Rejected):
        build_moment(LESSON, candidate(3, 3, [AMY], "vibes"))
    assert build_moment(LESSON, candidate(3, 3, [AMY], "Teaching")).category == "teaching_method"


def test_heavily_overlapping_candidates_collapse_deterministically():
    moments, rejections = validate(LESSON, [
        candidate(5, 6, [BEN], confidence=0.7), candidate(5, 5, [BEN], confidence=0.9),
        candidate(3, 3, [AMY], confidence=0.6)])
    assert [(m.start_cue, m.end_cue) for m in moments] == [(3, 3), (5, 5)]
    assert rejections[p.DUPLICATE_OVERLAP] == 1
    again, _ = validate(LESSON, [candidate(5, 5, [BEN], confidence=0.9),
                                 candidate(3, 3, [AMY], confidence=0.6),
                                 candidate(5, 6, [BEN], confidence=0.7)])
    assert [(m.start_cue, m.end_cue) for m in again] == [(3, 3), (5, 5)]


def test_long_evidence_loses_only_unrelated_context():
    long = transcript([(TRAINER, "setup talk " * 5, 30)] * 3 + [
        (TRAINER, "Any questions on the risk register?", 5),
        (AMY, "That exercise helped me understand the risk register properly.", 6),
        (TRAINER, "Moving on to budgets now.", 30), (TRAINER, "More budget talk.", 30)])
    moment = build_moment(long, candidate(1, 7, [AMY]))
    assert (moment.start_cue, moment.end_cue) == (4, 5)     # question kept, evidence kept
    assert moment.duration_seconds <= p.TARGET_MAX_EVIDENCE_SECONDS


def test_evidence_still_too_long_after_trimming_is_rejected():
    rambling = transcript([(AMY, "this framework helps my role " * 3, 50)] * 5)
    with pytest.raises(Rejected) as exc:
        build_moment(rambling, candidate(1, 5, [AMY]))
    assert exc.value.code == p.EVIDENCE_TOO_LONG


# ---------------------------------------------------------------------------
# 3. the two model stages
# ---------------------------------------------------------------------------

class ScriptedModel:
    def __init__(self, selector=None, verifier=None, error=None):
        self.selector, self.verifier, self.error = selector, verifier, error
        self.calls = []

    def complete_schema(self, *, system_message, user_message, schema_name, schema):
        self.calls.append((schema_name, user_message))
        if self.error:
            raise self.error
        output = self.selector if schema_name == "positive_moment_candidates" else self.verifier
        return {"output": output, "provider": "openai", "model_requested": "gpt-test",
                "usage": {"total_tokens": 42}, "attempts": 1}


def verdict(cid, accept=True, category="learning_experience", speakers=(AMY,), confidence=0.9,
            code=None):
    return {"candidate_id": cid, "accept": accept, "category": category if accept else None,
            "training_target": "learning_experience" if accept else "none",
            "positive_speakers": list(speakers), "rejection_code": code,
            "confidence": confidence}


def test_the_verifier_never_sees_the_selectors_category_reason_or_confidence():
    moment = build_moment(LESSON, candidate(2, 3, [AMY], "trainer", 0.99,
                                            "SELECTOR-SECRET-REASON"))
    message = ai.verifier_user_message([moment])
    assert "SELECTOR-SECRET-REASON" not in message and "0.99" not in message
    assert "trainer" not in message.split("\n", 1)[1].replace("(TRAINER)", "")


def test_the_verifier_can_reject_a_selector_candidate():
    moments, _ = validate(LESSON, [candidate(3, 3, [AMY]), candidate(5, 6, [BEN])])
    accepted, rejections = ai.apply_verdicts(moments, {
        "m1": verdict("m1", accept=False, code="CORRECT_ANSWER_ONLY"),
        "m2": verdict("m2", speakers=[BEN])})
    assert [m.start_cue for m in accepted] == [5]
    assert rejections[p.VERIFIER_REJECTED] == 1
    assert rejections["VERIFIER:CORRECT_ANSWER_ONLY"] == 1


def test_the_verifiers_category_and_speakers_win_but_cannot_add_the_trainer():
    moments, _ = validate(LESSON, [candidate(2, 3, [AMY], "content")])
    accepted, _ = ai.apply_verdicts(moments, {"m1": verdict(
        "m1", category="teaching_method", speakers=[TRAINER, AMY])})
    assert accepted[0].category == "teaching_method"
    assert accepted[0].positive_speakers == [AMY]
    assert accepted[0].verifier_verdict == "ACCEPTED"


def test_low_confidence_or_missing_verdicts_are_not_published():
    moments, _ = validate(LESSON, [candidate(3, 3, [AMY]), candidate(5, 6, [BEN])])
    accepted, rejections = ai.apply_verdicts(moments, {"m1": verdict("m1", confidence=0.3)})
    assert accepted == []
    assert rejections[p.VERIFIER_LOW_CONFIDENCE] == 1 and rejections[p.VERIFIER_NO_VERDICT] == 1


def test_selector_output_is_parsed_defensively():
    parsed = ai.parse_candidates({"candidates": [
        {"start_cue": "3", "end_cue": 3, "positive_speakers": [AMY], "category": "content",
         "reason": "r", "confidence": 0.5},
        {"start_cue": "x"}, "junk"]})
    assert [(c.start_cue, c.end_cue) for c in parsed] == [(3, 3)]


def test_strict_schemas_forbid_extra_fields_and_quote_fields():
    for schema in (ai.SELECTOR_SCHEMA, ai.VERIFIER_SCHEMA):
        item = schema["properties"][next(iter(schema["properties"]))]["items"]
        assert item["additionalProperties"] is False
        assert set(item["required"]) == set(item["properties"])
        assert not {"quote", "timestamp", "start", "end", "reasoning"} & set(item["properties"])


# ---------------------------------------------------------------------------
# 4. legacy V5 reuse
# ---------------------------------------------------------------------------

def v5_clip(start_cue, *, quote=None, speakers=(AMY,), category="learning_experience",
            transcript_=LESSON):
    cue = transcript_.by_index[start_cue]

    def stamp(ms):
        s = ms / 1000
        return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}"

    return {"start": stamp(cue.start_ms), "end": stamp(cue.end_ms),
            "positive_quote": quote or cue.text, "positive_speakers": list(speakers),
            "category": category, "start_cue": 900 + start_cue, "end_cue": 900 + start_cue,
            "semantic_verification": {"verdict": "accept", "confidence": 0.88}}


def test_a_provable_v5_result_is_imported_from_canonical_cues():
    clips = [v5_clip(3), v5_clip(5, speakers=[BEN])]
    result = try_import(LESSON, completeness=p.LEGACY_V5_COMPLETENESS,
                        positive_clips=json.dumps(clips))
    assert result.status == IMPORTED
    assert [(m.start_cue, m.end_cue) for m in result.moments] == [(3, 3), (5, 5)]
    assert all(m.source == p.SOURCE_LEGACY_V5 for m in result.moments)
    assert result.moments[0].verifier_confidence == 0.88
    # Rebuilt from cues, including the canonical cue numbers - not V5's.
    assert result.moments[0].exact_quote == f"{AMY}: {LESSON.by_index[3].text}"
    again = try_import(LESSON, completeness=p.LEGACY_V5_COMPLETENESS, positive_clips=clips)
    assert again.legacy_fingerprint == result.legacy_fingerprint


def test_a_v5_clip_whose_quote_is_not_in_the_current_cues_makes_it_stale():
    clips = [v5_clip(3), v5_clip(5, quote="a sentence from a different transcript entirely",
                                  speakers=[BEN])]
    result = try_import(LESSON, completeness=p.LEGACY_V5_COMPLETENESS, positive_clips=clips)
    assert result.status == STALE and result.moments == []
    assert result.reason == "LEGACY_QUOTE_NOT_IN_CANONICAL_CUES"


def test_stale_timestamps_empty_results_and_non_v5_rows_are_never_reused():
    shifted = dict(v5_clip(3), start="05:00:00.000", end="05:00:04.000")
    assert try_import(LESSON, completeness=p.LEGACY_V5_COMPLETENESS,
                      positive_clips=[shifted]).status == STALE
    assert try_import(LESSON, completeness=p.LEGACY_V5_COMPLETENESS,
                      positive_clips=[]).reason == "V5_RESULT_EMPTY_UNPROVABLE"
    assert try_import(LESSON, completeness="positive_clips_v4",
                      positive_clips=[v5_clip(3)]).status == NOT_AVAILABLE


def test_a_v5_clip_naming_the_trainer_is_not_imported():
    result = try_import(LESSON, completeness=p.LEGACY_V5_COMPLETENESS,
                        positive_clips=[v5_clip(10, speakers=[TRAINER], category="trainer")])
    assert result.status == STALE and result.reason == p.TRAINER_AS_POSITIVE_SPEAKER


# ---------------------------------------------------------------------------
# 5. the analyzer
# ---------------------------------------------------------------------------

class MemoryRepository:
    def __init__(self, transcript_=LESSON, legacy=(None, None)):
        self._transcript, self._legacy = transcript_, legacy
        self.saved = []

    def transcript(self, connection, document_id):
        return self._transcript

    def current_analysis(self, connection, lecture_id, *, document_id, transcript_fingerprint):
        for row in reversed(self.saved):
            if (row["document_id"], row["transcript_fingerprint"]) == (
                    document_id, transcript_fingerprint) and row["status"] != FAILED:
                return row
        return None

    def legacy_result(self, connection, legacy_session_id):
        return self._legacy

    def save_analysis(self, connection, *, transcript, source, status, moments, **kw):
        row = {"analysis_id": f"a{len(self.saved) + 1}", "document_id": transcript.document_id,
               "transcript_fingerprint": transcript.fingerprint, "source": source,
               "status": status, "moments": moments, **kw}
        self.saved.append(row)
        return row


def analyze(repository, model, allow_model=True):
    return PositiveMomentAnalyzer(repository=repository, model=model).analyze(
        None, lecture_id="lecture-1", document_id="doc-1", legacy_session_id="s-1",
        allow_model=allow_model)


def test_the_full_two_stage_analysis_persists_only_verified_moments():
    model = ScriptedModel(
        selector={"candidates": [
            {"start_cue": 2, "end_cue": 3, "positive_speakers": [AMY], "category": "content",
             "reason": "clarity", "confidence": 0.8},
            {"start_cue": 5, "end_cue": 6, "positive_speakers": [BEN],
             "category": "learning_experience", "reason": "role", "confidence": 0.9},
            {"start_cue": 8, "end_cue": 8, "positive_speakers": [AMY], "category": "trainer",
             "reason": "thanks", "confidence": 0.9},
            {"start_cue": 3, "end_cue": 77, "positive_speakers": [AMY], "category": "content",
             "reason": "x", "confidence": 0.9}]},
        verifier={"verdicts": [verdict("m1"), verdict("m2", accept=False,
                                                      code="WORKPLACE_STORY_NO_LEARNING")]})
    repository = MemoryRepository()
    outcome = analyze(repository, model)
    assert outcome.status == MOMENTS_FOUND and outcome.provider_calls == 2
    [saved] = repository.saved
    assert saved["source"] == p.SOURCE_CODED_AI
    assert [(m.start_cue, m.end_cue) for m in saved["moments"]] == [(2, 3)]
    assert saved["candidate_count"] == 4 and saved["structurally_valid_count"] == 2
    assert saved["rejection_summary"][p.UNKNOWN_CUE] == 1
    assert saved["rejection_summary"][p.GENERIC_PRAISE_NO_TARGET] == 1
    metadata = json.dumps(saved["model_metadata"])
    assert "gpt-test" in metadata and "reason" not in metadata.casefold().replace(
        "legacy_reason", "")
    # The verifier was sent rebuilt dialogue, not the selector's words.
    assert "clarity" not in model.calls[1][1]


def test_no_positive_moments_is_a_persisted_outcome():
    model = ScriptedModel(selector={"candidates": []})
    repository = MemoryRepository()
    outcome = analyze(repository, model)
    assert outcome.status == NO_POSITIVE_MOMENTS and outcome.provider_calls == 1
    assert repository.saved[0]["status"] == NO_POSITIVE_MOMENTS


def test_a_current_analysis_is_reused_without_a_model_call():
    model = ScriptedModel(selector={"candidates": []})
    repository = MemoryRepository()
    analyze(repository, model)
    again = analyze(repository, model)
    assert again.reused is True and len(model.calls) == 1


def test_a_provable_legacy_result_is_imported_without_any_model_call():
    model = ScriptedModel()
    repository = MemoryRepository(legacy=(p.LEGACY_V5_COMPLETENESS, [v5_clip(3)]))
    outcome = analyze(repository, model)
    assert outcome.status == MOMENTS_FOUND and outcome.source == p.SOURCE_LEGACY_V5
    assert model.calls == []


def test_a_stale_legacy_result_falls_back_to_coded_analysis():
    model = ScriptedModel(selector={"candidates": []})
    stale = [v5_clip(3, quote="words that are not in this transcript at all")]
    repository = MemoryRepository(legacy=(p.LEGACY_V5_COMPLETENESS, stale))
    outcome = analyze(repository, model)
    assert outcome.source == p.SOURCE_CODED_AI and len(model.calls) == 1
    assert repository.saved[0]["model_metadata"]["legacy_import_outcome"] == \
        "LEGACY_QUOTE_NOT_IN_CANONICAL_CUES"


def test_without_permission_to_call_a_model_nothing_is_bought():
    model = ScriptedModel(selector={"candidates": []})
    outcome = analyze(MemoryRepository(), model, allow_model=False)
    assert outcome.status == NOT_ANALYZED and model.calls == []


def test_a_provider_failure_is_persisted_as_failed_with_a_safe_code():
    error = ProviderError("provider_http_error", "HTTP 503", http_status=503)
    repository = MemoryRepository()
    outcome = analyze(repository, ScriptedModel(error=error))
    assert outcome.status == FAILED
    assert repository.saved[0]["error_code"] == "provider_http_error"


def test_a_transcript_change_makes_the_old_analysis_stale():
    edited = transcript([(line.speaker, line.text) for line in LESSON.cues[:-1]]
                        + [(TRAINER, "an edited final line")])
    assert edited.fingerprint != LESSON.fingerprint
    old = {"status": MOMENTS_FOUND}
    assert analysis_state(old, None) == STALE_STATE
    assert analysis_state(None, None) == NOT_ANALYZED
    assert analysis_state(old, old) == MOMENTS_FOUND


def test_the_transcript_fingerprint_ignores_nothing_that_matters():
    same = transcript([(c.speaker, c.text) for c in LESSON.cues])
    assert same.fingerprint == LESSON.fingerprint
    renamed = transcript([(c.speaker, c.text) for c in LESSON.cues], document_id="doc-2")
    assert renamed.fingerprint == LESSON.fingerprint       # content, not identity


def test_moment_fingerprints_change_with_policy_or_transcript():
    moment = build_moment(LESSON, candidate(3, 3, [AMY]))
    one = moment.fingerprint(document_id="d", transcript_fingerprint="t", policy_version="v1")
    assert one == moment.fingerprint(document_id="d", transcript_fingerprint="t",
                                     policy_version="v1")
    assert one != moment.fingerprint(document_id="d", transcript_fingerprint="t2",
                                     policy_version="v1")
    assert one != moment.fingerprint(document_id="d", transcript_fingerprint="t",
                                     policy_version="v2")


def test_deduplicate_is_order_independent():
    a = build_moment(LESSON, candidate(5, 6, [BEN], confidence=0.5))
    b = build_moment(LESSON, candidate(5, 5, [BEN], confidence=0.5))
    first, _ = deduplicate([a, b])
    second, _ = deduplicate([b, a])
    assert [(m.start_cue, m.end_cue) for m in first] == [(m.start_cue, m.end_cue) for m in second]
