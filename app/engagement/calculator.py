"""
Pure deterministic engagement, ported from the legacy
`Build Session and Checklist Rows` node (L273-317 of the v8 export):

    const attendedStudents = attendanceRows
      .map(r => (r.Name || r.name || "").trim())
      .filter(Boolean)
      .filter(name => !lecturerFromVtt || !isSimilar(name, lecturerFromVtt));
    const attendedCount = attendedStudents.length;
    const studentSpeakers = rawStudentSpeakers.filter(name =>
      attendedStudents.some(att => isSimilar(name, att)));
    const spokeCount = studentSpeakers.length;
    if (attendedCount > 0) {
      engagementPercent = Number(((spokeCount / attendedCount) * 100).toFixed(2));
      if (engagementPercent >= 80) engagement_score = 5; ... >= 20 -> 2
      if (engagementPercent > 75) "Met"; else if (>= 50) "Partially Met"; else "Not Met"
    }

Deliberate, documented divergences - each one a safety improvement:

- spoke_count counts UNIQUE attendance members, not speaker labels. Legacy
  counted labels, so two labels for one learner counted twice and the
  percentage could exceed 100.
- who spoke is read from persisted Phase 2C3 resolutions, never re-matched.
  AMBIGUOUS, NO_MATCH and OTHER_OR_UNRESOLVED speakers never count.
- trainer exclusion refuses to guess: more than one roster member matching the
  trainer yields TRAINER_ATTENDANCE_AMBIGUOUS and no percentage, where legacy
  silently removed every match.
- silent learners are denominator members with no safely resolved learner
  speaker. Legacy tested against every non-trainer label, including labels it
  had not matched to anyone.

No personal name leaves this module: results carry member and speaker ids.
"""
import hashlib
from decimal import ROUND_HALF_UP, Decimal

from app.attendance.legacy_matching import LEGACY_MATCHER_VERSION, is_similar
from app.attendance.resolver import AMBIGUOUS, MATCHED_STATUSES
from app.attendance.roles import LEARNER, TRAINER_CANDIDATE


ENGAGEMENT_ALGORITHM_VERSION = "legacy_qa_v8_engagement_v1"
TRAINER_EXCLUSION_VERSION = f"trainer_exclusion_{LEGACY_MATCHER_VERSION}_safe_v1"

# Calculation statuses.
CALCULATED = "CALCULATED"
CALCULATED_WITH_AMBIGUITY = "CALCULATED_WITH_AMBIGUITY"
REVIEW_AMBIGUITY_MAY_CHANGE_RESULT = "REVIEW_AMBIGUITY_MAY_CHANGE_RESULT"
NO_ATTENDED_LEARNERS = "NO_ATTENDED_LEARNERS"
TRAINER_ATTENDANCE_AMBIGUOUS = "TRAINER_ATTENDANCE_AMBIGUOUS"

# Trainer exclusion outcomes.
TRAINER_NOT_IN_ATTENDANCE = "TRAINER_NOT_IN_ATTENDANCE"
TRAINER_EXCLUDED = "TRAINER_EXCLUDED"
NO_TRAINER_CANDIDATE = "NO_TRAINER_CANDIDATE"

# Participant statuses.
SPOKE = "SPOKE"
SILENT = "SILENT"
EXCLUDED_TRAINER = "EXCLUDED_TRAINER"
UNDETERMINED = "UNDETERMINED"

# Legacy Item 7 strings, verbatim.
MET = "Met"
PARTIALLY_MET = "Partially Met"
NOT_MET = "Not Met"

_TWO_PLACES = Decimal("0.01")


def js_to_fixed_2(value: float) -> Decimal:
    """
    JavaScript `Number(x.toFixed(2))` for non-negative finite x.

    The ECMAScript spec rounds the EXACT value of the double to the nearest
    hundredth, taking the larger candidate on a tie. `Decimal(float)` is that
    exact value, so ROUND_HALF_UP on it reproduces JavaScript - including the
    cases where Python's `round()` differs (3.125 -> 3.13 in JS, 3.12 in
    Python) and where the double is just below the tie (1.005 -> 1.00 in both).
    """
    if value < 0:
        raise ValueError("engagement percentage cannot be negative")
    return Decimal(value).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def engagement_percentage(spoke_count: int, attended_count: int) -> Decimal:
    """Legacy arithmetic: IEEE double division and multiplication, then toFixed(2)."""
    if attended_count <= 0:
        return Decimal("0.00")
    return js_to_fixed_2((spoke_count / attended_count) * 100)


