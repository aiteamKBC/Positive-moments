"""
Phase 3A persistence: the shadow QA engine.

READ-ONLY inputs, all already validated by earlier phases: lecture_sessions,
lecture_transcript_selections, lecture_combined_transcripts,
lecture_transcript_documents, lecture_transcript_cues,
lecture_transcript_speakers / _roles, the Phase 2C3 v2 attendance snapshot and
the Phase 2C4 engagement result. qa_doctors_sessions and
qa_doctors_checklist_items are read for comparison only.

WRITES ONLY: lecture_qa_evaluations, lecture_qa_checklist_items,
lecture_qa_evidence_clips, lecture_qa_runs.

Absent from every statement here, deliberately: kbc_attendance, kbc_users_data,
aptem_auto_extracting, and any write to a qa_doctors_* table.
"""
import json
import uuid

from app.common.errors import DATABASE_ERROR, PlatformError


# One row per lecture: schedule, selection, combined transcript, canonical
# document, engagement and the deterministic trainer, all pinned to the
# approved roster rule and the validated algorithm versions.
LOAD_QA_INPUTS = """
SELECT l.lecture_id, l.subject, l.module, l.meeting_id,
       l.scheduled_start, l.scheduled_end, l.session_date,
       s.selection_id, s.primary_provider_transcript_id, s.actual_start, s.actual_end,
       s.start_difference_minutes, s.end_difference_minutes,
       cb.combined_id, cb.duration_minutes, cb.duration_seconds, cb.content_sha256,
       cb.content_bytes, cb.source_fingerprint, cb.combined_content,
       d.document_id, d.source_fingerprint, d.cue_count,
       d.first_cue_start_ms, d.last_cue_end_ms,
       e.engagement_id, e.attendance_snapshot_id, e.attended_count, e.spoke_count,
       e.engagement_percentage, e.engagement_score, e.learner_engagement_status,
       e.item7_override_applied, e.calculation_status, e.source_fingerprint,
       sn.attendance_resolution_version,
       tr.speaker_id, tr.speaker_label_raw,
       -- The frozen counts of the SAME snapshot this input is built on, so the
       -- service can ask the coverage question of the evidence it is actually
       -- about to evaluate. Appended, never interleaved: every positional
       -- index above is part of an existing contract.
       sn.source_row_count, sn.present_row_count, sn.effective_member_count,
       (sn.metadata ->> 'source_rows_any_status')::int
  FROM public.lecture_sessions l
  JOIN public.lecture_transcript_selections s ON s.lecture_id = l.lecture_id
  JOIN public.lecture_combined_transcripts cb ON cb.selection_id = s.selection_id
  JOIN public.lecture_transcript_documents d ON d.combined_id = cb.combined_id
  JOIN public.lecture_engagement_metrics e ON e.document_id = d.document_id
  JOIN public.lecture_attendance_snapshots sn ON sn.snapshot_id = e.attendance_snapshot_id
  LEFT JOIN LATERAL (
      SELECT sp.speaker_id, sp.speaker_label_raw
        FROM public.lecture_transcript_speaker_roles r
        JOIN public.lecture_transcript_speakers sp ON sp.speaker_id = r.speaker_id
       WHERE r.attendance_snapshot_id = e.attendance_snapshot_id
         AND r.resolver_version = e.resolver_version
         AND r.role_algorithm_version = e.role_algorithm_version
         AND r.role = 'TRAINER_CANDIDATE'
         -- One attendance snapshot can serve more than one canonical document
         -- version (Phase 3C2.3B), and each has its own top speaker. The
         -- trainer must come from the SAME document as the rest of this input,
         -- otherwise the row fans out and the ambiguity guard stops the run.
         AND sp.document_id = d.document_id
       ORDER BY sp.speaker_id
  ) tr ON true
 WHERE l.session_date = %s
   AND d.parser_version = %s
   -- Document lineage. A reselection rewrites the lecture's selection and
   -- combined row IN PLACE (their ids are derived from the lecture), so a
   -- document parsed from the SUPERSEDED combined bytes still joins on
   -- combined_id. Only the document parsed from the bytes the combined row
   -- holds NOW is a current QA input; the old one stays on record as history.
   AND d.source_content_sha256 = cb.content_sha256
   AND sn.attendance_resolution_version = %s
   AND e.resolver_version = %s
   AND e.role_algorithm_version = %s
   AND e.engagement_algorithm_version = %s
   -- Phase 3C3E. A lecture can legitimately hold more than one attendance
   -- snapshot once attendance recovery exists: the SOURCE_MISSING one it was
   -- first processed under, and the authoritative one that arrived later.
   -- Snapshots are content-addressed and immutable, so "current" is the
   -- newest - the SAME rule Phase 2C4 uses to pick the current snapshot and
   -- the same rule the coverage reader uses. Without this the loader returns
   -- two rows for one lecture and the ambiguity guard stops a lecture that is
   -- not actually ambiguous, only versioned.
   AND sn.snapshot_id = (
         SELECT sn2.snapshot_id
           FROM public.lecture_attendance_snapshots sn2
          WHERE sn2.lecture_id = l.lecture_id
            AND sn2.attendance_resolution_version = sn.attendance_resolution_version
          ORDER BY sn2.created_at DESC, sn2.snapshot_id DESC
          LIMIT 1)
 ORDER BY l.scheduled_start, l.subject
"""

