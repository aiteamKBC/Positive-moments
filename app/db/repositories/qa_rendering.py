"""
Phase 3B persistence: LMS snapshots and rendered legacy-compatible output.

READ-ONLY inputs: lecture_sessions, lecture_transcript_selections,
lecture_combined_transcripts, lecture_transcript_documents,
lecture_transcript_cues, the Phase 2C3 v2 snapshot and its members, the Phase
2C4 engagement result and participants, and the Phase 3A evaluation, checklist
and evidence clips. Legacy QA tables are read for comparison only.

public.kbc_users_data is read with a single SELECT during snapshot
acquisition, and never touched again during rendering.

WRITES ONLY: lecture_lms_snapshots, lecture_lms_snapshot_members,
lecture_qa_rendered_sessions, lecture_qa_rendered_checklist_items,
lecture_qa_render_runs.
"""
import json
import uuid

from app.common.errors import DATABASE_ERROR, PlatformError


# One row per Phase 3A evaluation for the date, with everything the renderer
# needs from earlier phases. Pinned to the approved roster rule.
LOAD_RENDER_INPUTS = """
SELECT e.evaluation_id, e.lecture_id, e.document_id, e.qa_status, e.delivery_status,
       e.source_fingerprint, e.primary_provider_transcript_id, e.meeting_id,
       e.canonical_trainer, e.duration_minutes, e.duration_text, e.duration_score,
       e.engagement_percentage, e.engagement_score, e.attended_count, e.spoke_count,
       e.teaching_quality_rating, e.teaching_quality_comments, e.overall_judgement,
       e.cancelled_session, e.ai_raw_output, e.engagement_id, e.attendance_snapshot_id,
       l.subject, l.module, l.session_date,
       sel.actual_start, d.source_fingerprint AS document_fingerprint,
       eng.source_fingerprint AS engagement_fingerprint, eng.item7_override_applied
  FROM public.lecture_qa_evaluations e
  JOIN public.lecture_sessions l ON l.lecture_id = e.lecture_id
  LEFT JOIN public.lecture_transcript_selections sel ON sel.selection_id = e.selection_id
  LEFT JOIN public.lecture_transcript_documents d ON d.document_id = e.document_id
  LEFT JOIN public.lecture_engagement_metrics eng ON eng.engagement_id = e.engagement_id
 WHERE l.session_date = %s
   AND e.attendance_roster_version = %s
   AND e.qa_engine_version = %s
 ORDER BY l.scheduled_start, l.subject
"""

LOAD_CUES = """
SELECT cue_id, cue_index, start_ms, end_ms, speaker_label_raw, text
  FROM public.lecture_transcript_cues
 WHERE document_id = %s
 ORDER BY start_ms, end_ms, cue_index
"""

LOAD_EVALUATION_CHECKLIST = """
SELECT checklist_order, checklist_item, status, ai_status, status_source, reasoning
  FROM public.lecture_qa_checklist_items
 WHERE evaluation_id = %s
 ORDER BY checklist_order
"""

LOAD_EVIDENCE_CLIPS = """
SELECT clip_id, clip_source, source_position, clip_index, start_text, end_text,
       start_ms, end_ms, speaker_label, validation_status
  FROM public.lecture_qa_evidence_clips
 WHERE evaluation_id = %s
 ORDER BY clip_source, source_position, clip_index
"""

# Phase 2C4 participation, for the deterministic Item 7 evidence. The display
# names come from the FROZEN Phase 2C3 snapshot, never from live attendance.
LOAD_PARTICIPANTS = """
SELECT p.participation_status, m.display_name_raw, p.first_speaker_id,
       sp.speaker_label_raw, sp.gross_spoken_ms
  FROM public.lecture_engagement_participants p
  JOIN public.lecture_attendance_snapshot_members m ON m.member_id = p.snapshot_member_id
  LEFT JOIN public.lecture_transcript_speakers sp ON sp.speaker_id = p.first_speaker_id
 WHERE p.engagement_id = %s
 ORDER BY p.participation_status, m.display_name_normalized, m.member_id
"""

