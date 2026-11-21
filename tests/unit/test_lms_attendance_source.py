"""
LMS attendance register as the attendance source for waiting lectures.

Pure rules, the resolution service and the recovery service with in-memory fakes. No
database, no LMS, no Graph, no model. Every name and address is invented.
"""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.attendance import lms_source as lms
from app.attendance.coverage import (
    SOURCE_AVAILABLE_WITH_MEMBERS,
    SOURCE_MISSING,
    SOURCE_PARTIAL_OR_INVALID,
    classify,
    provenance,
)
from app.attendance.recovery import (
    ATTENDANCE_SOURCE_RECOVERED,
    NO_CHANGE,
    WAIT_FOR_ATTENDANCE_SOURCE,
    AttendanceRecoveryService,
)
from app.attendance.roster import ATTENDANCE_RESOLUTION_VERSION, build_roster
from app.attendance.service import KBC_ATTENDANCE_SOURCE, SpeakerResolutionService
from app.config import environment
from app.lms import matching
from app.lms.sources import LectureFacts, LmsOccurrence, LmsSnapshot

START = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
DAY = date(2026, 11, 3)


# -- fixtures --------------------------------------------------------------------------

def row(profile, status="present", basis="teams", *, teams="present", module="M1",
        occurrence="O1", name=None, email=None):
    return {"occurrence_id": occurrence, "learner_profile_id": profile,
            "learner_name": name if name is not None else f"Learner {profile}",
            "learner_email": email if email is not None else f"l{profile}@example.invalid",
            "module_catalogue_id": module, "attendance_status": status,
            "attendance_basis": basis, "teams_attendance_status": teams}


def occurrence(oid="O1", *, status="completed", start=START, keys=("K1",), module="M1",
               programme="Real Programme"):
    return LmsOccurrence(
        occurrence_id=oid, live_session_id=f"S-{oid}", session_number=1, status=status,
        scheduled_start=start, scheduled_end=start + timedelta(hours=2),
        meeting_keys=frozenset(keys), online_meeting_keys=frozenset(), series_status="active",
        meeting_type="teams", module_catalogue_id=module, module_exists=True,
        module_deleted=False, programme_name=programme)


def lecture(keys=("K1",), start=START, suppressed=False):
    return LectureFacts(lecture_id="L1", session_date=DAY, scheduled_start=start,
                        scheduled_end=start + timedelta(hours=2), meeting_keys=frozenset(keys),
                        online_meeting_key=None, duplicate_suppressed=suppressed)


CONFIG = matching.MatchConfig.from_text("Test Programme")
BASES = ("teams", "qr", "teams_qr", "staff", "parallel")


def verified(*occurrences):
    snapshot = LmsSnapshot(tuple(occurrences), {}, START - timedelta(days=8), START + timedelta(days=8))
    return matching.match_lecture(lecture(), snapshot, CONFIG), {o.occurrence_id: o for o in occurrences}


def _roster(rows):
    match, occ = verified(occurrence())
    decision = lms.decide(match, occ, rows)
    assert decision.used, decision
    return decision, build_roster(list(decision.rows), version=ATTENDANCE_RESOLUTION_VERSION)


# -- 1. the final attendance_status decides; basis is informational --------------------

@pytest.mark.parametrize("basis", BASES)
@pytest.mark.parametrize("teams", ["present", "absent", None])
def test_present_counts_whatever_the_basis_or_raw_teams_status(basis, teams):
    assert lms.classify_row(row(1, "present", basis, teams=teams)) == lms.ATTENDED
    decision, roster = _roster([row(1, "present", basis, teams=teams)])
    assert roster["effective_member_count"] == 1 and roster["excluded_makeup_count"] == 0


@pytest.mark.parametrize("basis", BASES)
@pytest.mark.parametrize("teams", ["present", "absent", None])
def test_absent_never_counts_whatever_the_basis(basis, teams):
    assert lms.classify_row(row(1, "absent", basis, teams=teams)) == lms.NOT_PRESENT
    _, roster = _roster([row(1, "absent", basis, teams=teams), row(2)])
    assert roster["effective_member_count"] == 1


def test_parallel_present_is_an_attended_member_never_makeup():
    decision, roster = _roster([row(1, basis="parallel", teams="absent"), row(2)])
    assert roster["effective_member_count"] == 2 and roster["excluded_makeup_count"] == 0
    assert {r["attendance_status"] for r in decision.rows} == {"original"}
    assert decision.counts["learners_attended"] == 2


