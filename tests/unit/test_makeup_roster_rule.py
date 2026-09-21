"""
Makeup roster drift hardening: the versioned v2 roster rule.

Approved KBC rule: a learner marked makeup is not an attendee of the ORIGINAL
lecture for engagement. The only evidence accepted is the source's own
`attendance_status = 'makeup'`. Every name here is an invented fixture value.
"""
import uuid
from datetime import date

import pytest

from app.attendance.resolver import EXACT_MATCH, NO_MATCH, resolve_speaker
from app.attendance.roles import LEARNER, OTHER_OR_UNRESOLVED, TRAINER_CANDIDATE, assign_roles
from app.attendance.roster import (
    ATTENDANCE_RESOLUTION_VERSION,
    ATTENDANCE_ROSTER_V1,
    ATTENDANCE_ROSTER_V2,
    MAKEUP_STATUS,
    build_roster,
    is_makeup,
    roster_fingerprint,
    roster_rule,
)
from app.attendance.service import SpeakerResolutionService
from app.common.hashing import attendance_snapshot_identity
from app.db.repositories.attendance_resolution import LOAD_ATTENDANCE


def row(learner_id, name, *, status=None, email=None, activity=None, created=None):
    return {"learner_id": learner_id, "full_name": name, "email": email,
            "attendance_flag": 1, "module": "m", "attendance_status": status,
            # Present only to prove they are ignored by the rule.
            "activity": activity, "created_at": created}


V2 = {"version": ATTENDANCE_ROSTER_V2}
DAY = date(2026, 9, 4)


def ids(roster):
    return sorted(member["external_person_id"] for member in roster["members"])


# --- 1-2 --------------------------------------------------------------------

def test_explicit_makeup_row_is_excluded_under_v2():
    roster = build_roster([row(1, "Avery Holt"), row(2, "Blake Quinlan", status="makeup")], **V2)
    assert ids(roster) == ["1"]
    assert roster["excluded_makeup_count"] == 1
    assert roster["source_row_count"] == 2
    assert roster["present_row_count"] == 1


def test_normal_attended_row_is_retained_under_v2():
    roster = build_roster([row(1, "Avery Holt"), row(2, "Blake Quinlan", status="original")], **V2)
    assert ids(roster) == ["1", "2"]
    assert roster["excluded_makeup_count"] == 0


def test_absent_status_is_not_treated_as_makeup():
    # 'absent' is a different value in the source's own domain; the Attendance
    # = 1 filter, not this rule, governs presence.
    assert is_makeup({"attendance_status": "absent"}) is False


def test_marker_is_the_exact_source_value():
    assert MAKEUP_STATUS == "makeup"
    assert is_makeup({"attendance_status": "makeup"}) is True
    assert is_makeup({"attendance_status": None}) is False
    assert is_makeup({}) is False


# --- 3-4. no inference from clues --------------------------------------------

def test_zero_activity_without_the_marker_is_not_excluded():
    roster = build_roster([row(1, "Avery Holt", activity=0)], **V2)
    assert ids(roster) == ["1"]


def test_late_created_normal_row_is_not_excluded():
    roster = build_roster([row(1, "Avery Holt", created="2026-09-11T15:00:00Z")], **V2)
    assert ids(roster) == ["1"]


def test_rule_consults_only_attendance_status():
    import inspect
    from app.attendance import roster as module
    source = inspect.getsource(module.is_makeup)
    for clue in ("activity", "created_at", "resolved", "date", "speaker"):
        assert clue not in source.split('"""')[-1], clue


# --- 5-7. learner ids -------------------------------------------------------

def test_repeated_learner_id_across_different_dates_is_valid():
    # Each snapshot is built from one date; the same learner simply appears in
    # both, with independent snapshot identities.
    first = build_roster([row(7, "Avery Holt")], **V2)
    second = build_roster([row(7, "Avery Holt")], **V2)
    one = roster_fingerprint(session_date=date(2026, 9, 4), module_normalized="m",
                             members=first["members"], resolution_version=ATTENDANCE_ROSTER_V2)
    two = roster_fingerprint(session_date=date(2026, 9, 11), module_normalized="m",
                             members=second["members"], resolution_version=ATTENDANCE_ROSTER_V2)
    assert ids(first) == ids(second) == ["7"]
    assert one != two


def test_repeated_learner_id_across_different_lectures_is_valid():
    one = roster_fingerprint(session_date=DAY, module_normalized="module a",
                             members=build_roster([row(7, "Avery Holt")], **V2)["members"],
                             resolution_version=ATTENDANCE_ROSTER_V2)
    two = roster_fingerprint(session_date=DAY, module_normalized="module b",
                             members=build_roster([row(7, "Avery Holt")], **V2)["members"],
                             resolution_version=ATTENDANCE_ROSTER_V2)
    assert one != two


def test_same_learner_duplicate_inside_one_snapshot_counts_once():
    roster = build_roster([row(7, "Avery Holt", email="a@x.invalid"),
                           row(7, "avery holt", email="A@x.invalid")], **V2)
    assert roster["effective_member_count"] == 1
    assert roster["deduplicated_count"] == 1
    assert roster["duplicate_learner_id_count"] == 0


def test_same_learner_id_under_two_names_is_reported_not_silently_merged():
    roster = build_roster([row(7, "Avery Holt"), row(7, "Avery Holt-Brown")], **V2)
    assert roster["effective_member_count"] == 2
    assert roster["duplicate_learner_id_count"] == 1


