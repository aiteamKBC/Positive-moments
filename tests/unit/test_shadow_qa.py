"""
Phase 3A unit tests: prompt contract, deterministic rules, structured-output
and evidence validation, fingerprint provenance, and provider behaviour.

The provider is always a stub here - no test ever reaches a real model.
"""
import json
import uuid
from datetime import date, datetime, timezone

import pytest

from app.qa.checklist import (
    CHECKLIST_ITEMS,
    CHECKLIST_ITEM_COUNT,
    MET,
    NOT_MET,
    PARTIALLY_MET,
    SOURCE_AI,
    SOURCE_DETERMINISTIC_DURATION,
    SOURCE_DETERMINISTIC_ENGAGEMENT,
    SOURCE_DETERMINISTIC_PUNCTUALITY,
)
from app.qa.deterministic import (
    DELIVERED,
    DELIVERY_MINIMUM_MINUTES,
    NON_DELIVERED,
    delivery_status,
    duration_score,
    duration_text,
    item1_status,
    item2_status,
    non_delivered_checklist,
    non_delivered_summary,
)
from app.qa.inputs import (
    KSB_FRAMEWORK,
    QA_ENGINE_VERSION,
    REQUIRED_ATTENDANCE_ROSTER_VERSION,
    preview,
    qa_source_fingerprint,
)
from app.qa import prompt as prompt_module
from app.qa.provider import OpenAIChatProvider, ProviderError
from app.qa.service import (
    COMPLETED,
    INVALID_EVIDENCE,
    INVALID_STRUCTURED_OUTPUT,
    MODEL_ERROR,
    PENDING,
    REVIEW_REQUIRED,
    QaInputError,
    ShadowQaService,
)
from app.qa.validation import (
    NO_CUE_OVERLAP,
    NOT_POSITIVE,
    OUT_OF_RANGE,
    TOO_SHORT,
    UNPARSEABLE,
    VALID,
    collect_clips,
    parse_timestamp_ms,
    validate_clip,
    validate_structured_output,
)


TARGET = date(2026, 9, 4)


# --- fixtures ----------------------------------------------------------------

def good_output(**overrides):
    payload = {
        "session_info": {"trainer": "Morgan Trainerfield", "date": "2026-09-04"},
        "checklist_evaluation": [
            {"item": item, "status": MET, "evidence_clips": []}
            for item in CHECKLIST_ITEMS
        ],
        "overall_summary": {"strengths": [], "areas_for_improvement": [],
                            "overall_judgement": "Solid session."},
        "ksbs_covered": [],
        "teaching_quality": {"rating_1_5": 4, "comments": "Clear.", "evidence_clips": []},
    }
    payload.update(overrides)
    return payload


def package(**overrides):
    base = {
        "lecture_id": uuid.UUID(int=1), "subject": "Test Lecture", "module": "Test Lecture",
        "meeting_id": "MEETING", "scheduled_start": datetime(2026, 9, 4, 9, tzinfo=timezone.utc),
        "scheduled_end": datetime(2026, 9, 4, 11, tzinfo=timezone.utc),
        "session_date": "2026-09-04", "selection_id": uuid.UUID(int=2),
        "primary_provider_transcript_id": "TRANSCRIPT",
        # Phase 3C3B: Item 2 now derives its bounds from the call-start instant
        # plus the canonical cue offsets, so a package must carry both. The
        # call opens on the scheduled hour and the cues span the full two
        # hours, which reproduces the previous 0/0 differences exactly.
        "actual_start": datetime(2026, 9, 4, 9, tzinfo=timezone.utc),
        "actual_end": datetime(2026, 9, 4, 11, tzinfo=timezone.utc),
        "start_difference_minutes": 0, "end_difference_minutes": 0,
        "combined_id": uuid.UUID(int=3), "duration_minutes": 120, "duration_seconds": 7200,
        "combined_content_sha256": "a" * 64, "combined_content_bytes": 1000,
        "combined_source_fingerprint": "b" * 64, "combined_content": "WEBVTT\n\n",
        "document_id": uuid.UUID(int=4), "document_source_fingerprint": "c" * 64,
        "cue_count": 10, "first_cue_start_ms": 0, "last_cue_end_ms": 7_200_000,
        "engagement_id": uuid.UUID(int=5), "attendance_snapshot_id": uuid.UUID(int=6),
        "attended_count": 10, "spoke_count": 8, "engagement_percentage": "80.00",
        "engagement_score": 5, "learner_engagement_status": MET,
        "item7_override_applied": True, "engagement_calculation_status": "CALCULATED",
        "engagement_source_fingerprint": "d" * 64,
        "attendance_roster_version": REQUIRED_ATTENDANCE_ROSTER_VERSION,
        "canonical_trainer_speaker_id": uuid.UUID(int=7),
        "canonical_trainer": "Morgan Trainerfield",
        # F-03. The snapshot behind those ten attendees, stated explicitly: the
        # service now refuses to finalize on attendance it cannot show the
        # source actually answered. Ten rows, ten present, ten members is the
        # same fact `attended_count` above already asserts.
        "attendance_source_row_count": 10, "attendance_present_row_count": 10,
        "attendance_effective_member_count": 10,
        "attendance_source_rows_any_status": None,
    }
    base.update(overrides)
    base["delivery_status"] = delivery_status(base["duration_minutes"])
    return base