def test_present_with_teams_absent_is_still_present():
    decision, roster = _roster([row(1, "present", "teams", teams="absent")])
    assert roster["effective_member_count"] == 1


def test_an_unknown_basis_does_not_matter():
    _, roster = _roster([row(1, basis="something_new")])
    assert roster["effective_member_count"] == 1


@pytest.mark.parametrize("status", ["late", "", None, "excused"])
def test_an_unknown_final_status_holds_the_whole_lecture(status):
    match, occ = verified(occurrence())
    decision = lms.decide(match, occ, [row(1), row(2, status)])
    assert decision.status == lms.UNRECOGNISED_ROW and not decision.used and decision.rows == ()


# -- 2. one learner per lecture --------------------------------------------------------

def test_one_learner_under_two_modules_is_one_member_and_the_collapse_is_counted():
    decision, roster = _roster([row(1, module="M1"), row(1, module="M2", basis="parallel"), row(2)])
    assert roster["effective_member_count"] == 2 and len(decision.rows) == 2
    assert decision.counts["duplicate_rows_collapsed"] == 1
    assert decision.counts["learners_attended"] == 2


def test_duplicates_are_by_profile_id_even_when_the_name_differs():
    decision, roster = _roster([row(1, module="M1", name="Ann Lee"),
                                row(1, module="M2", name="Ann  Lee-Smith")])
    assert len(decision.rows) == 1 and roster["effective_member_count"] == 1


def test_conflicting_final_statuses_for_one_learner_hold_the_lecture():
    match, occ = verified(occurrence())
    decision = lms.decide(match, occ, [row(1, module="M1"),
                                       row(1, "absent", module="M2", teams="absent"), row(2)])
    assert decision.status == lms.CONFLICTING_LEARNER_STATUS and decision.rows == ()
    assert decision.counts == {"conflicting_learners": 1}


def test_all_absent_rows_for_one_learner_collapse_to_one_absent_learner():
    decision, roster = _roster([row(1, "absent", module="M1"), row(1, "absent", module="M2"), row(2)])
    assert decision.counts["learners_absent"] == 1 and roster["effective_member_count"] == 1


def test_roster_input_does_not_depend_on_query_order():
    rows = [row(3), row(1, module="M2"), row(1), row(2, basis="parallel")]
    first, _ = _roster(rows)
    second, _ = _roster(list(reversed(rows)))
    assert first.rows == second.rows


def test_bots_and_blank_names_still_filtered():
    _, roster = _roster([row(1), row(2, name="Fireflies Notetaker"), row(3, name="  ")])
    assert roster["effective_member_count"] == 1
    assert roster["excluded_bot_count"] == 1 and roster["excluded_no_name_count"] == 1


def test_external_person_id_is_the_lms_profile_id_and_email_is_hashed():
    _, roster = _roster([row(42, email="Someone@Example.invalid")])
    member = roster["members"][0]
    assert member["external_person_id"] == "42"
    assert "example" not in str(member["email_sha256"]) and len(member["email_sha256"]) == 64


# -- 3. matching: VERIFIED only, never title or date -----------------------------------

def test_verified_match_with_completed_occurrence_is_used():
    match, occ = verified(occurrence())
    decision = lms.decide(match, occ, [row(1), row(2, "absent", teams="absent")])
    assert match.status == matching.VERIFIED and decision.used
    assert decision.occurrence_ids == ("O1",) and decision.link_roles == (matching.PRIMARY,)
    assert decision.source_rows_any_status == 2 and decision.source_rows_present == 1


def test_same_date_but_different_meeting_is_not_a_match():
    snapshot = LmsSnapshot((occurrence(keys=("OTHER",)),), {}, START, START)
    match = matching.match_lecture(lecture(), snapshot, CONFIG)
    assert match.status == matching.NO_LMS_SESSION
    assert lms.decide(match, {}, [row(1)]).status == lms.NOT_VERIFIED


def test_same_meeting_at_another_start_is_only_a_reschedule_candidate():
    snapshot = LmsSnapshot((occurrence(start=START + timedelta(days=1)),), {}, START, START)
    match = matching.match_lecture(lecture(), snapshot, CONFIG)
    assert match.status == matching.RESCHEDULED_CANDIDATE
    assert lms.decide(match, {}, [row(1)]).status == lms.NOT_VERIFIED


