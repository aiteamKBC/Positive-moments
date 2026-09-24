"""
Phase 3C1 persistence: the legacy QA writer.

READ-ONLY: the Phase 3B rendered payload, and the legacy target rows.
WRITES (coded-platform only): lecture_qa_legacy_writes.

The legacy write statements live here too, but they are reachable only through
a write-enabled mode; `plan_only=True` (the default everywhere in Phase 3C1)
means nothing in this module executes an INSERT or UPDATE against a
qa_doctors_* table.

Only the 22 mapped session columns are ever written. The clips_* and
recording_* columns, owned by other workflows, are neither set nor cleared.
"""
import json
import uuid

from psycopg import sql

from app.common.errors import DATABASE_ERROR, PlatformError
from app.qa.generation_budget import attempt_row, generation_budget
from app.writer.mapping import (
    CHECKLIST_COLUMNS,
    LEGACY_CHECKLIST_KEY,
    LEGACY_SESSION_KEY,
    SESSION_COLUMNS,
    SESSION_JSON_COLUMNS,
)


LOAD_RENDERED = """
SELECT rs.rendered_session_id, rs.lecture_id, rs.evaluation_id, rs.render_status,
       rs.source_fingerprint, rs.session_id, rs.meeting_id, rs.subject, rs.trainer,
       rs.legacy_date, rs.canonical_session_date, rs.duration, rs.duration_score,
       rs.engagement, rs.engagement_score, rs.attended_count, rs.met_count, rs.partial_count,
       rs.not_met_count, rs.teaching_quality_rating, rs.teaching_quality_comments,
       rs.overall_judgement, rs.cancelled_session, rs.lms_module, rs.lms_students_count,
       rs.lms_students, rs.strengths, rs.areas_for_development, rs.ksb_coverage,
       e.qa_status, l.session_date
  FROM public.lecture_qa_rendered_sessions rs
  JOIN public.lecture_qa_evaluations e ON e.evaluation_id = rs.evaluation_id
  JOIN public.lecture_sessions l ON l.lecture_id = rs.lecture_id
 WHERE l.session_date = %s AND rs.renderer_version = %s
   -- A render of a SUPERSEDED answer is history, not a payload: once a
   -- strictly newer evaluation exists for the lecture - a NON_DELIVERED later
   -- replaced by a coverage review, say - it must never be written or judged
   -- for Perfect again. Strictly newer, deliberately: two evaluations with the
   -- same timestamp are indistinguishable, and guessing between them on a
   -- uuid would be worse than the behaviour this refines.
   AND NOT EXISTS (
         SELECT 1
           FROM public.lecture_qa_evaluations e2
          WHERE e2.lecture_id = rs.lecture_id
            AND e2.updated_at > e.updated_at)
 ORDER BY l.scheduled_start, l.subject
"""

# The lecture-scoped variant. A SEPARATE statement, for the same reason every
# other lecture-scoped loader is one: the scope belongs to the contract, so it
# must be impossible to reach the date-wide query by leaving an argument unset.
# Newest render first, so a deterministic refresh's payload is the one used.
LOAD_RENDERED_FOR_LECTURE = (
    LOAD_RENDERED
    .replace(" WHERE l.session_date = %s AND rs.renderer_version = %s",
             " WHERE rs.lecture_id = %s AND rs.renderer_version = %s")
    .replace(" ORDER BY l.scheduled_start, l.subject",
             " ORDER BY rs.updated_at DESC, rs.rendered_session_id DESC"))

LOAD_RENDERED_ITEMS = """
SELECT checklist_order, checklist_item, status, session_id, session_id_match, evidence
  FROM public.lecture_qa_rendered_checklist_items
 WHERE rendered_session_id = %s
 ORDER BY checklist_order
"""


