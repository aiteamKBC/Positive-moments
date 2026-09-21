"""
Phase 2C3 unit tests: legacy comparator parity, roster provenance, the safe
resolver and deterministic roles.

Pure functions only - no database, no Graph, no AI. Every personal name used
here is invented for the fixture.
"""
import uuid
from datetime import date

import pytest

from app.attendance.legacy_matching import (
    is_similar,
    legacy_normalize,
    legacy_tokens,
    levenshtein,
    levenshtein_similarity,
    token_subset_match,
)
from app.attendance.resolver import (
    AMBIGUOUS,
    EXACT_MATCH,
    LEGACY_FUZZY_MATCH,
    NORMALIZED_EXACT_MATCH,
    NO_MATCH,
    NOT_APPLICABLE,
    legacy_would_match,
    resolve_speaker,
)
from app.attendance.roles import (
    LEARNER,
    OTHER_OR_UNRESOLVED,
    TRAINER_CANDIDATE,
    assign_roles,
    rank_speakers,
)
from app.attendance.roster import (
    BOT_PATTERNS,
    build_roster,
    dedup_key,
    is_bot,
    roster_fingerprint,
)
from app.common.hashing import (
    attendance_snapshot_identity,
    speaker_resolution_identity,
    speaker_role_identity,
)
from app.db.repositories.attendance_resolution import LOAD_ATTENDANCE


def member(name, *, email=None, person_id=None):
    from app.attendance.roster import email_fingerprint, normalize_person_name
    return {
        "dedup_key": dedup_key(name, email),
        "external_person_id": person_id,
        "display_name_raw": name,
        "display_name_normalized": normalize_person_name(name),
        "email_sha256": email_fingerprint(email),
        "attendance_flag": 1,
        "member_id": uuid.uuid4(),
    }


def source_row(row_id, name, email=None):
    return {"learner_id": row_id, "full_name": name, "email": email,
            "attendance_flag": 1, "module": "m"}


# --- 1-3. the attendance query contract ------------------------------------

def test_attendance_query_filters_on_exact_date_normalized_module_and_attendance_one():
    sql = " ".join(LOAD_ATTENDANCE.split())
    assert "public.kbc_attendance" in sql
    assert "a.date = %s" in sql
    assert "regexp_replace(lower(a.module)" in sql
    assert '"Attendance" = 1' in sql


def test_attendance_query_is_a_plain_select_only():
    upper = LOAD_ATTENDANCE.upper()
    for forbidden in ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "TRIGGER"):
        assert forbidden not in upper


def test_attendance_query_does_not_touch_other_external_tables():
    assert "kbc_users_data" not in LOAD_ATTENDANCE
    assert "aptem_auto_extracting" not in LOAD_ATTENDANCE


# --- 4. bot / notetaker exclusion -------------------------------------------

@pytest.mark.parametrize("pattern", BOT_PATTERNS)
def test_every_legacy_bot_pattern_is_excluded_by_name(pattern):
    assert is_bot(f"Meeting {pattern} service", None) is True


def test_bot_pattern_also_matches_on_the_email():
    assert is_bot("Perfectly Normal Name", "fireflies@example.com") is True


def test_non_bot_attendee_is_kept():
    assert is_bot("Dana Whitfield", "dana@example.com") is False


def test_bot_rows_are_dropped_and_counted():
    roster = build_roster([
        source_row(1, "Dana Whitfield", "dana@example.com"),
        source_row(2, "Otter.ai Notetaker", "bot@otter.ai"),
    ])
    assert roster["excluded_bot_count"] == 1
    assert roster["effective_member_count"] == 1


# --- 5. deduplication -------------------------------------------------------

def test_dedup_is_by_name_and_email_not_name_alone():
    roster = build_roster([
        source_row(1, "Dana Whitfield", "dana.1@example.com"),
        source_row(2, "Dana Whitfield", "dana.2@example.com"),
    ])
    # Same name, different emails: two distinct people, never merged.
    assert roster["effective_member_count"] == 2
    assert roster["deduplicated_count"] == 0


def test_identical_name_and_email_collapse_first_occurrence_wins():
    roster = build_roster([
        source_row(1, "Dana Whitfield", "dana@example.com"),
        source_row(2, "dana whitfield", "DANA@example.com"),
    ])
    assert roster["effective_member_count"] == 1
    assert roster["deduplicated_count"] == 1
    assert roster["members"][0]["external_person_id"] == "1"