def test_two_unrelated_occurrences_at_one_start_are_ambiguous_and_never_first_taken():
    match, occ = verified(occurrence("O1", module="M1"), occurrence("O2", module="M2"))
    assert match.status == matching.AMBIGUOUS
    decision = lms.decide(match, occ, [row(1, occurrence="O1")])
    assert decision.status == lms.NOT_VERIFIED and decision.match_status == matching.AMBIGUOUS


def test_test_programme_occurrence_is_excluded():
    match, occ = verified(occurrence(programme="Test Programme"))
    assert match.status == matching.TEST_EXCLUDED
    assert lms.decide(match, occ, [row(1)]).status == lms.NOT_VERIFIED


def test_li_duplicate_is_not_matched():
    snapshot = LmsSnapshot((occurrence(),), {}, START, START)
    match = matching.match_lecture(lecture(suppressed=True), snapshot, CONFIG)
    assert lms.decide(match, {}, [row(1)]).status == lms.NOT_VERIFIED


@pytest.mark.parametrize("status", ["scheduled", "cancelled", None])
def test_rows_wait_until_every_linked_occurrence_is_completed(status):
    match, _ = verified(occurrence())
    decision = lms.decide(match, {"O1": occurrence(status=status)} if status else {}, [row(1)])
    assert decision.status == lms.OCCURRENCE_NOT_COMPLETED


def test_verified_without_rows_keeps_waiting():
    match, occ = verified(occurrence())
    assert lms.decide(match, occ, []).status == lms.NO_ROWS


def test_shared_module_occurrences_contribute_their_rows_and_share_learners():
    root, shared = occurrence("O1", module="M1"), occurrence("O2", module="M2")
    snapshot = LmsSnapshot((root, shared), {"M2": "M1"}, START, START)
    match = matching.match_lecture(lecture(), snapshot, CONFIG)
    assert match.status == matching.VERIFIED and len(match.links) == 2
    decision = lms.decide(match, {"O1": root, "O2": shared},
                          [row(1, occurrence="O1"), row(2, occurrence="O2", module="M2"),
                           row(1, occurrence="O2", module="M2")])
    assert decision.used and decision.link_roles == (matching.PRIMARY, matching.SHARED_MODULE)
    assert len(decision.rows) == 2 and decision.counts["duplicate_rows_collapsed"] == 1


# -- 4. coverage semantics unchanged ---------------------------------------------------

def _coverage(decision, roster):
    return classify(source_row_count=roster["source_row_count"],
                    present_row_count=roster["present_row_count"],
                    effective_member_count=roster["effective_member_count"],
                    source_rows_any_status=decision.source_rows_any_status)


def test_only_absences_is_partial_never_a_confirmed_zero():
    decision, roster = _roster([row(1, "absent", teams="absent")])
    assert _coverage(decision, roster) == SOURCE_PARTIAL_OR_INVALID


def test_members_are_authoritative():
    decision, roster = _roster([row(1)])
    assert _coverage(decision, roster) == SOURCE_AVAILABLE_WITH_MEMBERS


def test_provenance_carries_no_learner_data():
    decision, _ = _roster([row(7, name="Private Person", email="private@example.invalid")])
    text = repr(decision.provenance())
    assert "Private" not in text and "example" not in text and "'7'" not in text


# -- 7. the resolution service ---------------------------------------------------------

class FakeKbc:
    def __init__(self, rows=(), any_status=0):
        self.rows, self.any_status, self.calls = list(rows), any_status, 0

    def load_present_rows(self, connection, session_date, module):
        self.calls += 1
        return list(self.rows)

    def count_rows_any_status(self, connection, session_date, module):
        return {"source_rows_any_status": self.any_status, "source_rows_present": len(self.rows)}


class FakeLms:
    def __init__(self, decision):
        self.decision, self.calls = decision, 0

    def decide(self, connection, lecture_id):
        self.calls += 1
        return self.decision


class FakeCoverage:
    def __init__(self, current=None):
        self.current = current or {**provenance(status="SOURCE_UNKNOWN"), "snapshot_present": False}

    def for_lecture(self, connection, lecture_id, *, attendance_resolution_version):
        return self.current


