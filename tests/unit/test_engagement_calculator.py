"""
Phase 2C4 unit tests: pure deterministic engagement.

Rounding reference values were produced by Node.js with the exact legacy
expression `Number(((s / a) * 100).toFixed(2))` and are pinned here as data,
so the suite never needs a JavaScript runtime. Every name is an invented
fixture value.
"""
import uuid
from decimal import Decimal

import pytest

from app.attendance.resolver import (
    AMBIGUOUS,
    EXACT_MATCH,
    LEGACY_FUZZY_MATCH,
    NO_MATCH,
    NORMALIZED_EXACT_MATCH,
)
from app.attendance.roles import LEARNER, OTHER_OR_UNRESOLVED, TRAINER_CANDIDATE
from app.common.hashing import engagement_identity, engagement_participant_identity
from app.engagement.calculator import (
    CALCULATED,
    CALCULATED_WITH_AMBIGUITY,
    EXCLUDED_TRAINER,
    MET,
    NO_ATTENDED_LEARNERS,
    NO_TRAINER_CANDIDATE,
    NOT_MET,
    PARTIALLY_MET,
    REVIEW_AMBIGUITY_MAY_CHANGE_RESULT,
    SILENT,
    SPOKE,
    TRAINER_ATTENDANCE_AMBIGUOUS,
    TRAINER_EXCLUDED,
    TRAINER_NOT_IN_ATTENDANCE,
    UNDETERMINED,
    calculate,
    engagement_fingerprint,
    engagement_percentage,
    engagement_score,
    js_to_fixed_2,
    learner_engagement_status,
)


def member(name, person_id=None):
    return {"member_id": uuid.uuid4(), "external_person_id": person_id or str(uuid.uuid4()),
            "display_name_raw": name}


def trainer(label="Morgan Trainerfield"):
    return {"speaker_id": uuid.uuid4(), "speaker_label_raw": label,
            "role": TRAINER_CANDIDATE, "role_rank": 1,
            "resolution_status": NO_MATCH, "matched_member_id": None,
            "candidate_person_ids": []}


def speaker(target=None, *, role=LEARNER, status=EXACT_MATCH, rank=2, candidates=None):
    return {"speaker_id": uuid.uuid4(), "speaker_label_raw": "irrelevant label",
            "role": role, "role_rank": rank, "resolution_status": status,
            "matched_member_id": target["member_id"] if target else None,
            "candidate_person_ids": candidates or []}


def roster(count):
    names = ["Avery Holt", "Blake Quinlan", "Casey Ormond", "Devon Pryce", "Emery Stroud",
             "Finley Vance", "Gray Whitcombe", "Harper Yale", "Indy Zamora", "Jules Abernethy"]
    return [member(names[index]) for index in range(count)]


def by_member(result):
    return {row["member_id"]: row for row in result["participants"]}


# --- 1. denominator comes from snapshot members -----------------------------

def test_attended_count_is_the_snapshot_membership():
    members = roster(5)
    result = calculate(members=members, speakers=[trainer()])
    assert result["attendance_before_trainer_exclusion"] == 5
    assert result["attended_count"] == 5


# --- 2-4. trainer exclusion -------------------------------------------------

def test_single_attendance_match_for_the_trainer_is_excluded():
    members = roster(3) + [member("Morgan Trainerfield")]
    result = calculate(members=members, speakers=[trainer()])
    assert result["trainer_exclusion_status"] == TRAINER_EXCLUDED
    assert result["trainer_excluded_count"] == 1
    assert result["attended_count"] == 3
    assert by_member(result)[members[-1]["member_id"]]["participation_status"] == EXCLUDED_TRAINER


def test_trainer_exclusion_uses_the_legacy_fuzzy_comparator():
    # "Morgan Trainerfield" vs "Morgan Trainerfeld": Levenshtein >= 0.8.
    members = roster(2) + [member("Morgan Trainerfeld")]
    result = calculate(members=members, speakers=[trainer()])
    assert result["trainer_exclusion_status"] == TRAINER_EXCLUDED
    assert result["attended_count"] == 2


def test_no_trainer_match_excludes_nobody():
    members = roster(4)
    result = calculate(members=members, speakers=[trainer()])
    assert result["trainer_exclusion_status"] == TRAINER_NOT_IN_ATTENDANCE
    assert result["trainer_excluded_count"] == 0
    assert result["attended_count"] == 4


