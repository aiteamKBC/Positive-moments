"""
Phase 2C4 persistence: deterministic learner engagement.

READ-ONLY inputs, all persisted by earlier phases:
  lecture_sessions, lecture_transcript_speakers,
  lecture_attendance_snapshots, lecture_attendance_snapshot_members,
  lecture_transcript_speaker_identities, lecture_transcript_speaker_roles,
  and qa_doctors_sessions / qa_doctors_checklist_items for parity only.

WRITES ONLY: lecture_engagement_metrics, lecture_engagement_participants,
lecture_engagement_runs.

public.kbc_attendance is deliberately absent from every statement here: the
roster used is the one Phase 2C3 froze.
"""
import json
import uuid

from app.common.errors import DATABASE_ERROR, PlatformError


# One row per (snapshot, document) that has complete role evidence for the
# requested versions. Roles are only written after resolutions, so a role row
# implies its resolution row exists.
LOAD_SNAPSHOTS = """
SELECT DISTINCT sn.snapshot_id, sn.lecture_id, l.subject, sn.source_fingerprint,
       sn.created_at, sp.document_id, l.scheduled_start,
       sn.source_row_count, sn.present_row_count, sn.effective_member_count
  FROM public.lecture_attendance_snapshots sn
  JOIN public.lecture_sessions l ON l.lecture_id = sn.lecture_id
  JOIN public.lecture_transcript_speaker_roles r
    ON r.attendance_snapshot_id = sn.snapshot_id
   AND r.resolver_version = %s AND r.role_algorithm_version = %s
  JOIN public.lecture_transcript_speakers sp ON sp.speaker_id = r.speaker_id
 WHERE l.session_date = %s
   AND sn.attendance_resolution_version = %s
 ORDER BY l.scheduled_start, l.subject, sn.created_at, sn.snapshot_id
"""

# The lecture-scoped variant. Deliberately a SEPARATE statement rather than an
# extra predicate on LOAD_SNAPSHOTS: the scope is part of the contract, so it
# must be impossible to reach the date-wide query by leaving an argument unset.
# Note there is no session_date predicate at all - the lecture id IS the scope.
LOAD_SNAPSHOTS_FOR_LECTURE = """
SELECT DISTINCT sn.snapshot_id, sn.lecture_id, l.subject, sn.source_fingerprint,
       sn.created_at, sp.document_id, l.scheduled_start,
       sn.source_row_count, sn.present_row_count, sn.effective_member_count
  FROM public.lecture_attendance_snapshots sn
  JOIN public.lecture_sessions l ON l.lecture_id = sn.lecture_id
  JOIN public.lecture_transcript_speaker_roles r
    ON r.attendance_snapshot_id = sn.snapshot_id
   AND r.resolver_version = %s AND r.role_algorithm_version = %s
  JOIN public.lecture_transcript_speakers sp ON sp.speaker_id = r.speaker_id
 WHERE sn.lecture_id = %s
   AND sn.attendance_resolution_version = %s
 ORDER BY l.scheduled_start, l.subject, sn.created_at, sn.snapshot_id
"""

LOAD_MEMBERS = """
SELECT m.snapshot_id, m.member_id, m.external_person_id, m.display_name_raw
  FROM public.lecture_attendance_snapshot_members m
 WHERE m.snapshot_id = ANY(%s)
 ORDER BY m.snapshot_id, m.member_id
"""

LOAD_SPEAKERS = """
SELECT r.attendance_snapshot_id, sp.document_id, sp.speaker_id, sp.speaker_label_raw,
       r.role, r.role_rank, i.resolution_status, i.matched_member_id,
       i.metadata -> 'candidate_person_ids'
  FROM public.lecture_transcript_speaker_roles r
  JOIN public.lecture_transcript_speakers sp ON sp.speaker_id = r.speaker_id
  JOIN public.lecture_transcript_speaker_identities i
    ON i.speaker_id = r.speaker_id
   AND i.attendance_snapshot_id = r.attendance_snapshot_id
   AND i.resolver_version = r.resolver_version
 WHERE r.attendance_snapshot_id = ANY(%s)
   AND r.resolver_version = %s
   AND r.role_algorithm_version = %s
 ORDER BY r.attendance_snapshot_id, sp.document_id, r.role_rank
"""