class FakeSnapshots:
    def __init__(self):
        self.saved = {}

    def upsert(self, connection, snapshot, members):
        if snapshot["snapshot_id"] in self.saved:
            return {"created": 0, "reused": 1, "members_written": 0}
        self.saved[snapshot["snapshot_id"]] = (snapshot, members)
        return {"created": 1, "reused": 0, "members_written": len(members)}


class FakeUpserts:
    def upsert(self, *args):
        return {"created": 0, "updated": 0}


class FakeRuns:
    def start(self, connection, target_date, versions):
        return uuid.uuid4()

    def complete(self, connection, run_id, summary):
        pass


class FakeInventory:
    def load_lecture(self, connection, lecture_id, inventory_version, parser_version):
        return {"lecture_id": lecture_id, "subject": "Module One", "module": "Module One",
                "session_date": DAY, "document_id": uuid.uuid4(),
                "speakers": [{"speaker_id": uuid.uuid4(), "speaker_label_raw": "Trainer Person",
                              "speaker_label_normalized": "trainer person", "gross_spoken_ms": 9000,
                              "first_cue_index": 0, "cue_count": 9},
                             {"speaker_id": uuid.uuid4(), "speaker_label_raw": "Learner 1",
                              "speaker_label_normalized": "learner 1", "gross_spoken_ms": 900,
                              "first_cue_index": 1, "cue_count": 2}]}


def kbc_row(i, name):
    return {"learner_id": i, "full_name": name, "email": f"k{i}@example.invalid",
            "attendance_flag": 1, "module": "module one", "attendance_status": "original"}


def used_decision(*rows):
    match, occ = verified(occurrence())
    return lms.decide(match, occ, list(rows) or [row(1), row(2)])


def service(kbc, lms_source=None, snapshots=None):
    return SpeakerResolutionService(
        inventory_repository=FakeInventory(), attendance_repository=kbc,
        snapshot_repository=snapshots or FakeSnapshots(), identity_repository=FakeUpserts(),
        role_repository=FakeUpserts(), run_repository=FakeRuns(),
        lms_attendance_source=lms_source)


def _resolve(svc, persist=False):
    return svc.resolve_lecture(None, "L1", persist=persist)["lectures"][0]


def test_without_the_lms_source_behaviour_is_exactly_kbc():
    out = _resolve(service(FakeKbc()))
    assert out["attendance_source"] == KBC_ATTENDANCE_SOURCE and out["lms_attendance"] is None


def test_kbc_answering_means_the_lms_is_never_asked_and_the_snapshot_id_is_unchanged():
    kbc = FakeKbc([kbc_row(1, "Learner 1")])
    fake = FakeLms(used_decision())
    with_lms = _resolve(service(kbc, fake))
    without = _resolve(service(FakeKbc([kbc_row(1, "Learner 1")])))
    assert fake.calls == 0 and with_lms["attendance_source"] == KBC_ATTENDANCE_SOURCE
    assert with_lms["attendance_snapshot_id"] == without["attendance_snapshot_id"]


def test_kbc_only_absences_is_not_an_answer_so_the_lms_is_asked():
    fake = FakeLms(used_decision())
    out = _resolve(service(FakeKbc(any_status=3), fake))
    assert fake.calls == 1 and out["attendance_source"] == lms.LMS_ATTENDANCE_SOURCE


def test_waiting_lecture_uses_the_lms_register():
    snapshots = FakeSnapshots()
    out = _resolve(service(FakeKbc(), FakeLms(used_decision()), snapshots=snapshots), persist=True)
    assert out["attendance_source"] == lms.LMS_ATTENDANCE_SOURCE
    assert out["effective_attendance_count"] == 2 and out["exact_matches"] == 1
    snapshot, members = next(iter(snapshots.saved.values()))
    assert snapshot["attendance_resolution_version"] == ATTENDANCE_RESOLUTION_VERSION
    meta = snapshot["metadata"]
    assert meta["attendance_source"] == lms.LMS_ATTENDANCE_SOURCE
    assert meta["lms_attendance"]["decision"] == lms.USED
    assert meta["lms_attendance"]["occurrence_ids"] == ["O1"]
    assert meta["source_rows_any_status"] == 2
    assert {m["external_person_id"] for m in members} == {"1", "2"}


