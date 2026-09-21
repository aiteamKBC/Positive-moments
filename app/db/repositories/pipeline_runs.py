"""
Phase 4A: durable orchestration audit.

Audit only. This repository owns no pipeline state, and nothing in the platform
reads it to decide what to do next - the state resolver derives that from the
tables that actually own it. That separation is deliberate: an audit log that
also drives behaviour stops being an audit log the first time someone repairs
a row in it.
"""
import json
import uuid

from app.common.errors import DATABASE_ERROR, PlatformError


INSERT_RUN = """
INSERT INTO public.lecture_pipeline_runs (
    run_id, run_type, orchestration_version, target_date, window_start,
    window_end, status, legacy_qa_precheck_status, legacy_qa_node_disabled,
    metadata)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

FINISH_RUN = """
UPDATE public.lecture_pipeline_runs
   SET finished_at = now(), updated_at = now(), status = %s,
       lectures_seen = %s, completed_count = %s, waiting_count = %s,
       review_count = %s, failed_count = %s, skipped_count = %s,
       graph_calls = %s, provider_calls = %s, legacy_rows_written = %s,
       legacy_qa_precheck_status = coalesce(%s, legacy_qa_precheck_status),
       legacy_qa_node_disabled = coalesce(%s, legacy_qa_node_disabled),
       metadata = metadata || %s
 WHERE run_id = %s
"""

INSERT_ITEM = """
INSERT INTO public.lecture_pipeline_run_items (
    item_id, run_id, lecture_id, initial_state, action, final_state, status,
    error_code, error_message_safe, retryable, finished_at, duration_ms,
    graph_calls, provider_calls, metadata)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s, %s, %s)
ON CONFLICT (run_id, lecture_id) DO UPDATE SET
    final_state = EXCLUDED.final_state, status = EXCLUDED.status,
    error_code = EXCLUDED.error_code,
    error_message_safe = EXCLUDED.error_message_safe,
    retryable = EXCLUDED.retryable, finished_at = EXCLUDED.finished_at,
    duration_ms = EXCLUDED.duration_ms, graph_calls = EXCLUDED.graph_calls,
    provider_calls = EXCLUDED.provider_calls, metadata = EXCLUDED.metadata
"""

RECENT_RUNS = """
SELECT run_id, run_type, orchestration_version, target_date, window_start,
       window_end, started_at, finished_at, status, legacy_qa_precheck_status,
       legacy_qa_node_disabled, lectures_seen, completed_count, waiting_count,
       review_count, failed_count, skipped_count, graph_calls, provider_calls,
       legacy_rows_written, metadata
  FROM public.lecture_pipeline_runs
 ORDER BY started_at DESC, run_id DESC
 LIMIT %s
"""

RUN_ITEMS = """
SELECT i.item_id, i.lecture_id, l.subject, i.initial_state, i.action,
       i.final_state, i.status, i.error_code, i.error_message_safe,
       i.retryable, i.started_at, i.finished_at, i.duration_ms,
       i.graph_calls, i.provider_calls, i.metadata
  FROM public.lecture_pipeline_run_items i
  JOIN public.lecture_sessions l ON l.lecture_id = i.lecture_id
 WHERE i.run_id = %s
 ORDER BY l.scheduled_start, i.lecture_id
"""

LECTURE_HISTORY = """
SELECT i.item_id, i.run_id, r.run_type, r.target_date, i.initial_state,
       i.action, i.final_state, i.status, i.error_code, i.error_message_safe,
       i.retryable, i.started_at, i.duration_ms
  FROM public.lecture_pipeline_run_items i
  JOIN public.lecture_pipeline_runs r ON r.run_id = i.run_id
 WHERE i.lecture_id = %s
 ORDER BY i.started_at DESC
 LIMIT %s