class StubProvider:
    def __init__(self, output=None, error=None):
        self.output = output
        self.error = error
        self.calls = 0
        self.last_system = None
        self.last_user = None

    def complete_json(self, *, system_message, user_message):
        self.calls += 1
        self.last_system, self.last_user = system_message, user_message
        if self.error:
            raise self.error
        return {"output": self.output, "provider": "openai", "model_requested": "gpt-5.2",
                "model_reported": "gpt-5.2", "response_id": "resp_1", "usage": {},
                "attempts": 1}


class StubInputs:
    def __init__(self, packages, cues=((0, 5000), (6000, 9000))):
        self.packages = packages
        self.cues = list(cues)

    def load_inputs(self, connection, target_date, **kwargs):
        return [dict(item) for item in self.packages]

    def load_cues(self, connection, document_id):
        return self.cues


class StubEvaluations:
    def __init__(self, existing=None):
        self.rows = {}
        self.existing = existing or {}
        self.writes = 0

    def find_by_fingerprint(self, connection, source_fingerprint):
        return self.existing.get(source_fingerprint) or self.rows.get(source_fingerprint)

    def upsert(self, connection, evaluation, checklist, clips):
        self.writes += 1
        fingerprint = evaluation["source_fingerprint"]
        created = 0 if fingerprint in self.rows else 1
        self.rows[fingerprint] = {"evaluation_id": evaluation["evaluation_id"],
                                  "qa_status": evaluation["qa_status"],
                                  "ai_called": evaluation["ai_called"],
                                  "evaluation": evaluation, "checklist": checklist,
                                  "clips": clips}
        return {"evaluation_id": evaluation["evaluation_id"],
                "created": created, "updated": 1 - created}


class StubRuns:
    def __init__(self):
        self.summaries = []

    def start(self, connection, target_date, **kwargs):
        return uuid.uuid4()

    def complete(self, connection, run_id, summary):
        self.summaries.append(summary)


def service(packages, provider=None, evaluations=None, cues=((0, 5000), (6000, 9000))):
    return ShadowQaService(
        input_repository=StubInputs(packages, cues),
        evaluation_repository=evaluations or StubEvaluations(),
        run_repository=StubRuns(), provider=provider,
        resolver_version="r1", role_algorithm_version="o1",
        engagement_algorithm_version="e1", model_name="gpt-5.2")


# --- 1-2. pinned roster version ----------------------------------------------

def test_engagement_selection_pins_the_approved_roster_version():
    assert REQUIRED_ATTENDANCE_ROSTER_VERSION == "attendance_roster_v2_exclude_makeup"
    built = service([package()])
    assert built.attendance_roster_version == REQUIRED_ATTENDANCE_ROSTER_VERSION
    from app.db.repositories.qa_shadow import LOAD_QA_INPUTS
    assert "sn.attendance_resolution_version = %s" in LOAD_QA_INPUTS
    # Never "latest by timestamp": the v1 evidence is deliberately still stored.
    assert "ORDER BY sn.created_at DESC" not in LOAD_QA_INPUTS


def test_wrong_roster_version_is_refused_outright():
    with pytest.raises(QaInputError):
        ShadowQaService(
            input_repository=None, evaluation_repository=None, run_repository=None,
            attendance_roster_version="attendance_roster_legacy_v1",
            resolver_version="r", role_algorithm_version="o",
            engagement_algorithm_version="e", model_name="gpt-5.2")


def test_a_row_carrying_another_roster_version_is_rejected():
    rogue = package(attendance_roster_version="attendance_roster_legacy_v1")
    with pytest.raises(QaInputError):
        service([rogue]).run_day(None, TARGET)


def test_two_current_inputs_for_one_lecture_fail_safely():
    with pytest.raises(QaInputError):
        service([package(), package()]).run_day(None, TARGET)


def test_no_evidence_fails_safely():
    with pytest.raises(QaInputError):
        service([]).run_day(None, TARGET)