@pytest.mark.parametrize("decision", [
    lms.LmsAttendanceDecision(lms.NOT_VERIFIED, match_status=matching.AMBIGUOUS),
    lms.LmsAttendanceDecision(lms.OCCURRENCE_NOT_COMPLETED),
    lms.LmsAttendanceDecision(lms.UNRECOGNISED_ROW, reason="status:late"),
    lms.LmsAttendanceDecision(lms.CONFLICTING_LEARNER_STATUS, reason="learners_with_conflicting_status:1"),
    lms.LmsAttendanceDecision(lms.NO_ROWS),
    lms.LmsAttendanceDecision(lms.UNAVAILABLE, reason="OperationalError"),
])
def test_an_lms_decision_other_than_used_keeps_the_kbc_waiting_answer(decision):
    out = _resolve(service(FakeKbc(), FakeLms(decision)))
    assert out["attendance_source"] == KBC_ATTENDANCE_SOURCE
    assert out["lms_attendance"]["decision"] == decision.status
    assert classify(source_row_count=out["attendance_source_row_count"],
                    present_row_count=out["attendance_present_row_count"],
                    effective_member_count=out["effective_attendance_count"],
                    source_rows_any_status=out["attendance_source_rows_any_status"]) == SOURCE_MISSING


def test_same_lms_answer_twice_is_the_same_snapshot():
    snapshots = FakeSnapshots()
    svc = service(FakeKbc(), FakeLms(used_decision()), snapshots=snapshots)
    first = svc.resolve_lecture(None, "L1", persist=True)
    second = svc.resolve_lecture(None, "L1", persist=True)
    assert first["snapshots_created"] == 1 and second["snapshots_reused"] == 1
    assert first["lectures"][0]["attendance_snapshot_id"] == second["lectures"][0][
        "attendance_snapshot_id"]


def test_a_changed_lms_register_is_a_new_snapshot():
    a = _resolve(service(FakeKbc(), FakeLms(used_decision(row(1), row(2)))))
    b = _resolve(service(FakeKbc(), FakeLms(used_decision(row(1), row(2), row(3)))))
    assert a["attendance_snapshot_id"] != b["attendance_snapshot_id"]


# -- 8. recovery (unchanged service): idempotence, deterministic QA, no paid calls -----------------

class FakeResolution:
    """resolve_lecture as the real service reports it, for a scripted observation."""

    def __init__(self, observed):
        self.observed, self.persisted = observed, 0

    def resolve_lecture(self, connection, lecture_id, *, persist):
        self.persisted += int(persist)
        return {"target_date": DAY.isoformat(), "snapshots_created": int(persist),
                "snapshots_reused": 0, "resolutions_created": 0, "roles_created": 0,
                "graph_calls": 0, "ai_calls": 0, "lectures": [dict(self.observed)]}


def observed(snapshot_id, *, members, source=lms.LMS_ATTENDANCE_SOURCE, rows=None):
    rows = members if rows is None else rows
    return {"attendance_snapshot_id": snapshot_id, "attendance_source_row_count": rows,
            "attendance_present_row_count": rows, "effective_attendance_count": members,
            "attendance_source_rows_any_status": rows, "source_fingerprint_prefix": "f" * 16,
            "attendance_source": source, "lms_attendance": {"decision": lms.USED}}


class FakeEngagement:
    def __init__(self):
        self.calls = 0

    def calculate_lecture(self, connection, lecture_id, *, persist):
        self.calls += 1
        return {"scope": "LECTURE", "lectures_in_scope": 1, "engagements_created": 1,
                "engagements_updated": 0, "graph_calls": 0, "ai_calls": 0,
                "lectures": [{"attendance_snapshot_id": "NEW", "engagement_id": "E",
                              "calculation_status": "CALCULATED", "engagement_status_detail": None,
                              "attended_count": 2, "spoke_count": 1, "engagement_score": 50,
                              "item7_override_applied": False, "learner_engagement_status": "OK",
                              "attendance_zero_confirmed": False}]}


class FakeQa:
    def __init__(self):
        self.calls = []

    def refresh_deterministic(self, connection, lecture_id, *, persist):
        self.calls.append(persist)
        return {"refresh_status": "QA_DETERMINISTIC_REFRESHED", "provider_calls": 0,
                "openai_reused": True, "checklist_changed": False, "evaluation_id": "Q2"}


