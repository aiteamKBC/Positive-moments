"""
Operations Backfill: historical recovery over a business-date range.

WHAT THIS MODULE IS
-------------------
A loop and a checkpoint. That is the whole feature.

There is no second discovery pipeline, no second matcher, no second QA path
and no "backfill QA function". Recovering a historical day is exactly the
question the platform already answers every night - "what lectures existed on
this business date, and which of their stages are missing?" - so a backfill day
is one call to `PipelineOrchestrator.run_window` with `lookback_days=0` and
`discover=True`. Everything that call already does, backfill gets for free:

  * the read-only n8n ownership preflight, which fails closed
  * the cycle advisory lock, which is how backfill and the nightly scheduler
    coexist without two writers touching one lecture
  * active Aptem groups from `public.aptem_auto_extracting`
  * Microsoft calendar discovery and the production subject matcher
  * canonical lecture identity and registry reconciliation
  * the StageResolver, the StageRunner and every writer protection
  * a real `lecture_pipeline_runs` audit row per day

PREVIEW IS NOT A DRY-RUN CYCLE
------------------------------
`run_window(dry_run=True)` skips discovery entirely - by design, because a dry
run must not create registry rows. But an operator previewing September needs
to know what the CALENDAR holds, including lectures not yet in the registry.

So preview calls the same `LectureDiscoveryService.discover_day` with
`persist=False`. That is the production discovery implementation, the
production matcher and the production canonical identity function, run without
writing: no discovery run row, no registry upsert, no final persist. It reads
Microsoft Graph, which is a read, and it touches no table.
"""
import logging
import time
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta, timezone

from app.common.errors import PlatformError
from app.db.repositories.backfill import (
    MODE_EXECUTE,
    MODE_PREVIEW,
    BLOCKED_LEGACY_QA_ACTIVE,
    CANCELLED,
    COMPLETED,
    DAY_COMPLETED,
    DAY_DEFERRED_CYCLE_BUSY,
    DAY_FAILED,
    DAY_SKIPPED_CANCELLED,
    FAILED,
    BackfillRepository,
)
from app.orchestration.locks import SchedulerCycleBusy
from app.orchestration.n8n_preflight import LEGACY_QA_DISABLED, PRECHECK_SKIPPED
from app.orchestration.stages import (
    REVIEW_REQUIRED,
    RUN_TYPE_BACKFILL,
    WAITING,
)

BACKFILL_RUNNER_VERSION = "backfill_runner_v1"

# A guard, not a policy. September is 30 days; a year is a typo. The point is
# to refuse an obvious mistake before it spends hours of Graph quota, not to
# express an opinion about how much history is reasonable.
MAX_RANGE_DAYS = 120

INVALID_RANGE = "INVALID_BACKFILL_RANGE"
RANGE_TOO_LARGE = "BACKFILL_RANGE_TOO_LARGE"


def business_days(start: _date, end: _date) -> list[_date]:
    """
    Every calendar date in the inclusive range.

    Deliberately every date, not weekdays only: the college teaches when it
    teaches, and deciding which days "count" is the calendar's job, not this
    module's. A day with no lectures costs one discovery call and reports zero.
    """
    return [start + timedelta(days=offset)
            for offset in range((end - start).days + 1)]


def validate_range(requested_from: _date, requested_to: _date) -> None:
    if requested_to < requested_from:
        raise PlatformError(
            INVALID_RANGE,
            f"the end date {requested_to} is before the start date {requested_from}")
    span = (requested_to - requested_from).days + 1
    if span > MAX_RANGE_DAYS:
        raise PlatformError(
            RANGE_TOO_LARGE,
            f"{span} days requested; this version processes at most "
            f"{MAX_RANGE_DAYS} days in one run")