class EngagementInputRepository:
    """READ-ONLY access to persisted Phase 2C3 evidence."""

    def load_snapshots(self, connection, target_date, *, attendance_resolution_version,
                       resolver_version, role_algorithm_version) -> list[dict]:
        try:
            rows = connection.execute(LOAD_SNAPSHOTS, (
                resolver_version, role_algorithm_version, target_date,
                attendance_resolution_version)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "engagement snapshot query failed") from exc
        return [{"snapshot_id": row[0], "lecture_id": row[1], "subject": row[2],
                 "snapshot_fingerprint": row[3], "snapshot_created_at": row[4],
                 # Phase 3C3D: the frozen counts the coverage status is
                 # derived from, carried with the snapshot so no consumer has
                 # to re-query the external attendance source.
                 "source_row_count": row[7], "present_row_count": row[8],
                 "effective_member_count": row[9],
                 "document_id": row[5]} for row in rows]

    def load_snapshots_for_lecture(self, connection, lecture_id, *,
                                   attendance_resolution_version, resolver_version,
                                   role_algorithm_version) -> list[dict]:
        """Exactly one lecture's snapshots. Never widens to the date."""
        try:
            rows = connection.execute(LOAD_SNAPSHOTS_FOR_LECTURE, (
                resolver_version, role_algorithm_version, lecture_id,
                attendance_resolution_version)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR,
                                "engagement lecture snapshot query failed") from exc
        return [{"snapshot_id": row[0], "lecture_id": row[1], "subject": row[2],
                 "snapshot_fingerprint": row[3], "snapshot_created_at": row[4],
                 # Phase 3C3D: the frozen counts the coverage status is
                 # derived from, carried with the snapshot so no consumer has
                 # to re-query the external attendance source.
                 "source_row_count": row[7], "present_row_count": row[8],
                 "effective_member_count": row[9],
                 "document_id": row[5]} for row in rows]

    def load_lecture(self, connection, lecture_id) -> dict | None:
        try:
            row = connection.execute(
                "SELECT lecture_id, subject, session_date, downstream_ready "
                "  FROM public.lecture_sessions WHERE lecture_id = %s",
                (lecture_id,)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "lecture lookup failed") from exc
        if row is None:
            return None
        return {"lecture_id": row[0], "subject": row[1], "session_date": row[2],
                "downstream_ready": row[3]}

    def load_members(self, connection, snapshot_ids) -> dict:
        if not snapshot_ids:
            return {}
        try:
            rows = connection.execute(LOAD_MEMBERS, (list(snapshot_ids),)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "snapshot member query failed") from exc
        grouped: dict = {}
        for row in rows:
            grouped.setdefault(row[0], []).append({
                "member_id": row[1], "external_person_id": row[2],
                "display_name_raw": row[3]})
        return grouped

    def load_speakers(self, connection, snapshot_ids, *, resolver_version,
                      role_algorithm_version) -> dict:
        if not snapshot_ids:
            return {}
        try:
            rows = connection.execute(LOAD_SPEAKERS, (
                list(snapshot_ids), resolver_version, role_algorithm_version)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "speaker evidence query failed") from exc
        grouped: dict = {}
        for row in rows:
            grouped.setdefault((row[0], row[1]), []).append({
                "speaker_id": row[2], "speaker_label_raw": row[3], "role": row[4],
                "role_rank": row[5], "resolution_status": row[6],
                "matched_member_id": row[7], "candidate_person_ids": row[8] or []})
        return grouped