def recovery(resolution, before, engagement=None, qa=None):
    current = FakeCoverage(before)
    return AttendanceRecoveryService(
        resolution_service=resolution, engagement_service=engagement or FakeEngagement(),
        qa_service=qa or FakeQa(), coverage_repository=current)


def test_waiting_lecture_recovers_from_the_lms_with_a_deterministic_qa_refresh():
    before = {**provenance(status=SOURCE_MISSING, source_row_count=0, present_row_count=0,
                           effective_member_count=0, snapshot_id="OLD"),
              "attendance_source": KBC_ATTENDANCE_SOURCE, "snapshot_present": True}
    resolution, engagement, qa = FakeResolution(observed("NEW", members=2)), FakeEngagement(), FakeQa()
    out = recovery(resolution, before, engagement, qa).recover(None, "L1", persist=True)
    assert ATTENDANCE_SOURCE_RECOVERED in out["results"]
    assert engagement.calls == 1 and qa.calls == [True]
    assert out["provider_calls"] == 0 and out["openai_calls"] == 0


def test_dry_run_recovery_writes_nothing():
    before = {**provenance(status=SOURCE_MISSING, snapshot_id="OLD"), "snapshot_present": True}
    resolution, engagement, qa = FakeResolution(observed("NEW", members=2)), FakeEngagement(), FakeQa()
    out = recovery(resolution, before, engagement, qa).recover(None, "L1", persist=False)
    assert out["status"] == ATTENDANCE_SOURCE_RECOVERED
    assert resolution.persisted == 0 and engagement.calls == 0 and qa.calls == []


def test_second_recovery_with_the_same_lms_answer_is_a_noop():
    before = {**provenance(status=SOURCE_AVAILABLE_WITH_MEMBERS, source_row_count=2,
                           present_row_count=2, effective_member_count=2, snapshot_id="NEW"),
              "attendance_source": lms.LMS_ATTENDANCE_SOURCE, "snapshot_present": True}
    resolution, engagement, qa = FakeResolution(observed("NEW", members=2)), FakeEngagement(), FakeQa()
    out = recovery(resolution, before, engagement, qa).recover(None, "L1", persist=True)
    assert out["status"] == NO_CHANGE and resolution.persisted == 0
    assert engagement.calls == 0 and qa.calls == []


def test_still_waiting_writes_nothing():
    before = {**provenance(status=SOURCE_MISSING, snapshot_id="KBC0"), "snapshot_present": True}
    resolution = FakeResolution(observed("KBC0", members=0, source=KBC_ATTENDANCE_SOURCE))
    out = recovery(resolution, before).recover(None, "L1", persist=True)
    assert out["status"] == WAIT_FOR_ATTENDANCE_SOURCE and resolution.persisted == 0


# -- 9. the gateway: the pipeline's own connection, no new setting --------------------

class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Savepoint:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.savepoints += 1

    def __exit__(self, kind, exc, tb):
        if exc is not None:
            self.conn.rolled_back += 1
        return False


class FakeConnection:
    """Answers the gateway's SELECTs by statement; records every statement it was sent."""

    def __init__(self, *, lms_sync=True, persisted=None, occurrences=(), statuses=(),
                 register=(), fail_on=None):
        self.lms_sync, self.persisted, self.occurrences = lms_sync, persisted, occurrences
        self.statuses, self.register, self.fail_on = statuses, register, fail_on
        self.sql, self.savepoints, self.rolled_back = [], 0, 0

    def transaction(self):
        return _Savepoint(self)

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("foreign server unreachable")
        if sql is lms.LMS_SYNC_PRESENT_SQL:
            return _Result([(self.lms_sync,)])
        if sql is lms.PERSISTED_MATCH_SQL:
            return _Result(self.persisted or [])
        if sql is lms.LMS_SYNC_OCCURRENCE_STATUS_SQL:
            return _Result(list(self.statuses))
        if sql is lms.LMS_ATTENDANCE_SQL:
            return _Result(list(self.register))
        raise AssertionError(f"unexpected statement: {sql[:60]}")


def register(profile, status="present", occurrence="O1", module="M1"):
    return (occurrence, profile, f"Learner {profile}", f"l{profile}@example.invalid", module, status)