# Legacy `Get LMS Students`, ported verbatim in behaviour: the same
# normalization on both sides, the active-programme filter, the 500-row cap
# applied to the filtered rows ordered by FullName, and only then the distinct
# (ID, FullName) pairs. SELECT only.
LOAD_LMS_STUDENTS = """
WITH p AS (
  SELECT %s::text AS module_raw,
         lower(btrim(regexp_replace(replace(%s::text, '&amp;', '&'), '\\s+', ' ', 'g')))
             AS module_norm
),
filtered AS (
  SELECT k."ID" AS id, k."FullName" AS full_name
    FROM public.kbc_users_data k
    CROSS JOIN p
   WHERE lower(btrim(regexp_replace(replace(coalesce(k."Group", ''), '&amp;', '&'),
                                    '\\s+', ' ', 'g'))) = p.module_norm
     AND lower(coalesce(k."Program-Status", '')) = 'active'
   ORDER BY k."FullName"
   LIMIT 500
)
SELECT (SELECT count(*) FROM filtered) AS source_row_count,
       f.id, f.full_name
  FROM filtered f
 WHERE f.id IS NOT NULL AND f.full_name IS NOT NULL AND btrim(f.full_name) <> ''
"""

LOAD_LEGACY_SESSIONS = """
SELECT q.subject, q.session_id, q.meeting_id, q.trainer, q.date::text, q.duration,
       q.duration_score, q."Engagement", q.engagement_score, q.met_count,
       q.partial_count, q.not_met_count, q.teaching_quality_rating,
       q.cancelled_session, q.lms_module, q.lms_students_count, q.lms_students
  FROM public.qa_doctors_sessions q
 WHERE q.date = %s
 ORDER BY q.subject
"""

LOAD_LEGACY_CHECKLIST = """
SELECT q.subject, ci.checklist_order, ci.checklist_item, ci.status,
       ci.session_id_match, ci.evidence
  FROM public.qa_doctors_checklist_items ci
  JOIN public.qa_doctors_sessions q ON q.session_id = ci.session_id
 WHERE q.date = %s
 ORDER BY q.subject, ci.checklist_order
"""


