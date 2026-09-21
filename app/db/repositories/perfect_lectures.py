"""
Phase 3C2.3D persistence: the coded Perfect Lecture result, its legacy
ownership record, and the legacy public.qa_perfect_lectures target.

READ-ONLY here: the Phase 3B rendered payload (loaded elsewhere).
WRITES (coded-platform only): lecture_perfect_lecture_results,
                              lecture_perfect_lecture_legacy_writes.
WRITES (legacy, write-enabled modes only): public.qa_perfect_lectures.

Every legacy statement in this module is unreachable without an explicit
`allow_write=True` that only a write-enabled mode can supply, and there is no
DELETE path that does not first prove coded ownership.
"""
import json
import uuid

from psycopg import sql

from app.common.errors import DATABASE_ERROR, PlatformError
from app.writer.perfect_mapping import (
    CODED_OWNED_COLUMNS,
    LEGACY_PERFECT_KEY,
    PERFECT_COLUMNS,
    PERFECT_UPDATE_COALESCE_COLUMNS,
    PERFECT_UPDATE_REPLACE_COLUMNS,
)


# Deterministic identity so recomputing a result twice cannot create two rows.
RESULT_NAMESPACE = uuid.UUID("2f1f2a2c-8e2c-5c8e-9a0f-3d5a9c7b1e42")


def result_identity(lecture_id, eligibility_version) -> uuid.UUID:
    return uuid.uuid5(RESULT_NAMESPACE, f"{lecture_id}|{eligibility_version}")


class PerfectLectureResultRepository:
    """The coded platform's own eligibility answer."""

    def find(self, connection, lecture_id, eligibility_version) -> dict | None:
        try:
            row = connection.execute("""
            SELECT result_id, lecture_id, evaluation_id, rendered_session_id,
                   eligibility_version, source_fingerprint, is_perfect, reason,
                   met_count, partial_count, not_met_count, checklist_row_count,
                   distinct_order_count, cancelled_session, legacy_lecture_key,
                   legacy_session_id, computed_at, metadata
              FROM public.lecture_perfect_lecture_results
             WHERE lecture_id = %s AND eligibility_version = %s
            """, (lecture_id, eligibility_version)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "perfect lecture result read failed") from exc
        if row is None:
            return None
        names = ("result_id", "lecture_id", "evaluation_id", "rendered_session_id",
                 "eligibility_version", "source_fingerprint", "is_perfect", "reason",
                 "met_count", "partial_count", "not_met_count", "checklist_row_count",
                 "distinct_order_count", "cancelled_session", "legacy_lecture_key",
                 "legacy_session_id", "computed_at", "metadata")
        return dict(zip(names, row))

    def upsert(self, connection, entry: dict) -> dict:
        """
        Record (or refresh) one lecture's answer.

        Keyed on (lecture_id, eligibility_version), so re-deriving the same
        answer from the same frozen payload is a no-op rather than a second
        row. A CHANGED answer overwrites the current one and the previous
        value is appended to metadata, because losing the fact that a lecture
        used to be perfect is exactly what makes a PERFECT -> NOT PERFECT
        transition unauditable.
        """
        result_id = result_identity(entry["lecture_id"], entry["eligibility_version"])
        previous = self.find(connection, entry["lecture_id"], entry["eligibility_version"])
        metadata = dict(entry.get("metadata") or {})
        # Did the ANSWER change, as opposed to merely being re-derived? Decided
        # here rather than in SQL because the pre-update row is already in hand,
        # and because a RETURNING expression would compare against the values
        # the statement had just written.
        answer_changed = previous is None or any(
            previous[field] != entry[field] for field in (
                "source_fingerprint", "is_perfect", "reason", "met_count",
                "partial_count", "not_met_count", "checklist_row_count",
                "distinct_order_count", "cancelled_session", "legacy_lecture_key",
                "legacy_session_id")) or str(previous["evaluation_id"]) != str(
                    entry["evaluation_id"]) or str(
                    previous["rendered_session_id"]) != str(entry["rendered_session_id"])
        if previous is not None and previous["is_perfect"] != entry["is_perfect"]:
            history = list((previous.get("metadata") or {}).get("transitions", []))
            history.append({
                "from_is_perfect": previous["is_perfect"],
                "to_is_perfect": entry["is_perfect"],
                "from_reason": previous["reason"], "to_reason": entry["reason"],
                "from_source_fingerprint": previous["source_fingerprint"],
                "to_source_fingerprint": entry["source_fingerprint"],
            })
            # Bounded: provenance, not an unbounded log.
            metadata["transitions"] = history[-20:]
        elif previous is not None:
            metadata.setdefault("transitions",
                                (previous.get("metadata") or {}).get("transitions", []))
        try:
            row = connection.execute("""
            INSERT INTO public.lecture_perfect_lecture_results (
                result_id, lecture_id, evaluation_id, rendered_session_id,
                eligibility_version, source_fingerprint, is_perfect, reason,
                met_count, partial_count, not_met_count, checklist_row_count,
                distinct_order_count, cancelled_session, legacy_lecture_key,
                legacy_session_id, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s::jsonb)
            ON CONFLICT ON CONSTRAINT lecture_perfect_lecture_results_key DO UPDATE SET
                evaluation_id = EXCLUDED.evaluation_id,
                rendered_session_id = EXCLUDED.rendered_session_id,
                source_fingerprint = EXCLUDED.source_fingerprint,
                is_perfect = EXCLUDED.is_perfect, reason = EXCLUDED.reason,
                met_count = EXCLUDED.met_count, partial_count = EXCLUDED.partial_count,
                not_met_count = EXCLUDED.not_met_count,
                checklist_row_count = EXCLUDED.checklist_row_count,
                distinct_order_count = EXCLUDED.distinct_order_count,
                cancelled_session = EXCLUDED.cancelled_session,
                legacy_lecture_key = EXCLUDED.legacy_lecture_key,
                legacy_session_id = EXCLUDED.legacy_session_id,
                metadata = EXCLUDED.metadata,
                -- The timestamps move ONLY when the answer actually changed.
                -- Re-deriving the same answer from the same frozen source has
                -- to be a true no-op: a computed_at that advances on every run
                -- makes "what did this run change?" unanswerable, which is the
                -- same defect the date-scoped engagement path had.
                computed_at = CASE WHEN %s THEN now()
                              ELSE lecture_perfect_lecture_results.computed_at END,
                updated_at = CASE WHEN %s THEN now()
                             ELSE lecture_perfect_lecture_results.updated_at END
            RETURNING result_id, (xmax = 0) AS inserted
            """, (result_id, entry["lecture_id"], entry["evaluation_id"],
                  entry["rendered_session_id"], entry["eligibility_version"],
                  entry["source_fingerprint"], entry["is_perfect"], entry["reason"],
                  entry["met_count"], entry["partial_count"], entry["not_met_count"],
                  entry["checklist_row_count"], entry["distinct_order_count"],
                  entry["cancelled_session"], entry["legacy_lecture_key"],
                  entry["legacy_session_id"],
                  json.dumps(metadata, default=str),
                  answer_changed, answer_changed)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "perfect lecture result write failed") from exc
        return {"result_id": row[0], "created": bool(row[1]),
                "answer_changed": answer_changed,
                "previous_is_perfect": previous["is_perfect"] if previous else None}