class EngagementRepository:
    def exists(self, connection, engagement_id) -> bool:
        try:
            return connection.execute(
                "SELECT 1 FROM public.lecture_engagement_metrics WHERE engagement_id = %s",
                (engagement_id,)).fetchone() is not None
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "engagement lookup failed") from exc

    def upsert(self, connection, row, participants) -> dict:
        """
        Write one engagement result and its participants.

        The id embeds every input version and the input fingerprint, so a
        rerun on unchanged evidence lands on the same row, and changed
        evidence always creates a new one - history is never rewritten.
        """
        existed = self.exists(connection, row["engagement_id"])
        try:
            connection.execute("""
            INSERT INTO public.lecture_engagement_metrics (
                engagement_id, lecture_id, document_id, attendance_snapshot_id,
                engagement_algorithm_version, trainer_exclusion_version,
                resolver_version, role_algorithm_version,
                calculation_status, trainer_speaker_id, trainer_exclusion_status,
                trainer_attendance_match_count, attendance_before_trainer_exclusion,
                trainer_excluded_count, attended_count, resolved_learner_speaker_count,
                spoke_count, silent_count, ambiguous_speaker_count, unresolved_speaker_count,
                engagement_percentage, engagement_score, learner_engagement_status,
                item7_override_applied, source_fingerprint, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (engagement_id) DO UPDATE SET
                calculation_status = EXCLUDED.calculation_status,
                attended_count = EXCLUDED.attended_count,
                spoke_count = EXCLUDED.spoke_count,
                silent_count = EXCLUDED.silent_count,
                engagement_percentage = EXCLUDED.engagement_percentage,
                engagement_score = EXCLUDED.engagement_score,
                learner_engagement_status = EXCLUDED.learner_engagement_status,
                metadata = EXCLUDED.metadata, updated_at = now()
            """, (
                row["engagement_id"], row["lecture_id"], row["document_id"],
                row["attendance_snapshot_id"], row["engagement_algorithm_version"],
                row["trainer_exclusion_version"], row["resolver_version"],
                row["role_algorithm_version"], row["calculation_status"],
                row["trainer_speaker_id"], row["trainer_exclusion_status"],
                row["trainer_attendance_match_count"],
                row["attendance_before_trainer_exclusion"], row["trainer_excluded_count"],
                row["attended_count"], row["resolved_learner_speaker_count"],
                row["spoke_count"], row["silent_count"], row["ambiguous_speaker_count"],
                row["unresolved_speaker_count"], row["engagement_percentage"],
                row["engagement_score"], row["learner_engagement_status"],
                row["item7_override_applied"], row["source_fingerprint"],
                json.dumps(row.get("metadata", {}), default=str)))
            with connection.cursor() as cursor:
                cursor.executemany("""
                INSERT INTO public.lecture_engagement_participants (
                    participant_id, engagement_id, snapshot_member_id,
                    participation_status, matched_speaker_count, first_speaker_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (participant_id) DO UPDATE SET
                    participation_status = EXCLUDED.participation_status,
                    matched_speaker_count = EXCLUDED.matched_speaker_count,
                    first_speaker_id = EXCLUDED.first_speaker_id
                """, [
                    (item["participant_id"], row["engagement_id"], item["member_id"],
                     item["participation_status"], item["matched_speaker_count"],
                     item["first_speaker_id"])
                    for item in participants
                ])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "engagement write failed") from exc
        return {"created": 0 if existed else 1, "updated": 1 if existed else 0,
                "participants_written": len(participants)}


class EngagementRunRepository:
    COUNTERS = ("snapshots_considered", "engagements_created", "engagements_updated",
                "participants_written", "review_required_count", "error_count")

    def start(self, connection, target_date, versions, mode="SHADOW") -> uuid.UUID:
        run_id = uuid.uuid4()
        try:
            connection.execute(
                "INSERT INTO public.lecture_engagement_runs (run_id, target_date, mode, "
                " status, engagement_algorithm_version, resolver_version, "
                " role_algorithm_version) VALUES (%s, %s, %s, 'RUNNING', %s, %s, %s)",
                (run_id, target_date, mode, versions["engagement_algorithm_version"],
                 versions["resolver_version"], versions["role_algorithm_version"]))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not create engagement run") from exc
        return run_id

    def complete(self, connection, run_id, summary) -> None:
        assignments = ", ".join(f"{name} = %s" for name in self.COUNTERS)
        values = [int(summary.get(name, 0)) for name in self.COUNTERS]
        try:
            connection.execute(
                "UPDATE public.lecture_engagement_runs "
                f"SET completed_at = now(), status = %s, {assignments}, metadata = %s::jsonb "
                "WHERE run_id = %s",
                [summary["status"], *values,
                 json.dumps(summary.get("metadata", {}), default=str), run_id])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not complete engagement run") from exc


# Only numbers cross the wire: the evidence text itself contains learner names,
# so the counts are extracted inside PostgreSQL.
LOAD_LEGACY_ENGAGEMENT = """
SELECT q.subject, q."Engagement", q.engagement_score, ci.status,
       (regexp_match(ci.evidence, '^Engagement score = (\\d+) / (\\d+) = '))[1]::int,
       (regexp_match(ci.evidence, '^Engagement score = (\\d+) / (\\d+) = '))[2]::int,
       (regexp_match(ci.evidence, 'did not speak \\((\\d+)\\)'))[1]::int
  FROM public.qa_doctors_sessions q
  LEFT JOIN public.qa_doctors_checklist_items ci
    ON ci.session_id = q.session_id AND ci.checklist_order = 7
 WHERE q.date = %s
 ORDER BY q.subject
"""


class LegacyEngagementParityRepository:
    """READ-ONLY. Legacy engagement values for comparison; never written."""

    def load(self, connection, target_date) -> list[dict]:
        try:
            rows = connection.execute(LOAD_LEGACY_ENGAGEMENT, (target_date,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy engagement query failed") from exc
        return [{"subject": row[0], "engagement": row[1], "engagement_score": row[2],
                 "item7_status": row[3], "legacy_spoke_count": row[4],
                 "legacy_attended_count": row[5], "legacy_silent_count": row[6]}
                for row in rows]