def test_ambiguous_trainer_match_refuses_to_calculate():
    members = roster(3) + [member("Morgan Trainerfield"), member("Morgan Trainerfield-Lee")]
    result = calculate(members=members, speakers=[trainer("Morgan")])
    assert result["trainer_exclusion_status"] == TRAINER_ATTENDANCE_AMBIGUOUS
    assert result["calculation_status"] == TRAINER_ATTENDANCE_AMBIGUOUS
    assert result["trainer_attendance_match_count"] == 2
    assert result["trainer_excluded_count"] == 0
    assert result["attended_count"] is None
    assert result["engagement_percentage"] is None
    assert result["engagement_score"] is None
    assert result["learner_engagement_status"] is None
    assert result["item7_override_applied"] is False
    # Legacy would have removed both; kept only as a labelled diagnostic.
    assert result["legacy_compatible_attended_count"] == 3
    statuses = [row["participation_status"] for row in result["participants"]]
    assert statuses.count(UNDETERMINED) == 2


def test_missing_trainer_candidate_excludes_nobody():
    members = roster(2)
    result = calculate(members=members, speakers=[speaker(members[0], rank=1)])
    assert result["trainer_exclusion_status"] == NO_TRAINER_CANDIDATE
    assert result["attended_count"] == 2


# --- 5-11. who counts as having spoken --------------------------------------

def test_one_learner_spoke():
    members = roster(4)
    result = calculate(members=members, speakers=[trainer(), speaker(members[0])])
    assert result["spoke_count"] == 1
    assert result["engagement_percentage"] == Decimal("25.00")


def test_silent_learner_is_counted_in_the_denominator_only():
    members = roster(2)
    result = calculate(members=members, speakers=[trainer(), speaker(members[0])])
    assert result["silent_count"] == 1
    assert by_member(result)[members[1]["member_id"]]["participation_status"] == SILENT


def test_duplicate_speaker_aliases_count_one_attendance_person_once():
    members = roster(2)
    speakers = [trainer(), speaker(members[0], rank=2), speaker(members[0], rank=3)]
    result = calculate(members=members, speakers=speakers)
    assert result["resolved_learner_speaker_count"] == 2
    assert result["spoke_count"] == 1
    assert result["duplicate_speaker_aliases"] == 1
    assert result["engagement_percentage"] == Decimal("50.00")
    row = by_member(result)[members[0]["member_id"]]
    assert row["matched_speaker_count"] == 2
    assert row["first_speaker_id"] == speakers[1]["speaker_id"]


def test_no_match_speaker_does_not_count():
    members = roster(2)
    result = calculate(members=members, speakers=[
        trainer(), speaker(None, role=OTHER_OR_UNRESOLVED, status=NO_MATCH)])
    assert result["spoke_count"] == 0
    assert result["unresolved_speaker_count"] == 1


def test_ambiguous_speaker_does_not_count():
    members = roster(2)
    result = calculate(members=members, speakers=[
        trainer(), speaker(None, role=OTHER_OR_UNRESOLVED, status=AMBIGUOUS)])
    assert result["spoke_count"] == 0
    assert result["ambiguous_speaker_count"] == 1
    assert result["unresolved_speaker_count"] == 0


def test_other_or_unresolved_role_never_counts_even_with_a_member_reference():
    members = roster(2)
    rogue = speaker(members[0], role=OTHER_OR_UNRESOLVED, status=EXACT_MATCH)
    result = calculate(members=members, speakers=[trainer(), rogue])
    assert result["spoke_count"] == 0


@pytest.mark.parametrize("status", [EXACT_MATCH, NORMALIZED_EXACT_MATCH, LEGACY_FUZZY_MATCH])
def test_learner_with_a_safe_match_counts(status):
    members = roster(1)
    result = calculate(members=members, speakers=[trainer(), speaker(members[0], status=status)])
    assert result["spoke_count"] == 1


def test_trainer_never_counts_as_a_learner_even_when_resolved():
    members = roster(2)
    lead = trainer()
    lead.update(resolution_status=EXACT_MATCH, matched_member_id=members[0]["member_id"])
    result = calculate(members=members, speakers=[lead])
    assert result["spoke_count"] == 0


def test_learner_resolved_to_an_excluded_trainer_member_does_not_count():
    members = roster(2) + [member("Morgan Trainerfield")]
    result = calculate(members=members, speakers=[trainer(), speaker(members[-1])])
    assert result["spoke_count"] == 0
    assert result["learner_speakers_on_excluded_member"] == 1