# --- 3-8. input package and fingerprint --------------------------------------

def test_preview_reports_metadata_and_never_transcript_text():
    item = package(combined_content="WEBVTT\n\n00:00.000 --> 00:01.000\nsecret words")
    report = preview(item)
    assert "secret words" not in json.dumps(report)
    assert report["duration_minutes"] == 120
    assert report["transcript_cue_count"] == 10
    assert report["canonical_trainer_label_length"] == len("Morgan Trainerfield")
    assert "canonical_trainer" not in report


def test_fingerprint_is_deterministic():
    first = qa_source_fingerprint(package=package(), model="gpt-5.2")
    second = qa_source_fingerprint(package=package(), model="gpt-5.2")
    assert first == second and len(first) == 64


@pytest.mark.parametrize("field,value", [
    ("combined_source_fingerprint", "0" * 64),
    ("combined_content_sha256", "1" * 64),
    ("document_source_fingerprint", "2" * 64),
    ("selection_id", uuid.UUID(int=99)),
    ("duration_minutes", 121),
    ("start_difference_minutes", 5),
    ("end_difference_minutes", -5),
    ("engagement_id", uuid.UUID(int=98)),
    ("engagement_source_fingerprint", "3" * 64),
    ("attendance_snapshot_id", uuid.UUID(int=97)),
    ("canonical_trainer_speaker_id", uuid.UUID(int=96)),
])
def test_fingerprint_changes_when_a_material_input_changes(field, value):
    base = qa_source_fingerprint(package=package(), model="gpt-5.2")
    assert qa_source_fingerprint(package=package(**{field: value}), model="gpt-5.2") != base


def test_fingerprint_changes_with_model_and_engine_version():
    base = qa_source_fingerprint(package=package(), model="gpt-5.2")
    assert qa_source_fingerprint(package=package(), model="gpt-4.1") != base
    assert qa_source_fingerprint(package=package(), model="gpt-5.2",
                                 engine_version="other_engine") != base


def test_fingerprint_changes_when_the_prompt_version_changes(monkeypatch):
    base = qa_source_fingerprint(package=package(), model="gpt-5.2")
    monkeypatch.setattr("app.qa.inputs.PROMPT_VERSION", "legacy_qa_v8_prompt_v2")
    assert qa_source_fingerprint(package=package(), model="gpt-5.2") != base


# --- prompt contract ----------------------------------------------------------

def test_prompt_records_both_legacy_models_separately():
    assert prompt_module.LEGACY_QA_MODEL == "gpt-5.2"
    assert prompt_module.LEGACY_STRUCTURED_OUTPUT_FIXER_MODEL == "gpt-4.1"
    assert prompt_module.LEGACY_QA_MODEL != prompt_module.LEGACY_STRUCTURED_OUTPUT_FIXER_MODEL


def test_system_message_is_the_export_text_and_is_hashed():
    assert prompt_module.SYSTEM_MESSAGE.startswith("You are an educational QA evaluator")
    assert not prompt_module.SYSTEM_MESSAGE.startswith("=")   # n8n expression marker stripped
    assert len(prompt_module.SYSTEM_MESSAGE_SHA256) == 64
    for item in CHECKLIST_ITEMS:
        assert item in prompt_module.SYSTEM_MESSAGE


def test_user_message_matches_the_legacy_template_shape():
    message = prompt_module.build_user_message(
        transcript_text="WEBVTT", transcript_id="T", meeting_id="M", subject="S",
        scheduled_start="2026-09-04T09:00:00Z", created_datetime="2026-09-04T09:02:00Z",
        start_difference_minutes=2, start_status="Late",
        scheduled_end="2026-09-04T11:00:00Z", end_datetime="2026-09-04T11:05:00Z",
        end_difference_minutes=5, end_status="Overrun", duration_minutes=123)
    assert message.startswith("TRANSCRIPT:\nWEBVTT\n\nMETADATA:\n")
    # Legacy spacing, including the space before the colon.
    assert "startDifferenceMinutes : 2\n" in message
    assert "endDifferenceMinutes : 5\n" in message
    assert message.endswith("KSB_Framework (optional):\n")


def test_missing_values_render_empty_like_legacy():
    message = prompt_module.build_user_message(
        transcript_text="", transcript_id=None, meeting_id=None, subject=None,
        scheduled_start=None, created_datetime=None, start_difference_minutes=None,
        start_status=None, scheduled_end=None, end_datetime=None,
        end_difference_minutes=None, end_status=None, duration_minutes=None)
    assert "None" not in message
    assert "transcriptId: \n" in message