class RenderedPayloadRepository:
    """READ-ONLY access to the Phase 3B payload the writer would persist."""

    def is_deterministic_refresh_of(self, connection, published_evaluation_id,
                                    current_evaluation_id) -> bool:
        """
        Is the current evaluation a deterministic refresh that reuses the
        published evaluation's stored model answer - same lecture, same
        provider question, byte-identical output?

        This is what makes a late-attendance update a deterministic change
        rather than a new verdict: the AI analysis is the one already
        published, and only the fields attendance supplies can differ. A
        DIFFERENT evaluation is required, stamped by the refresh itself, so a
        regeneration written over the published evaluation in place can never
        pass by being compared with itself.
        """
        try:
            row = connection.execute("""
            SELECT p.lecture_id = c.lecture_id
               AND p.evaluation_id <> c.evaluation_id
               AND c.metadata ? 'deterministic_refresh'
               AND p.ai_raw_output IS NOT NULL
               AND p.ai_raw_output = c.ai_raw_output
               AND p.metadata ->> 'model_input_fingerprint' IS NOT NULL
               AND p.metadata ->> 'model_input_fingerprint'
                   = c.metadata ->> 'model_input_fingerprint'
              FROM public.lecture_qa_evaluations p, public.lecture_qa_evaluations c
             WHERE p.evaluation_id = %s AND c.evaluation_id = %s
            """, (published_evaluation_id, current_evaluation_id)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "model answer comparison failed") from exc
        return bool(row and row[0])

    def load_sessions(self, connection, target_date, renderer_version) -> list[dict]:
        try:
            rows = connection.execute(LOAD_RENDERED, (target_date, renderer_version)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "rendered payload query failed") from exc
        names = ("rendered_session_id", "lecture_id", "evaluation_id", "render_status",
                 "source_fingerprint", "session_id", "meeting_id", "subject", "trainer",
                 "legacy_date", "canonical_session_date", "duration", "duration_score",
                 "engagement", "engagement_score", "attended_count", "met_count",
                 "partial_count", "not_met_count", "teaching_quality_rating", "teaching_quality_comments",
                 "overall_judgement", "cancelled_session", "lms_module",
                 "lms_students_count", "lms_students", "strengths",
                 "areas_for_development", "ksb_coverage", "qa_status", "session_date")
        return [dict(zip(names, row)) for row in rows]

    def load_sessions_for_lecture(self, connection, lecture_id,
                                  renderer_version) -> list[dict]:
        """EXACTLY one lecture's rendered payloads, newest first."""
        try:
            rows = connection.execute(
                LOAD_RENDERED_FOR_LECTURE, (lecture_id, renderer_version)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR,
                                "lecture rendered payload query failed") from exc
        names = ("rendered_session_id", "lecture_id", "evaluation_id", "render_status",
                 "source_fingerprint", "session_id", "meeting_id", "subject", "trainer",
                 "legacy_date", "canonical_session_date", "duration", "duration_score",
                 "engagement", "engagement_score", "attended_count", "met_count",
                 "partial_count", "not_met_count", "teaching_quality_rating",
                 "teaching_quality_comments", "overall_judgement", "cancelled_session",
                 "lms_module", "lms_students_count", "lms_students", "strengths",
                 "areas_for_development", "ksb_coverage", "qa_status", "session_date")
        return [dict(zip(names, row)) for row in rows]

    def load_items(self, connection, rendered_session_id) -> list[dict]:
        try:
            rows = connection.execute(LOAD_RENDERED_ITEMS, (rendered_session_id,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "rendered checklist query failed") from exc
        return [{"checklist_order": row[0], "checklist_item": row[1], "status": row[2],
                 "session_id": row[3], "session_id_match": row[4], "evidence": row[5]}
                for row in rows]


class LegacyQaTargetRepository:
    """
    The legacy tables. Reads are unconditional; writes are reachable only from
    a write-enabled mode and are refused otherwise.
    """

    def load_session(self, connection, session_id) -> dict | None:
        columns = sql.SQL(", ").join(sql.Identifier(name) for name in SESSION_COLUMNS)
        statement = sql.SQL("SELECT {} FROM public.qa_doctors_sessions WHERE {} = %s").format(
            columns, sql.Identifier(LEGACY_SESSION_KEY))
        try:
            row = connection.execute(statement, (session_id,)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy session read failed") from exc
        return dict(zip(SESSION_COLUMNS, row)) if row else None

    def load_foreign_columns(self, connection, session_id) -> dict | None:
        """
        Every column of the legacy row the coded writer does NOT own.

        Read generically (`to_jsonb`) rather than from a list, so a column
        another system adds tomorrow is covered by the same check without
        anyone remembering to add it here.
        """
        try:
            row = connection.execute(
                "SELECT to_jsonb(s) FROM public.qa_doctors_sessions s "
                " WHERE s.session_id = %s", (session_id,)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy session read failed") from exc
        if row is None:
            return None
        return {name: value for name, value in row[0].items()
                if name not in SESSION_COLUMNS}

    def current_evaluation(self, connection, lecture_id) -> dict | None:
        """The lecture's CURRENT evaluation, by the recovery_state rule."""
        try:
            rows = connection.execute("""
            SELECT e.evaluation_id, e.qa_status, e.review_reason, e.updated_at
              FROM public.lecture_qa_evaluations e
             WHERE e.lecture_id = %s
             ORDER BY e.updated_at DESC, e.evaluation_id DESC
             LIMIT 2
            """, (lecture_id,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "current evaluation read failed") from exc
        if not rows:
            return None
        row = rows[0]
        return {"evaluation_id": str(row[0]), "qa_status": row[1], "review_reason": row[2],
                # Two evaluations with the same timestamp: which is current is
                # not knowable, and a deletion must never rest on a coin toss.
                "ambiguous": len(rows) > 1 and rows[1][3] == row[3]}

    def coded_perfect_writes(self, connection, lecture_id) -> int:
        try:
            row = connection.execute("""
            SELECT count(*) FROM public.lecture_perfect_lecture_legacy_writes p
             WHERE p.lecture_id = %s AND p.write_status IN ('WRITTEN', 'UPDATED')
            """, (lecture_id,)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "perfect ownership read failed") from exc
        return int(row[0])

    def load_checklist(self, connection, session_id) -> list[dict]:
        columns = sql.SQL(", ").join(sql.Identifier(name) for name in CHECKLIST_COLUMNS)
        statement = sql.SQL(
            "SELECT {} FROM public.qa_doctors_checklist_items WHERE {} = %s "
            " ORDER BY checklist_order").format(columns, sql.Identifier("session_id"))
        try:
            rows = connection.execute(statement, (session_id,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy checklist read failed") from exc
        return [dict(zip(CHECKLIST_COLUMNS, row)) for row in rows]

    def write_session(self, connection, row: dict, *, allow_write: bool) -> None:
        """
        Upsert the 22 mapped columns on the legacy session key.

        Refuses outright unless the caller proved a write-enabled mode. The
        update list deliberately excludes every column this writer does not
        own, so a clips or recording value written by another workflow is
        never overwritten or cleared.
        """
        if not allow_write:
            raise PlatformError(DATABASE_ERROR,
                                "legacy session write attempted without a write-enabled mode")
        updatable = [name for name in SESSION_COLUMNS if name != LEGACY_SESSION_KEY]
        statement = sql.SQL(
            "INSERT INTO public.qa_doctors_sessions ({columns}) VALUES ({values}) "
            "ON CONFLICT ({key}) DO UPDATE SET {updates}").format(
            columns=sql.SQL(", ").join(sql.Identifier(name) for name in SESSION_COLUMNS),
            values=sql.SQL(", ").join(
                sql.SQL("{}::jsonb").format(sql.Placeholder()) if name in SESSION_JSON_COLUMNS
                else sql.Placeholder() for name in SESSION_COLUMNS),
            key=sql.Identifier(LEGACY_SESSION_KEY),
            updates=sql.SQL(", ").join(
                sql.SQL("{name} = EXCLUDED.{name}").format(name=sql.Identifier(name))
                for name in updatable))
        values = [json.dumps(row[name], default=str) if name in SESSION_JSON_COLUMNS
                  and row[name] is not None else row[name] for name in SESSION_COLUMNS]
        try:
            connection.execute(statement, values)
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy session write failed") from exc

    def write_checklist(self, connection, rows, *, allow_write: bool) -> None:
        if not allow_write:
            raise PlatformError(DATABASE_ERROR,
                                "legacy checklist write attempted without a write-enabled mode")
        statement = sql.SQL(
            "INSERT INTO public.qa_doctors_checklist_items ({columns}) VALUES ({values}) "
            "ON CONFLICT ({key}) DO UPDATE SET {updates}").format(
            columns=sql.SQL(", ").join(sql.Identifier(name) for name in CHECKLIST_COLUMNS),
            values=sql.SQL(", ").join(sql.Placeholder() for _ in CHECKLIST_COLUMNS),
            key=sql.Identifier(LEGACY_CHECKLIST_KEY),
            updates=sql.SQL(", ").join(
                sql.SQL("{name} = EXCLUDED.{name}").format(name=sql.Identifier(name))
                for name in CHECKLIST_COLUMNS if name != LEGACY_CHECKLIST_KEY))
        try:
            with connection.cursor() as cursor:
                cursor.executemany(statement, [[row[name] for name in CHECKLIST_COLUMNS]
                                               for row in rows])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy checklist write failed") from exc

    def delete_owned_session(self, connection, session_id, *, allow_write: bool) -> None:
        """
        Rollback helper for a session this writer created.

        The caller must already have proven ownership; this method cannot be
        reached for a legacy-owned row. The checklist rows cascade.
        """
        if not allow_write:
            raise PlatformError(DATABASE_ERROR, "rollback attempted without a write-enabled mode")
        try:
            connection.execute("DELETE FROM public.qa_doctors_sessions WHERE session_id = %s",
                               (session_id,))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy rollback failed") from exc


class WriterOwnershipRepository:
    """The coded platform's own ownership and audit record."""

    def find(self, connection, legacy_session_id, writer_version) -> dict | None:
        try:
            row = connection.execute("""
            SELECT write_id, lecture_id, source_fingerprint, write_status, write_mode,
                   post_write_digest, evaluation_id, legacy_session_id
              FROM public.lecture_qa_legacy_writes
             WHERE legacy_session_id = %s AND writer_version = %s
            """, (legacy_session_id, writer_version)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "writer ownership lookup failed") from exc
        if row is None:
            return None
        return {"write_id": row[0], "lecture_id": row[1], "source_fingerprint": row[2],
                "write_status": row[3], "write_mode": row[4], "post_write_digest": row[5],
                "evaluation_id": row[6], "legacy_session_id": row[7]}

    def record(self, connection, entry: dict) -> dict:
        """
        Claim or refresh ownership of one legacy session.

        The unique key on (legacy_session_id, writer_version) is what makes two
        concurrent writers safe: the second one conflicts instead of creating a
        second owner.
        """
        try:
            row = connection.execute("""
            INSERT INTO public.lecture_qa_legacy_writes (
                write_id, lecture_id, evaluation_id, rendered_session_id, legacy_session_id,
                writer_version, source_fingerprint, write_mode, write_status,
                legacy_session_created, legacy_session_updated, checklist_rows_created,
                checklist_rows_updated, pre_write_digest, post_write_digest, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (legacy_session_id, writer_version) DO UPDATE SET
                rendered_session_id = EXCLUDED.rendered_session_id,
                evaluation_id = EXCLUDED.evaluation_id,
                source_fingerprint = EXCLUDED.source_fingerprint,
                write_mode = EXCLUDED.write_mode,
                write_status = EXCLUDED.write_status,
                legacy_session_updated = EXCLUDED.legacy_session_updated,
                checklist_rows_updated = EXCLUDED.checklist_rows_updated,
                pre_write_digest = EXCLUDED.pre_write_digest,
                post_write_digest = EXCLUDED.post_write_digest,
                metadata = EXCLUDED.metadata, updated_at = now(), written_at = now()
            RETURNING write_id, (xmax = 0) AS inserted
            """, (entry["write_id"], entry["lecture_id"], entry["evaluation_id"],
                  entry["rendered_session_id"], entry["legacy_session_id"],
                  entry["writer_version"], entry["source_fingerprint"], entry["write_mode"],
                  entry["write_status"], entry["legacy_session_created"],
                  entry["legacy_session_updated"], entry["checklist_rows_created"],
                  entry["checklist_rows_updated"], entry["pre_write_digest"],
                  entry["post_write_digest"],
                  json.dumps(entry.get("metadata", {}), default=str))).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "writer ownership write failed") from exc
        return {"write_id": row[0], "created": bool(row[1])}

    def live_writes_for_lecture(self, connection, lecture_id, writer_version) -> list[dict]:
        """This lecture's coded-owned legacy rows that are still published."""
        try:
            rows = connection.execute("""
            SELECT write_id, legacy_session_id, evaluation_id, write_status,
                   post_write_digest
              FROM public.lecture_qa_legacy_writes
             WHERE lecture_id = %s AND writer_version = %s
               AND write_status IN ('WRITTEN', 'UPDATED')
             ORDER BY written_at DESC, write_id DESC
            """, (lecture_id, writer_version)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "writer ownership lookup failed") from exc
        return [{"write_id": row[0], "legacy_session_id": row[1],
                 "evaluation_id": str(row[2]), "write_status": row[3],
                 "post_write_digest": row[4]} for row in rows]

    def mark_status(self, connection, write_id, status, metadata=None) -> None:
        try:
            connection.execute("""
            UPDATE public.lecture_qa_legacy_writes
               SET write_status = %s, updated_at = now(),
                   metadata = coalesce(%s::jsonb, metadata)
             WHERE write_id = %s
            """, (status, json.dumps(metadata, default=str) if metadata else None, write_id))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "writer status update failed") from exc


class GenerationAttemptRepository:
    """Append-only model-generation attempts, for the bounded retry policy."""

    def count(self, connection, source_fingerprint) -> int:
        """Attempt RECORDS for the fingerprint - the ledger's numbering, not the budget."""
        try:
            return connection.execute(
                "SELECT count(*) FROM public.lecture_qa_generation_attempts "
                " WHERE source_fingerprint = %s", (source_fingerprint,)).fetchone()[0]
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "generation attempt count failed") from exc

    def budget(self, connection, source_fingerprint, *, max_generations: int) -> dict:
        """
        The generation budget spent on this fingerprint, under the shared
        policy in `app.qa.generation_budget`. Reads only.
        """
        try:
            rows = connection.execute("""
            SELECT a.outcome, a.error_code, a.metadata ->> 'consumes_generation_budget',
                   a.metadata ->> 'generation_budget_used'
              FROM public.lecture_qa_generation_attempts a
             WHERE a.source_fingerprint = %s
             ORDER BY a.generation_number
            """, (source_fingerprint,)).fetchall()
            evaluation = connection.execute("""
            SELECT q.error_code, q.metadata ->> 'http_status', q.ai_raw_output IS NOT NULL
              FROM public.lecture_qa_evaluations AS q
             WHERE q.source_fingerprint = %s
            """, (source_fingerprint,)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "generation budget read failed") from exc
        return generation_budget(
            [attempt_row(*row) for row in rows],
            ({"error_code": evaluation[0], "http_status": evaluation[1],
              "has_model_output": evaluation[2]} if evaluation else None),
            max_generations=max_generations)

    def record(self, connection, entry: dict) -> int:
        """Append one attempt and return its generation number."""
        next_number = self.count(connection, entry["source_fingerprint"]) + 1
        try:
            connection.execute("""
            INSERT INTO public.lecture_qa_generation_attempts (
                attempt_id, lecture_id, source_fingerprint, qa_engine_version,
                prompt_version, model_name, generation_number, outcome, error_code,
                structured_output_error_count, invalid_evidence_clip_count, forced, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """, (uuid.uuid4(), entry.get("lecture_id"), entry["source_fingerprint"],
                  entry["qa_engine_version"], entry["prompt_version"], entry["model_name"],
                  next_number, entry["outcome"], entry.get("error_code"),
                  entry.get("structured_output_error_count", 0),
                  entry.get("invalid_evidence_clip_count", 0), bool(entry.get("forced")),
                  json.dumps(entry.get("metadata", {}), default=str)))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "generation attempt write failed") from exc
        return next_number
