"""
Historical Backfill: publish the lectures of an operator-chosen, INCLUSIVE range of past dates through
the publisher's own pipeline (2026-10-29).

    preview   read-only. The lectures of the range and what a run would do with each, from the publisher's
              stored state (the monitor's facts and classification, the delivery rule, the recording
              selection on record). No Graph, LMS or Bunny call; callers run it on a READ ONLY connection.
              Every answer says `data_source: DATABASE`: live verification is the run's job, through the
              same workers as the daily publisher.
    run       one operator-confirmed row in bunny_backfill_runs (written by the operations API after it has
              re-checked the preview), advanced by the daemon's HISTORICAL_BACKFILL task in small steps:
                1. LMS occurrence matching for the range (Planner.match_range - the tick only matches its
                   lookback window);
                2. for a bounded batch of the run's lectures whose LMS match is VERIFIED: the two track jobs
                   (ACQUIRE_TRANSCRIPT, RESOLVE_RECORDING - longest_recording_v1 selection unchanged), then
                   the Bunny jobs of the INCLUDED part (Planner.plan_lecture, the tick's own rules);
                3. each lecture's progress read back from the publisher's state until it is released (by the
                   existing AUTO_RELEASE task), or skipped / blocked / failed with a reason.
              Release, LMS matching and source verification are never relaxed and never done here.

DAILY PUBLISHING HAS PRIORITY
    * every historical job is enqueued below every daily priority (handlers.HISTORICAL_PRIORITY) and keeps
      that priority through its follow-ups, so a worker always claims due daily work first;
    * at most `backfill_max_imports` historical IMPORT_VIDEO jobs are live at once, and none is added while
      a daily import is waiting; the global import-slot limit still applies to both;
    * a run starts `backfill_batch_size` lectures per tick, with at most `backfill_max_active` in progress;
    * the daily schedule, lookback and start date are not changed: a run plans only its own lectures.

NO DUPLICATES, BY CONSTRUCTION
    A run owns no video, caption or release - it enqueues the publisher's ordinary jobs, whose one-live-job
    index, per-part video rows, Bunny token search, uncertain-submission reconciliation and release rules
    are exactly the daily publisher's. Overlapping runs, a rerun and the daily publisher therefore all
    converge on the same rows. One run is active at a time (bunny_backfill_runs_one_active).

RESTART / CANCEL
    All progress is in the database: a restarted daemon simply continues the active run. Cancelling stops
    further work: pending lectures are cancelled, historical imports that have not reached Bunny yet are
    cancelled (audited OPERATOR_CANCEL), and anything already submitted, encoding or captioning finishes
    normally. Track jobs are never cancelled (a cancelled track would block a later daily or historical
    start of that track) - they only read Graph.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.common.time import CAIRO
from app.db.repositories.publisher_backfill import BackfillRepository
from app.db.repositories.publisher_queue import PublisherQueueRepository
from app.publisher import monitor
from app.publisher.handlers import HISTORICAL_PRIORITY, HISTORICAL_TAG, MAX_ATTEMPTS, subject_key
from app.publisher.recording_mapping import RULE_SINGLE_RECORDING

log = logging.getLogger(__name__)

TRACK_KINDS = ("ACQUIRE_TRANSCRIPT", "RESOLVE_RECORDING")
LIVE = ("QUEUED", "RUNNING", "RETRY_WAIT")
STALL_TICKS = 3

# --------------------------------------------------------------------------- preview categories
ALREADY_PUBLISHED = "ALREADY_PUBLISHED"
ALREADY_PROCESSING = "ALREADY_PROCESSING"
READY_TO_QUEUE = "READY_TO_QUEUE"
MISSING_LMS_MATCH = "MISSING_LMS_MATCH"
MISSING_RECORDING = "MISSING_RECORDING"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
SKIPPED = "SKIPPED"
ERROR = "ERROR"
CATEGORIES = (ALREADY_PUBLISHED, ALREADY_PROCESSING, READY_TO_QUEUE, MISSING_LMS_MATCH, MISSING_RECORDING,
              REVIEW_REQUIRED, SKIPPED, ERROR)
BLOCKED_CATEGORIES = (MISSING_LMS_MATCH, MISSING_RECORDING, REVIEW_REQUIRED, ERROR)
ACTIONS = {ALREADY_PUBLISHED: "NONE", ALREADY_PROCESSING: "TRACK", READY_TO_QUEUE: "QUEUE",
           MISSING_LMS_MATCH: "NONE", MISSING_RECORDING: "NONE", REVIEW_REQUIRED: "NONE", SKIPPED: "NONE",
           ERROR: "NONE"}
# What a confirmed run records for each category at start.
ITEM_STATUS = {READY_TO_QUEUE: "PENDING", ALREADY_PROCESSING: "PROCESSING", ALREADY_PUBLISHED: "SKIPPED",
               SKIPPED: "SKIPPED", MISSING_LMS_MATCH: "BLOCKED", MISSING_RECORDING: "BLOCKED",
               REVIEW_REQUIRED: "BLOCKED", ERROR: "FAILED"}
PUBLISHED_STATES = (monitor.PLAYABLE, monitor.PLAYABLE_NO_CAPTIONS, monitor.RELEASED_PLAYBACK_OFF)
_MISSING_HINTS = ("NOT_FOUND", "NO_RECORDING", "DEADLINE_PASSED", "RECORDING_FILE_NOT_FOUND")


class RangeRefused(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


# --------------------------------------------------------------------------- range
def cairo_today(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(CAIRO).date()


def parse_date(value, name: str) -> date:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        raise RangeRefused("INVALID_DATE", f"{name} must be a date (YYYY-MM-DD)") from None


def validate_range(date_from: date, date_to: date, *, today: date, max_days: int) -> int:
    """
    The number of days in the INCLUSIVE range, or RangeRefused. Dates are Africa/Cairo business dates
    (lecture_sessions.session_date - the publisher's canonical date, DST included): a lecture belongs to
    exactly one date, so neither boundary leaks a neighbouring day.
    """
    if date_to < date_from:
        raise RangeRefused("INVALID_RANGE", "To Date is before From Date")
    if date_to > today:
        raise RangeRefused("FUTURE_DATE", f"To Date is in the future (today is {today.isoformat()} in Cairo)")
    days = (date_to - date_from).days + 1
    if days > max_days:
        raise RangeRefused("RANGE_TOO_LONG", f"choose at most {max_days} days (this range has {days})")
    return days


def business_days(date_from: date, date_to: date) -> list[date]:
    return [date_from + timedelta(days=n) for n in range((date_to - date_from).days + 1)]


# --------------------------------------------------------------------------- recordings on record
RECORDINGS = """
SELECT s.lecture_id::text, s.recording_part_index, s.locate_status, s.classification, s.source_duration_seconds,
       (d.metadata -> 'recording_selection' ->> 'part_index')::int, d.metadata -> 'recording_selection' ->> 'rule'
  FROM public.bunny_recording_sources s
  JOIN public.bunny_recording_discoveries d ON d.lecture_id = s.lecture_id
 WHERE s.lecture_id = ANY(%s::uuid[])
 ORDER BY s.lecture_id, s.recording_part_index
"""


def load_recordings(connection, lecture_ids) -> dict:
    """Per lecture: verified (LOCATED) recordings, and the one the longest-recording rule selected."""
    found: dict = {}
    ids = [str(i) for i in lecture_ids]
    if not ids:
        return found
    for lid, part, status, classification, duration, selected, rule in connection.execute(RECORDINGS, (ids,)).fetchall():
        entry = found.setdefault(lid, {"parts": [], "selected_part": selected, "rule": rule})
        entry["parts"].append({"part": part, "status": status, "classification": classification,
                               "duration_seconds": round(float(duration), 1) if duration is not None else None})
    return {lid: _recording_summary(entry) for lid, entry in found.items()}


def _recording_summary(entry: dict) -> dict:
    located = [p for p in entry["parts"] if p["status"] == "LOCATED"]
    selected, rule = entry["selected_part"], entry["rule"]
    if selected is None and len(located) == 1:
        selected, rule = located[0]["part"], RULE_SINGLE_RECORDING
    elif selected is None and len(located) > 1:
        rule = "no_selection_on_record"           # located before longest_recording_v1: every part counted
    chosen = next((p for p in located if p["part"] == selected), None)
    return {"verified_count": len(located), "candidates": len(entry["parts"]), "selected_part": selected,
            "selection_rule": rule, "selected_duration_seconds": chosen["duration_seconds"] if chosen else None}


# --------------------------------------------------------------------------- categories
def categorize(fact: dict, row: dict, *, auto_release: bool | None = None) -> tuple[str, str]:
    """(category, reason) for one lecture, from the monitor's classification of its stored state."""
    state, lms, recording = row["state"], row["lms"]["status"], row["recording"]
    live = any(j["status"] in LIVE for j in fact.get("jobs") or [])
    if state in PUBLISHED_STATES:
        return ALREADY_PUBLISHED, "Released to the LMS; nothing to do."
    if state == monitor.WITHDRAWN:
        return SKIPPED, "Withdrawn by an operator; a backfill never re-releases it."
    if state == monitor.NOT_APPLICABLE:
        return SKIPPED, row["reason"]
    if state == monitor.SCHEDULED:
        return SKIPPED, "The lecture has not finished yet."
    if state == monitor.BLOCKED_IDENTITY:
        if lms in monitor.LMS_REVIEW:
            return MISSING_LMS_MATCH, f"LMS match is {lms}: an operator resolves the LMS session first."
        return REVIEW_REQUIRED, row["reason"]
    if state in (monitor.VIDEO_FAILED, monitor.RELEASED_NOT_PLAYABLE):
        return REVIEW_REQUIRED, row["reason"]
    if state == monitor.REVIEW_REQUIRED:
        reason = " ".join(str(x or "") for x in (recording.get("reason"), row["reason"]))
        if recording["status"] in ("REVIEW_REQUIRED", "PERMANENT_FAILURE") and any(h in reason for h in _MISSING_HINTS):
            return MISSING_RECORDING, row["reason"]
        return REVIEW_REQUIRED, row["reason"]
    if state == monitor.READY_TO_RELEASE:
        return ALREADY_PROCESSING, ("Verified READY; the automatic release publishes it." if auto_release is not False
                                    else "Verified READY; awaiting a manual release.")
    if live:
        return ALREADY_PROCESSING, "The publisher is already working on it: " + row["reason"]
    if lms in monitor.LMS_REVIEW:
        return MISSING_LMS_MATCH, f"LMS match is {lms}: an operator resolves the LMS session first."
    if lms == "PENDING":
        return READY_TO_QUEUE, "Not picked up yet: the run matches it to the LMS, then locates its Teams recording."
    if recording["status"] == "LOCATED":
        return READY_TO_QUEUE, "LMS match verified and recording located: the run imports the selected recording."
    return READY_TO_QUEUE, "LMS match verified: the run locates the Teams recording, then imports it."


def _import_estimate(category: str, row: dict, recording: dict | None) -> tuple[int, int]:
    """(known imports, possible imports) the run would add for one lecture."""
    if category != READY_TO_QUEUE or row["bunny"]["status"] not in ("NOT_IMPORTED",):
        return 0, 0
    if recording and recording["verified_count"]:
        return 1, 0
    return 0, 1


def build_preview(facts: list[dict], views: dict, recordings: dict, *, date_from: date, date_to: date,
                  library_id: int, now: datetime, auto_release: bool | None = None,
                  playback_enabled: bool = False, errors: dict | None = None) -> dict:
    """
    The preview of a range from data already read (facts: monitor.load_facts; views: monitor.resolve per
    lecture with a video; recordings: load_recordings). Pure: no I/O.
    """
    errors = errors or {}
    rows, counts = [], {c: 0 for c in CATEGORIES}
    per_day = {d.isoformat(): {"total": 0, "eligible": 0} for d in business_days(date_from, date_to)}
    known_imports = possible_imports = 0
    known_seconds = 0.0
    for fact in facts:
        lid = fact["lecture_id"]
        recording = recordings.get(lid)
        if lid in errors:
            category, reason, row = ERROR, errors[lid], None
        else:
            row = monitor.classify(fact, views.get(lid), playback_enabled=playback_enabled, library_id=library_id,
                                   now=now)
            category, reason = categorize(fact, row, auto_release=auto_release)
        counts[category] += 1
        day = per_day.setdefault(str(fact["session_date"]), {"total": 0, "eligible": 0})
        day["total"] += 1
        known, possible = _import_estimate(category, row, recording) if row else (0, 0)
        known_imports, possible_imports = known_imports + known, possible_imports + possible
        if category == READY_TO_QUEUE:
            day["eligible"] += 1
            if recording and recording["selected_duration_seconds"]:
                known_seconds += recording["selected_duration_seconds"]
        rows.append(monitor.redact({
            "lecture_id": lid, "subject": fact["subject"], "module": fact["module"],
            "session_date": str(fact["session_date"]), "scheduled_start": _iso(fact["scheduled_start"]),
            "scheduled_end": _iso(fact["scheduled_end"]),
            "lms": row["lms"] if row else {"status": fact["match_status"] or "PENDING"},
            "recording": {**(row["recording"] if row else {"status": fact["recording_status"] or "PENDING"}),
                          **(recording or {"verified_count": 0, "candidates": 0, "selected_part": None,
                                           "selection_rule": None, "selected_duration_seconds": None})},
            "bunny": {"status": row["bunny"]["status"], "video": row["video"]["status"]} if row else None,
            "release": row["release"]["status"] if row else None,
            "playback": row["playback"] if row else None,
            "monitor_state": row["state"] if row else None,
            "category": category, "proposed_action": ACTIONS[category], "reason": reason,
            "data_source": "DATABASE"}))
    summary = {
        "total": len(rows), "eligible": counts[READY_TO_QUEUE], "already_published": counts[ALREADY_PUBLISHED],
        "already_processing": counts[ALREADY_PROCESSING],
        "blocked": sum(counts[c] for c in BLOCKED_CATEGORIES), "skipped": counts[SKIPPED],
        "categories": counts, "estimated_imports": {"known": known_imports, "possible": possible_imports,
                                                    "upper_bound": known_imports + possible_imports},
        "known_selected_duration_seconds": round(known_seconds, 1), "per_day": per_day}
    return {"date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "days": len(per_day),
            "library_id": int(library_id), "generated_at": now.isoformat(), "data_source": "DATABASE",
            "summary": summary, "lectures": rows, "fingerprint": fingerprint(date_from, date_to, library_id, rows)}


def fingerprint(date_from: date, date_to: date, library_id: int, rows: list[dict]) -> str:
    """What a confirmation is bound to: the range, the library and each lecture's category."""
    material = json.dumps({"from": date_from.isoformat(), "to": date_to.isoformat(), "library": int(library_id),
                           "lectures": sorted([r["lecture_id"], r["category"]] for r in rows)}, sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()[:40]


def run_items(preview: dict) -> list[dict]:
    """The items a confirmed run records: every lecture of the range, with its starting status."""
    return [{"lecture_id": r["lecture_id"], "session_date": r["session_date"], "category": r["category"],
             "status": ITEM_STATUS[r["category"]],
             "reason": None if ITEM_STATUS[r["category"]] in ("PENDING", "PROCESSING") else r["category"]}
            for r in preview["lectures"]]


def _iso(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


# --------------------------------------------------------------------------- the coordinator
LIVE_LECTURE_JOBS = "SELECT count(*) FROM public.bunny_publisher_jobs WHERE lecture_id = %s AND status = ANY(%s)"
IMPORT_LOAD = """
SELECT count(*) FILTER (WHERE payload ? %(tag)s),
       count(*) FILTER (WHERE NOT payload ? %(tag)s AND status IN ('QUEUED', 'RETRY_WAIT'))
  FROM public.bunny_publisher_jobs
 WHERE job_kind = 'IMPORT_VIDEO' AND status IN ('QUEUED', 'RUNNING', 'RETRY_WAIT')
"""
CANCELLABLE_IMPORTS = """
SELECT j.job_id, j.lecture_id
  FROM public.bunny_publisher_jobs j
 WHERE j.job_kind = 'IMPORT_VIDEO' AND j.status IN ('QUEUED', 'RETRY_WAIT') AND j.locked_by IS NULL
   AND j.payload ->> %(tag)s = %(run)s
   AND NOT EXISTS (SELECT 1 FROM public.bunny_videos v
                    WHERE v.lecture_id = j.lecture_id AND v.bunny_library_id = %(library)s AND v.retired_at IS NULL
                      AND j.subject_key = 'lecture:' || v.lecture_id || ':part:' || v.recording_part_index
                      AND (v.import_status <> 'PENDING' OR v.bunny_video_id IS NOT NULL))
"""


class HistoricalBackfill:
    """The daemon's HISTORICAL_BACKFILL task: advance the one active run by one bounded step."""

    def __init__(self, *, config, connect, planner, queue=None, repository=None, clock=None):
        self.config, self.connect, self.planner = config, connect, planner
        self.queue = queue or PublisherQueueRepository()
        self.runs = repository or BackfillRepository()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # -- the task ------------------------------------------------------------------------------
    def run(self) -> dict:
        import psycopg
        connection = self.connect()
        connection.autocommit = True
        try:
            try:
                run = self.runs.active(connection)
            except psycopg.errors.UndefinedTable:
                return {"status": "NOT_INSTALLED"}            # migration 029 not applied: nothing to do
            if run is None:
                return {"status": "IDLE"}
            run_id = str(run["run_id"])
            if run["cancel_requested_at"] is not None:
                return self._cancel(connection, run)
            if self.config.library_id is None or int(run["library_id"]) != int(self.config.library_id):
                self._finish(connection, run_id, "FAILED", "HISTORICAL_BACKFILL_FAILED",
                             {"code": "LIBRARY_MISMATCH", "run_library_id": int(run["library_id"])})
                return {"status": "FAILED", "code": "LIBRARY_MISMATCH"}
            if run["status"] == "QUEUED":
                with connection.transaction():
                    self.runs.start(connection, run_id)
                    self.queue.event(connection, "HISTORICAL_BACKFILL_STARTED", actor="publisher:backfill",
                                     detail={"run_id": run_id, "date_from": str(run["date_from"]),
                                             "date_to": str(run["date_to"])})
            if run["lms_matched_at"] is None:
                lms = self.planner.match_range(run["date_from"], run["date_to"])
                done = lms.get("status") == "COMPLETED"
                self.runs.lms_matched(connection, run_id, str(lms.get("status") or "UNKNOWN"), done=done)
                if not done:
                    detail = {"status": "WAITING_FOR_LMS", "lms": str(lms.get("status")), "code": lms.get("error")}
                    self.runs.ticked(connection, run_id, detail)
                    return detail
            detail = self._advance(connection, run)
            self.runs.ticked(connection, run_id, detail)
            counts = self.runs.counts(connection, run_id)
            if counts["PENDING"] + counts["PROCESSING"] == 0:
                self._finish(connection, run_id, "COMPLETED", "HISTORICAL_BACKFILL_COMPLETED", {"counts": counts})
                detail["status"] = "COMPLETED"
            return {**detail, "counts": counts}
        finally:
            connection.close()

    def _finish(self, connection, run_id, status, event, detail) -> None:
        with connection.transaction():
            if self.runs.finish(connection, run_id, status):
                self.queue.event(connection, event, actor="publisher:backfill", detail={"run_id": run_id, **detail})

    # -- one step ------------------------------------------------------------------------------
    def _advance(self, connection, run) -> dict:
        run_id = str(run["run_id"])
        budget = self._import_budget(connection)
        out = {"status": "RUNNING", "started": 0, "advanced": 0, "jobs_created": 0, "imports_deferred": 0,
               "import_budget": budget}
        processing = self.runs.items(connection, run_id, statuses=("PROCESSING",))
        for item in processing:
            budget = self._step(connection, run_id, item, budget, out)
            out["advanced"] += 1
        room = max(0, self.config.backfill_max_active - len(processing))
        for item in self.runs.pending(connection, run_id, min(room, self.config.backfill_batch_size)):
            self.runs.update_item(connection, run_id, item["lecture_id"], status="PROCESSING", stage="LMS_MATCH")
            budget = self._step(connection, run_id, item, budget, out)
            out["started"] += 1
        return out

    def _import_budget(self, connection) -> int:
        """How many historical imports may be added now: none while a daily import waits."""
        if not self.config.writes_bunny:
            return 0
        historical, daily_waiting = connection.execute(IMPORT_LOAD, {"tag": HISTORICAL_TAG}).fetchone()
        if daily_waiting:
            return 0
        return max(0, self.config.backfill_max_imports - int(historical))

    def _classify(self, connection, lecture_id):
        library = int(self.config.library_id)
        fact = monitor.load_fact(connection, lecture_id, library)
        if fact is None:
            return None, None
        view = monitor.resolve(connection, fact, library_id=library, language=self.config.caption_language) \
            if monitor.needs_resolution(fact) else None
        return fact, monitor.classify(fact, view, playback_enabled=False, library_id=library, now=self.clock())

    def _step(self, connection, run_id, item, budget, out) -> int:
        """Re-read one lecture, settle its item or schedule its next work. Returns the remaining budget."""
        from app.publisher.progress import load_facts
        lid = str(item["lecture_id"])
        fact, row = self._classify(connection, lid)
        if fact is None:
            self.runs.update_item(connection, run_id, lid, status="FAILED", reason="LECTURE_NOT_FOUND")
            return budget
        status, reason = self._settled(fact, row)
        stage = stage_of(row)
        detail = progress_of(row)
        if status is not None:
            self.runs.update_item(connection, run_id, lid, status=status, reason=reason, stage=stage, detail=detail)
            return budget
        created = 0
        if row["lms"]["status"] == "VERIFIED" and row["recording"]["status"] != "LOCATED":
            with connection.transaction():
                facts = load_facts(connection, lid)
            if facts.duplicate_unresolved:
                self.runs.update_item(connection, run_id, lid, status="BLOCKED", reason=facts.duplicate_block,
                                      stage=stage, detail=detail)
                return budget
            for kind in TRACK_KINDS:
                if self.queue.enqueue_once(connection, job_kind=kind, subject_key=subject_key(lid), lecture_id=lid,
                                           priority=HISTORICAL_PRIORITY[kind], max_attempts=MAX_ATTEMPTS,
                                           payload={HISTORICAL_TAG: run_id}):
                    created += 1
        deferred = 0
        if row["lms"]["status"] == "VERIFIED" and row["recording"]["status"] == "LOCATED":
            planned = self.planner.plan_lecture(lid, priorities=HISTORICAL_PRIORITY, payload={HISTORICAL_TAG: run_id},
                                                allow_import=budget > 0)
            imports = planned["jobs_created"].get("IMPORT_VIDEO", 0)
            budget = max(0, budget - imports)
            created += sum(planned["jobs_created"].values())
            deferred = planned["imports_deferred"]
        out["jobs_created"] += created
        out["imports_deferred"] += deferred
        live = connection.execute(LIVE_LECTURE_JOBS, (lid, list(LIVE))).fetchone()[0]
        stalled = 0 if (live or created or deferred or stage == "RELEASE") else int(
            (item.get("detail") or {}).get("stalled_ticks", 0)) + 1
        if stalled >= STALL_TICKS:
            self.runs.update_item(connection, run_id, lid, status="BLOCKED", reason=f"NO_ACTIVE_WORK:{row['state']}",
                                  stage=stage, detail=detail)
            return budget
        self.runs.update_item(connection, run_id, lid, status="PROCESSING",
                              reason="IMPORT_WAITING_FOR_SLOT" if deferred else None, stage=stage,
                              detail={**detail, "stalled_ticks": stalled})
        return budget

    def _settled(self, fact, row) -> tuple[str | None, str | None]:
        """(final item status, reason), or (None, None) while the pipeline is still moving."""
        state, lms = row["state"], row["lms"]["status"]
        if state in PUBLISHED_STATES:
            return "COMPLETED", "RELEASED"
        if state in (monitor.NOT_APPLICABLE, monitor.WITHDRAWN):
            return "SKIPPED", state
        if lms in monitor.LMS_OUT_OF_SCOPE:
            return "SKIPPED", f"LMS_{lms}"
        if lms != "VERIFIED":
            return "BLOCKED", f"LMS_{lms}"           # the run matched the range first: an operator resolves it
        if state == monitor.VIDEO_FAILED:
            return "FAILED", row["reason"][:200]
        if state in (monitor.BLOCKED_IDENTITY, monitor.REVIEW_REQUIRED, monitor.RELEASED_NOT_PLAYABLE):
            return "BLOCKED", row["reason"][:200]
        if state == monitor.READY_TO_RELEASE and not self.config.auto_release:
            return "COMPLETED", "VERIFIED_READY_AWAITING_MANUAL_RELEASE"
        if state == monitor.SCHEDULED:
            return "SKIPPED", "NOT_ENDED"
        return None, None

    # -- cancellation --------------------------------------------------------------------------
    def _cancel(self, connection, run) -> dict:
        run_id = str(run["run_id"])
        cancelled_jobs = 0
        rows = connection.execute(CANCELLABLE_IMPORTS, {"tag": HISTORICAL_TAG, "run": run_id,
                                                       "library": int(run["library_id"])}).fetchall()
        for job_id, lecture_id in rows:
            with connection.transaction():
                if self.queue.operator_cancel(connection, job_id, actor=str(run["cancel_requested_by"] or "operator"),
                                              reason="historical backfill cancelled before the import reached Bunny",
                                              detail={"run_id": run_id}):
                    cancelled_jobs += 1
        with connection.transaction():
            items = self.runs.cancel_open_items(connection, run_id)
            if self.runs.finish(connection, run_id, "CANCELLED"):
                self.queue.event(connection, "HISTORICAL_BACKFILL_CANCELLED",
                                 actor=str(run["cancel_requested_by"] or "operator"),
                                 detail={"run_id": run_id, "imports_cancelled": cancelled_jobs, "items": items})
        return {"status": "CANCELLED", "imports_cancelled": cancelled_jobs, **items}


# --------------------------------------------------------------------------- per-lecture progress
def stage_of(row: dict) -> str:
    """Where the lecture is in Teams -> LMS match -> Bunny import -> encoding -> release -> LMS playback."""
    if row["lms"]["status"] != "VERIFIED":
        return "LMS_MATCH"
    if row["recording"]["status"] != "LOCATED":
        return "TEAMS"
    if row["bunny"]["status"] in ("NOT_IMPORTED", "UPLOADING"):
        return "BUNNY_IMPORT"
    if row["video"]["status"] != "VERIFIED":
        return "ENCODING"
    if row["release"]["status"] != "RELEASED":
        return "RELEASE"
    return "LMS_PLAYBACK"


def progress_of(row: dict) -> dict:
    """The stage statuses as codes (stored on the item; the run view re-reads them live)."""
    return {"teams": row["recording"]["status"], "lms": row["lms"]["status"], "import": row["bunny"]["status"],
            "encoding": row["video"]["status"], "captions": row["captions"]["status"],
            "release": row["release"]["status"], "playback": bool(row["playback"]["available"]),
            "state": row["state"]}


def pipeline(row: dict) -> list[dict]:
    """Teams -> LMS Match -> Bunny Import -> Encoding -> Captions -> Release -> LMS Playback, for display."""
    def step(key, label, status, done, failed=False):
        return {"key": key, "label": label, "status": status, "done": bool(done), "failed": bool(failed)}
    rec, lms, bunny, video = row["recording"]["status"], row["lms"]["status"], row["bunny"]["status"], row["video"]["status"]
    return [
        step("teams", "Teams", rec, rec == "LOCATED", rec in ("REVIEW_REQUIRED", "PERMANENT_FAILURE")),
        step("lms", "LMS Match", lms, lms == "VERIFIED", lms in monitor.LMS_REVIEW),
        step("import", "Bunny Import", bunny, bunny == "UPLOADED", bunny in ("FAILED", "REVIEW_REQUIRED")),
        step("encoding", "Encoding", video, video == "VERIFIED", video in ("FAILED", "REVIEW_REQUIRED")),
        step("captions", "Captions", row["captions"]["status"], row["captions"]["status"] == "VERIFIED"),
        step("release", "Release", row["release"]["status"], row["release"]["status"] == "RELEASED"),
        step("playback", "LMS Playback", row["playback"]["reason"] or "AVAILABLE", row["playback"]["available"]),
    ]


# --------------------------------------------------------------------------- operator retry
STOPPED_JOBS = """
SELECT DISTINCT ON (job_kind, subject_key) job_id, job_kind, status
  FROM public.bunny_publisher_jobs
 WHERE lecture_id = %s AND job_kind IN ('RESOLVE_RECORDING', 'IMPORT_VIDEO', 'POLL_ENCODING', 'PUBLISH_CAPTION')
 ORDER BY job_kind, subject_key, created_at DESC
"""


def retry_items(connection, run_id, lecture_ids, *, actor: str, reason: str) -> dict:
    """
    Re-open BLOCKED / FAILED lectures of a run (operator, audited). Each stopped publisher job of the lecture
    (FAILED / REVIEW_REQUIRED) is re-opened through operations.operator_retry - the same audited path as the
    monitor's retry, with its safeguards (never a second live job; an uncertain import is reconciled, never
    resubmitted). A lecture blocked on its LMS match makes the run re-match the range. The run is re-opened
    when it had finished. Runs in the caller's transaction-free autocommit connection.
    """
    from app.publisher.operations import OperatorActionRefused, operator_retry
    runs = BackfillRepository()
    run = runs.get(connection, run_id)
    if run is None:
        raise OperatorActionRefused("RUN_NOT_FOUND", "no such historical backfill run")
    if run["status"] == "CANCELLED":
        raise OperatorActionRefused("RUN_CANCELLED", "a cancelled run is not retried; start a new run")
    wanted = {str(i) for i in lecture_ids} if lecture_ids else None
    items = [i for i in runs.items(connection, run_id, statuses=("BLOCKED", "FAILED"))
             if wanted is None or str(i["lecture_id"]) in wanted]
    if not items:
        raise OperatorActionRefused("NOTHING_TO_RETRY", "no blocked or failed lecture to retry in this run")
    rematch = any(str(i["reason"] or "").startswith("LMS_") or i["initial_category"] == MISSING_LMS_MATCH for i in items)
    with connection.transaction():
        reopened = runs.reopen(connection, run_id, rematch_lms=rematch)
    if reopened is None:
        raise OperatorActionRefused("ANOTHER_RUN_ACTIVE", "another historical run is active; retry after it ends")
    jobs, refused, lectures = [], [], 0
    for item in items:
        lid = str(item["lecture_id"])
        for job_id, kind, status in connection.execute(STOPPED_JOBS, (lid,)).fetchall():
            if status not in ("FAILED", "REVIEW_REQUIRED"):
                continue
            try:
                jobs.append(operator_retry(connection, str(job_id), actor=actor,
                                           reason=f"historical backfill {run_id} retry: {reason}")["job_id"])
            except OperatorActionRefused as refusal:
                refused.append({"lecture_id": lid, "job_kind": kind, "code": refusal.code})
        with connection.transaction():
            if runs.reopen_item(connection, run_id, lid):
                lectures += 1
    with connection.transaction():
        PublisherQueueRepository().event(connection, "HISTORICAL_BACKFILL_RETRY", actor=actor,
                                         detail={"run_id": str(run_id), "lectures": lectures, "jobs_reopened": len(jobs),
                                                 "lms_rematch": rematch, "reason": reason[:300]})
    return {"run_id": str(run_id), "lectures_reopened": lectures, "jobs_reopened": len(jobs), "refused": refused,
            "lms_rematch": rematch}


# --------------------------------------------------------------------------- schema (proposed migration 029)
MIGRATION = Path(__file__).resolve().parents[1] / "db" / "migrations_proposed" / "029_create_publisher_backfill_tables.sql"
SCHEMA_TABLES = ("bunny_backfill_runs", "bunny_backfill_items")


def schema_status(connection) -> dict:
    """Which of 029's tables exist, and how many runs are recorded (read-only)."""
    present = {t: connection.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{t}",)).fetchone()[0]
               for t in SCHEMA_TABLES}
    runs = connection.execute("SELECT count(*) FROM public.bunny_backfill_runs").fetchone()[0]         if present["bunny_backfill_runs"] else None
    return {"tables": present, "installed": all(present.values()), "runs": runs}


def install_schema(connection) -> dict:
    """
    Apply 029 once, in its own transaction (the file's BEGIN / COMMIT), on an autocommit connection. Refuses a
    half-installed schema rather than guessing; an installed schema is left exactly as it is.
    """
    before = schema_status(connection)
    if before["installed"]:
        return {**before, "applied": False, "status": "ALREADY_INSTALLED"}
    if any(before["tables"].values()):
        return {**before, "applied": False, "status": "REFUSED_PARTIAL_SCHEMA"}
    connection.execute(MIGRATION.read_text(encoding="utf-8"))
    return {**schema_status(connection), "applied": True, "status": "APPLIED"}
