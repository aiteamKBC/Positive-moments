"""
Phase 3C2.3D unit tests: Perfect Lecture eligibility, legacy mapping,
ownership, idempotency and the two transition policies.

Everything here is in-memory. No database, no network, no legacy row.
"""
import json
import pathlib
import re

import pytest

from app.qa.perfect import (
    ELIGIBLE,
    NOT_ELIGIBLE_CANCELLED,
    NOT_ELIGIBLE_CHECKLIST_ROW_COUNT,
    NOT_ELIGIBLE_DUPLICATE_ORDER,
    NOT_ELIGIBLE_MISSING_IDENTIFIERS,
    NOT_ELIGIBLE_ORDERS_NOT_1_TO_11,
    NOT_ELIGIBLE_STATUS_NOT_ALL_MET,
    PERFECT_ELIGIBILITY_VERSION,
    evaluate,
    is_cancelled,
    legacy_is_perfect,
    legacy_lecture_key,
)
from app.writer.mapping import PayloadInvariantError
from app.writer.modes import (
    CANARY_NEW_ONLY,
    DRY_RUN,
    PERFECT_BLOCKED_KEY_COLLISION,
    PERFECT_BLOCKED_NOT_READY,
    PERFECT_NOT_ELIGIBLE,
    PERFECT_NOT_ELIGIBLE_LEGACY_ROW_EXISTS,
    PERFECT_PROTECTED_EXISTING_LEGACY_ROW,
    PERFECT_SUPERSEDED_NOT_PERFECT,
    PERFECT_WOULD_INSERT,
    PERFECT_WOULD_SKIP_IDENTICAL,
    PERFECT_WOULD_UPDATE,
    plan_perfect_decision,
)
from app.writer.perfect_mapping import (
    CODED_OWNED_COLUMNS,
    PERFECT_COLUMNS,
    PERFECT_UPDATE_COALESCE_COLUMNS,
    RECORDING_OWNED_COLUMNS,
    perfect_digest,
    perfect_row,
)
from app.writer.perfect_service import PerfectLecturePlanner


ROOT = pathlib.Path(__file__).resolve().parents[2]
LEGACY_CHILD = ROOT / "automation" / "legacy_n8n" / \
    "QA_One_Lecture_Safe_Exact_Recording_v8.json"


# --------------------------------------------------------------------------
# payload builders
# --------------------------------------------------------------------------

def rendered(**overrides) -> dict:
    row = {
        "lecture_id": "11111111-1111-5111-8111-111111111111",
        "evaluation_id": "22222222-2222-5222-8222-222222222222",
        "rendered_session_id": "33333333-3333-5333-8333-333333333333",
        "render_status": "RENDERED", "qa_status": "COMPLETED",
        "source_fingerprint": "a" * 64,
        "session_id": "SESSION-1", "meeting_id": "MEETING-1",
        "subject": "Scheduling Professional", "trainer": "A Trainer",
        "legacy_date": "2026-09-16", "lms_module": "SP Jan 2026",
        "engagement": 100, "attended_count": 7,
        "met_count": 11, "partial_count": 0, "not_met_count": 0,
        "cancelled_session": False,
    }
    row.update(overrides)
    return row


def items(statuses=None, orders=None) -> list[dict]:
    statuses = statuses or ["Met"] * 11
    orders = orders if orders is not None else list(range(1, 12))
    return [{"checklist_order": order, "status": status,
             "checklist_item": f"item {order}", "session_id": "SESSION-1",
             "session_id_match": f"SESSION-1_{order}", "evidence": ""}
            for order, status in zip(orders, statuses)]


# --------------------------------------------------------------------------
# 1-8: the eligibility rule
# --------------------------------------------------------------------------

def test_exactly_eleven_met_is_perfect():
    facts = evaluate(rendered(), items())
    assert facts["is_perfect"] is True
    assert facts["reason"] == ELIGIBLE
    assert facts["eligibility_version"] == PERFECT_ELIGIBILITY_VERSION


def test_ten_met_and_one_partially_met_is_not_perfect():
    statuses = ["Met"] * 10 + ["Partially Met"]
    facts = evaluate(rendered(met_count=10, partial_count=1), items(statuses))
    assert facts["is_perfect"] is False
    assert facts["reason"] == NOT_ELIGIBLE_STATUS_NOT_ALL_MET