def test_ksb_framework_is_empty_because_no_legacy_node_populated_it():
    assert KSB_FRAMEWORK == ""


# --- 9-12. delivery gate and the cancelled contract ---------------------------

@pytest.mark.parametrize("minutes,expected", [
    (0, NON_DELIVERED), (13, NON_DELIVERED), (19, NON_DELIVERED),
    (19.9, NON_DELIVERED), (20, DELIVERED), (21, DELIVERED), (210, DELIVERED),
])
def test_delivery_gate_boundaries(minutes, expected):
    assert delivery_status(minutes) == expected


def test_delivery_minimum_matches_the_legacy_node():
    assert DELIVERY_MINIMUM_MINUTES == 20


def test_missing_duration_is_not_delivered():
    assert delivery_status(None) == NON_DELIVERED


def test_short_session_never_calls_the_model():
    provider = StubProvider(good_output())
    evaluations = StubEvaluations()
    summary = service([package(duration_minutes=13)], provider, evaluations).run_day(
        None, TARGET, execute=True)
    assert provider.calls == 0
    assert summary["provider_calls"] == 0
    assert summary["non_delivered_count"] == 1
    assert summary["lectures"][0]["qa_status"] == "NON_DELIVERED"


def test_cancelled_output_matches_the_legacy_contract():
    rows = non_delivered_checklist()
    assert len(rows) == CHECKLIST_ITEM_COUNT
    assert [row["checklist_item"] for row in rows] == list(CHECKLIST_ITEMS)
    assert all(row["status"] == NOT_MET for row in rows)
    summary = non_delivered_summary()
    assert (summary["met_count"], summary["partial_count"], summary["not_met_count"]) == (0, 0, 11)
    assert summary["trainer_display"] == "Session not delivered"
    assert summary["engagement_percentage"] == 0 and summary["engagement_score"] == 0
    assert summary["duration_text"] == "0 minutes" and summary["duration_score"] == 0
    # Legacy uses rating 1 here, not 0.
    assert summary["teaching_quality_rating"] == 1


# --- 13. checklist identity ---------------------------------------------------

def test_exactly_eleven_checklist_items_with_the_legacy_strings():
    assert CHECKLIST_ITEM_COUNT == 11
    assert CHECKLIST_ITEMS[0] == "1) Session duration: Minimum of two hours"
    assert CHECKLIST_ITEMS[2] == "3) Professional demeanor: Maintained throughout the session"
    assert CHECKLIST_ITEMS[4] == ("5) Content alignment: Matches the curriculum/ "
                                  "apprenticeship standard")
    assert CHECKLIST_ITEMS[10] == "11) Next steps: Clear follow-up activities communicated"


def test_checklist_strings_are_defined_once():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    needle = "Professional demeanor: Maintained throughout the session"
    holders = [path.name for path in root.rglob("*.py") if needle in path.read_text("utf-8")]
    # checklist.py defines it; prompt.py carries the verbatim legacy prompt.
    assert sorted(holders) == ["checklist.py", "prompt.py"]


# --- 14-19. structured output validation --------------------------------------

def test_valid_output_passes():
    assert validate_structured_output(good_output()) == []


def test_missing_top_level_key_is_rejected():
    payload = good_output()
    del payload["ksbs_covered"]
    assert any(error.startswith("MISSING_TOP_LEVEL_KEY") for error
               in validate_structured_output(payload))


def test_extra_top_level_key_is_rejected():
    errors = validate_structured_output(good_output(extra_notes="hello"))
    assert any(error.startswith("UNEXPECTED_TOP_LEVEL_KEY") for error in errors)


def test_wrong_checklist_count_is_rejected():
    payload = good_output()
    payload["checklist_evaluation"] = payload["checklist_evaluation"][:10]
    assert any(error.startswith("CHECKLIST_COUNT") for error in validate_structured_output(payload))


def test_wrong_checklist_order_is_rejected():
    payload = good_output()
    rows = payload["checklist_evaluation"]
    rows[0], rows[1] = rows[1], rows[0]
    assert any(error.startswith("CHECKLIST_ITEM_MISMATCH")
               for error in validate_structured_output(payload))


def test_renamed_checklist_item_is_rejected():
    payload = good_output()
    payload["checklist_evaluation"][2]["item"] = ("3) Professional demeanour: Maintained "
                                                  "throughout the session")
    assert "CHECKLIST_ITEM_MISMATCH:3" in validate_structured_output(payload)


def test_invalid_status_is_rejected():
    payload = good_output()
    payload["checklist_evaluation"][3]["status"] = "Mostly Met"
    assert "CHECKLIST_STATUS_INVALID:4" in validate_structured_output(payload)