# The lecture-scoped variant, a SEPARATE statement for the same reason the
# engagement loader keeps one: the scope is part of the contract, so it must be
# impossible to reach the date-wide query by leaving an argument unset. There
# is no session_date predicate here at all.
LOAD_QA_INPUTS_FOR_LECTURE = LOAD_QA_INPUTS.replace(
    " WHERE l.session_date = %s", " WHERE l.lecture_id = %s")

LOAD_CUES = """
SELECT start_ms, end_ms FROM public.lecture_transcript_cues
 WHERE document_id = %s ORDER BY start_ms, end_ms
"""

# Legacy values for read-only comparison. Item text is not needed, and the
# evidence body is never selected: it contains learner names.
LOAD_LEGACY_QA = """
SELECT q.subject, q.session_id, q.meeting_id, q.trainer, q.duration,
       q.duration_score, q."Engagement", q.engagement_score,
       q.met_count, q.partial_count, q.not_met_count,
       q.teaching_quality_rating, q.cancelled_session
  FROM public.qa_doctors_sessions q
 WHERE q.date = %s
 ORDER BY q.subject
"""

# Only the numbers are extracted, inside PostgreSQL: the Item 7 evidence body
# contains learner names and must not cross the wire.
LOAD_LEGACY_ENGAGEMENT_COUNTS = """
SELECT q.subject,
       (regexp_match(ci.evidence, '^Engagement score = (\\d+) / (\\d+) = '))[1]::int,
       (regexp_match(ci.evidence, '^Engagement score = (\\d+) / (\\d+) = '))[2]::int
  FROM public.qa_doctors_sessions q
  JOIN public.qa_doctors_checklist_items ci
    ON ci.session_id = q.session_id AND ci.checklist_order = 7
 WHERE q.date = %s
"""

# Legacy's own punctuality numbers, taken from the Item 2 text it wrote. Only
# the integers are extracted, inside PostgreSQL. Both spacings the legacy model
# produced are matched ("= -181" and "=-192").
LOAD_LEGACY_TIMING = """
SELECT q.subject,
       (regexp_match(ci.evidence, 'startDifferenceMinutes\\s*=\\s*(-?\\d+)'))[1]::int,
       (regexp_match(ci.evidence, 'endDifferenceMinutes\\s*=\\s*(-?\\d+)'))[1]::int
  FROM public.qa_doctors_sessions q
  JOIN public.qa_doctors_checklist_items ci
    ON ci.session_id = q.session_id AND ci.checklist_order = 2
 WHERE q.date = %s
"""