def test_member_reference_outside_this_snapshot_is_ignored():
    members = roster(2)
    outsider = member("Quincy Outsider")
    result = calculate(members=members, speakers=[trainer(), speaker(outsider)])
    assert result["spoke_count"] == 0
    assert result["attended_count"] == 2


# --- 12. zero attendees -----------------------------------------------------

def test_zero_attendees_keeps_legacy_values_and_an_explicit_status():
    result = calculate(members=[], speakers=[trainer(), speaker(None, status=NO_MATCH,
                                                                 role=OTHER_OR_UNRESOLVED)])
    assert result["calculation_status"] == NO_ATTENDED_LEARNERS
    assert result["attended_count"] == 0
    assert result["engagement_percentage"] == Decimal("0.00")
    assert result["engagement_score"] == 1
    # Legacy applied no Item 7 override here: the AI answer stood.
    assert result["learner_engagement_status"] is None
    assert result["item7_override_applied"] is False


def test_only_the_trainer_on_the_roster_is_no_attended_learners():
    result = calculate(members=[member("Morgan Trainerfield")], speakers=[trainer()])
    assert result["calculation_status"] == NO_ATTENDED_LEARNERS
    assert result["trainer_excluded_count"] == 1


def test_zero_percent_with_attendees_is_distinct_from_nobody_attending():
    result = calculate(members=roster(3), speakers=[trainer()])
    assert result["calculation_status"] == CALCULATED
    assert result["engagement_percentage"] == Decimal("0.00")
    assert result["learner_engagement_status"] == NOT_MET
    assert result["item7_override_applied"] is True


# --- 13-21. percentage boundaries -------------------------------------------

@pytest.mark.parametrize("spoke,attended,percentage,score,status", [
    (0, 5, "0.00", 1, NOT_MET),
    (1, 5, "20.00", 2, NOT_MET),
    (2, 5, "40.00", 3, NOT_MET),
    (1, 2, "50.00", 3, PARTIALLY_MET),
    (3, 5, "60.00", 4, PARTIALLY_MET),
    (3, 4, "75.00", 4, PARTIALLY_MET),
    (61, 80, "76.25", 4, MET),
    (4, 5, "80.00", 5, MET),
    (5, 5, "100.00", 5, MET),
])
def test_percentage_score_and_item7_boundaries(spoke, attended, percentage, score, status):
    members = roster(attended) if attended <= 10 else [
        member(f"Learner Number{index}") for index in range(attended)]
    speakers = [trainer()] + [speaker(members[index], rank=index + 2) for index in range(spoke)]
    result = calculate(members=members, speakers=speakers)
    assert result["engagement_percentage"] == Decimal(percentage)
    assert result["engagement_score"] == score
    assert result["learner_engagement_status"] == status


# --- 22. score bands --------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("0", 1), ("19.99", 1), ("20", 2), ("39.99", 2), ("40", 3), ("59.99", 3),
    ("60", 4), ("79.99", 4), ("80", 5), ("100", 5),
])
def test_score_band_boundaries(value, expected):
    assert engagement_score(Decimal(value)) == expected


# --- 23. Item 7 thresholds --------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("0", NOT_MET), ("49.99", NOT_MET), ("50", PARTIALLY_MET), ("75", PARTIALLY_MET),
    ("75.01", MET), ("100", MET),
])
def test_item7_boundaries(value, expected):
    assert learner_engagement_status(Decimal(value)) == expected


def test_75_exactly_is_partially_met_although_it_is_below_the_top_score_band():
    assert learner_engagement_status(Decimal("75")) == PARTIALLY_MET
    assert engagement_score(Decimal("75")) == 4


# --- 24. JavaScript toFixed(2) parity ---------------------------------------

# (spoke, attended, Node.js output of Number(((s/a)*100).toFixed(2)))
NODE_REFERENCE = [
    (1, 32, "3.13"), (5, 32, "15.63"), (3, 32, "9.38"),
    (7, 12, "58.33"), (11, 18, "61.11"), (4, 11, "36.36"), (5, 14, "35.71"),
    (1, 3, "33.33"), (2, 3, "66.67"), (1, 6, "16.67"), (1, 7, "14.29"),
    (3, 4, "75"), (29, 30, "96.67"), (1, 8, "12.5"), (1, 16, "6.25"),
    (7, 40, "17.5"), (13, 40, "32.5"), (57, 76, "75"), (61, 80, "76.25"),
]