class RenderInputRepository:
    """READ-ONLY assembly of everything the renderer consumes."""

    def load_inputs(self, connection, target_date, *, attendance_roster_version,
                    qa_engine_version) -> list[dict]:
        try:
            rows = connection.execute(LOAD_RENDER_INPUTS, (
                target_date, attendance_roster_version, qa_engine_version)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "render input query failed") from exc
        names = ("evaluation_id", "lecture_id", "document_id", "qa_status", "delivery_status",
                 "evaluation_fingerprint", "primary_provider_transcript_id", "meeting_id",
                 "canonical_trainer", "duration_minutes", "duration_text", "duration_score",
                 "engagement_percentage", "engagement_score", "attended_count", "spoke_count",
                 "teaching_quality_rating", "teaching_quality_comments", "overall_judgement",
                 "cancelled_session", "ai_raw_output", "engagement_id",
                 "attendance_snapshot_id", "subject", "module", "session_date",
                 "actual_start", "document_fingerprint", "engagement_fingerprint",
                 "item7_override_applied")
        return [dict(zip(names, row)) for row in rows]

    def load_cues(self, connection, document_id) -> list[dict]:
        try:
            rows = connection.execute(LOAD_CUES, (document_id,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "cue query failed") from exc
        return [{"cue_id": row[0], "cue_index": row[1], "start_ms": row[2],
                 "end_ms": row[3], "speaker_label_raw": row[4], "text": row[5]}
                for row in rows]

    def load_checklist(self, connection, evaluation_id) -> list[dict]:
        try:
            rows = connection.execute(LOAD_EVALUATION_CHECKLIST, (evaluation_id,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "checklist query failed") from exc
        return [{"checklist_order": row[0], "checklist_item": row[1], "status": row[2],
                 "ai_status": row[3], "status_source": row[4], "reasoning": row[5]}
                for row in rows]

    def load_clips(self, connection, evaluation_id) -> list[dict]:
        try:
            rows = connection.execute(LOAD_EVIDENCE_CLIPS, (evaluation_id,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "evidence clip query failed") from exc
        return [{"clip_id": row[0], "clip_source": row[1], "source_position": row[2],
                 "clip_index": row[3], "start_text": row[4], "end_text": row[5],
                 "start_ms": row[6], "end_ms": row[7], "speaker_label": row[8],
                 "validation_status": row[9]} for row in rows]

    def load_participants(self, connection, engagement_id) -> list[dict]:
        if engagement_id is None:
            return []
        try:
            rows = connection.execute(LOAD_PARTICIPANTS, (engagement_id,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "participant query failed") from exc
        return [{"participation_status": row[0], "display_name_raw": row[1],
                 "first_speaker_id": row[2], "speaker_label_raw": row[3],
                 "gross_spoken_ms": row[4]} for row in rows]


class LmsSourceRepository:
    """READ-ONLY gateway to the externally owned public.kbc_users_data."""

    def load_students(self, connection, module_raw: str) -> dict:
        try:
            rows = connection.execute(LOAD_LMS_STUDENTS, (module_raw, module_raw)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "LMS roster query failed") from exc
        source_row_count = rows[0][0] if rows else 0
        # DISTINCT over (id, full_name), as the legacy `pairs` CTE did.
        pairs = {(row[1], row[2]) for row in rows}
        members = [{"external_learner_id": int(identifier), "full_name": name}
                   for identifier, name in pairs]
        return {"source_row_count": source_row_count, "members": members}


class LmsSnapshotRepository:
    def find_latest(self, connection, lecture_id, version) -> dict | None:
        try:
            row = connection.execute("""
            SELECT snapshot_id, source_fingerprint, module, module_normalized,
                   source_row_count, student_count
              FROM public.lecture_lms_snapshots
             WHERE lecture_id = %s AND lms_snapshot_version = %s
             ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1
            """, (lecture_id, version)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "LMS snapshot lookup failed") from exc
        if row is None:
            return None
        return {"snapshot_id": row[0], "source_fingerprint": row[1], "module": row[2],
                "module_normalized": row[3], "source_row_count": row[4],
                "student_count": row[5]}

    def load_members(self, connection, snapshot_id) -> list[dict]:
        try:
            rows = connection.execute("""
            SELECT external_learner_id, full_name
              FROM public.lecture_lms_snapshot_members
             WHERE snapshot_id = %s ORDER BY full_name, external_learner_id
            """, (snapshot_id,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "LMS member query failed") from exc
        return [{"external_learner_id": row[0], "full_name": row[1]} for row in rows]

    def upsert(self, connection, snapshot, members) -> dict:
        existing = connection.execute(
            "SELECT snapshot_id FROM public.lecture_lms_snapshots WHERE snapshot_id = %s",
            (snapshot["snapshot_id"],)).fetchone()
        if existing is not None:
            return {"created": 0, "reused": 1}
        try:
            connection.execute("""
            INSERT INTO public.lecture_lms_snapshots (
                snapshot_id, lecture_id, lms_snapshot_version, module, module_normalized,
                source_row_count, student_count, row_cap_reached, source_fingerprint, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (snapshot_id) DO NOTHING
            """, (snapshot["snapshot_id"], snapshot["lecture_id"],
                  snapshot["lms_snapshot_version"], snapshot["module"],
                  snapshot["module_normalized"], snapshot["source_row_count"],
                  snapshot["student_count"], snapshot["row_cap_reached"],
                  snapshot["source_fingerprint"],
                  json.dumps(snapshot.get("metadata", {}), default=str)))
            if members:
                with connection.cursor() as cursor:
                    cursor.executemany("""
                    INSERT INTO public.lecture_lms_snapshot_members (
                        snapshot_member_id, snapshot_id, external_learner_id, full_name
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (snapshot_member_id) DO NOTHING
                    """, [(uuid.uuid5(snapshot["snapshot_id"],
                                      f"{member['external_learner_id']}|{member['full_name']}"),
                           snapshot["snapshot_id"], member["external_learner_id"],
                           member["full_name"]) for member in members])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "LMS snapshot write failed") from exc
        return {"created": 1, "reused": 0}


class RenderedOutputRepository:
    COLUMNS = ("lecture_id", "evaluation_id", "lms_snapshot_id", "renderer_version",
               "render_status", "session_id", "meeting_id", "subject", "trainer",
               "legacy_date", "canonical_session_date", "duration", "duration_score",
               "engagement", "engagement_score", "attended_count", "spoke_count",
               "met_count", "partial_count", "not_met_count", "teaching_quality_rating",
               "teaching_quality_comments", "teaching_quality_evidence",
               "overall_judgement", "cancelled_session", "lms_module",
               "lms_students_count", "evidence_clip_count", "rendered_block_count",
               "source_fingerprint")
    JSON_COLUMNS = ("lms_students", "strengths", "areas_for_development", "ksb_coverage",
                    "metadata")

    def find_by_fingerprint(self, connection, source_fingerprint):
        try:
            row = connection.execute(
                "SELECT rendered_session_id FROM public.lecture_qa_rendered_sessions "
                " WHERE source_fingerprint = %s", (source_fingerprint,)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "rendered session lookup failed") from exc
        return row[0] if row else None

    def upsert(self, connection, session, checklist) -> dict:
        existing = self.find_by_fingerprint(connection, session["source_fingerprint"])
        rendered_id = existing or session["rendered_session_id"]
        columns = ", ".join(self.COLUMNS + self.JSON_COLUMNS)
        placeholders = ", ".join(["%s"] * len(self.COLUMNS)
                                 + ["%s::jsonb"] * len(self.JSON_COLUMNS))
        updates = ", ".join(f"{name} = EXCLUDED.{name}"
                            for name in self.COLUMNS + self.JSON_COLUMNS
                            if name != "source_fingerprint")
        try:
            connection.execute(f"""
            INSERT INTO public.lecture_qa_rendered_sessions (rendered_session_id, {columns})
            VALUES (%s, {placeholders})
            ON CONFLICT (source_fingerprint) DO UPDATE SET {updates}, updated_at = now()
            """, (rendered_id,
                  *[session.get(name) for name in self.COLUMNS],
                  *[json.dumps(session.get(name), default=str)
                    if session.get(name) is not None else None
                    for name in self.JSON_COLUMNS]))
            connection.execute(
                "DELETE FROM public.lecture_qa_rendered_checklist_items "
                " WHERE rendered_session_id = %s", (rendered_id,))
            if checklist:
                with connection.cursor() as cursor:
                    cursor.executemany("""
                    INSERT INTO public.lecture_qa_rendered_checklist_items (
                        rendered_item_id, rendered_session_id, session_id, session_id_match,
                        checklist_order, checklist_item, status, evidence, severity,
                        rendered_evidence, reasoning, ai_status, status_source,
                        evidence_clip_ids, cue_ids, rendered_block_count
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, [(uuid.uuid5(rendered_id, f"item:{row['checklist_order']}"),
                           rendered_id, row["session_id"], row["session_id_match"],
                           row["checklist_order"], row["checklist_item"], row["status"],
                           row["evidence"], row["severity"], row["rendered_evidence"],
                           row["reasoning"], row["ai_status"], row["status_source"],
                           row["evidence_clip_ids"], row["cue_ids"],
                           row["rendered_block_count"]) for row in checklist])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "rendered output write failed") from exc
        return {"rendered_session_id": rendered_id,
                "created": 0 if existing else 1, "reused": 1 if existing else 0}


class LegacyRenderComparisonRepository:
    """READ-ONLY legacy values for the parity report; never written."""

    def load_sessions(self, connection, target_date) -> list[dict]:
        try:
            rows = connection.execute(LOAD_LEGACY_SESSIONS, (target_date,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy session query failed") from exc
        names = ("subject", "session_id", "meeting_id", "trainer", "date", "duration",
                 "duration_score", "engagement", "engagement_score", "met_count",
                 "partial_count", "not_met_count", "teaching_quality_rating",
                 "cancelled_session", "lms_module", "lms_students_count", "lms_students")
        return [dict(zip(names, row)) for row in rows]

    def load_checklist(self, connection, target_date) -> dict:
        try:
            rows = connection.execute(LOAD_LEGACY_CHECKLIST, (target_date,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy checklist query failed") from exc
        grouped: dict = {}
        for subject, order, item, status, match, evidence in rows:
            grouped.setdefault(subject, {})[order] = {
                "checklist_item": item, "status": status,
                "session_id_match": match, "evidence": evidence}
        return grouped


class RenderRunRepository:
    COUNTERS = ("lectures_considered", "sessions_rendered", "sessions_reused",
                "checklist_rows_rendered", "evidence_clips_consumed", "blocks_rendered",
                "lms_snapshots_created", "lms_snapshots_reused", "not_ready_count",
                "provider_calls", "error_count")

    def start(self, connection, target_date, *, renderer_version,
              lms_snapshot_version) -> uuid.UUID:
        run_id = uuid.uuid4()
        try:
            connection.execute(
                "INSERT INTO public.lecture_qa_render_runs (run_id, target_date, status, "
                " renderer_version, lms_snapshot_version) VALUES (%s, %s, 'RUNNING', %s, %s)",
                (run_id, target_date, renderer_version, lms_snapshot_version))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not create render run") from exc
        return run_id

    def complete(self, connection, run_id, summary) -> None:
        assignments = ", ".join(f"{name} = %s" for name in self.COUNTERS)
        values = [int(summary.get(name, 0)) for name in self.COUNTERS]
        try:
            connection.execute(
                "UPDATE public.lecture_qa_render_runs "
                f"SET completed_at = now(), status = %s, {assignments}, metadata = %s::jsonb "
                "WHERE run_id = %s",
                [summary["status"], *values,
                 json.dumps(summary.get("metadata", {}), default=str), run_id])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not complete render run") from exc