@dataclass
class DayPreview:
    business_date: _date
    calendar_events_considered: int = 0
    matched_lectures: int = 0
    known_lectures: int = 0
    newly_discoverable: int = 0
    already_complete: int = 0
    needs_processing: int = 0
    waiting: int = 0
    review_required: int = 0
    suppressed: int = 0
    discovery_status: str = "COMPLETED"
    error_code: str | None = None

    def as_dict(self) -> dict:
        return {"business_date": self.business_date.isoformat(),
                "calendar_events_considered": self.calendar_events_considered,
                "matched_lectures": self.matched_lectures,
                "known_lectures": self.known_lectures,
                "newly_discoverable": self.newly_discoverable,
                "already_complete": self.already_complete,
                "needs_processing": self.needs_processing,
                "waiting": self.waiting,
                "review_required": self.review_required,
                "suppressed": self.suppressed,
                "discovery_status": self.discovery_status,
                "error_code": self.error_code}


@dataclass
class BackfillPreview:
    requested_from: _date
    requested_to: _date
    days: list[DayPreview] = field(default_factory=list)
    active_groups: int = 0
    legacy_qa_precheck: dict = field(default_factory=dict)
    discovery_available: bool = True
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        totals = {key: sum(getattr(day, key) for day in self.days) for key in (
            "calendar_events_considered", "matched_lectures", "known_lectures",
            "newly_discoverable", "already_complete", "needs_processing",
            "waiting", "review_required", "suppressed")}
        return {
            "requested_from": self.requested_from.isoformat(),
            "requested_to": self.requested_to.isoformat(),
            "requested_days": len(self.days),
            "active_groups": self.active_groups,
            "discovery_available": self.discovery_available,
            "legacy_qa_precheck": self.legacy_qa_precheck,
            "writes_permitted": bool(
                self.legacy_qa_precheck.get("writes_permitted")),
            "totals": totals,
            "days": [day.as_dict() for day in self.days],
            "notes": self.notes,
            "read_only": True,
        }