def test_too_many_evidence_clips_is_rejected():
    payload = good_output()
    payload["checklist_evaluation"][0]["evidence_clips"] = [
        {"start": "00:00:00.000", "end": "00:00:05.000"} for _ in range(4)]
    assert any(error.startswith("TOO_MANY_EVIDENCE_CLIPS")
               for error in validate_structured_output(payload))


def test_rating_out_of_range_is_rejected():
    payload = good_output()
    payload["teaching_quality"]["rating_1_5"] = 9
    assert "INVALID_TEACHING_QUALITY_RATING" in validate_structured_output(payload)


def test_invalid_ksb_type_is_rejected():
    payload = good_output(ksbs_covered=[{"type": "Attitude", "title": "x", "evidence_clips": []}])
    assert "INVALID_KSB_TYPE:1" in validate_structured_output(payload)


def test_trailing_whitespace_item_variants_are_accepted_and_reported():
    """
    The legacy system message lists the items twice and items 1, 2 and 10 carry
    trailing spaces in one of the lists, so the model legitimately returns
    either spelling - and the legacy production table holds both. A
    whitespace-only difference must not be treated as malformed output.
    """
    from app.qa.validation import (
        EXACT, MISMATCH, WHITESPACE_ONLY, checklist_item_match,
        checklist_item_whitespace_variants,
    )
    payload = good_output()
    payload["checklist_evaluation"][1]["item"] = CHECKLIST_ITEMS[1] + " "
    payload["checklist_evaluation"][9]["item"] = CHECKLIST_ITEMS[9] + "  "
    assert validate_structured_output(payload) == []
    assert checklist_item_whitespace_variants(payload) == [2, 10]
    assert checklist_item_match(CHECKLIST_ITEMS[1], CHECKLIST_ITEMS[1]) == EXACT
    assert checklist_item_match(" " + CHECKLIST_ITEMS[1], CHECKLIST_ITEMS[1]) == WHITESPACE_ONLY
    assert checklist_item_match(CHECKLIST_ITEMS[1].replace("ends", "finishes"),
                                CHECKLIST_ITEMS[1]) == MISMATCH
    assert checklist_item_match(None, CHECKLIST_ITEMS[1]) == MISMATCH


def test_whitespace_variant_still_persists_the_canonical_item_string():
    ai = good_output()
    ai["checklist_evaluation"][1]["item"] = CHECKLIST_ITEMS[1] + " "
    result, stored, _ = run_one(package(), StubProvider(ai))
    assert result["qa_status"] == COMPLETED
    assert [row["checklist_item"] for row in stored["checklist"]] == list(CHECKLIST_ITEMS)
    assert stored["evaluation"]["metadata"]["checklist_item_whitespace_variants"] == [2]


def test_inner_whitespace_change_is_still_a_mismatch():
    payload = good_output()
    payload["checklist_evaluation"][1]["item"] = CHECKLIST_ITEMS[1].replace(
        "Session starts", "Session  starts")
    assert "CHECKLIST_ITEM_MISMATCH:2" in validate_structured_output(payload)


def test_non_object_output_is_rejected():
    assert validate_structured_output(["not", "an", "object"]) == ["OUTPUT_NOT_AN_OBJECT"]


# --- 20-27. deterministic items ----------------------------------------------

@pytest.mark.parametrize("minutes,expected", [
    (0, NOT_MET), (94, NOT_MET), (94.9, NOT_MET), (95, PARTIALLY_MET),
    (104, PARTIALLY_MET), (105, MET), (120, MET), (210, MET),
])
def test_item1_duration_thresholds(minutes, expected):
    assert item1_status(minutes) == expected


def test_item1_never_uses_the_120_minute_band():
    # 120 is a duration_score boundary, not an Item 1 threshold.
    assert item1_status(110) == MET
    assert duration_score(110) == 3


def test_item1_with_an_unusable_duration_is_not_met():
    assert item1_status(None) == NOT_MET
    assert item1_status("120") == NOT_MET


@pytest.mark.parametrize("start,end,expected", [
    (0, 0, MET), (20, 0, MET), (21, 0, NOT_MET),
    (0, -20, MET), (0, -21, NOT_MET),
    (-45, 0, MET), (0, 90, MET), (-45, 90, MET), (25, -25, NOT_MET),
])
def test_item2_punctuality_rules(start, end, expected):
    assert item2_status(start, end) == expected


def test_item2_missing_timing_is_not_met():
    assert item2_status(None, 0) == NOT_MET
    assert item2_status(0, None) == NOT_MET