def test_ten_met_and_one_not_met_is_not_perfect():
    statuses = ["Met"] * 10 + ["Not Met"]
    facts = evaluate(rendered(met_count=10, not_met_count=1), items(statuses))
    assert facts["is_perfect"] is False
    assert facts["reason"] == NOT_ELIGIBLE_STATUS_NOT_ALL_MET


def test_a_cancelled_eleven_of_eleven_is_not_perfect():
    facts = evaluate(rendered(cancelled_session=True), items())
    assert facts["is_perfect"] is False
    assert facts["reason"] == NOT_ELIGIBLE_CANCELLED


def test_the_legacy_text_true_also_counts_as_cancelled():
    # The legacy column is TEXT holding the JS literals n8n wrote.
    assert is_cancelled("true") is True
    assert is_cancelled("false") is False
    assert is_cancelled(None) is False
    facts = evaluate(rendered(cancelled_session="true"), items())
    assert facts["reason"] == NOT_ELIGIBLE_CANCELLED


def test_a_missing_checklist_row_is_not_perfect():
    facts = evaluate(rendered(), items(["Met"] * 10, list(range(1, 11))))
    assert facts["is_perfect"] is False
    assert facts["reason"] == NOT_ELIGIBLE_CHECKLIST_ROW_COUNT


def test_a_duplicate_checklist_order_is_not_perfect():
    orders = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10]
    facts = evaluate(rendered(), items(["Met"] * 11, orders))
    assert facts["is_perfect"] is False
    assert facts["reason"] == NOT_ELIGIBLE_DUPLICATE_ORDER


def test_orders_must_be_exactly_one_to_eleven():
    orders = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12]
    facts = evaluate(rendered(), items(["Met"] * 11, orders))
    assert facts["is_perfect"] is False
    assert facts["reason"] == NOT_ELIGIBLE_ORDERS_NOT_1_TO_11


def test_a_payload_without_identifiers_is_refused_not_guessed():
    facts = evaluate(rendered(meeting_id=None), items())
    assert facts["reason"] == NOT_ELIGIBLE_MISSING_IDENTIFIERS


def test_the_result_is_deterministic():
    first = evaluate(rendered(), items())
    second = evaluate(rendered(), items())
    assert first == second
    # And it does not depend on the order the rows arrive in.
    shuffled = list(reversed(items()))
    assert evaluate(rendered(), shuffled)["is_perfect"] is True


# --------------------------------------------------------------------------
# 9-10: legacy parity, against the executable workflow
# --------------------------------------------------------------------------

def test_the_legacy_expression_is_still_the_one_we_transcribed():
    """If the legacy branch changes, this test must fail rather than drift."""
    workflow = json.loads(LEGACY_CHILD.read_text(encoding="utf-8"))
    node = next(n for n in workflow["nodes"] if n["name"] == "Prepare Lecture Recording")
    source = node["parameters"]["jsCode"]
    assert "const isPerfect =" in source
    assert "metCount === 11" in source
    assert "uniqueOrders.size === 11" in source
    assert "allMet" in source
    assert "first.cancelled_session !== true" in source
    assert "lecture_key: `${sessionDate}|${subject}`" in source


@pytest.mark.parametrize("statuses,met_count,cancelled", [
    (["Met"] * 11, 11, False),
    (["Met"] * 10 + ["Partially Met"], 10, False),
    (["Met"] * 10 + ["Not Met"], 10, False),
    (["Met"] * 11, 11, True),
    (["Met"] * 11, 10, False),
    (["Met"] * 11, 11, "true"),
    (["Met"] * 11, 11, "false"),
])
def test_eligibility_agrees_with_the_legacy_expression(statuses, met_count, cancelled):
    rows = items(statuses)
    ours = evaluate(rendered(met_count=met_count, cancelled_session=cancelled,
                             partial_count=sum(1 for s in statuses if s == "Partially Met"),
                             not_met_count=sum(1 for s in statuses if s == "Not Met")),
                    rows)
    theirs = legacy_is_perfect(rows, met_count, cancelled)
    assert ours["is_perfect"] == theirs