def persisted_verified(*occurrence_ids):
    return [("VERIFIED", oid, f"S-{oid}", "M1", 1, START, "PRIMARY" if i == 0 else "SHARED_MODULE",
             None if i == 0 else "M1") for i, oid in enumerate(occurrence_ids)]


def test_the_saved_verified_decision_is_used():
    conn = FakeConnection(persisted=persisted_verified("O1"), statuses=[("O1", "completed")],
                          register=[register(1), register(2, "absent"), register(1, module="M2")])
    decision = lms.LmsAttendanceSource().decide(conn, "L1")
    assert decision.used and decision.occurrence_ids == ("O1",)
    assert decision.counts == {"learners_attended": 1, "learners_absent": 1,
                               "duplicate_rows_collapsed": 1}
    assert conn.savepoints == 1


def test_a_saved_shared_module_decision_reads_every_linked_occurrence():
    conn = FakeConnection(persisted=persisted_verified("O1", "O2"),
                          statuses=[("O1", "completed"), ("O2", "completed")],
                          register=[register(1), register(1, occurrence="O2", module="M2"), register(2)])
    decision = lms.LmsAttendanceSource().decide(conn, "L1")
    assert decision.used and decision.occurrence_ids == ("O1", "O2") and len(decision.rows) == 2


@pytest.mark.parametrize("status", [matching.TEST_EXCLUDED, matching.AMBIGUOUS, matching.CONFLICT,
                                    matching.NO_LMS_SESSION, matching.LI_DUPLICATE,
                                    matching.RESCHEDULED_CANDIDATE])
def test_a_saved_non_verified_decision_fetches_nothing(status):
    conn = FakeConnection(persisted=[(status, None, None, None, None, None, None, None)])
    decision = lms.LmsAttendanceSource().decide(conn, "L1")
    assert decision.status == lms.NOT_VERIFIED and decision.match_status == status
    assert conn.sql == [lms.PERSISTED_MATCH_SQL]


def test_no_saved_decision_means_no_matching_and_no_fetch():
    conn = FakeConnection(persisted=None)
    decision = lms.LmsAttendanceSource().decide(conn, "L1")
    assert decision.status == lms.NO_SAVED_MATCH and not decision.used
    assert conn.sql == [lms.PERSISTED_MATCH_SQL]


def test_no_saved_decision_leaves_the_lecture_on_its_existing_kbc_answer():
    class NoMatch:
        def decide(self, connection, lecture_id):
            return lms.LmsAttendanceDecision(lms.NO_SAVED_MATCH)
    out = _resolve(service(FakeKbc(), NoMatch()))
    without = _resolve(service(FakeKbc()))
    assert out["attendance_source"] == KBC_ATTENDANCE_SOURCE
    assert out["attendance_snapshot_id"] == without["attendance_snapshot_id"]


def test_missing_lms_sync_tables_mean_unavailable_without_any_attendance_query():
    conn = FakeConnection(lms_sync=False, persisted=persisted_verified("O1"))
    decision = lms.LmsAttendanceSource().decide(conn, "L1")
    assert decision.status == lms.UNAVAILABLE
    assert conn.sql == [lms.PERSISTED_MATCH_SQL, lms.LMS_SYNC_PRESENT_SQL]


def test_the_module_does_no_matching_of_its_own():
    import inspect
    source = inspect.getsource(lms)
    assert "match_lecture" not in source and "lms_sync.live_sessions" not in source


def test_a_failing_foreign_table_rolls_back_only_the_savepoint():
    conn = FakeConnection(persisted=persisted_verified("O1"), fail_on="lms_sync.live_session_learner_attendance a")
    decision = lms.LmsAttendanceSource().decide(conn, "L1")
    assert decision.status == lms.UNAVAILABLE and decision.reason == "RuntimeError"
    assert conn.savepoints == 1 and conn.rolled_back == 1


def test_the_pipeline_needs_no_new_setting():
    every = set().union(*environment.COMPONENT_VARIABLES.values())
    assert not any(name.startswith("ATTENDANCE_LMS") for name in every)
    from app.orchestration.runner import StageRunner
    service = StageRunner(settings=None, persist=False)._resolution_service()
    assert isinstance(service.lms_attendance_source, lms.LmsAttendanceSource)