@pytest.mark.parametrize("spoke,attended,expected", NODE_REFERENCE)
def test_percentage_matches_node_reference(spoke, attended, expected):
    assert engagement_percentage(spoke, attended) == Decimal(expected)


@pytest.mark.parametrize("value,expected", [
    (1.005, "1.00"), (2.675, "2.67"), (0.125, "0.13"), (79.995, "80.00"), (74.995, "75.00"),
])
def test_to_fixed_matches_node_on_binary_edge_cases(value, expected):
    assert js_to_fixed_2(value) == Decimal(expected)


def test_python_round_is_not_equivalent_and_is_not_used():
    # Why a dedicated helper exists: Python rounds 3.125 to 3.12, JS to 3.13.
    assert round(3.125, 2) == 3.12
    assert js_to_fixed_2(3.125) == Decimal("3.13")


def test_bands_are_evaluated_on_the_rounded_value():
    # 79.995 rounds to 80.00 in JS, which legacy then scored as 5.
    assert engagement_score(js_to_fixed_2(79.995)) == 5
    # 74.995 rounds to 75.00: Partially Met, not Met.
    assert learner_engagement_status(js_to_fixed_2(74.995)) == PARTIALLY_MET


def test_negative_percentage_is_rejected():
    with pytest.raises(ValueError):
        js_to_fixed_2(-0.1)


# --- 25-26. silent learners and participant rows ----------------------------

def test_silent_learners_are_denominator_members_without_a_safe_learner():
    members = roster(4) + [member("Morgan Trainerfield")]
    speakers = [trainer(), speaker(members[0]),
                speaker(None, role=OTHER_OR_UNRESOLVED, status=AMBIGUOUS)]
    result = calculate(members=members, speakers=speakers)
    silent = {row["member_id"] for row in result["participants"]
              if row["participation_status"] == SILENT}
    assert silent == {m["member_id"] for m in members[1:4]}
    assert result["silent_count"] == 3
    assert result["spoke_count"] + result["silent_count"] == result["attended_count"]


def test_exactly_one_participant_row_per_snapshot_member():
    members = roster(3)
    speakers = [trainer(), speaker(members[0]), speaker(members[0], rank=3)]
    result = calculate(members=members, speakers=speakers)
    ids = [row["member_id"] for row in result["participants"]]
    assert sorted(ids, key=str) == sorted((m["member_id"] for m in members), key=str)
    assert len(ids) == len(set(ids))


def test_participant_rows_carry_no_names():
    members = roster(2)
    result = calculate(members=members, speakers=[trainer(), speaker(members[0])])
    for row in result["participants"]:
        assert set(row) == {"member_id", "participation_status",
                            "matched_speaker_count", "first_speaker_id"}
    assert "Avery" not in repr(result)


def test_participant_identity_is_unique_per_engagement_and_member():
    engagement, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert engagement_participant_identity(engagement_id=engagement, member_id=first) == \
        engagement_participant_identity(engagement_id=engagement, member_id=first)
    assert engagement_participant_identity(engagement_id=engagement, member_id=first) != \
        engagement_participant_identity(engagement_id=engagement, member_id=second)


# --- ambiguity impact -------------------------------------------------------

def test_ambiguity_that_cannot_change_the_outcome_is_recorded_but_not_escalated():
    members = roster(10)
    speakers = [trainer()] + [speaker(members[i], rank=i + 2) for i in range(9)]
    speakers.append(speaker(None, role=OTHER_OR_UNRESOLVED, status=AMBIGUOUS, rank=20,
                            candidates=[members[9]["external_person_id"]]))
    result = calculate(members=members, speakers=speakers)
    # 90% -> at most 100%: score 5 and Met either way.
    assert result["calculation_status"] == CALCULATED_WITH_AMBIGUITY
    assert result["ambiguity_upper_bound"]["could_change_score_or_item7"] is False
    assert result["spoke_count"] == 9


def test_ambiguity_that_could_change_item7_requires_review():
    members = roster(4)
    speakers = [trainer(), speaker(members[0]), speaker(members[1], rank=3),
                speaker(None, role=OTHER_OR_UNRESOLVED, status=AMBIGUOUS, rank=4,
                        candidates=[members[2]["external_person_id"],
                                    members[3]["external_person_id"]])]
    result = calculate(members=members, speakers=speakers)
    # 50% (Partially Met, score 3) could be 75% (still Partially Met, score 4).
    assert result["engagement_percentage"] == Decimal("50.00")
    assert result["calculation_status"] == REVIEW_AMBIGUITY_MAY_CHANGE_RESULT
    assert result["ambiguity_upper_bound"]["spoke_count"] == 3
    # The recorded result itself never guesses.
    assert result["spoke_count"] == 2