def test_missing_email_becomes_the_empty_string_deterministically():
    assert dedup_key("Dana Whitfield", None) == "dana whitfield|"
    assert dedup_key("Dana Whitfield", "  ") == "dana whitfield|"
    roster = build_roster([
        source_row(1, "Dana Whitfield", None),
        source_row(2, "Dana Whitfield", ""),
    ])
    assert roster["effective_member_count"] == 1


def test_blank_names_are_dropped_before_the_bot_filter():
    roster = build_roster([source_row(1, "   ", "x@example.com"),
                           source_row(2, "Dana Whitfield", None)])
    assert roster["excluded_no_name_count"] == 1
    assert roster["present_row_count"] == 1
    assert roster["effective_member_count"] == 1


# --- 6-8. fingerprint and snapshot provenance -------------------------------

def test_fingerprint_is_independent_of_row_order():
    members = [member("Dana Whitfield", person_id="1"),
               member("Elias Brandt", person_id="2")]
    forward = roster_fingerprint(session_date=date(2026, 9, 4),
                                 module_normalized="m", members=members)
    backward = roster_fingerprint(session_date=date(2026, 9, 4),
                                  module_normalized="m", members=list(reversed(members)))
    assert forward == backward
    assert len(forward) == 64


def test_fingerprint_changes_when_a_member_changes():
    base = [member("Dana Whitfield", person_id="1")]
    changed = [member("Dana Whitfield-Cole", person_id="1")]
    added = base + [member("Elias Brandt", person_id="2")]
    args = {"session_date": date(2026, 9, 4), "module_normalized": "m"}
    first = roster_fingerprint(members=base, **args)
    assert roster_fingerprint(members=changed, **args) != first
    assert roster_fingerprint(members=added, **args) != first


def test_fingerprint_changes_when_only_the_email_changes():
    args = {"session_date": date(2026, 9, 4), "module_normalized": "m"}
    one = roster_fingerprint(members=[member("Dana Whitfield", email="a@x.com")], **args)
    two = roster_fingerprint(members=[member("Dana Whitfield", email="b@x.com")], **args)
    assert one != two


def test_same_roster_yields_the_same_snapshot_identity():
    args = {"session_date": date(2026, 9, 4), "module_normalized": "m"}
    members = [member("Dana Whitfield", person_id="1")]
    fingerprint = roster_fingerprint(members=members, **args)
    lecture = uuid.uuid4()
    first = attendance_snapshot_identity(
        lecture_id=lecture, attendance_resolution_version="v1",
        source_fingerprint=fingerprint)
    second = attendance_snapshot_identity(
        lecture_id=lecture, attendance_resolution_version="v1",
        source_fingerprint=fingerprint)
    assert first == second


def test_changed_roster_yields_a_new_snapshot_identity():
    args = {"session_date": date(2026, 9, 4), "module_normalized": "m"}
    lecture = uuid.uuid4()
    before = attendance_snapshot_identity(
        lecture_id=lecture, attendance_resolution_version="v1",
        source_fingerprint=roster_fingerprint(members=[member("Dana Whitfield")], **args))
    after = attendance_snapshot_identity(
        lecture_id=lecture, attendance_resolution_version="v1",
        source_fingerprint=roster_fingerprint(
            members=[member("Dana Whitfield"), member("Elias Brandt")], **args))
    assert before != after


def test_persisted_member_never_carries_the_plain_email():
    """The dedup key is stored on every member row, so it must not hold the address."""
    roster = build_roster([source_row(1, "Dana Whitfield", "dana@example.com")])
    stored = roster["members"][0]
    assert "dana@example.com" not in repr(stored)
    assert "@" not in stored["dedup_key"]
    assert len(stored["email_sha256"]) == 64


def test_hashed_dedup_key_preserves_legacy_equality_semantics():
    same = dedup_key("Dana Whitfield", "DANA@example.com")
    assert same == dedup_key("dana whitfield ", " dana@example.com")
    assert same != dedup_key("Dana Whitfield", "other@example.com")
    assert same != dedup_key("Dana Whitfield", None)


# --- 9-10. exact and normalized-exact matching ------------------------------

def test_exact_raw_label_equality_wins():
    members = [member("Dana Whitfield", person_id="7")]
    outcome = resolve_speaker("Dana Whitfield", members)
    assert outcome["resolution_status"] == EXACT_MATCH
    assert outcome["matched_member"]["external_person_id"] == "7"
    assert outcome["match_score"] == 1.0


def test_normalized_exact_match_handles_case_and_spacing():
    members = [member("Dana  WHITFIELD ")]
    outcome = resolve_speaker("dana whitfield", members)
    assert outcome["resolution_status"] == NORMALIZED_EXACT_MATCH