def test_the_coded_rule_is_never_looser_than_legacy():
    # Legacy accepts twelve rows with one duplicated order; we refuse. The
    # divergence is one-directional by design.
    orders = list(range(1, 12)) + [11]
    rows = items(["Met"] * 12, orders)
    assert legacy_is_perfect(rows, 11, False) is True
    assert evaluate(rendered(), rows)["is_perfect"] is False


def test_the_legacy_lecture_key_is_reproduced_exactly():
    assert legacy_lecture_key("2026-09-16", "Andrew-Scheduling Professional (SP) Jan 2026") \
        == "2026-09-16|Andrew-Scheduling Professional (SP) Jan 2026"
    # Legacy slices the first ten characters, and trims the subject.
    assert legacy_lecture_key("2026-09-16T00:00:00Z", "  Subject  ") == "2026-09-16|Subject"
    from datetime import date as _date
    assert legacy_lecture_key(_date(2026, 9, 16), "S") == "2026-09-16|S"


def test_a_key_without_a_date_is_a_refusal():
    from app.qa.perfect import PerfectLectureInputError
    with pytest.raises(PerfectLectureInputError):
        legacy_lecture_key(None, "Subject")


# --------------------------------------------------------------------------
# 9, 17, 18: the legacy column mapping
# --------------------------------------------------------------------------

def test_the_mapped_columns_are_exactly_the_legacy_upsert_columns():
    workflow = json.loads(LEGACY_CHILD.read_text(encoding="utf-8"))
    node = next(n for n in workflow["nodes"] if n["name"] == "Upsert Perfect Lecture")
    query = node["parameters"]["query"]
    block = query[query.index("(") + 1:query.index(")")]
    legacy_columns = tuple(part.strip() for part in block.split(",") if part.strip())
    assert legacy_columns == PERFECT_COLUMNS


def test_the_legacy_conflict_key_and_merge_semantics_are_reproduced():
    workflow = json.loads(LEGACY_CHILD.read_text(encoding="utf-8"))
    node = next(n for n in workflow["nodes"] if n["name"] == "Upsert Perfect Lecture")
    query = node["parameters"]["query"]
    assert "ON CONFLICT (lecture_key) DO UPDATE SET" in query
    for column in PERFECT_UPDATE_COALESCE_COLUMNS:
        assert re.search(rf"{column}\s*=\s*COALESCE\(EXCLUDED\.{column}", query)
    # session_date and subject are NOT updatable in legacy, so an update can
    # never move a row to a different lecture.
    assert "session_date  =" not in query and "session_date =" not in query
    assert "subject =" not in query


def test_the_mapping_never_supplies_recording_url():
    row = perfect_row(rendered())
    assert row["recording_url"] is None
    for column in RECORDING_OWNED_COLUMNS:
        assert column not in CODED_OWNED_COLUMNS


def test_meeting_id_and_session_id_are_mapped_from_the_rendered_payload():
    row = perfect_row(rendered())
    assert row["meeting_id"] == "MEETING-1"
    assert row["session_id"] == "SESSION-1"
    assert row["session_date"] == "2026-09-16"
    assert row["module"] == "SP Jan 2026"
    assert row["attended_count"] == 7
    assert row["met_count"] == 11


@pytest.mark.parametrize("missing", ["session_id", "meeting_id", "legacy_date",
                                     "subject", "met_count"])
def test_a_not_null_column_is_a_refusal_not_an_improvised_value(missing):
    with pytest.raises(PayloadInvariantError):
        perfect_row(rendered(**{missing: None}))


def test_the_digest_covers_the_mapped_columns_and_nothing_else():
    row = perfect_row(rendered())
    assert perfect_digest(row) == perfect_digest(dict(row))
    noisy = dict(row)
    noisy["excel_synced_at"] = "2026-09-17T00:00:00Z"
    noisy["detected_at"] = "2026-09-17T00:00:00Z"
    assert perfect_digest(noisy) == perfect_digest(row)
    # A recording_url appearing later IS part of the legacy contract and shows.
    enriched = dict(row, recording_url="https://example.invalid/x")
    assert perfect_digest(enriched) != perfect_digest(row)


# --------------------------------------------------------------------------
# 11-16, 21-24: policy
# --------------------------------------------------------------------------

def _decide(**overrides) -> str:
    kwargs = {"mode": DRY_RUN, "render_status": "RENDERED", "qa_status": "COMPLETED",
              "payload_valid": True, "is_perfect": True, "target_exists": False,
              "coded_owned": False, "foreign_session_on_key": False,
              "fingerprint_matches": False}
    kwargs.update(overrides)
    return plan_perfect_decision(**kwargs)