def test_ambiguity_with_no_silent_candidate_cannot_raise_the_count():
    members = roster(2)
    speakers = [trainer(), speaker(members[0]),
                speaker(None, role=OTHER_OR_UNRESOLVED, status=AMBIGUOUS, rank=3,
                        candidates=[members[0]["external_person_id"]])]
    result = calculate(members=members, speakers=speakers)
    assert result["ambiguity_upper_bound"]["spoke_count"] == 1
    assert result["calculation_status"] == CALCULATED_WITH_AMBIGUITY


# --- 27-31. fingerprint and provenance --------------------------------------

def fingerprint_args(**overrides):
    members = overrides.pop("members", None) or roster(2)
    speakers = overrides.pop("speakers", None) or [trainer(), speaker(members[0])]
    args = {"algorithm_version": "a1", "snapshot_id": "snap", "snapshot_fingerprint": "f" * 64,
            "resolver_version": "r1", "role_algorithm_version": "o1", "document_id": "doc",
            "members": members, "speakers": speakers}
    args.update(overrides)
    return args


def test_fingerprint_is_deterministic_and_order_independent():
    args = fingerprint_args()
    forward = engagement_fingerprint(**args)
    reordered = dict(args, members=list(reversed(args["members"])),
                     speakers=list(reversed(args["speakers"])))
    assert engagement_fingerprint(**reordered) == forward
    assert len(forward) == 64


@pytest.mark.parametrize("field,value", [
    ("snapshot_id", "snap-2"), ("snapshot_fingerprint", "0" * 64),
    ("resolver_version", "r2"), ("role_algorithm_version", "o2"),
    ("algorithm_version", "a2"), ("document_id", "doc-2"),
])
def test_fingerprint_changes_with_every_input_version(field, value):
    args = fingerprint_args()
    assert engagement_fingerprint(**dict(args, **{field: value})) != engagement_fingerprint(**args)


def test_fingerprint_changes_when_a_resolution_changes():
    args = fingerprint_args()
    changed = [dict(row) for row in args["speakers"]]
    changed[1]["resolution_status"] = AMBIGUOUS
    assert engagement_fingerprint(**dict(args, speakers=changed)) != engagement_fingerprint(**args)


def test_fingerprint_changes_when_the_roster_changes():
    args = fingerprint_args()
    grown = args["members"] + [member("Kit Lowell")]
    assert engagement_fingerprint(**dict(args, members=grown)) != engagement_fingerprint(**args)


@pytest.mark.parametrize("field,value", [
    ("attendance_snapshot_id", "other-snapshot"),
    ("resolver_version", "r2"),
    ("role_algorithm_version", "o2"),
    ("engagement_algorithm_version", "a2"),
    ("source_fingerprint", "1" * 64),
])
def test_engagement_identity_changes_with_each_provenance_input(field, value):
    base = {"document_id": "doc", "attendance_snapshot_id": "snap", "resolver_version": "r1",
            "role_algorithm_version": "o1", "engagement_algorithm_version": "a1",
            "source_fingerprint": "f" * 64}
    assert engagement_identity(**base) == engagement_identity(**dict(base))
    assert engagement_identity(**dict(base, **{field: value})) != engagement_identity(**base)


def test_engagement_identity_requires_a_fingerprint():
    with pytest.raises(ValueError):
        engagement_identity(document_id="d", attendance_snapshot_id="s", resolver_version="r",
                            role_algorithm_version="o", engagement_algorithm_version="a",
                            source_fingerprint="")


# --- no live attendance, no QA, no AI in this package -----------------------

def test_engagement_package_never_names_live_or_qa_write_targets():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    sources = [*(root / "app" / "engagement").glob("*.py"),
               root / "app" / "db" / "repositories" / "engagement.py"]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines()
                         if not line.lstrip().startswith("#"))
        assert "kbc_attendance" not in code.replace("public.kbc_attendance is", ""), path.name
        assert "kbc_users_data" not in code, path.name
        assert "aptem_auto_extracting" not in code, path.name
        for verb in ("INSERT INTO public.qa_", "UPDATE public.qa_", "DELETE FROM public.qa_"):
            assert verb not in code, path.name
        assert "anthropic" not in code.lower() and "openai" not in code.lower(), path.name