def engagement_score(percentage) -> int:
    """Legacy bands, evaluated on the ROUNDED percentage, as legacy did."""
    value = Decimal(percentage)
    if value >= 80:
        return 5
    if value >= 60:
        return 4
    if value >= 40:
        return 3
    if value >= 20:
        return 2
    return 1


def learner_engagement_status(percentage) -> str:
    """Legacy Item 7 override. Note 75 exactly is Partially Met; 80 is a score band."""
    value = Decimal(percentage)
    if value > 75:
        return MET
    if value >= 50:
        return PARTIALLY_MET
    return NOT_MET


def trainer_attendance_matches(members, trainer_label):
    """
    Legacy `isSimilar(name, lecturerFromVtt)`, with legacy argument order.

    This is the one comparison Phase 2C4 is allowed to make itself: Phase 2C3
    deliberately kept the full roster, and the denominator exclusion is an
    engagement decision with its own version.
    """
    if not trainer_label:
        return []
    return [member for member in members
            if is_similar(member["display_name_raw"], trainer_label)]


def calculate(*, members, speakers) -> dict:
    """
    Compute engagement for ONE attendance snapshot.

    members  - effective snapshot members: member_id, external_person_id,
               display_name_raw (used only for the trainer comparison)
    speakers - one row per document speaker: speaker_id, speaker_label_raw,
               role, role_rank, resolution_status, matched_member_id,
               candidate_person_ids
    """
    trainer = next((row for row in speakers if row["role"] == TRAINER_CANDIDATE), None)
    trainer_matches = trainer_attendance_matches(
        members, trainer["speaker_label_raw"] if trainer else None)

    if trainer is None:
        exclusion = NO_TRAINER_CANDIDATE
    elif not trainer_matches:
        exclusion = TRAINER_NOT_IN_ATTENDANCE
    elif len(trainer_matches) == 1:
        exclusion = TRAINER_EXCLUDED
    else:
        exclusion = TRAINER_ATTENDANCE_AMBIGUOUS

    matched_ids = {member["member_id"] for member in trainer_matches}
    excluded_ids = matched_ids if exclusion == TRAINER_EXCLUDED else set()
    undetermined_ids = matched_ids if exclusion == TRAINER_ATTENDANCE_AMBIGUOUS else set()
    denominator = [member for member in members
                   if member["member_id"] not in excluded_ids | undetermined_ids]
    denominator_ids = {member["member_id"] for member in denominator}

    non_trainer = [row for row in speakers if row["role"] != TRAINER_CANDIDATE]
    safe_learners = [row for row in non_trainer
                     if row["role"] == LEARNER
                     and row["resolution_status"] in MATCHED_STATUSES
                     and row["matched_member_id"] is not None]

    # Unique attendance members, never speaker labels.
    speakers_by_member: dict = {}
    for row in sorted(safe_learners, key=lambda item: (item["role_rank"], str(item["speaker_id"]))):
        if row["matched_member_id"] in denominator_ids:
            speakers_by_member.setdefault(row["matched_member_id"], []).append(row["speaker_id"])

    ambiguous = [row for row in non_trainer if row["resolution_status"] == AMBIGUOUS]
    unresolved = [row for row in non_trainer
                  if row["role"] != LEARNER and row["resolution_status"] != AMBIGUOUS]

    spoke_ids = set(speakers_by_member)
    silent_ids = denominator_ids - spoke_ids

    participants = []
    for member in sorted(members, key=lambda item: str(item["member_id"])):
        member_id = member["member_id"]
        if member_id in excluded_ids:
            status = EXCLUDED_TRAINER
        elif member_id in undetermined_ids:
            status = UNDETERMINED
        elif member_id in spoke_ids:
            status = SPOKE
        else:
            status = SILENT
        matched = speakers_by_member.get(member_id, [])
        participants.append({
            "member_id": member_id, "participation_status": status,
            "matched_speaker_count": len(matched),
            "first_speaker_id": matched[0] if matched else None,
        })

    attended_before = len(members)
    result = {
        "trainer_speaker_id": trainer["speaker_id"] if trainer else None,
        "trainer_exclusion_status": exclusion,
        "trainer_attendance_match_count": len(trainer_matches),
        "attendance_before_trainer_exclusion": attended_before,
        "trainer_excluded_count": len(excluded_ids),
        "resolved_learner_speaker_count": len(safe_learners),
        "learner_speakers_on_excluded_member": sum(
            1 for row in safe_learners if row["matched_member_id"] not in denominator_ids),
        "duplicate_speaker_aliases": sum(len(ids) - 1 for ids in speakers_by_member.values()),
        "ambiguous_speaker_count": len(ambiguous),
        "unresolved_speaker_count": len(unresolved),
        "participants": participants,
        "item7_override_applied": False,
        "ambiguity_upper_bound": None,
    }

    if exclusion == TRAINER_ATTENDANCE_AMBIGUOUS:
        # Refuse to calculate a percentage on a denominator nobody can defend.
        # Legacy would have removed EVERY match; that value is kept only as a
        # labelled diagnostic, never as the result.
        legacy_attended = attended_before - len(trainer_matches)
        result.update({
            "calculation_status": TRAINER_ATTENDANCE_AMBIGUOUS,
            "attended_count": None, "spoke_count": len(spoke_ids),
            "silent_count": len(silent_ids),
            "engagement_percentage": None, "engagement_score": None,
            "learner_engagement_status": None,
            "legacy_compatible_attended_count": legacy_attended,
        })
        return result

    attended = len(denominator)
    spoke = len(spoke_ids)
    result.update({"attended_count": attended, "spoke_count": spoke,
                   "silent_count": len(silent_ids)})

    if attended == 0:
        # Legacy parity: 0% and score 1, and NO Item 7 override - the AI's own
        # Item 7 answer stood. The explicit status keeps "nobody attended"
        # distinguishable from "everybody was silent".
        result.update({
            "calculation_status": NO_ATTENDED_LEARNERS,
            "engagement_percentage": Decimal("0.00"), "engagement_score": 1,
            "learner_engagement_status": None,
        })
        return result

    percentage = engagement_percentage(spoke, attended)
    score = engagement_score(percentage)
    status = learner_engagement_status(percentage)
    result.update({
        "engagement_percentage": percentage, "engagement_score": score,
        "learner_engagement_status": status, "item7_override_applied": True,
        "calculation_status": CALCULATED,
    })

    if ambiguous:
        # Best case: each ambiguous speaker is one more silent learner who
        # actually spoke, limited to silent members it was a candidate for.
        candidate_ids = {pid for row in ambiguous
                         for pid in (row.get("candidate_person_ids") or []) if pid}
        silent_candidates = [member for member in denominator
                             if member["member_id"] in silent_ids
                             and member.get("external_person_id") in candidate_ids]
        upper_spoke = spoke + min(len(ambiguous), len(silent_candidates))
        upper_percentage = engagement_percentage(upper_spoke, attended)
        changes = (engagement_score(upper_percentage) != score
                   or learner_engagement_status(upper_percentage) != status)
        result["ambiguity_upper_bound"] = {
            "spoke_count": upper_spoke,
            "engagement_percentage": str(upper_percentage),
            "could_change_score_or_item7": changes,
        }
        result["calculation_status"] = (REVIEW_AMBIGUITY_MAY_CHANGE_RESULT if changes
                                        else CALCULATED_WITH_AMBIGUITY)
    return result


def engagement_fingerprint(*, algorithm_version, snapshot_id, snapshot_fingerprint,
                           resolver_version, role_algorithm_version, document_id,
                           members, speakers) -> str:
    """
    SHA-256 over every input that can change the result, sorted, newline-joined:

      version, snapshot id + its roster fingerprint, resolver version, role
      version, document id, then one sorted line per member id, then one
      sorted line per speaker: id | role | rank | status | matched member.

    Names are not ingredients: they already feed the snapshot fingerprint, and
    the trainer label is pinned by its speaker id through the document.
    """
    lines = [
        f"algorithm:{algorithm_version}",
        f"snapshot:{snapshot_id}:{snapshot_fingerprint}",
        f"resolver:{resolver_version}",
        f"role:{role_algorithm_version}",
        f"document:{document_id}",
        *sorted(f"member:{member['member_id']}" for member in members),
        *sorted("speaker:{}|{}|{}|{}|{}".format(
            row["speaker_id"], row["role"], row["role_rank"],
            row["resolution_status"], row["matched_member_id"] or "")
            for row in speakers),
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
