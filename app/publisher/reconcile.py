"""
Reconciliation and alerts for the continuous publisher.

LIGHT (every 6 hours) and FULL (daily at 05:00 Africa/Cairo) runs compare
every track against what should exist and keep ONE open alert per condition
(bunny_publisher_alerts, deduplicated); a condition that clears resolves its alert.

    LIGHT   stuck / final-attempt jobs, review backlog, stalled encodings,
            uncertain Fetch submissions, failed cleanups, missing recording,
            transcript, caption or LMS mapping, LMS-only sessions, fragments
            waiting for a decision, stale heartbeats
    FULL    LIGHT + calendar discovery and LMS matching over the whole lookback
            window (the catch-up after downtime) + a Bunny library scan:
            orphan videos, duplicate identity tokens, mapped videos missing in
            Bunny + the daily digest

DELIVERY IS NOT ENABLED IN THIS PHASE. AlertDispatcher renders a CRITICAL
alert's immediate notice and the daily digest, records notification_status
(DISABLED when a destination is configured, NOT_SENT otherwise) and sends
nothing. Nothing in an alert is a URL, a key or transcript text.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from app.db.repositories.bunny_publisher import BunnyVideoRepository
from app.db.repositories.publisher_ops import PublisherAlertRepository, PublisherReconciliationRepository
from app.db.repositories.publisher_queue import PublisherQueueRepository
from app.publisher.progress import NEEDS_DECISION, load_facts

log = logging.getLogger(__name__)
CRITICAL, WARNING, DIGEST = "CRITICAL", "WARNING", "DIGEST"
LIGHT, FULL = "LIGHT", "FULL"
_TOKEN = re.compile(r"rec-[0-9a-f]{8}")

ELIGIBLE_ENDED = """
SELECT l.lecture_id, l.session_date, COALESCE(e.match_status, 'PENDING'), COALESCE(d.status, 'PENDING'),
       COALESCE((SELECT s.selection_status FROM public.lecture_transcript_selections s
                  WHERE s.lecture_id = l.lecture_id AND s.selection_version = %(selection)s), 'PENDING')
  FROM public.lecture_sessions l
  LEFT JOIN public.bunny_lms_match_evaluations e ON e.lecture_id = l.lecture_id AND e.is_current
  LEFT JOIN public.bunny_recording_discoveries d ON d.lecture_id = l.lecture_id
 WHERE l.session_date >= %(start)s AND (%(end)s::date IS NULL OR l.session_date <= %(end)s)
   AND NOT l.is_cancelled
   AND l.metadata -> 'duplicate_suppression' IS NULL
   AND l.scheduled_end < %(overdue)s
   AND COALESCE(e.match_status, 'PENDING') NOT IN ('TEST_EXCLUDED', 'OUT_OF_LMS_SCOPE', 'LI_DUPLICATE')