class PerfectLectureOwnershipRepository:
    """Which legacy qa_perfect_lectures rows the coded platform created."""

    def find(self, connection, legacy_lecture_key, writer_version) -> dict | None:
        try:
            row = connection.execute("""
            SELECT perfect_write_id, lecture_id, result_id, source_fingerprint,
                   write_status, write_mode, post_write_digest, eligibility_version,
                   metadata, written_at
              FROM public.lecture_perfect_lecture_legacy_writes
             WHERE legacy_lecture_key = %s AND writer_version = %s
            """, (legacy_lecture_key, writer_version)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "perfect ownership lookup failed") from exc
        if row is None:
            return None
        names = ("perfect_write_id", "lecture_id", "result_id", "source_fingerprint",
                 "write_status", "write_mode", "post_write_digest", "eligibility_version",
                 "metadata", "written_at")
        record = dict(zip(names, row))
        # Provenance of the LAST mapped payload this writer produced. Absent on
        # rows written before mapping versioning existed, which is itself
        # meaningful: identity cannot be proven from the audit alone and the
        # live target has to be compared instead.
        metadata = record.get("metadata") or {}
        record["mapping_version"] = metadata.get("mapping_version")
        record["mapped_digest"] = metadata.get("mapped_digest")
        return record

    def record(self, connection, entry: dict) -> dict:
        try:
            row = connection.execute("""
            INSERT INTO public.lecture_perfect_lecture_legacy_writes (
                perfect_write_id, lecture_id, result_id, evaluation_id,
                legacy_lecture_key, legacy_session_id, writer_version,
                eligibility_version, source_fingerprint, write_mode, write_status,
                legacy_row_created, legacy_row_updated, pre_write_digest,
                post_write_digest, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s::jsonb)
            ON CONFLICT (legacy_lecture_key, writer_version) DO UPDATE SET
                result_id = EXCLUDED.result_id,
                evaluation_id = EXCLUDED.evaluation_id,
                legacy_session_id = EXCLUDED.legacy_session_id,
                eligibility_version = EXCLUDED.eligibility_version,
                source_fingerprint = EXCLUDED.source_fingerprint,
                write_mode = EXCLUDED.write_mode, write_status = EXCLUDED.write_status,
                legacy_row_updated = EXCLUDED.legacy_row_updated,
                pre_write_digest = EXCLUDED.pre_write_digest,
                post_write_digest = EXCLUDED.post_write_digest,
                metadata = EXCLUDED.metadata, updated_at = now(), written_at = now()
            RETURNING perfect_write_id, (xmax = 0) AS inserted
            """, (entry.get("perfect_write_id") or uuid.uuid4(), entry["lecture_id"],
                  entry["result_id"], entry["evaluation_id"], entry["legacy_lecture_key"],
                  entry["legacy_session_id"], entry["writer_version"],
                  entry["eligibility_version"], entry["source_fingerprint"],
                  entry["write_mode"], entry["write_status"],
                  entry.get("legacy_row_created", False),
                  entry.get("legacy_row_updated", False), entry.get("pre_write_digest"),
                  entry.get("post_write_digest"),
                  json.dumps(entry.get("metadata", {}), default=str))).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "perfect ownership write failed") from exc
        return {"perfect_write_id": row[0], "created": bool(row[1])}

    def mark_status(self, connection, perfect_write_id, status, metadata=None) -> None:
        try:
            connection.execute("""
            UPDATE public.lecture_perfect_lecture_legacy_writes
               SET write_status = %s, updated_at = now(),
                   metadata = coalesce(%s::jsonb, metadata)
             WHERE perfect_write_id = %s
            """, (status, json.dumps(metadata, default=str) if metadata else None,
                  perfect_write_id))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "perfect status update failed") from exc