def test_exact_match_is_preferred_over_an_available_fuzzy_candidate():
    members = [member("Danny Whitfield", person_id="fuzzy"),
               member("Dana Whitfield", person_id="exact")]
    outcome = resolve_speaker("Dana Whitfield", members)
    assert outcome["resolution_status"] == EXACT_MATCH
    assert outcome["matched_member"]["external_person_id"] == "exact"
    assert outcome["candidate_count"] == 1


# --- 11-13. the legacy comparator -------------------------------------------

def test_legacy_normalize_strips_everything_outside_a_to_z():
    assert legacy_normalize("Zoë Dodd-Smith 3") == "zododdsmith"


def test_legacy_tokens_drop_lone_initials():
    assert legacy_tokens("Dana J Whitfield") == ["dana", "whitfield"]


def test_levenshtein_matches_known_distances():
    assert levenshtein("kitten", "sitting") == 3
    assert levenshtein("", "abc") == 3
    assert levenshtein("same", "same") == 0


def test_levenshtein_similarity_accepts_a_minor_spelling_difference():
    # "Katie Hewitt-Brake" vs "Katie Hewitt Brake": identical once normalized.
    assert levenshtein_similarity("Katie Hewitt-Brake", "Katie Hewitt Brake") == 1.0
    assert is_similar("Katie Hewitt-Brake", "Katie Hewitt Brake") is True


def test_token_prefix_match_handles_shortened_first_names():
    assert token_subset_match("Chris Earle", "Christopher Earle") is True
    assert is_similar("Ali", "Ali Mohamedin") is True


def test_token_match_rejects_a_different_surname():
    assert is_similar("John Doe", "John Smith") is False


def test_prefix_rule_requires_three_characters_on_both_sides():
    # "Jo" is only two characters, so the prefix rule must not fire, and the
    # token is dropped by neither side's minimum-length rule by accident.
    assert token_subset_match("Jo", "Joanne") is False


def test_similarity_below_the_threshold_is_rejected():
    assert levenshtein_similarity("Dana Whitfield", "Marcus Ellery") < 0.8
    assert is_similar("Dana Whitfield", "Marcus Ellery") is False


def test_empty_names_never_match():
    assert is_similar("", "Dana Whitfield") is False
    assert is_similar("123", "456") is False


def test_fuzzy_runs_only_as_a_fallback():
    members = [member("Christopher Earle", person_id="9")]
    outcome = resolve_speaker("Chris Earle", members)
    assert outcome["resolution_status"] == LEGACY_FUZZY_MATCH
    assert outcome["match_method"] == "legacy_is_similar_v7"


# --- 14-16. the ambiguity guard and no-match --------------------------------

def test_multiple_fuzzy_candidates_produce_ambiguous_and_no_person():
    members = [member("Ali Mohamedin", person_id="a"),
               member("Ali Rahman", person_id="b")]
    outcome = resolve_speaker("Ali", members)
    assert outcome["resolution_status"] == AMBIGUOUS
    assert outcome["matched_member"] is None
    assert outcome["candidate_count"] == 2


def test_ambiguity_is_not_resolved_by_input_order():
    left = member("Ali Mohamedin", person_id="a")
    right = member("Ali Rahman", person_id="b")
    forward = resolve_speaker("Ali", [left, right])
    backward = resolve_speaker("Ali", [right, left])
    assert forward["resolution_status"] == backward["resolution_status"] == AMBIGUOUS
    assert forward["candidate_person_ids"] == backward["candidate_person_ids"]


def test_duplicate_exact_names_are_ambiguous_rather_than_first_row():
    members = [member("Dana Whitfield", email="a@x.com", person_id="1"),
               member("Dana Whitfield", email="b@x.com", person_id="2")]
    outcome = resolve_speaker("Dana Whitfield", members)
    assert outcome["resolution_status"] == AMBIGUOUS
    assert outcome["matched_member"] is None


def test_no_candidate_produces_no_match():
    outcome = resolve_speaker("Marcus Ellery", [member("Dana Whitfield")])
    assert outcome["resolution_status"] == NO_MATCH
    assert outcome["candidate_count"] == 0


def test_empty_roster_is_not_applicable_rather_than_no_match():
    outcome = resolve_speaker("Dana Whitfield", [])
    assert outcome["resolution_status"] == NOT_APPLICABLE


def test_legacy_boolean_probe_would_have_matched_where_the_guard_refuses():
    members = [member("Ali Mohamedin", person_id="a"),
               member("Ali Rahman", person_id="b")]
    assert legacy_would_match("Ali", members) is True
    assert resolve_speaker("Ali", members)["resolution_status"] == AMBIGUOUS