"""


class AlertDispatcher:
    """Renders notices; delivery is switched off in this phase (records why nothing was sent)."""

    def __init__(self, config, alerts: PublisherAlertRepository | None = None):
        self.config = config
        self.alerts = alerts or PublisherAlertRepository()
        self.rendered: list[str] = []      # what WOULD have been sent (tests and dry runs read it)

    @property
    def notification_status(self) -> str:
        return "DISABLED" if self.config.alert_destination else "NOT_SENT"

    @staticmethod
    def render(alert: dict) -> str:
        lecture = f" lecture {alert['lecture_id']}" if alert.get("lecture_id") else ""
        return f"[{alert['severity']}] {alert['kind']}{lecture}: {alert['summary']}"

    @staticmethod
    def render_digest(summary: dict, day) -> str:
        lines = [f"KBC Bunny publisher daily digest {day}"]
        for key in sorted(summary):
            value = summary[key]
            if isinstance(value, (int, float, str)):
                lines.append(f"  {key}: {value}")
        return "\n".join(lines)

    def immediate(self, connection, alert: dict) -> None:
        self.rendered.append(self.render(alert))
        self.alerts.mark_delivery(connection, alert["alert_id"], self.notification_status)

    def digest(self, connection, alert: dict, text: str) -> None:
        self.rendered.append(text)
        self.alerts.mark_delivery(connection, alert["alert_id"], self.notification_status)


class Reconciler:
    def __init__(self, *, config, connect, bunny=None, planner=None, dispatcher=None, clock=None, queue=None):
        self.config = config
        self.connect = connect
        self.bunny = bunny                    # stream client of config.library_id (FULL scan), or None
        self.planner = planner
        self.queue = queue or PublisherQueueRepository()
        self.alerts = PublisherAlertRepository()
        self.runs = PublisherReconciliationRepository()
        self.dispatcher = dispatcher or AlertDispatcher(config, self.alerts)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self, kind: str = LIGHT) -> dict:
        now = self.clock()
        summary: dict = {"kind": kind}
        if kind == FULL and self.planner is not None:
            start, today = self.planner.window(now)
            days = [start + timedelta(days=n) for n in range((today - start).days + 1)]
            summary["catch_up"] = self.planner.tick(calendar_days=days)
        connection = self.connect()
        connection.autocommit = True
        try:
            run_id = self.runs.start(connection, kind)
            try:
                findings = self._light(connection, now)
                if kind == FULL:
                    findings.update(self._bunny_scan(connection))
                summary.update(self._publish(connection, findings, now))
                if kind == FULL:
                    summary["digest"] = self._digest(connection, summary, now)
                self.runs.finish(connection, run_id, status="SUCCEEDED", summary=_jsonable(summary))
            except Exception as exc:
                self.runs.finish(connection, run_id, status="FAILED", summary={"error": type(exc).__name__})
                raise
        finally:
            connection.close()
        return summary

    # -- findings: kind -> [(dedupe_key, severity, summary, detail, lecture_id)] ---------------------
    def _light(self, connection, now) -> dict:
        from app.transcripts.selection import SELECTION_VERSION
        cfg, lease = self.config, timedelta(seconds=self.config.job_lease_seconds)
        found: dict[str, list] = {k: [] for k in (
            "JOB_STUCK", "JOB_REVIEW_BACKLOG", "ENCODING_STALLED", "IMPORT_UNCERTAIN", "CLEANUP_FAILED",
            "MISSING_RECORDING", "MISSING_TRANSCRIPT", "MISSING_LMS_MAPPING", "MISSING_CAPTION", "LMS_ONLY_SESSION",
            "FRAGMENT_DECISION_NEEDED", "DUPLICATE_BLOCKED", "HEARTBEAT_STALE")}
        with connection.transaction():
            expired = self.queue.expire_final_attempts(connection, lease_seconds=cfg.job_lease_seconds)
        # Job leases and heartbeats are stamped by the database clock: judged by it too.
        for job_id, kind, locked_at in connection.execute(
                "SELECT job_id, job_kind, locked_at FROM public.bunny_publisher_jobs WHERE status = 'RUNNING' "
                "AND locked_at < now() - make_interval(secs => %s)", (2 * lease.total_seconds(),)).fetchall():
            found["JOB_STUCK"].append((f"job_stuck:{job_id}", WARNING, f"{kind} job running since {locked_at:%Y-%m-%d %H:%M}",
                                       {"job_id": str(job_id), "job_kind": kind}, None))
        backlog = connection.execute("SELECT job_kind, count(*) FROM public.bunny_publisher_jobs "
                                     "WHERE status IN ('REVIEW_REQUIRED', 'FAILED') GROUP BY 1").fetchall()
        if backlog:
            counts = {k: n for k, n in backlog}
            found["JOB_REVIEW_BACKLOG"].append(("job_review_backlog", WARNING,
                                                f"{sum(counts.values())} jobs wait for an operator", counts, None))
        library = cfg.library_id
        if library is not None:
            for row in BunnyVideoRepository().for_library(connection, library):
                if row["retired_at"] is not None:
                    continue
                lid, part, rid = str(row["lecture_id"]), row["recording_part_index"], str(row["bunny_video_record_id"])
                cleanup = (row["metadata"] or {}).get("cleanup")
                if row["import_status"] == "IDENTIFIED" and row["encoding_status"] in ("NOT_STARTED", "IN_PROGRESS") \
                        and row["video_identified_at"] and row["video_identified_at"] < now - cfg.encoding_stall_after:
                    found["ENCODING_STALLED"].append((f"encoding_stalled:{rid}", WARNING,
                                                      f"part {part} encoding has not finished", {"part": part}, lid))
                if row["import_status"] in ("SUBMISSION_UNCERTAIN", "SUBMITTING") or (
                        row["import_status"] == "REVIEW_REQUIRED" and row["bunny_video_id"] is None):
                    found["IMPORT_UNCERTAIN"].append((f"import_uncertain:{rid}", CRITICAL,
                                                      f"part {part} import outcome unresolved ({row['import_status']})",
                                                      {"part": part, "code": row["last_error_code"]}, lid))
                if cleanup in ("DELETE_FAILED", "DELETE_UNCERTAIN"):
                    found["CLEANUP_FAILED"].append((f"cleanup_failed:{rid}", CRITICAL,
                                                    f"part {part} failed video could not be removed",
                                                    {"part": part, "bunny_video_id": str(row["bunny_video_id"])}, lid))
                if row["encoding_status"] == "VERIFIED" and row["media_verified_at"] \
                        and row["media_verified_at"] < now - timedelta(hours=6):
                    current = connection.execute(
                        "SELECT p.publication_status FROM public.bunny_caption_current c "
                        "JOIN public.bunny_caption_publications p ON p.publication_id = c.publication_id "
                        "WHERE c.bunny_library_id = %s AND c.bunny_video_id = %s AND c.language_code = %s",
                        (library, row["bunny_video_id"], cfg.caption_language)).fetchone()
                    if not current or current[0] != "VERIFIED":
                        found["MISSING_CAPTION"].append((f"missing_caption:{rid}", WARNING,
                                                         f"part {part} video verified but has no verified caption",
                                                         {"part": part}, lid))
        if cfg.start_date:
            for lid, day, lms, recording, transcript in connection.execute(ELIGIBLE_ENDED, {
                    "selection": SELECTION_VERSION, "start": cfg.start_date, "end": cfg.end_date,
                    "overdue": now - timedelta(hours=24)}).fetchall():
                lid = str(lid)
                if recording not in ("LOCATED", "NOT_APPLICABLE"):
                    found["MISSING_RECORDING"].append((f"missing_recording:{lid}", WARNING,
                                                       f"{day}: recording {recording}", {"status": recording}, lid))
                if transcript != "SELECTED":
                    found["MISSING_TRANSCRIPT"].append((f"missing_transcript:{lid}", WARNING,
                                                        f"{day}: transcript {transcript}", {"status": transcript}, lid))
                if lms != "VERIFIED":
                    found["MISSING_LMS_MAPPING"].append((f"missing_lms:{lid}", WARNING,
                                                         f"{day}: LMS occurrence {lms}", {"status": lms}, lid))
                if recording == "LOCATED":
                    facts = load_facts(connection, lid)
                    waiting = [p["part_index"] for p in facts.parts if facts.policy(p) == NEEDS_DECISION]
                    if waiting:
                        found["FRAGMENT_DECISION_NEEDED"].append((f"fragment_decision:{lid}", WARNING,
                                                                  f"{day}: part(s) {waiting} need an include/exclude "
                                                                  "decision", {"parts": waiting}, lid))
                    if facts.duplicate_block:
                        found["DUPLICATE_BLOCKED"].append((f"duplicate_blocked:{lid}", WARNING,
                                                           f"{day}: {facts.duplicate_block} - not imported until "
                                                           "the duplicate group is resolved",
                                                           {"case": facts.duplicate_case,
                                                            "role": facts.duplicate_role}, lid))
            for occurrence, meeting_type, start, status in connection.execute(
                    "SELECT lms_occurrence_id, lms_meeting_type, lms_scheduled_start, coverage_status "
                    "FROM public.bunny_lms_occurrence_coverage WHERE coverage_status = 'LMS_SESSION_NOT_DISCOVERED' "
                    "AND lms_scheduled_start >= %s AND (%s::date IS NULL OR lms_scheduled_start < %s::date + 1)",
                    (cfg.start_date, cfg.end_date, cfg.end_date)).fetchall():
                found["LMS_ONLY_SESSION"].append((f"lms_only:{occurrence}", WARNING,
                                                  f"LMS {meeting_type} session on {start:%Y-%m-%d} has no "
                                                  "Lecture Intelligence lecture",
                                                  {"lms_occurrence_id": occurrence, "meeting_type": meeting_type}, None))
        for worker, seen in connection.execute(
                "SELECT worker_id, seen_at FROM public.bunny_publisher_heartbeats "
                "WHERE seen_at < now() - make_interval(secs => %s) "
                "AND NOT coalesce((detail ->> 'stopped')::boolean, false)",
                (cfg.heartbeat_stale_after.total_seconds(),)).fetchall():
            found["HEARTBEAT_STALE"].append((f"heartbeat:{worker}", CRITICAL,
                                             f"worker {worker} last seen {seen:%Y-%m-%d %H:%M} UTC",
                                             {"worker_id": worker}, None))
        found["_expired_final_attempts"] = len(expired)
        return found

    def _bunny_scan(self, connection) -> dict:
        found = {"ORPHAN_BUNNY_VIDEO": [], "DUPLICATE_BUNNY_VIDEO": [], "VIDEO_MISSING_IN_BUNNY": []}
        library = self.config.library_id
        if self.bunny is None or library is None:
            return found
        videos, page = [], 1
        while True:
            body = self.bunny.list_videos(page=page, items_per_page=1000)
            videos += body["items"]
            if page * int(body.get("itemsPerPage") or 1000) >= int(body.get("totalItems") or 0) or page >= 200:
                break
            page += 1
        rows = BunnyVideoRepository().for_library(connection, library)
        known = {str(r["bunny_video_id"]).lower() for r in rows if r["bunny_video_id"]}
        live = {str(r["bunny_video_id"]).lower(): r for r in rows if r["bunny_video_id"] and r["retired_at"] is None}
        by_token: dict[str, list] = {}
        present = set()
        for video in videos:
            guid = str(video.get("guid") or "").lower()
            present.add(guid)
            token = _TOKEN.search(str(video.get("title") or ""))
            if token:
                by_token.setdefault(token.group(0), []).append(guid)
            if guid not in known:
                found["ORPHAN_BUNNY_VIDEO"].append((f"orphan:{guid}", WARNING, "Bunny video with no publisher row",
                                                    {"bunny_video_id": guid, "token": token.group(0) if token else None},
                                                    None))
        for token, guids in by_token.items():
            if len(guids) > 1:
                found["DUPLICATE_BUNNY_VIDEO"].append((f"duplicate:{token}", CRITICAL,
                                                       f"{len(guids)} Bunny videos carry {token}",
                                                       {"token": token, "bunny_video_ids": sorted(guids)}, None))
        retired = 0
        for guid, row in live.items():
            if guid not in present:
                if self._retire_unselected_deleted(connection, row):
                    retired += 1
                    continue
                found["VIDEO_MISSING_IN_BUNNY"].append((f"missing_in_bunny:{guid}", CRITICAL,
                                                        f"part {row['recording_part_index']} video is not in Bunny",
                                                        {"bunny_video_id": guid}, str(row["lecture_id"])))
        found["_retired_deleted_unselected_videos"] = retired
        return found

    def _retire_unselected_deleted(self, connection, row) -> bool:
        """
        A live row whose Bunny video an operator deleted, for a part the lecture no longer delivers
        (EXCLUDE: not the selected recording, or an operator's exclusion). Retired - never deleted -
        once Bunny itself answers 404 for that exact video id, with an audit event; the source's
        generation count is untouched. An INCLUDED part's missing video stays a CRITICAL alert.
        """
        from app.bunny.client import BunnyError
        from app.publisher.progress import EXCLUDE
        lecture_id, part_index = str(row["lecture_id"]), row["recording_part_index"]
        with connection.transaction():
            facts = load_facts(connection, lecture_id)
        part = facts.part(part_index)
        if part is None or facts.policy(part) != EXCLUDE:
            return False
        try:
            self.bunny.get_video(str(row["bunny_video_id"]))
            return False                                   # still there: the listing was incomplete
        except BunnyError as error:
            if error.status != 404:
                return False
        changes = {"visibility_state": "WITHDRAWN"} if row["visibility_state"] == "READY_FOR_LMS" else {}
        with connection.transaction():
            done = BunnyVideoRepository().transition(
                connection, row["bunny_video_record_id"],
                expect={"retired_at": None, "bunny_video_id": row["bunny_video_id"]},
                retired_at=self.clock(), retire_reason="VIDEO_DELETED_IN_BUNNY_PART_NOT_DELIVERED",
                metadata={"cleanup": "DELETED_OUTSIDE_PUBLISHER"}, **changes)
            if done is None:
                return False
            self.queue.event(connection, "VIDEO_RETIRED_DELETED_IN_BUNNY", lecture_id=lecture_id, detail={
                "part": part_index, "bunny_video_id": str(row["bunny_video_id"]),
                "record_id": str(row["bunny_video_record_id"]), "previous_visibility": row["visibility_state"],
                "selected_part": facts.selected_part})
        return True

    # -- alerts ------------------------------------------------------------------------------
    def _publish(self, connection, findings, now) -> dict:
        counts = {}
        for kind, items in findings.items():
            if kind.startswith("_"):
                counts[kind.lstrip("_")] = items
                continue
            keys = []
            for key, severity, text, detail, lecture_id in items:
                with connection.transaction():
                    alert, created = self.alerts.raise_alert(connection, severity=severity, kind=kind,
                                                             dedupe_key=key, summary=text, detail=detail,
                                                             lecture_id=lecture_id, now=now)
                    if created and severity == CRITICAL:
                        self.dispatcher.immediate(connection, alert)
                keys.append(key)
            with connection.transaction():
                self.alerts.resolve_absent(connection, kind, keys, now=now)
            counts[kind.lower()] = len(items)
        return counts

    def _digest(self, connection, summary, now) -> dict:
        from app.publisher.planner import cairo_today
        day = cairo_today(now)
        text = AlertDispatcher.render_digest(summary, day)
        with connection.transaction():
            alert, _ = self.alerts.raise_alert(connection, severity=DIGEST, kind="DAILY_DIGEST",
                                               dedupe_key=f"digest:{day.isoformat()}",
                                               summary=f"daily digest {day.isoformat()}",
                                               detail={k: v for k, v in summary.items() if isinstance(v, int)}, now=now)
            self.alerts.resolve_absent(connection, "DAILY_DIGEST", [f"digest:{day.isoformat()}"], now=now)
            self.dispatcher.digest(connection, alert, text)
        return {"alert_id": str(alert["alert_id"]), "notification_status": self.dispatcher.notification_status}


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)
