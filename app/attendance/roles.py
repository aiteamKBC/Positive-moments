"""
Deterministic speaker roles.

Ported from the legacy `lecturerFromVtt` rule ONLY:

    const entries = [...secsBySpeaker.entries()].sort((a, b) => b[1] - a[1]);
    const lecturerFromVtt = entries.length ? entries[0][0] : "";

The speaker with the most speech time is the trainer candidate. Nothing else
in this phase infers a role from the transcript.

DELIBERATELY NOT PORTED: the historical final precedence
`trainer = aiTrainer || lecturerFromVtt || lectureRow.trainer`. The AI override
belongs to the QA/AI migration. What is stored here is the deterministic
VTT-derived candidate and nothing else, so a future AI layer can record a
separate decision without ever rewriting this evidence.
"""
from app.attendance.resolver import MATCHED_STATUSES


ROLE_ALGORITHM_VERSION = "vtt_top_speaker_v1"
DETERMINISTIC_TRAINER_SOURCE = "VTT_TOP_SPEAKER"

TRAINER_CANDIDATE = "TRAINER_CANDIDATE"
LEARNER = "LEARNER"
OTHER_OR_UNRESOLVED = "OTHER_OR_UNRESOLVED"


def trainer_ranking_key(speaker) -> tuple:
    """
    Descending speech time, then earliest appearance, then the raw label.

    Legacy used a stable descending sort over a Map built by walking the cues
    in order, so on a tie the speaker who spoke FIRST won. `first_cue_index`
    reproduces that insertion order exactly; the raw label is a final
    tiebreaker so the outcome can never depend on database row order.
    """
    return (-int(speaker["gross_spoken_ms"]),
            int(speaker["first_cue_index"]),
            str(speaker["speaker_label_raw"]))


def rank_speakers(speakers) -> list:
    return sorted(speakers, key=trainer_ranking_key)


def assign_roles(speakers, resolutions_by_speaker_id) -> list:
    """
    Assign one deterministic role per speaker.

    - the top-ranked speaker becomes TRAINER_CANDIDATE, on transcript evidence
      alone. An attendance match does NOT change it: role inference and person
      identity are separate facts and both are preserved.
    - any other speaker becomes LEARNER only when person resolution safely tied
      it to a roster member. Being "not the trainer" is never sufficient.
    - everything else is OTHER_OR_UNRESOLVED, including AMBIGUOUS, so an
      unresolved person can never be counted as a learner.
    """
    ranked = rank_speakers(speakers)
    roles = []
    for rank, speaker in enumerate(ranked, start=1):
        resolution = resolutions_by_speaker_id.get(speaker["speaker_id"]) or {}
        status = resolution.get("resolution_status")
        matched = status in MATCHED_STATUSES
        if rank == 1:
            role = TRAINER_CANDIDATE
            role_source = DETERMINISTIC_TRAINER_SOURCE
        elif matched:
            role = LEARNER
            role_source = "ATTENDANCE_RESOLVED_NON_TRAINER_SPEAKER"
        else:
            role = OTHER_OR_UNRESOLVED
            role_source = "UNRESOLVED_NON_TRAINER_SPEAKER"
        roles.append({
            "speaker_id": speaker["speaker_id"],
            "role": role,
            "role_source": role_source,
            "role_rank": rank,
            "gross_spoken_ms": int(speaker["gross_spoken_ms"]),
            # Diagnostic, never a role change: the trainer candidate may also
            # legitimately appear on the attendance roster.
            "trainer_also_matched_attendance": bool(rank == 1 and matched),
            "person_resolution_status": status,
        })
    return roles