def test_a_perfect_lecture_with_no_legacy_row_would_insert():
    assert _decide() == PERFECT_WOULD_INSERT


def test_a_not_perfect_lecture_does_nothing():
    assert _decide(is_perfect=False) == PERFECT_NOT_ELIGIBLE


def test_a_legacy_owned_row_is_protected_in_every_mode():
    for mode in (DRY_RUN, CANARY_NEW_ONLY, "PRODUCTION_NEW_ONLY", "EXPLICIT_BACKFILL"):
        assert _decide(mode=mode, target_exists=True, coded_owned=False) \
            == PERFECT_PROTECTED_EXISTING_LEGACY_ROW


def test_a_coded_owned_row_is_recognised_and_updatable():
    assert _decide(target_exists=True, coded_owned=True) == PERFECT_WOULD_UPDATE


def test_the_same_fingerprint_is_a_noop():
    assert _decide(target_exists=True, coded_owned=True,
                   fingerprint_matches=True) == PERFECT_WOULD_SKIP_IDENTICAL


def test_a_lecture_key_pointing_at_a_different_session_is_refused():
    # The production table already contains one date+subject pair mapping to
    # two distinct legacy sessions.
    assert _decide(foreign_session_on_key=True) == PERFECT_BLOCKED_KEY_COLLISION
    # And the refusal wins even when the row would otherwise be ours.
    assert _decide(foreign_session_on_key=True, target_exists=True,
                   coded_owned=True) == PERFECT_BLOCKED_KEY_COLLISION


def test_an_unfinished_qa_result_never_reaches_the_legacy_table():
    assert _decide(qa_status="REVIEW_REQUIRED") == PERFECT_BLOCKED_NOT_READY
    assert _decide(render_status="FAILED") == PERFECT_BLOCKED_NOT_READY


def test_perfect_to_not_perfect_leaves_a_coded_owned_row_standing():
    assert _decide(is_perfect=False, target_exists=True, coded_owned=True) \
        == PERFECT_SUPERSEDED_NOT_PERFECT


def test_perfect_to_not_perfect_never_touches_a_legacy_owned_row():
    assert _decide(is_perfect=False, target_exists=True, coded_owned=False) \
        == PERFECT_NOT_ELIGIBLE_LEGACY_ROW_EXISTS


def test_not_perfect_to_perfect_inserts_when_no_row_exists():
    assert _decide(is_perfect=True, target_exists=False) == PERFECT_WOULD_INSERT


def test_not_perfect_to_perfect_updates_only_a_row_we_own():
    assert _decide(is_perfect=True, target_exists=True, coded_owned=True) \
        == PERFECT_WOULD_UPDATE
    assert _decide(is_perfect=True, target_exists=True, coded_owned=False) \
        == PERFECT_PROTECTED_EXISTING_LEGACY_ROW


# --------------------------------------------------------------------------
# the planner's guards and rollback, with fakes
# --------------------------------------------------------------------------

class FakeOwnership:
    def __init__(self, record=None):
        self.record_value = record
        self.marked = []

    def find(self, connection, key, version):
        return self.record_value

    def mark_status(self, connection, write_id, status, metadata=None):
        self.marked.append((write_id, status, metadata))


class FakeLegacy:
    def __init__(self, row=None, excel_synced=False):
        self.row = row
        self.excel_synced = excel_synced
        self.deleted = []

    def load(self, connection, key):
        return self.row

    def delete_owned(self, connection, key, *, allow_write):
        assert allow_write
        self.deleted.append(key)
        self.row = None


class FakeConnection:
    def __init__(self, excel_synced=False):
        self.excel_synced = excel_synced

    def execute(self, statement, params=None):
        class _Cursor:
            def __init__(self, value):
                self.value = value

            def fetchone(self):
                return (self.value,)
        return _Cursor(self.excel_synced)

    def transaction(self):
        class _Transaction:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False
        return _Transaction()


def _planner(ownership, legacy, mode=CANARY_NEW_ONLY):
    return PerfectLecturePlanner(
        result_repository=None, ownership_repository=ownership,
        legacy_repository=legacy, mode=mode,
        lecture_ids=["11111111-1111-5111-8111-111111111111"], confirmed=True)