"""


# Anything that could carry transcript content, learner identity or a
# credential must never reach an audit row. The cap is the blunt half of that;
# the callers pass codes rather than payloads, which is the real half.
MAX_SAFE_MESSAGE = 500


def safe_message(value) -> str | None:
    """
    Reduce an exception to something an operator may safely be shown.

    Deliberately conservative: the message is truncated, newlines collapsed,
    and nothing is ever formatted in from a payload. An audit trail that leaks
    a transcript line is worse than one that says too little.
    """
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:MAX_SAFE_MESSAGE]


def _json(value) -> str:
    return json.dumps(value or {}, default=str)


class PipelineRunRepository:
    def start(self, connection, *, run_type, orchestration_version, target_date,
              window_start, window_end, status, legacy_qa_precheck_status=None,
              legacy_qa_node_disabled=None, metadata=None) -> str:
        run_id = str(uuid.uuid4())
        try:
            connection.execute(INSERT_RUN, (
                run_id, run_type, orchestration_version, target_date,
                window_start, window_end, status, legacy_qa_precheck_status,
                legacy_qa_node_disabled, _json(metadata)))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "pipeline run insert failed") from exc
        return run_id

    def finish(self, connection, run_id, *, status, counts, graph_calls=0,
               provider_calls=0, legacy_rows_written=0,
               legacy_qa_precheck_status=None, legacy_qa_node_disabled=None,
               metadata=None) -> None:
        try:
            connection.execute(FINISH_RUN, (
                status, counts.get("lectures_seen", 0),
                counts.get("completed_count", 0), counts.get("waiting_count", 0),
                counts.get("review_count", 0), counts.get("failed_count", 0),
                counts.get("skipped_count", 0), graph_calls, provider_calls,
                legacy_rows_written, legacy_qa_precheck_status,
                legacy_qa_node_disabled, _json(metadata), run_id))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "pipeline run update failed") from exc

    def record_item(self, connection, run_id, *, lecture_id, initial_state,
                    action, final_state, status, error_code=None,
                    error_message=None, retryable=False, duration_ms=None,
                    graph_calls=0, provider_calls=0, metadata=None) -> str:
        item_id = str(uuid.uuid4())
        try:
            connection.execute(INSERT_ITEM, (
                item_id, run_id, str(lecture_id), initial_state, action,
                final_state, status, error_code, safe_message(error_message),
                retryable, duration_ms, graph_calls, provider_calls,
                _json(metadata)))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR,
                                "pipeline run item insert failed") from exc
        return item_id

    # -- reads, for the Operations layer ------------------------------------

    def recent_runs(self, connection, limit: int = 20) -> list[dict]:
        names = ("run_id", "run_type", "orchestration_version", "target_date",
                 "window_start", "window_end", "started_at", "finished_at",
                 "status", "legacy_qa_precheck_status", "legacy_qa_node_disabled",
                 "lectures_seen", "completed_count", "waiting_count",
                 "review_count", "failed_count", "skipped_count", "graph_calls",
                 "provider_calls", "legacy_rows_written", "metadata")
        try:
            rows = connection.execute(RECENT_RUNS, (limit,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "pipeline run read failed") from exc
        return [{name: _plain(value) for name, value in zip(names, row)}
                for row in rows]

    def items_for_run(self, connection, run_id) -> list[dict]:
        names = ("item_id", "lecture_id", "subject", "initial_state", "action",
                 "final_state", "status", "error_code", "error_message_safe",
                 "retryable", "started_at", "finished_at", "duration_ms",
                 "graph_calls", "provider_calls", "metadata")
        try:
            rows = connection.execute(RUN_ITEMS, (run_id,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "pipeline item read failed") from exc
        return [{name: _plain(value) for name, value in zip(names, row)}
                for row in rows]

    def history_for_lecture(self, connection, lecture_id,
                            limit: int = 20) -> list[dict]:
        names = ("item_id", "run_id", "run_type", "target_date", "initial_state",
                 "action", "final_state", "status", "error_code",
                 "error_message_safe", "retryable", "started_at", "duration_ms")
        try:
            rows = connection.execute(LECTURE_HISTORY,
                                      (str(lecture_id), limit)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "pipeline history read failed") from exc
        return [{name: _plain(value) for name, value in zip(names, row)}
                for row in rows]


def _plain(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