@pytest.mark.parametrize("minutes,expected", [
    (0, 1), (104, 1), (105, 2), (109, 2), (110, 3), (114, 3),
    (115, 4), (120, 4), (120.5, 5), (121, 5), (210, 5),
])
def test_duration_score_boundaries(minutes, expected):
    # Legacy's first band is strictly greater than 120, so 120 scores 4.
    assert duration_score(minutes) == expected


def test_duration_text_matches_legacy():
    assert duration_text(120) == "2 hours"
    assert duration_text(125) == "2 hours 5 minutes"


# --- 28-31. item 7 and the trainer policy -------------------------------------

def run_one(item, provider=None, evaluations=None, cues=((0, 5000), (6000, 9000))):
    provider = provider or StubProvider(good_output())
    evaluations = evaluations or StubEvaluations()
    summary = service([item], provider, evaluations, cues).run_day(
        None, TARGET, execute=True)
    stored = list(evaluations.rows.values())[0] if evaluations.rows else None
    return summary["lectures"][0], stored, provider


def test_item7_uses_the_deterministic_override():
    ai = good_output()
    ai["checklist_evaluation"][6]["status"] = MET
    result, stored, _ = run_one(package(learner_engagement_status=NOT_MET,
                                        item7_override_applied=True),
                                StubProvider(ai))
    assert result["ai_item7_status"] == MET
    assert result["final_item7_status"] == NOT_MET
    row = stored["checklist"][6]
    assert row["status"] == NOT_MET
    assert row["ai_status"] == MET
    assert row["status_source"] == SOURCE_DETERMINISTIC_ENGAGEMENT


def test_item7_falls_back_to_the_model_when_no_override_applied():
    ai = good_output()
    ai["checklist_evaluation"][6]["status"] = PARTIALLY_MET
    result, stored, _ = run_one(package(item7_override_applied=False,
                                        learner_engagement_status=None), StubProvider(ai))
    assert result["final_item7_status"] == PARTIALLY_MET
    assert stored["checklist"][6]["status_source"] == SOURCE_AI


def test_items_1_and_2_override_the_model():
    ai = good_output()
    ai["checklist_evaluation"][0]["status"] = MET
    ai["checklist_evaluation"][1]["status"] = MET
    # Lateness is expressed where the system now reads it from: the first
    # canonical cue, 45 minutes into a call that opened on the hour. Setting
    # start_difference_minutes directly would be overwritten, because Phase
    # 3C3B derives that number rather than trusting a pre-computed one.
    _, stored, _ = run_one(package(duration_minutes=60,
                                   first_cue_start_ms=45 * 60_000),
                           StubProvider(ai))
    assert stored["checklist"][0]["status"] == NOT_MET
    assert stored["checklist"][0]["status_source"] == SOURCE_DETERMINISTIC_DURATION
    assert stored["checklist"][0]["ai_status"] == MET
    assert stored["checklist"][1]["status"] == NOT_MET
    assert stored["checklist"][1]["status_source"] == SOURCE_DETERMINISTIC_PUNCTUALITY


def test_engagement_review_state_does_not_silently_finalize():
    result, stored, _ = run_one(
        package(engagement_calculation_status="REVIEW_AMBIGUITY_MAY_CHANGE_RESULT"))
    assert result["qa_status"] == REVIEW_REQUIRED
    assert stored["evaluation"]["review_reason"].startswith("ENGAGEMENT_")


def test_ai_trainer_never_overwrites_the_canonical_trainer():
    ai = good_output()
    ai["session_info"]["trainer"] = "Someone Else Entirely"
    result, stored, _ = run_one(package(), StubProvider(ai))
    assert stored["evaluation"]["canonical_trainer"] == "Morgan Trainerfield"
    assert stored["evaluation"]["ai_suggested_trainer"] == "Someone Else Entirely"
    assert stored["evaluation"]["trainer_source"] == "PHASE_2C3_VTT_TOP_SPEAKER"
    assert result["ai_trainer_matches_canonical"] == "DIFFERENT"


# --- 32-35. evidence clips ----------------------------------------------------

CUES = [(0, 5000), (6000, 9000)]


def test_timestamp_parsing():
    assert parse_timestamp_ms("00:00:05.250") == 5250
    assert parse_timestamp_ms("01:02:03") == 3_723_000
    assert parse_timestamp_ms("bad") is None


def test_valid_clip_is_accepted():
    verdict = validate_clip({"start": "00:00:00.000", "end": "00:00:04.000"},
                            document_start_ms=0, document_end_ms=9000, cues=CUES)
    assert verdict["status"] == VALID and verdict["overlapping_cue_count"] == 1