class BackfillPreviewService:
    """
    Read-only. Answers "what would a backfill of this range find?".

    It writes nothing at all: no QA row, no Perfect row, no registry row, no
    discovery run row, no backfill row. It makes Microsoft Graph READS through
    the production discovery service with `persist=False`, and it reads the
    platform's own state through the same resolver the console already uses.
    """

    def __init__(self, *, resolver, discovery_service=None, preflight=None):
        self.resolver = resolver
        self.discovery_service = discovery_service
        self.preflight = preflight
        self.log = logging.getLogger(__name__)

    def preview(self, connection, requested_from: _date, requested_to: _date, *,
                aptem_connection=None, aptem_connection_factory=None,
                discover: bool = True) -> BackfillPreview:
        validate_range(requested_from, requested_to)
        window = business_days(requested_from, requested_to)
        result = BackfillPreview(requested_from=requested_from,
                                 requested_to=requested_to)

        if self.preflight is not None:
            result.legacy_qa_precheck = self.preflight.check()
            if result.legacy_qa_precheck.get("status") not in (
                    LEGACY_QA_DISABLED, PRECHECK_SKIPPED):
                result.notes.append(
                    "The legacy n8n QA execution path is active, so a backfill "
                    "run would be refused. This preview is unaffected.")

        # 1. What the platform already knows, from the same resolver the
        #    Operations console reads. No Graph call, no mutation.
        known = self._known_state(connection, window, result)

        # 2. What the calendar holds. Read-only discovery through the
        #    production implementation, or skipped entirely on request.
        if discover and self.discovery_service is not None:
            self._preview_discovery(connection, window, known, result,
                                    aptem_connection, aptem_connection_factory)
        else:
            result.discovery_available = False
            result.notes.append(
                "Calendar discovery was not run, so newly discoverable "
                "lectures are unknown. Counts describe lectures already in the "
                "registry only.")
        return result

    # -- what we already know ------------------------------------------------

    def _known_state(self, connection, window, result) -> dict:
        """
        One `resolver.for_day` call per date - the same call the orchestrator
        makes. Per-day rather than one window query because that is the
        resolver's actual interface, and writing a window variant here would
        be a second state model.
        """
        by_day: dict[_date, list[dict]] = {}
        for day in window:
            preview = DayPreview(business_date=day)
            try:
                states = self.resolver.for_day(connection, day)
            except Exception as exc:                            # noqa: BLE001
                self.log.warning("backfill preview state read failed for %s: %s",
                                 day, exc)
                result.notes.append(
                    f"Existing lecture state for {day.isoformat()} could not "
                    "be read; that day shows calendar discovery only.")
                states = []
            by_day[day] = states
            for state in states:
                preview.known_lectures += 1
                self._classify(state, preview)
            result.days.append(preview)
        return by_day

    @staticmethod
    def _classify(state: dict, preview: DayPreview) -> None:
        """
        Bucket one lecture using the resolver's own verdict.

        Nothing is recomputed here: `next_executable_action` and the stage
        states are what the nightly cycle acts on, so a preview that read them
        differently would be predicting a different pipeline.
        """
        stages = state.get("stages") or {}
        blocking = state.get("blocking_stage")
        blocking_state = (stages.get(blocking) or {}).get("state") if blocking else None

        # The resolver answers suppression itself, before it looks at a single
        # downstream stage. Re-deriving it from stage shapes would be guessing
        # at something already decided.
        if state.get("duplicate_suppression"):
            preview.suppressed += 1
            return
        if blocking_state == REVIEW_REQUIRED:
            preview.review_required += 1
            return
        if blocking_state == WAITING:
            preview.waiting += 1
            return
        if state.get("next_executable_action"):
            preview.needs_processing += 1
            return
        preview.already_complete += 1

    # -- what the calendar holds --------------------------------------------

    def _preview_discovery(self, connection, window, known, result,
                           aptem_connection, aptem_connection_factory) -> None:
        try:
            if aptem_connection_factory is not None:
                with aptem_connection_factory() as aptem:
                    self._discover_days(connection, aptem, window, known, result)
                return
            self._discover_days(connection, aptem_connection, window, known, result)
        except PlatformError as exc:
            result.discovery_available = False
            result.notes.append(
                f"The active Aptem group source was unavailable ({exc.code}), "
                "so calendar discovery was skipped. Counts describe lectures "
                "already in the registry only.")

    def _discover_days(self, connection, aptem, window, known, result) -> None:
        by_date = {day.business_date: day for day in result.days}
        for day in window:
            preview = by_date.get(day)
            if preview is None:
                continue
            try:
                # persist=False: production discovery, production matcher,
                # production canonical identity - writing nothing.
                outcome = self.discovery_service.discover_day(
                    connection, aptem, day, persist=False)
            except PlatformError as exc:
                preview.discovery_status = "FAILED"
                preview.error_code = exc.code
                continue

            preview.calendar_events_considered = int(
                outcome.get("calendar_events_found") or 0)
            # Reported by discovery itself, so preview and production cannot
            # disagree about how many groups were active.
            result.active_groups = max(
                result.active_groups,
                int(outcome.get("active_aptem_groups_loaded") or 0))
            matched = int(outcome.get("canonical_lecture_candidates") or 0)
            preview.matched_lectures = matched
            # A candidate the registry has never seen. Clamped at zero because
            # the registry can legitimately hold MORE lectures for a day than
            # today's calendar returns - a cancelled or rescheduled event still
            # has real history, and that is not a negative discovery.
            preview.newly_discoverable = max(matched - preview.known_lectures, 0)