class LegacyPerfectLectureRepository:
    """
    public.qa_perfect_lectures. Reads are unconditional; writes are reachable
    only from a write-enabled mode and are refused otherwise.
    """

    def load(self, connection, lecture_key) -> dict | None:
        columns = sql.SQL(", ").join(sql.Identifier(name) for name in PERFECT_COLUMNS)
        statement = sql.SQL(
            "SELECT {} FROM public.qa_perfect_lectures WHERE {} = %s").format(
            columns, sql.Identifier(LEGACY_PERFECT_KEY))
        try:
            row = connection.execute(statement, (lecture_key,)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy perfect lecture read failed") from exc
        return dict(zip(PERFECT_COLUMNS, row)) if row else None

    def collisions(self, connection, lecture_key, session_id) -> dict:
        """
        Look for the known lecture_key hazard BEFORE using it as an upsert key.

        Two different sessions on the same date with the same subject produce
        the same key; production already contains one such pair. If a row
        exists under our key but carries a different session_id, the key is
        pointing at somebody else's lecture and the write must not proceed.
        """
        try:
            row = connection.execute("""
            SELECT lecture_key, session_id,
                   (session_id IS NOT NULL AND session_id <> %s) AS foreign_session
              FROM public.qa_perfect_lectures WHERE lecture_key = %s
            """, (session_id, lecture_key)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy perfect collision check failed") from exc
        if row is None:
            return {"key_in_use": False, "foreign_session": False,
                    "existing_session_id": None}
        return {"key_in_use": True, "foreign_session": bool(row[2]),
                "existing_session_id": row[1]}

    def write(self, connection, row: dict, *, allow_write: bool) -> None:
        """
        Upsert the eleven mapped columns on `lecture_key`, reproducing the
        legacy merge semantics exactly.

        COALESCE on recording_url / meeting_id / session_id / module / trainer
        is the legacy behaviour and is what makes a recording_url written by
        another workflow survive this statement untouched - the coded writer
        always passes recording_url = NULL, so the existing value wins.

        lecture_key, session_date and subject are absent from the update list,
        exactly as in legacy: an update can never move a row to a different
        lecture.
        """
        if not allow_write:
            raise PlatformError(
                DATABASE_ERROR,
                "legacy perfect lecture write attempted without a write-enabled mode")
        if row.get("recording_url") is not None:
            # Defence in depth: the mapping already forces NULL.
            raise PlatformError(DATABASE_ERROR,
                                "the coded writer must never supply recording_url")
        updates = [sql.SQL("{name} = EXCLUDED.{name}").format(name=sql.Identifier(name))
                   for name in PERFECT_UPDATE_REPLACE_COLUMNS]
        updates += [sql.SQL(
            "{name} = COALESCE(EXCLUDED.{name}, public.qa_perfect_lectures.{name})"
        ).format(name=sql.Identifier(name)) for name in PERFECT_UPDATE_COALESCE_COLUMNS]
        statement = sql.SQL(
            "INSERT INTO public.qa_perfect_lectures ({columns}) VALUES ({values}) "
            "ON CONFLICT ({key}) DO UPDATE SET {updates}").format(
            columns=sql.SQL(", ").join(sql.Identifier(name) for name in PERFECT_COLUMNS),
            values=sql.SQL(", ").join(sql.Placeholder() for _ in PERFECT_COLUMNS),
            key=sql.Identifier(LEGACY_PERFECT_KEY),
            updates=sql.SQL(", ").join(updates))
        try:
            connection.execute(statement, [row[name] for name in PERFECT_COLUMNS])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy perfect lecture write failed") from exc

    def delete_owned(self, connection, lecture_key, *, allow_write: bool) -> None:
        """
        Rollback helper for a Perfect Lecture row this platform created.

        The caller must already have proven ownership AND that no downstream
        enrichment depends on the row; this method cannot be reached for a
        legacy-owned row.
        """
        if not allow_write:
            raise PlatformError(DATABASE_ERROR,
                                "perfect rollback attempted without a write-enabled mode")
        try:
            connection.execute(
                "DELETE FROM public.qa_perfect_lectures WHERE lecture_key = %s",
                (lecture_key,))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy perfect rollback failed") from exc


# Exported for the writer's verification step.
VERIFY_COLUMNS = CODED_OWNED_COLUMNS