def test_clip_outside_the_transcript_is_rejected():
    verdict = validate_clip({"start": "00:59:00.000", "end": "00:59:30.000"},
                            document_start_ms=0, document_end_ms=9000, cues=CUES)
    assert verdict["status"] == OUT_OF_RANGE


def test_clip_with_end_not_after_start_is_rejected():
    for start, end in (("00:00:05.000", "00:00:05.000"), ("00:00:06.000", "00:00:04.000")):
        assert validate_clip({"start": start, "end": end}, document_start_ms=0,
                             document_end_ms=9000, cues=CUES)["status"] == NOT_POSITIVE


def test_clip_shorter_than_two_seconds_is_flagged():
    verdict = validate_clip({"start": "00:00:01.000", "end": "00:00:02.500"},
                            document_start_ms=0, document_end_ms=9000, cues=CUES)
    assert verdict["status"] == TOO_SHORT


def test_clip_in_a_silent_gap_has_no_cue_overlap():
    verdict = validate_clip({"start": "00:00:05.100", "end": "00:00:05.900"},
                            document_start_ms=0, document_end_ms=9000, cues=CUES)
    assert verdict["status"] == NO_CUE_OVERLAP


def test_unparseable_clip_is_reported_not_repaired():
    verdict = validate_clip({"start": "five past two", "end": "later"},
                            document_start_ms=0, document_end_ms=9000, cues=CUES)
    assert verdict["status"] == UNPARSEABLE
    assert verdict["start_ms"] is None


def test_clips_are_collected_from_every_section():
    payload = good_output()
    clip = {"start": "00:00:00.000", "end": "00:00:03.000"}
    payload["checklist_evaluation"][0]["evidence_clips"] = [clip]
    payload["overall_summary"]["strengths"] = [{"title": "s", "evidence_clips": [clip]}]
    payload["overall_summary"]["areas_for_improvement"] = [{"title": "a", "evidence_clips": [clip]}]
    payload["ksbs_covered"] = [{"type": "Skill", "title": "k", "evidence_clips": [clip]}]
    payload["teaching_quality"]["evidence_clips"] = [clip]
    sources = sorted({found["source"] for found in collect_clips(payload)})
    assert sources == ["areas_for_improvement", "checklist", "ksb", "strengths",
                       "teaching_quality"]


def test_hallucinated_timestamp_marks_the_evaluation_invalid():
    ai = good_output()
    ai["checklist_evaluation"][3]["evidence_clips"] = [
        {"start": "09:59:00.000", "end": "09:59:30.000"}]
    result, stored, _ = run_one(package(), StubProvider(ai), cues=CUES)
    assert result["qa_status"] == INVALID_EVIDENCE
    assert result["invalid_evidence_clip_count"] == 1
    assert stored["clips"][0]["validation_status"] == OUT_OF_RANGE


# --- 36-39. cost control and error states -------------------------------------

def test_identical_inputs_reuse_the_existing_evaluation():
    item = package()
    evaluations = StubEvaluations()
    provider = StubProvider(good_output())
    built = service([item], provider, evaluations)
    built.run_day(None, TARGET, execute=True)
    assert provider.calls == 1
    second = built.run_day(None, TARGET, execute=True)
    assert provider.calls == 1
    assert second["reused_evaluations"] == 1
    assert second["provider_calls"] == 0


def test_force_re_evaluates_and_calls_the_model_again():
    item = package()
    evaluations = StubEvaluations()
    provider = StubProvider(good_output())
    built = service([item], provider, evaluations)
    built.run_day(None, TARGET, execute=True)
    built.run_day(None, TARGET, execute=True, force=True)
    assert provider.calls == 2


def test_preview_mode_never_calls_the_model_or_writes():
    provider = StubProvider(good_output())
    evaluations = StubEvaluations()
    summary = service([package()], provider, evaluations).run_day(None, TARGET)
    assert provider.calls == 0 and evaluations.writes == 0
    assert summary["mode"] == "PREVIEW"
    assert summary["lectures"][0]["qa_status"] == PENDING


def test_provider_error_is_preserved_as_an_error_not_as_not_met():
    provider = StubProvider(error=ProviderError("provider_http_error", "boom", http_status=500))
    result, stored, _ = run_one(package(), provider)
    assert result["qa_status"] == MODEL_ERROR
    assert stored["evaluation"]["error_code"] == "provider_http_error"
    # No fabricated checklist verdicts from a transport failure.
    assert stored["checklist"] == []