class BackfillRunner:
    """
    The durable loop. Claims a run, walks its dates in order, checkpoints.

    Every day is one `run_window` call. The runner adds scheduling and
    bookkeeping around that call and makes no pipeline decision of its own.
    """

    def __init__(self, *, orchestrator, connection_factory,
                 aptem_connection_factory=None, repository=None,
                 preview_service=None, readonly_connection_factory=None,
                 recording_report=None,
                 runner_version: str = BACKFILL_RUNNER_VERSION, now=None):
        self.orchestrator = orchestrator
        # Recording Links (app/recordings/coverage.py). A PREVIEW day asks it
        # for a live, read-only verdict per lecture; an EXECUTE day asks it
        # what the orchestrator's RECORDING_LINK stage left behind. Either
        # way it only reads, and its answer is stored beside the day row.
        self.recording_report = recording_report
        # PREVIEW runs never touch the orchestrator. They use the read-only
        # preview service and a read-only connection, so a preview cannot
        # write even if a shared component changed underneath it.
        self.preview_service = preview_service
        self.readonly_connection_factory = (
            readonly_connection_factory or connection_factory)
        self.connection_factory = connection_factory
        self.aptem_connection_factory = aptem_connection_factory
        self.repository = repository or BackfillRepository()
        self.runner_version = runner_version
        self._now = now or time.monotonic
        self._stopping = False
        self.log = logging.getLogger(__name__)

    def request_stop(self, *_args) -> None:
        """Finish the day in flight, then exit. Never kill one mid-write."""
        self._stopping = True

    # -- one run -------------------------------------------------------------

    def run_claimed(self, run_id: str) -> dict:
        """
        Process one backfill run to completion, cancellation or failure.

        Each day is its own transaction. That is the interruption boundary: a
        crash mid-day loses that day's bookkeeping, the checkpoint does not
        move, and the day is re-run - which existing stage resolution turns
        into NOTHING_TO_DO for everything already finished.
        """
        with self.connection_factory() as connection:
            run = self.repository.get(connection, run_id)
        if run is None:
            return {"outcome": "NOT_FOUND", "backfill_run_id": run_id}

        mode = run.get("mode") or MODE_EXECUTE
        window = business_days(run["requested_from"], run["requested_to"])
        # Resume: skip every day before the checkpoint.
        checkpoint = run.get("current_business_date") or run["requested_from"]
        remaining = [day for day in window if day >= checkpoint]
        self.log.info("backfill run claimed", extra={"fields": {
            "service": "backfill_runner", "backfill_run_id": run_id,
            "requested_from": run["requested_from"].isoformat(),
            "requested_to": run["requested_to"].isoformat(),
            "resume_from": checkpoint.isoformat(), "mode": mode,
            "days_remaining": len(remaining)}})

        days_done = 0
        for index, day in enumerate(remaining):
            with self.connection_factory() as connection:
                if self.repository.cancel_requested(connection, run_id):
                    self.repository.record_day(
                        connection, run_id, business_date=day,
                        status=DAY_SKIPPED_CANCELLED)
                    self.repository.finish(connection, run_id, status=CANCELLED)
                    connection.commit()
                    self.log.info("backfill cancelled at a day boundary",
                                  extra={"fields": {"backfill_run_id": run_id,
                                                    "stopped_before": day.isoformat()}})
                    return {"outcome": "CANCELLED", "days_processed": days_done}

            if self._stopping:
                # Not a cancellation: the process is shutting down. The run
                # stays claimable and resumes from this exact day.
                self.log.info("backfill runner stopping; run left resumable",
                              extra={"fields": {"backfill_run_id": run_id,
                                                "resume_from": day.isoformat()}})
                return {"outcome": "STOPPED", "days_processed": days_done,
                        "resume_from": day.isoformat()}

            outcome = (self._preview_day(run_id, day) if mode == MODE_PREVIEW
                       else self._process_day(run_id, day))
            if outcome["status"] == "GLOBAL_BLOCKER":
                return {"outcome": outcome["run_status"],
                        "days_processed": days_done,
                        "error": outcome.get("error")}
            days_done += 1

            next_day = remaining[index + 1] if index + 1 < len(remaining) else None
            with self.connection_factory() as connection:
                self.repository.advance(connection, run_id,
                                        next_business_date=next_day,
                                        counts=outcome["counts"])
                connection.commit()

        with self.connection_factory() as connection:
            self.repository.finish(connection, run_id, status=COMPLETED)
            connection.commit()
        return {"outcome": COMPLETED, "days_processed": days_done}

    # -- one day, previewed --------------------------------------------------

    def _preview_day(self, run_id: str, day: _date) -> dict:
        """
        One day of READ-ONLY inspection.

        Same production discovery implementation with `persist=False` and the
        same resolver the console reads, on a connection PostgreSQL will not
        let anything write through.
        """
        started = time.monotonic()
        if self.preview_service is None:
            self._record(run_id, day, DAY_FAILED, None, {}, started,
                         error_code="PREVIEW_UNAVAILABLE",
                         error_message="this runner was built without a preview service")
            return {"status": "FAILED", "counts": {}}
        try:
            with self.readonly_connection_factory() as connection:
                preview = self.preview_service.preview(
                    connection, day, day,
                    aptem_connection_factory=self.aptem_connection_factory)
        except PlatformError as exc:
            self._record(run_id, day, DAY_FAILED, None, {}, started,
                         error_code=exc.code, error_message=str(exc)[:500])
            return {"status": "FAILED", "counts": {}}
        except Exception as exc:                                # noqa: BLE001
            self.log.exception("backfill preview day failed", extra={"fields": {
                "backfill_run_id": run_id, "business_date": day.isoformat()}})
            self._record(run_id, day, DAY_FAILED, None, {}, started,
                         error_code=type(exc).__name__, error_message=str(exc)[:500])
            return {"status": "FAILED", "counts": {}}

        found = preview.days[0] if preview.days else DayPreview(business_date=day)
        recordings = self._recordings(
            day, lambda connection: self.recording_report.preview_day(connection, day))
        day_counts = {
            "calendar_events_considered": found.calendar_events_considered,
            "matched_lectures": found.matched_lectures,
            "newly_discovered": found.newly_discoverable,
            "already_complete": found.already_complete,
            # In a preview nothing is processed; "needs processing" is what
            # an execute run WOULD act on, and calling it `processed` would
            # claim work that has not happened.
            "processed": 0,
            "waiting": found.waiting,
            "review_required": found.review_required,
            "failed": 0,
            "suppressed": found.suppressed,
        }
        self._record(run_id, day, DAY_COMPLETED, None, {}, started,
                     graph_calls=1 + recordings["graph_calls"],
                     day_counts=day_counts)
        self._store_recordings(run_id, day, recordings)
        return {"status": "COMPLETED", "counts": {
            "discovered": found.newly_discoverable,
            "matched": found.matched_lectures,
            "processed": 0,
            "already_complete": found.already_complete,
            "waiting": found.waiting,
            "review_required": found.review_required,
            "failed": 0,
            "suppressed": found.suppressed,
            # Carried so the run total can report what an execute run faces.
            "needs_processing": found.needs_processing,
        }}

    # -- one day, executed ----------------------------------------------------

    def _process_day(self, run_id: str, day: _date) -> dict:
        started = time.monotonic()
        # Anything the RECORDING_LINK stage writes during this day carries a
        # `written_at` at or after this instant; that is how the snapshot
        # tells "written by this run" from "already linked".
        written_since = datetime.now(timezone.utc)
        counts = {}
        try:
            with self.connection_factory() as connection:
                self.repository.heartbeat(connection, run_id, business_date=day)
                connection.commit()

            with self.connection_factory() as connection:
                summary = self.orchestrator.run_window(
                    connection, day, lookback_days=0,
                    run_type=RUN_TYPE_BACKFILL, dry_run=False, discover=True,
                    aptem_connection_factory=self.aptem_connection_factory)
                connection.commit()
        except SchedulerCycleBusy:
            # The nightly scheduler holds the cycle lock. Deferring is correct:
            # it is reconciling the same registry with the same rules, and the
            # day is re-attempted on the next pass rather than fought over.
            self._record(run_id, day, DAY_DEFERRED_CYCLE_BUSY, None, {},
                         started, error_code="SCHEDULER_CYCLE_BUSY",
                         error_message="another cycle held the lock; deferred")
            return {"status": "DEFERRED", "counts": {}}
        except PlatformError as exc:
            if self._is_global(exc):
                self._record(run_id, day, DAY_FAILED, None, {}, started,
                             error_code=exc.code, error_message=str(exc))
                with self.connection_factory() as connection:
                    self.repository.finish(connection, run_id, status=FAILED,
                                           error_summary=f"{exc.code}: {exc}")
                    connection.commit()
                return {"status": "GLOBAL_BLOCKER", "run_status": FAILED,
                        "error": exc.code}
            self._record(run_id, day, DAY_FAILED, None, {}, started,
                         error_code=exc.code, error_message=str(exc))
            return {"status": "FAILED", "counts": {}}
        except Exception as exc:                                # noqa: BLE001
            # One bad day must not abandon the month. The day is recorded as
            # failed and the run continues; a genuinely global fault will fail
            # every remaining day and be obvious in the audit.
            self.log.exception("backfill day failed", extra={"fields": {
                "backfill_run_id": run_id, "business_date": day.isoformat()}})
            self._record(run_id, day, DAY_FAILED, None, {}, started,
                         error_code=type(exc).__name__, error_message=str(exc)[:500])
            return {"status": "FAILED", "counts": {}}

        # The legacy QA preflight failed closed inside run_window. That is a
        # platform-wide blocker: every remaining day would refuse identically.
        if summary.get("status") == "BLOCKED_LEGACY_QA_ACTIVE":
            self._record(run_id, day, DAY_FAILED, summary.get("run_id"), {},
                         started, error_code="BLOCKED_LEGACY_QA_ACTIVE",
                         error_message="the legacy n8n QA path is active")
            with self.connection_factory() as connection:
                self.repository.finish(
                    connection, run_id, status=BLOCKED_LEGACY_QA_ACTIVE,
                    error_summary="the legacy n8n QA execution path is active; "
                                  "coded QA writes were refused")
                connection.commit()
            return {"status": "GLOBAL_BLOCKER",
                    "run_status": BLOCKED_LEGACY_QA_ACTIVE,
                    "error": "BLOCKED_LEGACY_QA_ACTIVE"}

        counts = self._counts_from(summary)
        recordings = self._recordings(
            day, lambda connection: (self.recording_report.execute_day(
                connection, day, written_since=written_since), 0))
        self._record(run_id, day, DAY_COMPLETED, summary.get("run_id"), counts,
                     started, graph_calls=summary.get("graph_calls", 0),
                     provider_calls=summary.get("provider_calls", 0))
        self._store_recordings(run_id, day, recordings)
        return {"status": "COMPLETED", "counts": counts}

    # -- Recording Links, per day --------------------------------------------

    def _recordings(self, day: _date, evaluate) -> dict:
        """
        Ask the recording report about one day, on a READ-ONLY connection.

        A failure here is recorded on the day and never fails it: the day's
        pipeline work is already committed, and a Graph refusal during a
        preview is information for the operator, not a reason to stop.
        """
        if self.recording_report is None:
            return {"items": None, "graph_calls": 0, "error_code": None}
        try:
            with self.readonly_connection_factory() as connection:
                # The production database sits behind a connection pooler;
                # never leave a prepared statement on a pooled backend.
                if hasattr(connection, "prepare_threshold"):
                    connection.prepare_threshold = None
                items, graph_calls = evaluate(connection)
        except PlatformError as exc:
            return {"items": [], "graph_calls": 0, "error_code": exc.code}
        except Exception as exc:                                # noqa: BLE001
            self.log.exception("backfill recording evaluation failed", extra={
                "fields": {"business_date": day.isoformat()}})
            return {"items": [], "graph_calls": 0, "error_code": type(exc).__name__}
        return {"items": items, "graph_calls": graph_calls, "error_code": None}

    def _store_recordings(self, run_id: str, day: _date, recordings: dict) -> None:
        if recordings["items"] is None:
            return
        with self.connection_factory() as connection:
            self.repository.replace_recording_items(
                connection, run_id, business_date=day, items=recordings["items"],
                error_code=recordings["error_code"])
            connection.commit()

    @staticmethod
    def _is_global(exc: PlatformError) -> bool:
        """
        A fault that will defeat every remaining day, not just this one.

        Anything else - a slow calendar, one unreadable transcript - is a
        day-level problem and the run continues.
        """
        return exc.code in ("DATABASE_ERROR", "DATABASE_UNAVAILABLE",
                            "GRAPH_AUTHENTICATION_FAILED",
                            "BLOCKED_LEGACY_QA_ACTIVE")

    def _counts_from(self, summary: dict) -> dict:
        """Translate the orchestrator's own report; invent nothing."""
        discovery = summary.get("discovery") or []
        discovered = sum(int(item.get("registry_rows_created") or 0)
                         for item in discovery if isinstance(item, dict))
        lectures = summary.get("lectures") or []

        # `_counts` in the orchestrator is a PARTITION: every lecture lands in
        # exactly one bucket and they sum to the lectures seen. Those numbers
        # are reported here unaltered, so the backfill totals and the pipeline
        # audit can never tell different stories about the same day.
        counts = summary.get("counts") or {}
        return {
            "discovered": discovered,
            "matched": len(lectures),
            "processed": sum(1 for item in lectures if item.get("changed")),
            "already_complete": int(counts.get("completed_count") or 0),
            "waiting": int(counts.get("waiting_count") or 0),
            "review_required": int(counts.get("review_count") or 0),
            "failed": int(counts.get("failed_count") or 0),
            # A suppressed duplicate never reaches the pipeline, so it has no
            # bucket of its own upstream; it is the skipped lecture the
            # resolver refused before any stage ran.
            "suppressed": int(counts.get("skipped_count") or 0),
        }

    def _record(self, run_id, day, status, pipeline_run_id, counts, started,
                *, graph_calls=0, provider_calls=0, error_code=None,
                error_message=None, day_counts=None) -> None:
        duration_ms = round((time.monotonic() - started) * 1000)
        day_counts = day_counts or {
            "calendar_events_considered": 0,
            "matched_lectures": counts.get("matched", 0),
            "newly_discovered": counts.get("discovered", 0),
            "already_complete": counts.get("already_complete", 0),
            "processed": counts.get("processed", 0),
            "waiting": counts.get("waiting", 0),
            "review_required": counts.get("review_required", 0),
            "failed": counts.get("failed", 0),
            "suppressed": counts.get("suppressed", 0),
        }
        with self.connection_factory() as connection:
            self.repository.record_day(
                connection, run_id, business_date=day, status=status,
                pipeline_run_id=pipeline_run_id, counts=day_counts,
                graph_calls=graph_calls, provider_calls=provider_calls,
                error_code=error_code, error_message=error_message,
                duration_ms=duration_ms)
            connection.commit()

    # -- the poll loop -------------------------------------------------------

    def run_forever(self, *, poll_seconds: int = 20, sleep=time.sleep,
                    max_runs: int | None = None) -> dict:
        """
        Claim work, do it, sleep, repeat.

        A poll loop rather than a queue because the repository already IS the
        queue: `backfill_runs` is durable, ordered and claimable with
        `FOR UPDATE SKIP LOCKED`. Adding Celery and Redis to move one row an
        operator creates a few times a month would be more moving parts, not
        fewer.
        """
        handled = 0
        while not self._stopping:
            if max_runs is not None and handled >= max_runs:
                break
            try:
                with self.connection_factory() as connection:
                    run_id = self.repository.claim(
                        connection, runner_version=self.runner_version)
                    connection.commit()
            except Exception as exc:                            # noqa: BLE001
                self.log.exception("backfill claim failed", extra={"fields": {
                    "service": "backfill_runner", "error": type(exc).__name__}})
                sleep(poll_seconds)
                continue

            if run_id is None:
                sleep(poll_seconds)
                continue

            handled += 1
            try:
                self.run_claimed(run_id)
            except Exception as exc:                            # noqa: BLE001
                self.log.exception("backfill run failed", extra={"fields": {
                    "service": "backfill_runner", "backfill_run_id": run_id,
                    "error": type(exc).__name__}})
                try:
                    with self.connection_factory() as connection:
                        self.repository.finish(
                            connection, run_id, status=FAILED,
                            error_summary=f"{type(exc).__name__}: {exc}"[:2000])
                        connection.commit()
                except Exception:                               # noqa: BLE001
                    self.log.exception("could not mark backfill run failed")
        return {"runs_handled": handled, "stopped": self._stopping}