# --- 17-18. person identity scope -------------------------------------------

def test_stable_external_person_id_is_preserved_not_invented():
    outcome = resolve_speaker("Dana Whitfield", [member("Dana Whitfield", person_id="4211")])
    assert outcome["matched_member"]["external_person_id"] == "4211"


def test_missing_external_id_is_left_null_and_never_derived_from_the_name():
    outcome = resolve_speaker("Dana Whitfield", [member("Dana Whitfield", person_id=None)])
    assert outcome["matched_member"]["external_person_id"] is None


def test_resolution_identity_is_scoped_to_speaker_snapshot_and_resolver():
    speaker, snapshot = uuid.uuid4(), uuid.uuid4()
    base = speaker_resolution_identity(
        speaker_id=speaker, attendance_snapshot_id=snapshot, resolver_version="v1")
    assert base == speaker_resolution_identity(
        speaker_id=speaker, attendance_snapshot_id=snapshot, resolver_version="v1")
    assert base != speaker_resolution_identity(
        speaker_id=speaker, attendance_snapshot_id=snapshot, resolver_version="v2")
    assert base != speaker_resolution_identity(
        speaker_id=speaker, attendance_snapshot_id=uuid.uuid4(), resolver_version="v1")
    # Two different speakers are never merged into one person row.
    assert base != speaker_resolution_identity(
        speaker_id=uuid.uuid4(), attendance_snapshot_id=snapshot, resolver_version="v1")


# --- 19-23. deterministic roles ---------------------------------------------

def speaker(label, gross, first_cue=1, speaker_id=None):
    return {"speaker_id": speaker_id or uuid.uuid4(), "speaker_label_raw": label,
            "gross_spoken_ms": gross, "first_cue_index": first_cue}


def test_top_gross_spoken_speaker_is_the_trainer_candidate():
    top = speaker("Trainer One", 900_000)
    roles = assign_roles([speaker("Learner", 10_000), top], {})
    trainer = next(role for role in roles if role["role"] == TRAINER_CANDIDATE)
    assert trainer["speaker_id"] == top["speaker_id"]
    assert trainer["role_source"] == "VTT_TOP_SPEAKER"
    assert trainer["role_rank"] == 1


def test_trainer_tie_is_broken_by_first_appearance_then_label():
    early = speaker("Zeta", 500, first_cue=2)
    late = speaker("Alpha", 500, first_cue=9)
    assert rank_speakers([late, early])[0]["speaker_id"] == early["speaker_id"]
    # Same speech time AND same first cue: the raw label decides, never row order.
    same_a = speaker("Alpha", 500, first_cue=2)
    same_z = speaker("Zeta", 500, first_cue=2)
    assert rank_speakers([same_z, same_a])[0]["speaker_label_raw"] == "Alpha"
    assert rank_speakers([same_a, same_z])[0]["speaker_label_raw"] == "Alpha"


def test_matched_non_trainer_becomes_a_learner():
    trainer = speaker("Trainer", 900_000)
    learner = speaker("Learner", 1_000)
    roles = assign_roles([trainer, learner],
                         {learner["speaker_id"]: {"resolution_status": EXACT_MATCH}})
    assert next(r["role"] for r in roles
                if r["speaker_id"] == learner["speaker_id"]) == LEARNER


def test_unmatched_non_trainer_is_other_or_unresolved():
    trainer = speaker("Trainer", 900_000)
    guest = speaker("Guest", 1_000)
    roles = assign_roles([trainer, guest],
                         {guest["speaker_id"]: {"resolution_status": NO_MATCH}})
    assert next(r["role"] for r in roles
                if r["speaker_id"] == guest["speaker_id"]) == OTHER_OR_UNRESOLVED


def test_ambiguous_speaker_is_never_promoted_to_learner():
    trainer = speaker("Trainer", 900_000)
    unclear = speaker("Ali", 1_000)
    roles = assign_roles([trainer, unclear],
                         {unclear["speaker_id"]: {"resolution_status": AMBIGUOUS}})
    assert next(r["role"] for r in roles
                if r["speaker_id"] == unclear["speaker_id"]) == OTHER_OR_UNRESOLVED


def test_trainer_matching_attendance_keeps_the_trainer_role_and_records_both_facts():
    trainer = speaker("Trainer", 900_000)
    roles = assign_roles([trainer, speaker("Learner", 10)],
                         {trainer["speaker_id"]: {"resolution_status": EXACT_MATCH}})
    row = next(role for role in roles if role["speaker_id"] == trainer["speaker_id"])
    assert row["role"] == TRAINER_CANDIDATE
    assert row["trainer_also_matched_attendance"] is True
    assert row["person_resolution_status"] == EXACT_MATCH