LOAD_LEGACY_CHECKLIST = """
SELECT q.subject, ci.checklist_order, ci.status
  FROM public.qa_doctors_checklist_items ci
  JOIN public.qa_doctors_sessions q ON q.session_id = ci.session_id
 WHERE q.date = %s
 ORDER BY q.subject, ci.checklist_order
"""


class QaInputRepository:
    """READ-ONLY assembly of the QA input package from persisted evidence."""

    def load_inputs(self, connection, target_date, *, parser_version,
                    attendance_roster_version, resolver_version,
                    role_algorithm_version, engagement_algorithm_version) -> list[dict]:
        try:
            rows = connection.execute(LOAD_QA_INPUTS, (
                target_date, parser_version, attendance_roster_version,
                resolver_version, role_algorithm_version,
                engagement_algorithm_version)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "QA input query failed") from exc
        return self._rows_to_packages(rows)

    @staticmethod
    def _rows_to_packages(rows) -> list[dict]:
        return [
            {"lecture_id": row[0], "subject": row[1], "module": row[2], "meeting_id": row[3],
             "scheduled_start": row[4], "scheduled_end": row[5], "session_date": row[6],
             "selection_id": row[7], "primary_provider_transcript_id": row[8],
             "actual_start": row[9], "actual_end": row[10],
             "start_difference_minutes": row[11], "end_difference_minutes": row[12],
             "combined_id": row[13], "duration_minutes": row[14],
             "duration_seconds": row[15], "combined_content_sha256": row[16],
             "combined_content_bytes": row[17], "combined_source_fingerprint": row[18],
             "combined_content": row[19],
             "document_id": row[20], "document_source_fingerprint": row[21],
             "cue_count": row[22], "first_cue_start_ms": row[23], "last_cue_end_ms": row[24],
             "engagement_id": row[25], "attendance_snapshot_id": row[26],
             "attended_count": row[27], "spoke_count": row[28],
             "engagement_percentage": row[29], "engagement_score": row[30],
             "learner_engagement_status": row[31], "item7_override_applied": row[32],
             "engagement_calculation_status": row[33], "engagement_source_fingerprint": row[34],
             "attendance_roster_version": row[35],
             "canonical_trainer_speaker_id": row[36], "canonical_trainer": row[37],
             "attendance_source_row_count": row[38],
             "attendance_present_row_count": row[39],
             "attendance_effective_member_count": row[40],
             "attendance_source_rows_any_status": row[41]}
            for row in rows
        ]

    def load_inputs_for_lecture(self, connection, lecture_id, *, parser_version,
                                attendance_roster_version, resolver_version,
                                role_algorithm_version,
                                engagement_algorithm_version) -> list[dict]:
        """EXACTLY one lecture's QA input. Never widens to the date."""
        try:
            rows = connection.execute(LOAD_QA_INPUTS_FOR_LECTURE, (
                lecture_id, parser_version, attendance_roster_version,
                resolver_version, role_algorithm_version,
                engagement_algorithm_version)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "lecture QA input query failed") from exc
        return self._rows_to_packages(rows)

    def load_cues(self, connection, document_id) -> list[tuple]:
        try:
            return [(row[0], row[1]) for row in
                    connection.execute(LOAD_CUES, (document_id,)).fetchall()]
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "cue timeline query failed") from exc