def test_lms_queries_are_plain_selects_on_the_pipeline_connection():
    for sql in (lms.LMS_ATTENDANCE_SQL, lms.PERSISTED_MATCH_SQL,
                lms.LMS_SYNC_OCCURRENCE_STATUS_SQL, lms.LMS_SYNC_PRESENT_SQL):
        upper = sql.upper()
        assert upper.lstrip().startswith("SELECT")
        for forbidden in ("INSERT", "UPDATE ", "DELETE", "CREATE", "ALTER", "TRUNCATE"):
            assert forbidden not in upper


# -- 10. where the lms_sync foreign tables are read (2026-11-20) ---------------------------
# postgres_fdw keeps its remote transaction open until the LOCAL transaction ends, so a
# foreign read on the scheduler cycle's connection held an LMS session idle in transaction
# for the rest of the cycle; the LMS server's 5-minute timeout killed it and the cycle's
# COMMIT failed. The reads now go to their own short-lived autocommit connection.

class RegisterConnection(FakeConnection):
    """A FakeConnection opened by the register factory: records that it was closed."""
    closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


FOREIGN_READS = [lms.LMS_SYNC_PRESENT_SQL, lms.LMS_SYNC_OCCURRENCE_STATUS_SQL, lms.LMS_ATTENDANCE_SQL]


def _split(fail_on=None):
    caller = FakeConnection(persisted=persisted_verified("O1"))
    reg = RegisterConnection(statuses=[("O1", "completed")],
                             register=[register(1), register(2, "absent")], fail_on=fail_on)
    opened = []
    return caller, reg, opened, lms.LmsAttendanceSource(lambda: opened.append(reg) or reg)


def test_foreign_tables_are_read_on_their_own_connection_never_the_cycles():
    caller, reg, opened, source = _split()
    decision = source.decide(caller, "L1")
    assert decision.used and decision.source_rows_present == 1
    assert caller.sql == [lms.PERSISTED_MATCH_SQL]           # the cycle's transaction: local only
    assert reg.sql == FOREIGN_READS and reg.closed and len(opened) == 1


def test_the_split_decision_equals_the_single_connection_decision():
    caller, _, _, source = _split()
    single = FakeConnection(persisted=persisted_verified("O1"), statuses=[("O1", "completed")],
                            register=[register(1), register(2, "absent")])
    assert source.decide(caller, "L1") == lms.LmsAttendanceSource().decide(single, "L1")


@pytest.mark.parametrize("persisted", [None, [(matching.AMBIGUOUS,) + (None,) * 7]])
def test_no_verified_match_opens_no_register_connection(persisted):
    opened = []
    source = lms.LmsAttendanceSource(lambda: opened.append(1))
    assert not source.decide(FakeConnection(persisted=persisted), "L1").used
    assert opened == []


def test_a_failing_foreign_read_is_unavailable_and_still_closes_its_connection():
    caller, reg, _, source = _split(fail_on="lms_sync.live_session_learner_attendance a")
    decision = source.decide(caller, "L1")
    assert decision.status == lms.UNAVAILABLE and decision.reason == "RuntimeError"
    assert reg.closed and caller.rolled_back == 1


def test_a_register_connection_that_cannot_open_is_unavailable():
    def refuse():
        raise OSError("connection refused")
    decision = lms.LmsAttendanceSource(refuse).decide(
        FakeConnection(persisted=persisted_verified("O1")), "L1")
    assert decision.status == lms.UNAVAILABLE and decision.reason == "OSError"


def test_register_connections_are_autocommit_with_no_prepared_statements(monkeypatch):
    import psycopg
    calls = []
    monkeypatch.setattr(psycopg, "connect", lambda url, **kw: calls.append((url, kw)) or "conn")
    assert lms.register_connection_factory("postgresql://example.invalid/db")() == "conn"
    assert calls == [("postgresql://example.invalid/db",
                      {"autocommit": True, "prepare_threshold": None})]


def test_the_stage_runner_reads_the_register_on_its_own_connection():
    from types import SimpleNamespace
    from app.orchestration.runner import StageRunner
    with_db = StageRunner(settings=SimpleNamespace(database_url="postgresql://example.invalid/db"),
                          persist=False)
    assert with_db._resolution_service().lms_attendance_source.register_connection is not None
    # No settings (the dry-run planner): nothing to connect to; reads stay on the caller's.
    without = StageRunner(settings=None, persist=False)
    assert without._resolution_service().lms_attendance_source.register_connection is None