def test_makeup_duplicate_of_an_original_row_leaves_the_original():
    roster = build_roster([row(7, "Avery Holt", status="makeup"), row(7, "Avery Holt")], **V2)
    assert ids(roster) == ["7"]
    assert roster["excluded_makeup_count"] == 1


# --- 8-10. versioned provenance ---------------------------------------------

SOURCE = [row(1, "Avery Holt"), row(2, "Blake Quinlan", status="makeup"),
          row(3, "Casey Ormond")]


def test_old_roster_version_is_still_reproducible_and_ignores_the_marker():
    roster = build_roster(SOURCE, version=ATTENDANCE_ROSTER_V1)
    assert ids(roster) == ["1", "2", "3"]
    assert roster["excluded_makeup_count"] == 0
    assert build_roster(SOURCE) == roster  # v1 is the builder's historical default


def test_new_roster_version_creates_distinct_snapshot_provenance():
    lecture = uuid.uuid4()

    def snapshot(version):
        members = build_roster(SOURCE, version=version)["members"]
        fingerprint = roster_fingerprint(session_date=DAY, module_normalized="m",
                                         members=members, resolution_version=version)
        return fingerprint, attendance_snapshot_identity(
            lecture_id=lecture, attendance_resolution_version=version,
            source_fingerprint=fingerprint)

    old_fp, old_id = snapshot(ATTENDANCE_ROSTER_V1)
    new_fp, new_id = snapshot(ATTENDANCE_ROSTER_V2)
    assert old_fp != new_fp
    assert old_id != new_id


def test_version_alone_changes_provenance_for_identical_members():
    members = build_roster([row(1, "Avery Holt")], **V2)["members"]
    v1 = roster_fingerprint(session_date=DAY, module_normalized="m", members=members,
                            resolution_version=ATTENDANCE_ROSTER_V1)
    v2 = roster_fingerprint(session_date=DAY, module_normalized="m", members=members,
                            resolution_version=ATTENDANCE_ROSTER_V2)
    assert v1 != v2


def test_new_snapshot_fingerprint_is_deterministic_and_order_independent():
    forward = build_roster(SOURCE, **V2)["members"]
    backward = build_roster(list(reversed(SOURCE)), **V2)["members"]
    args = {"session_date": DAY, "module_normalized": "m",
            "resolution_version": ATTENDANCE_ROSTER_V2}
    assert roster_fingerprint(members=forward, **args) == roster_fingerprint(members=backward, **args)


def test_new_runs_default_to_the_v2_rule():
    assert ATTENDANCE_RESOLUTION_VERSION == ATTENDANCE_ROSTER_V2
    assert roster_rule(ATTENDANCE_ROSTER_V2) == {"exclude_makeup": True}
    assert roster_rule(ATTENDANCE_ROSTER_V1) == {"exclude_makeup": False}


def test_unknown_roster_version_is_refused():
    with pytest.raises(ValueError):
        roster_rule("attendance_roster_guess")
    with pytest.raises(ValueError):
        build_roster(SOURCE, version="attendance_roster_guess")
    with pytest.raises(ValueError):
        SpeakerResolutionService(
            inventory_repository=None, attendance_repository=None, snapshot_repository=None,
            identity_repository=None, role_repository=None, run_repository=None,
            attendance_resolution_version="attendance_roster_guess")


def test_source_query_selects_the_marker_but_does_not_filter_on_it():
    sql = " ".join(LOAD_ATTENDANCE.split())
    assert "a.attendance_status" in sql.split("FROM")[0]
    assert "attendance_status" not in sql.split("WHERE")[1]


# --- 11-13. matching and roles are unchanged ---------------------------------

def test_identity_resolution_is_unchanged_by_the_roster_rule():
    members = build_roster([row(1, "Avery Holt")], **V2)["members"]
    for member in members:
        member["member_id"] = uuid.uuid4()
    first = resolve_speaker("Avery Holt", members)
    second = resolve_speaker("Avery Holt", members)
    assert first["resolution_status"] == second["resolution_status"] == EXACT_MATCH
    assert first["matched_member"] is second["matched_member"]


def test_makeup_learner_who_speaks_is_unresolved_under_v2_not_invented():
    members = build_roster([row(2, "Blake Quinlan", status="makeup")], **V2)["members"]
    assert resolve_speaker("Blake Quinlan", members)["resolution_status"] != EXACT_MATCH


def _speaker(label, gross, first):
    return {"speaker_id": uuid.uuid4(), "speaker_label_raw": label,
            "gross_spoken_ms": gross, "first_cue_index": first}


def test_trainer_rule_and_learner_rule_are_unchanged():
    trainer = _speaker("Morgan Trainerfield", 900_000, 1)
    learner = _speaker("Avery Holt", 10_000, 2)
    guest = _speaker("Unlisted Guest", 5_000, 3)
    roles = {row["speaker_id"]: row["role"] for row in assign_roles(
        [guest, learner, trainer],
        {learner["speaker_id"]: {"resolution_status": EXACT_MATCH},
         guest["speaker_id"]: {"resolution_status": NO_MATCH}})}
    assert roles == {trainer["speaker_id"]: TRAINER_CANDIDATE,
                     learner["speaker_id"]: LEARNER,
                     guest["speaker_id"]: OTHER_OR_UNRESOLVED}