class LegacyQaComparisonRepository:
    """READ-ONLY. Legacy QA values for the parity report; never written."""

    def load_sessions(self, connection, target_date) -> list[dict]:
        try:
            rows = connection.execute(LOAD_LEGACY_QA, (target_date,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy QA query failed") from exc
        return [
            {"subject": row[0], "session_id": row[1], "meeting_id": row[2], "trainer": row[3],
             "duration": row[4], "duration_score": row[5], "engagement": row[6],
             "engagement_score": row[7], "met_count": row[8], "partial_count": row[9],
             "not_met_count": row[10], "teaching_quality_rating": row[11],
             "cancelled_session": row[12]}
            for row in rows
        ]

    def load_engagement_counts(self, connection, target_date) -> dict:
        """Legacy spoke/attended counts, parsed from its own Item 7 evidence."""
        try:
            rows = connection.execute(LOAD_LEGACY_ENGAGEMENT_COUNTS, (target_date,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy engagement count query failed") from exc
        return {row[0]: {"spoke_count": row[1], "attended_count": row[2]} for row in rows}

    def load_timing(self, connection, target_date) -> dict:
        """Legacy start/end difference minutes, parsed from its own Item 2 text."""
        try:
            rows = connection.execute(LOAD_LEGACY_TIMING, (target_date,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy timing query failed") from exc
        return {row[0]: {"start_difference_minutes": row[1],
                         "end_difference_minutes": row[2]} for row in rows}

    def load_checklist(self, connection, target_date) -> dict:
        try:
            rows = connection.execute(LOAD_LEGACY_CHECKLIST, (target_date,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy checklist query failed") from exc
        grouped: dict = {}
        for subject, order, status in rows:
            grouped.setdefault(subject, {})[order] = status
        return grouped


class QaEvaluationRepository:
    def find_by_fingerprint(self, connection, source_fingerprint) -> dict | None:
        try:
            row = connection.execute(
                "SELECT evaluation_id, qa_status, ai_called FROM public.lecture_qa_evaluations "
                " WHERE source_fingerprint = %s", (source_fingerprint,)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "QA evaluation lookup failed") from exc
        if row is None:
            return None
        return {"evaluation_id": row[0], "qa_status": row[1], "ai_called": row[2]}

    def find_stored_output(self, connection, source_fingerprint) -> dict | None:
        """
        The whole stored answer for one fingerprint, including its raw output.

        Phase 4B re-judges an answer the provider already gave, so it needs
        what was returned and under which generation - not merely whether a row
        exists.
        """
        try:
            row = connection.execute("""
            SELECT evaluation_id, qa_status, ai_called, ai_raw_output, metadata,
                   provider_attempts, model_reported, ai_suggested_trainer
              FROM public.lecture_qa_evaluations
             WHERE source_fingerprint = %s
            """, (source_fingerprint,)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "QA stored output lookup failed") from exc
        if row is None:
            return None
        return {"evaluation_id": row[0], "qa_status": row[1], "ai_called": row[2],
                "ai_raw_output": row[3], "metadata": row[4] or {},
                "provider_attempts": row[5], "model_reported": row[6],
                "ai_suggested_trainer": row[7]}

    def mark_review_required(self, connection, evaluation_id, *, reason, attempts) -> None:
        """
        Terminal review state after the bounded generation cap.

        Only the status, the review reason and the attempt aggregate change;
        the stored model output and every earlier attempt are preserved as
        provenance.

        Phase 3C3D: provider_attempts is raised to the authoritative generation
        count here too. It used to hold the HTTP retry count of the LAST call,
        so an evaluation with three exhausted generations reported 1 and any
        recovery layer reading the evaluation alone under-counted.
        """
        try:
            connection.execute("""
            UPDATE public.lecture_qa_evaluations
               SET qa_status = 'REVIEW_REQUIRED', review_reason = %s, updated_at = now(),
                   provider_attempts = GREATEST(provider_attempts, %s::int),
                   metadata = metadata || jsonb_build_object('generation_attempts', %s::int)
             WHERE evaluation_id = %s
            """, (reason, attempts, attempts, evaluation_id))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not mark review required") from exc

    def find_reusable_output(self, connection, lecture_id, model_input_fingerprint):
        """
        A frozen model answer for this lecture that is still valid.

        "Still valid" means the MODEL INPUT fingerprint matches: the provider
        was asked exactly the same question, so its answer is exactly as good
        as it was when it was bought. Attendance and engagement are not part of
        that question, which is what makes attendance-only recovery free.

        The newest matching COMPLETED evaluation wins, and the whole point is
        that the older ones stay on record untouched.
        """
        try:
            row = connection.execute("""
            SELECT e.evaluation_id, e.ai_raw_output, e.source_fingerprint,
                   e.model_reported, e.provider_attempts, e.ai_suggested_trainer,
                   e.metadata, e.updated_at
              FROM public.lecture_qa_evaluations e
             WHERE e.lecture_id = %s
               AND e.qa_status = 'COMPLETED'
               AND e.ai_called = true
               AND e.ai_raw_output IS NOT NULL
               AND e.metadata ->> 'model_input_fingerprint' = %s
             ORDER BY e.updated_at DESC, e.evaluation_id DESC
             LIMIT 1
            """, (lecture_id, model_input_fingerprint)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "reusable output lookup failed") from exc
        if row is None:
            return None
        return {"evaluation_id": row[0], "ai_raw_output": row[1],
                "source_fingerprint": row[2], "model_reported": row[3],
                "provider_attempts": row[4], "ai_suggested_trainer": row[5],
                "metadata": row[6] or {}, "updated_at": row[7]}

    def sync_provider_attempts(self, connection, source_fingerprint) -> dict | None:
        """
        Re-derive one evaluation's attempt aggregate from the authoritative
        append-only attempts table.

        Repairs history WITHOUT touching a single attempt row: the attempts are
        the record, this column is a cached aggregate of them, and the two had
        drifted. Returns the before/after, or None if there is no evaluation.
        """
        try:
            row = connection.execute("""
            WITH authoritative AS (
                SELECT count(*)::int AS attempts
                  FROM public.lecture_qa_generation_attempts
                 WHERE source_fingerprint = %s)
            UPDATE public.lecture_qa_evaluations e
               SET provider_attempts = authoritative.attempts,
                   updated_at = CASE WHEN e.provider_attempts <> authoritative.attempts
                                     THEN now() ELSE e.updated_at END,
                   metadata = e.metadata || jsonb_build_object(
                       'provider_attempts_source', 'lecture_qa_generation_attempts',
                       'provider_attempts_reconciled_from', e.provider_attempts)
              FROM authoritative
             WHERE e.source_fingerprint = %s
               AND e.provider_attempts <> authoritative.attempts
         RETURNING e.evaluation_id,
                   (e.metadata ->> 'provider_attempts_reconciled_from')::int,
                   e.provider_attempts
            """, (source_fingerprint, source_fingerprint)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "attempt reconciliation failed") from exc
        if row is None:
            return None
        return {"evaluation_id": row[0], "previous": row[1], "current": row[2]}

    COLUMNS = (
        "lecture_id", "document_id", "selection_id", "attendance_snapshot_id", "engagement_id",
        "qa_engine_version", "prompt_version", "prompt_sha256", "structured_schema_version",
        "model_provider", "model_name", "model_reported", "attendance_roster_version",
        "qa_status", "delivery_status", "ai_called", "provider_attempts", "error_code",
        "primary_provider_transcript_id", "meeting_id", "duration_minutes", "duration_score",
        "duration_text", "start_difference_minutes", "end_difference_minutes",
        "cancelled_session", "canonical_trainer", "canonical_trainer_speaker_id",
        "ai_suggested_trainer", "trainer_source", "attended_count", "spoke_count",
        "engagement_percentage", "engagement_score", "ai_item7_status", "final_item7_status",
        "item7_override_applied", "met_count", "partial_count", "not_met_count",
        "teaching_quality_rating", "teaching_quality_comments", "overall_judgement",
        "evidence_clip_count", "invalid_evidence_clip_count", "structured_output_error_count",
        "review_reason", "source_fingerprint",
    )

    def upsert(self, connection, evaluation, checklist, clips) -> dict:
        """Write one evaluation with its checklist rows and evidence clips."""
        existing = self.find_by_fingerprint(connection, evaluation["source_fingerprint"])
        evaluation_id = existing["evaluation_id"] if existing else evaluation["evaluation_id"]
        columns = ", ".join(self.COLUMNS)
        placeholders = ", ".join(["%s"] * len(self.COLUMNS))
        updates = ", ".join(f"{name} = EXCLUDED.{name}" for name in self.COLUMNS
                            if name != "source_fingerprint")
        try:
            connection.execute(f"""
            INSERT INTO public.lecture_qa_evaluations (
                evaluation_id, {columns}, ai_raw_output, metadata
            ) VALUES (%s, {placeholders}, %s::jsonb, %s::jsonb)
            ON CONFLICT (source_fingerprint) DO UPDATE SET
                {updates}, ai_raw_output = EXCLUDED.ai_raw_output,
                metadata = EXCLUDED.metadata, updated_at = now()
            """, (evaluation_id, *[evaluation.get(name) for name in self.COLUMNS],
                  json.dumps(evaluation.get("ai_raw_output"), default=str)
                  if evaluation.get("ai_raw_output") is not None else None,
                  json.dumps(evaluation.get("metadata", {}), default=str)))

            connection.execute(
                "DELETE FROM public.lecture_qa_checklist_items WHERE evaluation_id = %s",
                (evaluation_id,))
            if checklist:
                with connection.cursor() as cursor:
                    cursor.executemany("""
                    INSERT INTO public.lecture_qa_checklist_items (
                        checklist_row_id, evaluation_id, checklist_order, checklist_item,
                        status, ai_status, status_source, reasoning, evidence_text,
                        evidence_clip_count, invalid_evidence_clip_count
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, [
                        (uuid.uuid5(evaluation_id, f"checklist:{row['checklist_order']}"),
                         evaluation_id, row["checklist_order"], row["checklist_item"],
                         row["status"], row.get("ai_status"), row["status_source"],
                         row.get("reasoning"), row.get("evidence_text"),
                         row.get("evidence_clip_count", 0),
                         row.get("invalid_evidence_clip_count", 0))
                        for row in checklist
                    ])

            connection.execute(
                "DELETE FROM public.lecture_qa_evidence_clips WHERE evaluation_id = %s",
                (evaluation_id,))
            if clips:
                with connection.cursor() as cursor:
                    cursor.executemany("""
                    INSERT INTO public.lecture_qa_evidence_clips (
                        clip_id, evaluation_id, clip_source, source_position, clip_index,
                        start_text, end_text, start_ms, end_ms, speaker_label,
                        validation_status, overlapping_cue_count
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, [
                        (uuid.uuid5(evaluation_id,
                                    f"clip:{row['clip_source']}:{row['source_position']}"
                                    f":{row['clip_index']}"),
                         evaluation_id, row["clip_source"], row["source_position"],
                         row["clip_index"], row.get("start_text"), row.get("end_text"),
                         row.get("start_ms"), row.get("end_ms"), row.get("speaker_label"),
                         row["validation_status"], row.get("overlapping_cue_count", 0))
                        for row in clips
                    ])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "QA evaluation write failed") from exc
        return {"evaluation_id": evaluation_id,
                "created": 0 if existing else 1, "updated": 1 if existing else 0}


class QaRunRepository:
    COUNTERS = ("lectures_considered", "delivered_count", "non_delivered_count",
                "provider_calls", "reused_evaluations", "evaluations_created",
                "evaluations_updated", "review_required_count", "error_count")

    def start(self, connection, target_date, *, mode, engine_version, prompt_version,
              model_name) -> uuid.UUID:
        run_id = uuid.uuid4()
        try:
            connection.execute(
                "INSERT INTO public.lecture_qa_runs (run_id, target_date, mode, status, "
                " qa_engine_version, prompt_version, model_name) "
                "VALUES (%s, %s, %s, 'RUNNING', %s, %s, %s)",
                (run_id, target_date, mode, engine_version, prompt_version, model_name))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not create QA run") from exc
        return run_id

    def complete(self, connection, run_id, summary) -> None:
        assignments = ", ".join(f"{name} = %s" for name in self.COUNTERS)
        values = [int(summary.get(name, 0)) for name in self.COUNTERS]
        try:
            connection.execute(
                "UPDATE public.lecture_qa_runs "
                f"SET completed_at = now(), status = %s, {assignments}, metadata = %s::jsonb "
                "WHERE run_id = %s",
                [summary["status"], *values,
                 json.dumps(summary.get("metadata", {}), default=str), run_id])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not complete QA run") from exc