def test_malformed_model_output_is_preserved_as_an_error():
    bad = good_output()
    bad["checklist_evaluation"] = bad["checklist_evaluation"][:9]
    result, stored, _ = run_one(package(), StubProvider(bad))
    assert result["qa_status"] == INVALID_STRUCTURED_OUTPUT
    assert stored["evaluation"]["structured_output_error_count"] > 0


def test_missing_provider_records_pending_rather_than_guessing():
    evaluations = StubEvaluations()
    summary = service([package()], None, evaluations).run_day(None, TARGET, execute=True)
    result = summary["lectures"][0]
    assert result["qa_status"] == PENDING
    assert result["error_code"] == "provider_not_configured"
    # The deterministic layer is still complete and recorded.
    assert result["deterministic_item1"] == MET
    assert result["deterministic_item2"] == MET
    assert result["final_item7_status"] == MET
    assert summary["provider_calls"] == 0


# --- provider behaviour --------------------------------------------------------

def test_provider_requires_a_key_and_a_model():
    with pytest.raises(ProviderError):
        OpenAIChatProvider(api_key="", model="gpt-5.2")
    with pytest.raises(ProviderError):
        OpenAIChatProvider(api_key="k", model="")


def test_provider_retries_are_bounded_and_never_change_the_model():
    waits = []
    provider = OpenAIChatProvider(api_key="k", model="gpt-5.2", max_tries=3,
                                  sleep=waits.append)
    attempts = {"count": 0}

    def failing(path, body):
        attempts["count"] += 1
        raise ProviderError("provider_http_error", "rate limited", http_status=429)

    provider._post = failing
    with pytest.raises(ProviderError):
        provider.complete_json(system_message="s", user_message="u")
    assert attempts["count"] == 3
    assert len(waits) == 2
    assert provider.model == "gpt-5.2"


def test_provider_does_not_retry_a_client_error():
    provider = OpenAIChatProvider(api_key="k", model="gpt-5.2", max_tries=3, sleep=lambda _: None)
    attempts = {"count": 0}

    def failing(path, body):
        attempts["count"] += 1
        raise ProviderError("provider_http_error", "bad request", http_status=400)

    provider._post = failing
    with pytest.raises(ProviderError):
        provider.complete_json(system_message="s", user_message="u")
    assert attempts["count"] == 1


def test_provider_never_puts_the_key_in_an_error():
    provider = OpenAIChatProvider(api_key="sk-secret-value", model="gpt-5.2",
                                  sleep=lambda _: None)

    def failing(path, body):
        raise ProviderError("provider_http_error", "denied", http_status=401)

    provider._post = failing
    with pytest.raises(ProviderError) as caught:
        provider.complete_json(system_message="s", user_message="u")
    assert "sk-secret-value" not in str(caught.value)


# --- 42-45. boundaries --------------------------------------------------------

def test_shadow_qa_never_writes_legacy_or_external_tables():
    """
    Scans the SQL the phase can execute, not its prose: the modules are
    allowed to DOCUMENT which tables they avoid.
    """
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    sources = [*(root / "app" / "qa").glob("*.py"),
               root / "app" / "db" / "repositories" / "qa_shadow.py"]
    statements = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {ast.get_docstring(node) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if text in docstrings:
                    continue
                if any(word in text.upper() for word in
                       ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "CREATE ")):
                    statements.append((path.name, text))
    assert statements, "no SQL was found to scan"
    for name, text in statements:
        upper = " ".join(text.split()).upper()
        for table in ("KBC_ATTENDANCE", "KBC_USERS_DATA", "APTEM_AUTO_EXTRACTING"):
            assert table not in upper, (name, table)
        for verb in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "TRUNCATE"):
            if verb in upper:
                for legacy in ("QA_DOCTORS_SESSIONS", "QA_DOCTORS_CHECKLIST_ITEMS",
                               "QA_PERFECT_LECTURES", "QA_DOCTORS_TRANSCRIPTS"):
                    assert legacy not in upper, (name, verb, legacy)
        assert "CREATE VIEW" not in upper and "CREATE OR REPLACE VIEW" not in upper


def test_shadow_qa_code_calls_no_external_service():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    for path in (root / "app" / "qa").glob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in ("graph.microsoft.com", "sharepoint", "ffmpeg"):
            assert forbidden not in text, (path.name, forbidden)


def test_summary_reports_the_zero_markers():
    summary = service([package()], StubProvider(good_output())).run_day(None, TARGET)
    for key in ("graph_calls", "live_attendance_queries", "lms_queries",
                "transcript_rebuilds", "speaker_rematches", "engagement_recalculations",
                "legacy_qa_writes"):
        assert summary[key] == 0
    assert summary["qa_engine_version"] == QA_ENGINE_VERSION
