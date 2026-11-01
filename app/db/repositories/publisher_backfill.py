"""
Persistence for Historical Backfill previews and runs (proposed migration 029).

A run and its items are the operator's record of what was asked and what happened. They never hold the
publishing work itself - that is the publisher's ordinary jobs and rows - so a run can be re-read, resumed
after a restart or cancelled without any second copy of a video, caption or release existing anywhere.

    mode PREVIEW   a live, read-only discovery of the range, done by the publisher service; its items
                   (status PREVIEWED) are the snapshot an operator confirms.
    mode EXECUTE   the confirmed run of a COMPLETE preview.

Concurrency: at most ONE run per mode is QUEUED / RUNNING (partial unique index); `create` turns the conflict
into None, so two simultaneous requests produce exactly one run. Codes, counts and identities only.
"""
from __future__ import annotations

import uuid

from psycopg.types.json import Jsonb

PREVIEW, EXECUTE = "PREVIEW", "EXECUTE"
ACTIVE = ("QUEUED", "RUNNING")
TERMINAL = ("COMPLETED", "INCOMPLETE", "CANCELLED", "FAILED")
OPEN_ITEMS = ("PENDING", "PROCESSING")
ITEM_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "SKIPPED", "BLOCKED", "FAILED", "CANCELLED")
RUN_FIELDS = ("run_id", "mode", "preview_run_id", "date_from", "date_to", "library_id", "status", "requested_by",
              "reason", "preview_fingerprint", "preview_summary", "discovery", "lms_matched_at", "lms_status",
              "last_tick_at", "last_tick", "cancel_requested_at", "cancel_requested_by", "created_at", "started_at",
              "finished_at", "updated_at")
ITEM_FIELDS = ("run_id", "lecture_id", "session_date", "status", "origin", "initial_category", "reason", "stage",
               "retries", "detail", "created_at", "started_at", "finished_at", "updated_at")
_RUN = ", ".join(RUN_FIELDS)
_ITEM = ", ".join(ITEM_FIELDS)
_INSERT_ITEMS = """
INSERT INTO public.bunny_backfill_items (run_id, lecture_id, session_date, status, origin, initial_category, reason,
                                         detail, finished_at)
SELECT %s, x.lecture_id, x.session_date, x.status, x.origin, x.category, nullif(x.reason, ''), coalesce(x.detail, '{}'),
       CASE WHEN x.status IN ('PENDING', 'PROCESSING') THEN NULL ELSE now() END
  FROM jsonb_to_recordset(%s) AS x(lecture_id uuid, session_date date, status text, origin text, category text,
                                   reason text, detail jsonb)
ON CONFLICT (run_id, lecture_id) DO NOTHING
"""


def _run(row):
    return dict(zip(RUN_FIELDS, row)) if row else None


def _item(row):
    return dict(zip(ITEM_FIELDS, row)) if row else None


def _items_json(items) -> Jsonb:
    return Jsonb([{"lecture_id": str(i["lecture_id"]), "session_date": str(i["session_date"]), "status": i["status"],
                   "origin": i.get("origin") or "STORED_ONLY", "category": i["category"],
                   "reason": (i.get("reason") or "")[:200], "detail": i.get("detail") or {}} for i in items])