def test_rollback_refuses_a_row_it_does_not_own():
    legacy = FakeLegacy(row={"recording_url": None})
    planner = _planner(FakeOwnership(None), legacy)
    outcome = planner.rollback_owned_row(FakeConnection(), "2026-09-16|S")
    assert outcome["status"] == "PROTECTED_EXISTING_LEGACY_ROW"
    assert outcome["deleted"] is False
    assert legacy.deleted == []


def test_rollback_refuses_when_a_recording_url_already_exists():
    legacy = FakeLegacy(row={"recording_url": "https://example.invalid/rec"})
    planner = _planner(FakeOwnership({"perfect_write_id": "w1"}), legacy)
    outcome = planner.rollback_owned_row(FakeConnection(), "2026-09-16|S")
    assert outcome["status"] == "BLOCKED_DOWNSTREAM_DEPENDENCY"
    assert "RECORDING_URL_POPULATED" in outcome["downstream_dependencies"]
    assert legacy.deleted == []


def test_rollback_refuses_when_the_row_was_already_published_to_excel():
    legacy = FakeLegacy(row={"recording_url": None})
    planner = _planner(FakeOwnership({"perfect_write_id": "w1"}), legacy)
    outcome = planner.rollback_owned_row(FakeConnection(excel_synced=True),
                                         "2026-09-16|S")
    assert outcome["status"] == "BLOCKED_DOWNSTREAM_DEPENDENCY"
    assert "EXCEL_SYNCED" in outcome["downstream_dependencies"]
    assert legacy.deleted == []


def test_rollback_deletes_only_a_clean_coded_owned_row():
    legacy = FakeLegacy(row={"recording_url": None})
    ownership = FakeOwnership({"perfect_write_id": "w1"})
    planner = _planner(ownership, legacy)
    outcome = planner.rollback_owned_row(FakeConnection(), "2026-09-16|S")
    assert outcome["status"] == "ROLLED_BACK" and outcome["deleted"] is True
    assert legacy.deleted == ["2026-09-16|S"]
    assert ownership.marked[-1][1] == "ROLLED_BACK"


def test_rollback_needs_a_write_enabled_mode():
    from app.writer.perfect_service import PerfectWriteVerificationError
    planner = PerfectLecturePlanner(result_repository=None,
                                    ownership_repository=FakeOwnership(None),
                                    legacy_repository=FakeLegacy(), mode=DRY_RUN)
    with pytest.raises(PerfectWriteVerificationError):
        planner.rollback_owned_row(FakeConnection(), "2026-09-16|S")


def test_a_write_enabled_planner_must_be_scoped_and_confirmed():
    from app.writer.modes import WriterModeError
    with pytest.raises(WriterModeError):
        PerfectLecturePlanner(result_repository=None, ownership_repository=None,
                              legacy_repository=None, mode=CANARY_NEW_ONLY)
    with pytest.raises(WriterModeError):
        PerfectLecturePlanner(result_repository=None, ownership_repository=None,
                              legacy_repository=None, mode=CANARY_NEW_ONLY,
                              lecture_ids=["a"], confirmed=False)
    with pytest.raises(WriterModeError):
        PerfectLecturePlanner(result_repository=None, ownership_repository=None,
                              legacy_repository=None, mode=CANARY_NEW_ONLY,
                              lecture_ids=["a", "b"], confirmed=True)


def test_the_legacy_repository_refuses_a_write_without_a_write_enabled_mode():
    from app.common.errors import PlatformError
    from app.db.repositories.perfect_lectures import LegacyPerfectLectureRepository
    repository = LegacyPerfectLectureRepository()
    with pytest.raises(PlatformError):
        repository.write(None, perfect_row(rendered()), allow_write=False)
    with pytest.raises(PlatformError):
        repository.delete_owned(None, "k", allow_write=False)


def test_the_legacy_repository_refuses_to_carry_a_recording_url():
    from app.common.errors import PlatformError
    from app.db.repositories.perfect_lectures import LegacyPerfectLectureRepository
    row = dict(perfect_row(rendered()), recording_url="https://example.invalid/x")
    with pytest.raises(PlatformError):
        LegacyPerfectLectureRepository().write(None, row, allow_write=True)