# --- 24-26. provenance independence and version coexistence -----------------

def test_role_identity_is_independent_of_the_role_algorithm_and_the_resolver():
    speaker_id, snapshot = uuid.uuid4(), uuid.uuid4()
    base = speaker_role_identity(speaker_id=speaker_id, role_algorithm_version="r1",
                                 resolver_version="p1", attendance_snapshot_id=snapshot)
    # A new role algorithm coexists with the old one.
    assert base != speaker_role_identity(
        speaker_id=speaker_id, role_algorithm_version="r2",
        resolver_version="p1", attendance_snapshot_id=snapshot)
    # A new resolver cannot silently overwrite stored role evidence either.
    assert base != speaker_role_identity(
        speaker_id=speaker_id, role_algorithm_version="r1",
        resolver_version="p2", attendance_snapshot_id=snapshot)


def test_role_and_identity_use_different_namespaces():
    speaker_id, snapshot = uuid.uuid4(), uuid.uuid4()
    assert speaker_role_identity(
        speaker_id=speaker_id, role_algorithm_version="v",
        resolver_version="v", attendance_snapshot_id=snapshot) != speaker_resolution_identity(
        speaker_id=speaker_id, attendance_snapshot_id=snapshot, resolver_version="v")


def test_changing_the_resolver_does_not_change_the_trainer_candidate():
    trainer = speaker("Trainer", 900_000)
    other = speaker("Other", 10)
    lenient = assign_roles([trainer, other], {other["speaker_id"]:
                                              {"resolution_status": EXACT_MATCH}})
    strict = assign_roles([trainer, other], {other["speaker_id"]:
                                             {"resolution_status": NO_MATCH}})
    assert (next(r["speaker_id"] for r in lenient if r["role"] == TRAINER_CANDIDATE)
            == next(r["speaker_id"] for r in strict if r["role"] == TRAINER_CANDIDATE))


# --- 32. no engagement anywhere in this phase -------------------------------

# Phase 3C3E added two ORCHESTRATION modules to this package: attendance
# recovery, which calls the Phase 2C4 service when new attendance arrives, and
# the coverage backfill, which stamps a derived status onto engagement rows.
# Both necessarily name engagement. Neither computes any of it - which is the
# invariant this test exists to protect, and which the companion test below
# now checks directly instead of by the absence of a word.
ORCHESTRATION_MODULES = {"recovery.py", "coverage_backfill.py"}


def test_no_engagement_is_computed_anywhere_in_the_phase_2c3_package():
    """
    Scans identifiers only, not prose: the modules are allowed to DESCRIBE the
    deferred engagement behaviour, but must not implement any of it.
    """
    import ast
    import pathlib
    package = pathlib.Path(__file__).resolve().parents[2] / "app" / "attendance"
    banned = ("engagement", "spoke", "silent", "item7", "checklist", "percent", "score")
    allowed = {"match_score", "MATCH_SCORE_BY_STATUS", "gross_spoken_ms", "match_score"}
    offenders = []
    for path in sorted(package.glob("*.py")):
        if path.name in ORCHESTRATION_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, ast.keyword) and node.arg:
                names = [node.arg]
            for name in names:
                if name in allowed:
                    continue
                if any(token in name.lower() for token in banned):
                    offenders.append(f"{path.name}:{name}")
    assert offenders == [], offenders


def test_the_orchestration_modules_delegate_engagement_rather_than_reimplement_it():
    """
    The real invariant, checked directly on the two modules excluded above.

    They may CALL the Phase 2C4 service and read what it produced. They may not
    contain any of its arithmetic - no percentage, no score bands, no Item 7
    thresholds - because a second implementation of engagement is exactly the
    thing versioned provenance cannot survive.
    """
    import ast
    import pathlib
    package = pathlib.Path(__file__).resolve().parents[2] / "app" / "attendance"
    # Identifiers that would mean the maths had been copied in.
    reimplementation = ("engagement_percentage", "engagement_score",
                        "learner_engagement_status", "spoke_count", "silent_count",
                        "trainer_attendance_matches", "item7_status")
    for name in sorted(ORCHESTRATION_MODULES):
        path = package / name
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = {node.name for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
        assert not defined & set(reimplementation), (name, defined)
        # No arithmetic on engagement at all: the numbers arrive already made.
        assert "Decimal" not in path.read_text(encoding="utf-8"), name