class BackfillRepository:

    # -- runs -----------------------------------------------------------------------------------
    def create(self, connection, *, date_from, date_to, library_id: int, requested_by: str, reason: str = "",
               fingerprint: str = "", summary: dict | None = None, items: list[dict] | None = None,
               mode: str = EXECUTE, preview_run_id=None) -> dict | None:
        """
        One QUEUED run (and its items), in the caller's transaction. None when another run of the same mode
        is already active (the partial unique index decided it; nothing of this run is written).
        Every statement runs once (never executemany): the web reaches this database through a pooler,
        where a server-side prepared statement must never be created.
        """
        run_id = uuid.uuid4()
        row = connection.execute(
            f"""INSERT INTO public.bunny_backfill_runs (run_id, mode, preview_run_id, date_from, date_to, library_id,
                                                       requested_by, reason, preview_fingerprint, preview_summary)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (mode) WHERE status IN ('QUEUED', 'RUNNING') DO NOTHING
                RETURNING {_RUN}""",
            (run_id, mode, preview_run_id, date_from, date_to, int(library_id), requested_by[:150], reason[:300],
             fingerprint, Jsonb(summary or {}))).fetchone()
        if row is None:
            return None
        if items:
            connection.execute(_INSERT_ITEMS, (run_id, _items_json(items)))
        return _run(row)

    def get(self, connection, run_id) -> dict | None:
        return _run(connection.execute(f"SELECT {_RUN} FROM public.bunny_backfill_runs WHERE run_id = %s",
                                       (run_id,)).fetchone())

    def active(self, connection, mode: str = EXECUTE) -> dict | None:
        return _run(connection.execute(f"SELECT {_RUN} FROM public.bunny_backfill_runs WHERE mode = %s AND status IN "
                                       "('QUEUED', 'RUNNING') LIMIT 1", (mode,)).fetchone())

    def recent(self, connection, *, limit: int = 25, mode: str = EXECUTE) -> list[dict]:
        return [_run(r) for r in connection.execute(
            f"SELECT {_RUN} FROM public.bunny_backfill_runs WHERE mode = %s ORDER BY created_at DESC, run_id LIMIT %s",
            (mode, int(limit))).fetchall()]

    def start(self, connection, run_id) -> None:
        connection.execute("UPDATE public.bunny_backfill_runs SET status = 'RUNNING', started_at = coalesce(started_at, "
                           "now()), updated_at = now() WHERE run_id = %s AND status = 'QUEUED'", (run_id,))

    def record_day(self, connection, run_id, day: str, coverage: dict) -> None:
        """One day's source-discovery coverage (merged into `discovery`)."""
        connection.execute("UPDATE public.bunny_backfill_runs SET discovery = discovery || %s, updated_at = now() "
                           "WHERE run_id = %s", (Jsonb({day: coverage}), run_id))

    def lms_matched(self, connection, run_id, status: str, *, done: bool) -> None:
        connection.execute("UPDATE public.bunny_backfill_runs SET lms_status = %s, lms_matched_at = CASE WHEN %s "
                           "THEN now() ELSE lms_matched_at END, updated_at = now() WHERE run_id = %s",
                           (status[:60], done, run_id))

    def ticked(self, connection, run_id, detail: dict) -> None:
        connection.execute("UPDATE public.bunny_backfill_runs SET last_tick_at = now(), last_tick = %s, "
                           "updated_at = now() WHERE run_id = %s", (Jsonb(detail), run_id))

    def finish(self, connection, run_id, status: str, *, fingerprint: str | None = None,
               summary: dict | None = None) -> bool:
        return connection.execute(
            """UPDATE public.bunny_backfill_runs
                  SET status = %s, finished_at = now(), updated_at = now(),
                      preview_fingerprint = coalesce(%s, preview_fingerprint),
                      preview_summary = coalesce(%s, preview_summary)
                WHERE run_id = %s AND status IN ('QUEUED', 'RUNNING') RETURNING run_id""",
            (status, fingerprint, Jsonb(summary) if summary is not None else None, run_id)).fetchone() is not None

    def request_cancel(self, connection, run_id, *, requested_by: str) -> dict | None:
        """The cancel request (idempotent); the coordinator applies it at its next tick. None if not active."""
        return _run(connection.execute(
            f"""UPDATE public.bunny_backfill_runs
                   SET cancel_requested_at = coalesce(cancel_requested_at, now()),
                       cancel_requested_by = coalesce(cancel_requested_by, %s), updated_at = now()
                 WHERE run_id = %s AND mode = 'EXECUTE' AND status IN ('QUEUED', 'RUNNING') RETURNING {_RUN}""",
            (requested_by[:150], run_id)).fetchone())

    def reopen(self, connection, run_id, *, rematch_lms: bool) -> dict | None:
        """A COMPLETED / FAILED execute run back to RUNNING for an operator retry. None if it cannot be (another
        run is active, or it was cancelled - a cancelled run stays cancelled)."""
        row = connection.execute(
            f"""UPDATE public.bunny_backfill_runs
                   SET status = 'RUNNING', finished_at = NULL, updated_at = now(),
                       lms_matched_at = CASE WHEN %s THEN NULL ELSE lms_matched_at END
                 WHERE run_id = %s AND mode = 'EXECUTE' AND status IN ('COMPLETED', 'FAILED', 'RUNNING', 'QUEUED')
                   AND NOT EXISTS (SELECT 1 FROM public.bunny_backfill_runs o
                                    WHERE o.mode = 'EXECUTE' AND o.status IN ('QUEUED', 'RUNNING') AND o.run_id <> %s)
             RETURNING {_RUN}""", (rematch_lms, run_id, run_id)).fetchone()
        return _run(row)

    # -- items ----------------------------------------------------------------------------------
    def add_items(self, connection, run_id, items: list[dict]) -> None:
        """Items the run has not recorded yet (an existing (run, lecture) is left as it is)."""
        if items:
            connection.execute(_INSERT_ITEMS, (run_id, _items_json(items)))

    def replace_preview_items(self, connection, run_id, items: list[dict]) -> None:
        """A preview's final snapshot (in the caller's transaction)."""
        connection.execute("DELETE FROM public.bunny_backfill_items WHERE run_id = %s AND status = 'PREVIEWED'",
                           (run_id,))
        self.add_items(connection, run_id, items)

    def items(self, connection, run_id, *, statuses=None) -> list[dict]:
        rows = connection.execute(
            f"SELECT {_ITEM} FROM public.bunny_backfill_items WHERE run_id = %s "
            "AND (%s::text[] IS NULL OR status = ANY(%s::text[])) ORDER BY session_date, lecture_id",
            (run_id, list(statuses) if statuses else None, list(statuses) if statuses else None)).fetchall()
        return [_item(r) for r in rows]

    def counts(self, connection, run_id) -> dict:
        found = dict(connection.execute("SELECT status, count(*) FROM public.bunny_backfill_items WHERE run_id = %s "
                                        "GROUP BY status", (run_id,)).fetchall())
        return {s: int(found.get(s, 0)) for s in ITEM_STATUSES}

    def pending(self, connection, run_id, limit: int) -> list[dict]:
        return [_item(r) for r in connection.execute(
            f"SELECT {_ITEM} FROM public.bunny_backfill_items WHERE run_id = %s AND status = 'PENDING' "
            "ORDER BY session_date, lecture_id LIMIT %s", (run_id, int(limit))).fetchall()]

    def update_item(self, connection, run_id, lecture_id, *, status: str, reason: str | None = None,
                    stage: str | None = None, detail: dict | None = None) -> None:
        connection.execute(
            """UPDATE public.bunny_backfill_items
                  SET status = %s, reason = %s, stage = %s, detail = coalesce(%s, detail), updated_at = now(),
                      started_at = CASE WHEN %s = 'PROCESSING' THEN coalesce(started_at, now()) ELSE started_at END,
                      finished_at = CASE WHEN %s IN ('PENDING', 'PROCESSING') THEN NULL
                                         ELSE coalesce(finished_at, now()) END
                WHERE run_id = %s AND lecture_id = %s""",
            (status, (reason or None) and reason[:200], stage, Jsonb(detail) if detail is not None else None,
             status, status, run_id, lecture_id))

    def reopen_item(self, connection, run_id, lecture_id) -> bool:
        """A BLOCKED / FAILED item back to PROCESSING after an operator retry."""
        return connection.execute(
            """UPDATE public.bunny_backfill_items
                  SET status = 'PROCESSING', retries = retries + 1, finished_at = NULL, updated_at = now(),
                      reason = 'OPERATOR_RETRY', started_at = coalesce(started_at, now())
                WHERE run_id = %s AND lecture_id = %s AND status IN ('BLOCKED', 'FAILED') RETURNING lecture_id""",
            (run_id, lecture_id)).fetchone() is not None

    def cancel_open_items(self, connection, run_id) -> dict:
        """Every PENDING item -> CANCELLED; PROCESSING ones too (their in-flight work is left to finish)."""
        pending = connection.execute(
            "UPDATE public.bunny_backfill_items SET status = 'CANCELLED', reason = 'CANCELLED_BY_OPERATOR', "
            "finished_at = now(), updated_at = now() WHERE run_id = %s AND status = 'PENDING' RETURNING 1",
            (run_id,)).fetchall()
        processing = connection.execute(
            "UPDATE public.bunny_backfill_items SET status = 'CANCELLED', "
            "reason = 'CANCELLED_IN_FLIGHT_WORK_FINISHES', finished_at = now(), updated_at = now() "
            "WHERE run_id = %s AND status = 'PROCESSING' RETURNING 1", (run_id,)).fetchall()
        return {"pending": len(pending), "processing": len(processing)}
