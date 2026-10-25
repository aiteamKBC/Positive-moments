"""
Lecture Publishing Monitor: the read model behind the operator dashboard (read-only).

Where every answer comes from
-----------------------------
* Stage FACTS (LMS match, Teams recording discovery, transcript selection, jobs, Bunny videos) come
  from ONE set-based query over the date range (FACTS) - no per-lecture round trips for them.
* Readiness, release and LMS playback availability are NOT re-derived here. For every lecture that
  has a live video in the delivery library, the caller resolves `delivery.lecture_delivery()` (the
  same rule the LMS API, the release command and AUTO_RELEASE use) and `classify()` reads its answer.
  A lecture without a video can be neither READY nor released, so it needs no resolution.
* `classify()` turns those two inputs into one row: a status per stage, one monitor STATE, a reason
  an operator can act on, and whether the publisher will retry by itself.

Captions are optional for playback (2026-10-25): a missing, late or failed transcript or caption
never makes a lecture "failed" or "needs attention"; it is reported in its own columns only.

Every count on the dashboard is a count of LECTURES (never of video parts or job attempts), over the
lectures whose Africa/Cairo business date (lecture_sessions.session_date) is in the chosen range. Each
KPI is defined once, in KPIS, and the table filter for a KPI uses the very same predicate - so the
number on a card and the rows behind it always agree.

Nothing here writes, calls Microsoft Graph or Bunny, or returns a URL, key or transcript text.
"""
from __future__ import annotations

from datetime import date, datetime

# --------------------------------------------------------------------------- monitor states
SCHEDULED = "SCHEDULED"                        # the lecture has not ended yet
NOT_TRACKED = "NOT_TRACKED"                    # no publisher evidence at all (yet)
WAITING_FOR_RECORDING = "WAITING_FOR_RECORDING"
RETRY_SCHEDULED = "RETRY_SCHEDULED"
PROCESSING = "PROCESSING"
READY_TO_RELEASE = "READY_TO_RELEASE"          # verified READY, not released yet
PLAYABLE = "PLAYABLE"
PLAYABLE_NO_CAPTIONS = "PLAYABLE_NO_CAPTIONS"
RELEASED_PLAYBACK_OFF = "RELEASED_PLAYBACK_OFF"  # released; LMS playback switched off globally
RELEASED_NOT_PLAYABLE = "RELEASED_NOT_PLAYABLE"  # released, but no longer READY
BLOCKED_IDENTITY = "BLOCKED_IDENTITY"          # LMS match / duplicate / part identity needs a person
VIDEO_FAILED = "VIDEO_FAILED"                  # Bunny import or encoding failed / needs review
REVIEW_REQUIRED = "REVIEW_REQUIRED"            # recording or other publisher review
WITHDRAWN = "WITHDRAWN"
NOT_APPLICABLE = "NOT_APPLICABLE"              # cancelled, duplicate entry, test / out of LMS scope

ATTENTION = (BLOCKED_IDENTITY, VIDEO_FAILED, REVIEW_REQUIRED, RELEASED_NOT_PLAYABLE)
AUTOMATIC = (SCHEDULED, WAITING_FOR_RECORDING, RETRY_SCHEDULED, PROCESSING, READY_TO_RELEASE)
STATES = (PLAYABLE, PLAYABLE_NO_CAPTIONS, RELEASED_PLAYBACK_OFF, READY_TO_RELEASE, PROCESSING, RETRY_SCHEDULED,
          WAITING_FOR_RECORDING, SCHEDULED, NOT_TRACKED, BLOCKED_IDENTITY, VIDEO_FAILED, REVIEW_REQUIRED,
          RELEASED_NOT_PLAYABLE, WITHDRAWN, NOT_APPLICABLE)

LMS_REVIEW = ("AMBIGUOUS", "RESCHEDULED_CANDIDATE", "NO_LMS_SESSION", "LMS_RECORD_INACTIVE", "CONFLICT", "STALE",
              "NO_MEETING_KEY")
LMS_OUT_OF_SCOPE = ("TEST_EXCLUDED", "OUT_OF_LMS_SCOPE", "LI_DUPLICATE")
LIVE_JOB = ("QUEUED", "RUNNING", "RETRY_WAIT")
STOPPED_JOB = ("FAILED", "REVIEW_REQUIRED")
VIDEO_JOBS = ("IMPORT_VIDEO", "POLL_ENCODING", "RECONCILE_BUNNY")
PIPELINE_JOBS = ("MATCH_LMS", "RESOLVE_RECORDING") + VIDEO_JOBS      # not transcript / caption work

# --------------------------------------------------------------------------- health thresholds
SCHEDULER_STALE_SECONDS = 300      # the scheduler heartbeats every tick (seconds); 5 min = not running
WORKER_STALE_SECONDS = 300         # an idle worker heartbeats every poll
WORKER_BUSY_SECONDS = 900          # a worker on a job heartbeats at claim; the job lease is 15 min
AUTO_RELEASE_RECENT_SECONDS = 900  # AUTO_RELEASE runs every discovery interval (5 min)
SCHEDULE_TASKS = ("DISCOVERY", "AUTO_RELEASE", "RECONCILE_LIGHT", "RECONCILE_FULL")
SAFE_DETAIL_KEYS = ("candidates", "eligible", "released", "unchanged", "deferred", "skipped", "refused", "error",
                    "code")

# --------------------------------------------------------------------------- the batched facts
_FACTS_SELECT = """
SELECT l.lecture_id::text, l.session_date, l.scheduled_start, l.scheduled_end, l.subject, l.module,
       l.is_cancelled, l.metadata -> 'duplicate_suppression' IS NOT NULL,
       m.match_status, m.requires_review, k.lms_occurrence_id, k.lecture_id IS NOT NULL,
       d.status, d.reason_code, d.review_reason, d.expected_part_count, d.located_part_count, d.next_check_after,
       s.selection_status,
       COALESCE(j.jobs, '[]'::jsonb), COALESCE(v.videos, '[]'::jsonb),
       GREATEST(l.updated_at, m.last_evaluated_at, d.updated_at, s.updated_at, j.updated_at, v.updated_at, e.at)
  FROM public.lecture_sessions l
  LEFT JOIN public.bunny_lms_match_evaluations m ON m.lecture_id = l.lecture_id AND m.is_current
  LEFT JOIN LATERAL (
        SELECT k.lms_occurrence_id, k.lecture_id FROM public.bunny_lms_occurrence_links k
         WHERE k.lecture_id = l.lecture_id AND k.is_current
         ORDER BY k.link_role = 'PRIMARY' DESC, k.created_at DESC LIMIT 1) k ON true
  LEFT JOIN public.bunny_recording_discoveries d ON d.lecture_id = l.lecture_id
  LEFT JOIN public.lecture_transcript_selections s
         ON s.lecture_id = l.lecture_id AND s.selection_version = %(selection_version)s
  LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object(
                   'kind', x.job_kind, 'status', x.status, 'error', x.last_error_code,
                   'next_attempt_at', x.next_attempt_at, 'attempts', x.attempt_count,
                   'max_attempts', x.max_attempts)) AS jobs,
               max(x.updated_at) AS updated_at
          FROM (SELECT DISTINCT ON (b.job_kind, b.subject_key) b.job_kind, b.status, b.last_error_code,
                       b.next_attempt_at, b.attempt_count, b.max_attempts, b.updated_at
                  FROM public.bunny_publisher_jobs b
                 WHERE b.lecture_id = l.lecture_id AND b.status <> 'CANCELLED'
                 ORDER BY b.job_kind, b.subject_key, b.created_at DESC) x) j ON true
  LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object(
                   'part', b.recording_part_index, 'import', b.import_status, 'encoding', b.encoding_status,
                   'visibility', b.visibility_state) ORDER BY b.recording_part_index) AS videos,
               max(b.updated_at) AS updated_at
          FROM public.bunny_videos b
         WHERE b.lecture_id = l.lecture_id AND b.bunny_library_id = %(library)s AND b.retired_at IS NULL) v ON true
  LEFT JOIN LATERAL (
        SELECT max(ev.at) AS at FROM public.bunny_publisher_events ev
         WHERE ev.lecture_id = l.lecture_id
           AND ev.event IN ('PUBLICATION_RELEASED', 'PUBLICATION_WITHDRAWN')) e ON true
"""
FACTS = _FACTS_SELECT + """
 WHERE l.session_date BETWEEN %(date_from)s AND %(date_to)s
 ORDER BY l.scheduled_start, l.lecture_id
"""
FACTS_ONE = _FACTS_SELECT + " WHERE l.lecture_id = %(lecture_id)s"
FACT_FIELDS = ("lecture_id", "session_date", "scheduled_start", "scheduled_end", "subject", "module", "cancelled",
               "suppressed", "match_status", "match_review", "occurrence_id", "link_current", "recording_status",
               "recording_reason", "recording_review_reason", "expected_parts", "located_parts", "next_check_after",
               "selection_status", "jobs", "videos", "updated_at")


def _selection_version() -> str:
    from app.transcripts.selection import SELECTION_VERSION
    return SELECTION_VERSION


def load_facts(connection, date_from: date, date_to: date, library_id: int) -> list[dict]:
    rows = connection.execute(FACTS, {"date_from": date_from, "date_to": date_to, "library": int(library_id),
                                      "selection_version": _selection_version()}).fetchall()
    return [dict(zip(FACT_FIELDS, r)) for r in rows]


def load_fact(connection, lecture_id: str, library_id: int) -> dict | None:
    row = connection.execute(FACTS_ONE, {"lecture_id": str(lecture_id), "library": int(library_id),
                                         "selection_version": _selection_version()}).fetchone()
    return dict(zip(FACT_FIELDS, row)) if row else None


def needs_resolution(fact: dict) -> bool:
    """Only a lecture with a live video in the library can be READY / released: resolve those."""
    return bool(fact["videos"])


def resolve(connection, fact: dict, *, library_id: int, language: str = "en") -> dict | None:
    """The delivery rule's answer for one lecture (the same call the LMS API makes)."""
    from app.publisher import delivery
    return delivery.lecture_delivery(connection, fact["lecture_id"], library_id=int(library_id), language=language,
                                     link_current=bool(fact["link_current"]))


# --------------------------------------------------------------------------- classification
def _jobs(fact, kinds=None):
    return [j for j in fact["jobs"] if kinds is None or j["kind"] in kinds]


def _earliest_retry(jobs) -> str | None:
    times = sorted(j["next_attempt_at"] for j in jobs if j["status"] == "RETRY_WAIT" and j.get("next_attempt_at"))
    return times[0] if times else None


def _iso(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _bunny_status(videos, view) -> tuple[str, str]:
    """(import status, encoding status) of the lecture's INCLUDED parts in the delivery library."""
    if not videos:
        return "NOT_IMPORTED", "NOT_STARTED"
    imports = [v["import"] for v in videos]
    encodings = [v["encoding"] for v in videos]
    included = view["parts"] if view else []
    missing_part = any(p["encoding"] == "NOT_IMPORTED" for p in included)
    if "FAILED" in imports:
        upload = "FAILED"
    elif "REVIEW_REQUIRED" in imports:
        upload = "REVIEW_REQUIRED"
    elif all(i == "IDENTIFIED" for i in imports) and not missing_part:
        upload = "UPLOADED"
    else:
        upload = "UPLOADING"
    if "FAILED" in encodings:
        encoding = "FAILED"
    elif "REVIEW_REQUIRED" in encodings:
        encoding = "REVIEW_REQUIRED"
    elif included and all(p["encoding"] == "VERIFIED" for p in included):
        encoding = "VERIFIED"
    elif all(e == "NOT_STARTED" for e in encodings):
        encoding = "NOT_STARTED"
    else:
        encoding = "IN_PROGRESS"
    return upload, encoding


def _transcript_status(fact) -> tuple[str, str | None]:
    if fact["selection_status"] == "SELECTED":
        return "AVAILABLE", None
    jobs = _jobs(fact, ("ACQUIRE_TRANSCRIPT",))
    if any(j["status"] in LIVE_JOB for j in jobs):
        return "WAITING", _earliest_retry(jobs)
    if fact["selection_status"] or any(j["status"] in STOPPED_JOB + ("SUCCEEDED",) for j in jobs):
        return "MISSING", None
    return "NOT_STARTED", None


def _caption_status(fact, view) -> str:
    if not view:
        return "NOT_AVAILABLE"
    if view["caption_status"] in ("VERIFIED", "PARTIAL"):
        return view["caption_status"]
    jobs = _jobs(fact, ("PUBLISH_CAPTION",))
    if any(j["status"] in LIVE_JOB for j in jobs):
        return "PENDING"
    if any(p["captions"] for p in view["parts"]):
        return "UNVERIFIED"                     # published, but the read-back never matched
    return "NOT_AVAILABLE"


def classify(fact: dict, view: dict | None, *, playback_enabled: bool, library_id: int, now: datetime) -> dict:
    """One dashboard row. `view` is delivery.lecture_delivery() for a lecture with a video, else None."""
    from app.publisher import delivery
    videos = fact["videos"]
    upload, encoding = _bunny_status(videos, view)
    transcript, transcript_retry = _transcript_status(fact)
    captions = _caption_status(fact, view)
    release = view["release"] if view else delivery.NOT_RELEASED
    if view:
        playback = delivery._playback(view["state"] == delivery.READY, view["state"], release, playback_enabled,
                                      library_id)
    else:
        playback = {"available": False, "reason": "NO_VIDEO"}
    blocking = list(view["blocking"]) if view else []
    match = fact["match_status"] or "PENDING"
    recording = fact["recording_status"] or "PENDING"
    pipeline_jobs = _jobs(fact, PIPELINE_JOBS)
    stopped = [j for j in pipeline_jobs if j["status"] in STOPPED_JOB]
    ended = fact["scheduled_end"] is not None and fact["scheduled_end"] <= now
    retry_at = None

    if fact["cancelled"]:
        state, reason = NOT_APPLICABLE, "The lecture was cancelled."
    elif fact["suppressed"]:
        state, reason = NOT_APPLICABLE, "A duplicate calendar entry: another lecture holds this meeting."
    elif match in LMS_OUT_OF_SCOPE:
        state, reason = NOT_APPLICABLE, f"Not published: LMS match {match.replace('_', ' ').lower()}."
    elif recording == "NOT_APPLICABLE":
        state, reason = NOT_APPLICABLE, "The publisher marked this lecture's recording as not applicable."
    elif release == delivery.WITHDRAWN:
        state, reason = WITHDRAWN, "Withdrawn from the LMS by an operator."
    elif playback["available"]:
        state = PLAYABLE if captions == "VERIFIED" else PLAYABLE_NO_CAPTIONS
        reason = "Released and available to the LMS." if state == PLAYABLE else \
            "Released and available to the LMS. Captions are optional and not available yet."
    elif release == delivery.RELEASED and playback["reason"] == "PLAYBACK_DISABLED":
        state, reason = RELEASED_PLAYBACK_OFF, "Released; LMS playback is switched off for every lecture."
    elif release == delivery.RELEASED:
        state = RELEASED_NOT_PLAYABLE
        reason = "Released, but no longer READY: " + (", ".join(blocking) or view["state"].replace("_", " ").lower())
    elif match in LMS_REVIEW or fact["match_review"] or (view and view["state"] == delivery.BLOCKED) or any(
            b.startswith("DUPLICATE_") or b.endswith("_NEEDS_DECISION") or b.startswith("LMS_") for b in blocking):
        state = BLOCKED_IDENTITY
        reason = (f"LMS match {match.replace('_', ' ').lower()}: an operator decides." if match in LMS_REVIEW
                  else "Identity needs an operator: " + (", ".join(blocking) or "see the lecture"))
    elif upload in ("FAILED", "REVIEW_REQUIRED") or encoding in ("FAILED", "REVIEW_REQUIRED") or any(
            j["kind"] in VIDEO_JOBS for j in stopped):
        state = VIDEO_FAILED
        codes = [j["error"] for j in stopped if j["kind"] in VIDEO_JOBS and j.get("error")]
        reason = "Bunny import or encoding needs an operator" + (f": {', '.join(codes)}." if codes else ".")
    elif recording in ("REVIEW_REQUIRED", "PERMANENT_FAILURE") or stopped or (
            view and view["state"] == delivery.REVIEW_REQUIRED):
        state = REVIEW_REQUIRED
        detail = fact["recording_review_reason"] or fact["recording_reason"] if recording in (
            "REVIEW_REQUIRED", "PERMANENT_FAILURE") else ", ".join(
            [j["error"] for j in stopped if j.get("error")] or blocking)
        reason = "Operator review required" + (f": {detail}." if detail else ".")
    elif view and view["state"] == delivery.READY:
        state, reason = READY_TO_RELEASE, "Verified and READY: automatic release picks it up on its next run."
    elif recording == "RETRY_SCHEDULED" or any(j["status"] == "RETRY_WAIT" for j in pipeline_jobs):
        state = RETRY_SCHEDULED
        retry_at = fact["next_check_after"] if recording == "RETRY_SCHEDULED" else _earliest_retry(pipeline_jobs)
        reason = "A temporary problem; the publisher retries automatically."
    elif not ended and not videos:
        state, reason = SCHEDULED, "The lecture has not finished yet."
    elif not videos and recording in ("WAITING", "PENDING") and (
            fact["recording_status"] or fact["jobs"] or fact["match_status"]):
        state = WAITING_FOR_RECORDING
        retry_at = fact["next_check_after"]
        reason = "Waiting for Teams to make the recording available; checked again automatically."
    elif not (fact["match_status"] or fact["recording_status"] or fact["jobs"] or videos):
        state, reason = NOT_TRACKED, "Not picked up by the publisher yet."
    else:
        state = PROCESSING
        if match == "PENDING":
            reason = "LMS matching is in progress."
        elif upload in ("NOT_IMPORTED", "UPLOADING"):
            reason = "The recording is being imported into Bunny."
        else:
            reason = "Bunny is encoding the video; it is verified when encoding finishes."

    return {
        "lecture_id": fact["lecture_id"], "session_date": _iso(fact["session_date"]),
        "scheduled_start": _iso(fact["scheduled_start"]), "scheduled_end": _iso(fact["scheduled_end"]),
        "subject": fact["subject"], "module": fact["module"],
        "lms": {"status": match, "occurrence_id": fact["occurrence_id"], "link_current": bool(fact["link_current"])},
        "recording": {"status": recording, "reason": fact["recording_reason"],
                      "expected_parts": fact["expected_parts"], "located_parts": fact["located_parts"]},
        "bunny": {"status": upload, "parts": len(videos)},
        "video": {"status": encoding},
        "transcript": {"status": transcript, "retry_at": _iso(transcript_retry)},
        "captions": {"status": captions},
        "release": {"status": release, "released_at": view["released_at"] if view else None},
        "playback": {"available": bool(playback["available"]), "reason": playback["reason"]},
        "state": state, "reason": reason, "needs_attention": state in ATTENTION,
        "automatic": state in AUTOMATIC, "retry_at": _iso(retry_at),
        "updated_at": _iso(fact["updated_at"]),
    }


# --------------------------------------------------------------------------- KPIs = filters
def _missing_recording(row) -> bool:
    # A lecture the publisher has not picked up (NOT_TRACKED: before its start, or not backfilled yet) is
    # not "missing a recording" - nobody has looked yet. It is counted in states, never here.
    return row["state"] not in (NOT_APPLICABLE, SCHEDULED, NOT_TRACKED) and row["recording"]["status"] != "LOCATED" \
        and row["bunny"]["status"] == "NOT_IMPORTED"


# key: (label, definition, predicate). The summary counts these; the table filter `kpi=<key>` uses them.
KPIS = {
    "discovered": ("Lectures discovered", "Every lecture whose Cairo date is in the range (cancelled included).",
                   lambda r: True),
    "located": ("Teams recordings located", "Lectures whose every expected Teams recording part was located.",
                lambda r: r["recording"]["status"] == "LOCATED"),
    "uploaded": ("Videos uploaded to Bunny", "Lectures whose every included part has a Bunny video (identified).",
                 lambda r: r["bunny"]["status"] == "UPLOADED"),
    "verified": ("Videos encoded & verified", "Lectures whose every included part is encoded and verified.",
                 lambda r: r["video"]["status"] == "VERIFIED"),
    "released": ("Released to LMS", "Lectures whose every included part is released (READY_FOR_LMS).",
                 lambda r: r["release"]["status"] == "RELEASED"),
    "playable": ("Available for LMS playback", "What the LMS API answers right now: playback.available is true.",
                 lambda r: r["playback"]["available"]),
    "missing": ("Missing / pending recordings", "Ended, applicable lectures the publisher is tracking with no "
                "located recording and no Bunny video yet (waiting, retrying or under review). Lectures not "
                "picked up by the publisher yet are not counted.", _missing_recording),
    "attention": ("Needs attention", "Lectures an operator must act on: identity, video failure, review, or "
                  "released but no longer READY. Missing captions never count here.",
                  lambda r: r["needs_attention"]),
}

FILTERS = {
    "video": ("NOT_IMPORTED", "UPLOADING", "IN_PROGRESS", "VERIFIED", "FAILED"),
    "playback": ("AVAILABLE", "NOT_AVAILABLE"),
    "captions": ("VERIFIED", "PARTIAL", "PENDING", "UNVERIFIED", "NOT_AVAILABLE"),
    "transcript": ("AVAILABLE", "WAITING", "MISSING", "NOT_STARTED"),
}


def summarize(rows: list[dict]) -> dict:
    states = {s: 0 for s in STATES}
    for row in rows:
        states[row["state"]] = states.get(row["state"], 0) + 1
    return {
        "kpis": [{"key": key, "label": label, "definition": definition, "count": sum(1 for r in rows if test(r))}
                 for key, (label, definition, test) in KPIS.items()],
        "states": states,
        "playable_without_captions": states[PLAYABLE_NO_CAPTIONS],
    }


def _video_filter(row, value) -> bool:
    if value == "FAILED":
        return row["bunny"]["status"] in ("FAILED", "REVIEW_REQUIRED") or row["video"]["status"] in (
            "FAILED", "REVIEW_REQUIRED")
    if value in ("NOT_IMPORTED", "UPLOADING"):
        return row["bunny"]["status"] == value
    return row["video"]["status"] == value and row["bunny"]["status"] not in ("NOT_IMPORTED",)


def apply_filters(rows: list[dict], *, q: str = "", kpi: str = "", state: str = "", video: str = "",
                  playback: str = "", captions: str = "", transcript: str = "", attention: bool = False) -> list[dict]:
    out = rows
    if q:
        needle = q.strip().lower()
        out = [r for r in out if needle in " ".join(str(x or "") for x in (
            r["subject"], r["module"], r["lms"]["occurrence_id"], r["lecture_id"])).lower()]
    if kpi:
        test = KPIS[kpi][2]
        out = [r for r in out if test(r)]
    if state:
        out = [r for r in out if r["state"] == state]
    if video:
        out = [r for r in out if _video_filter(r, video)]
    if playback:
        out = [r for r in out if r["playback"]["available"] is (playback == "AVAILABLE")]
    if captions:
        out = [r for r in out if r["captions"]["status"] == captions]
    if transcript:
        out = [r for r in out if r["transcript"]["status"] == transcript]
    if attention:
        out = [r for r in out if r["needs_attention"]]
    return out


# --------------------------------------------------------------------------- health
HEARTBEATS = """
SELECT worker_id, started_at, seen_at, version, current_job_id IS NOT NULL, detail,
       extract(epoch FROM now() - seen_at)
  FROM public.bunny_publisher_heartbeats ORDER BY seen_at DESC
"""
SCHEDULE = """
SELECT task, last_started_at, last_finished_at, last_succeeded_at, last_status, last_detail,
       extract(epoch FROM now() - last_started_at)
  FROM public.bunny_publisher_schedule WHERE task = ANY(%s)
"""


def health(connection, *, library_id, playback_enabled: bool) -> dict:
    """Publisher health from heartbeats and the schedule. An old heartbeat is never reported healthy."""
    checked_at = connection.execute("SELECT now()").fetchone()[0]
    beats = connection.execute(HEARTBEATS).fetchall()
    schedulers = [b for b in beats if b[0].endswith(":scheduler")]
    out = {"checked_at": _iso(checked_at), "library_id": library_id, "playback_enabled": bool(playback_enabled),
           "status": "UNKNOWN", "scheduler": None, "workers": [], "workers_healthy": 0, "workers_seen": 0,
           "mode": None, "daemon_version": None}
    if schedulers:
        current = schedulers[0]                     # the most recently seen scheduler = the live daemon
        identity = current[0][: -len(":scheduler")]
        detail = current[5] or {}
        scheduler_state = "STOPPED" if detail.get("stopped") else (
            "OK" if current[6] <= SCHEDULER_STALE_SECONDS else "STALE")
        out["scheduler"] = {"state": scheduler_state, "seen_at": _iso(current[2]),
                            "age_seconds": round(current[6]), "started_at": _iso(current[1])}
        out["mode"], out["daemon_version"] = detail.get("mode"), current[3]
        out["heartbeat_library_id"] = detail.get("library_id")
        for worker_id, started, seen, version, busy, wdetail, age in beats:
            if not worker_id.startswith(identity + ":w"):
                continue
            if (wdetail or {}).get("stopped"):
                worker_state = "STOPPED"
            elif age <= WORKER_STALE_SECONDS:
                worker_state = "BUSY" if busy else "OK"
            elif busy and age <= WORKER_BUSY_SECONDS:
                worker_state = "BUSY"
            else:
                worker_state = "STALE"
            out["workers"].append({"name": worker_id.rsplit(":", 1)[-1], "state": worker_state,
                                   "seen_at": _iso(seen), "age_seconds": round(age)})
        out["workers"].sort(key=lambda w: w["name"])
        out["workers_seen"] = len(out["workers"])
        out["workers_healthy"] = sum(1 for w in out["workers"] if w["state"] in ("OK", "BUSY"))
        if scheduler_state != "OK":
            out["status"] = scheduler_state
        elif out["workers_seen"] and out["workers_healthy"] == out["workers_seen"]:
            out["status"] = "HEALTHY"
        else:
            out["status"] = "DEGRADED"
    tasks = {}
    for task, started, finished, succeeded, status, detail, age in connection.execute(
            SCHEDULE, (list(SCHEDULE_TASKS),)).fetchall():
        tasks[task] = {"last_started_at": _iso(started), "last_finished_at": _iso(finished),
                       "last_succeeded_at": _iso(succeeded), "last_status": status,
                       "started_age_seconds": round(age) if age is not None else None,
                       "detail": {k: v for k, v in (detail or {}).items() if k in SAFE_DETAIL_KEYS}}
    out["tasks"] = tasks
    auto = tasks.get("AUTO_RELEASE")
    age = auto["started_age_seconds"] if auto else None
    out["auto_release"] = ("NEVER_RAN" if age is None else
                           "RUNNING_RECENTLY" if age <= AUTO_RELEASE_RECENT_SECONDS else "NOT_RUNNING_RECENTLY")
    out["open_alerts"] = {s: n for s, n in connection.execute(
        "SELECT severity, count(*) FROM public.bunny_publisher_alerts WHERE status = 'OPEN' GROUP BY 1").fetchall()}
    return out


# --------------------------------------------------------------------------- one lecture
DISCOVERY_DETAIL = """
SELECT status, reason_code, review_reason, expected_part_count, located_part_count, missing_part_count,
       transcript_pairing, attempt_count, first_checked_at, last_checked_at, next_check_after, located_at,
       last_verified_at, last_error_code
  FROM public.bunny_recording_discoveries WHERE lecture_id = %s
"""
DISCOVERY_FIELDS = ("status", "reason_code", "review_reason", "expected_part_count", "located_part_count",
                    "missing_part_count", "transcript_pairing", "attempt_count", "first_checked_at",
                    "last_checked_at", "next_check_after", "located_at", "last_verified_at", "last_error_code")
SOURCES = """
SELECT recording_part_index, locate_status, classification, recording_created_at, recording_end_at,
       source_duration_seconds, transcript_artifact_id IS NOT NULL
  FROM public.bunny_recording_sources WHERE lecture_id = %s ORDER BY recording_part_index
"""
LINKS = """
SELECT lms_occurrence_id, lms_live_session_id, lms_module_catalogue_id, lms_session_number, link_role, is_current
  FROM public.bunny_lms_occurrence_links WHERE lecture_id = %s ORDER BY is_current DESC, created_at DESC LIMIT 5
"""
MATCH_DETAIL = """
SELECT match_status, rule_version, requires_review, last_evaluated_at
  FROM public.bunny_lms_match_evaluations WHERE lecture_id = %s AND is_current
"""


def lecture_detail(connection, lecture_id: str, *, library_id: int, playback_enabled: bool,
                   language: str = "en") -> dict | None:
    from app.publisher import operations
    fact = load_fact(connection, lecture_id, library_id)
    if fact is None:
        return None
    now = connection.execute("SELECT now()").fetchone()[0]
    view = resolve(connection, fact, library_id=library_id, language=language) if needs_resolution(fact) else None
    row = classify(fact, view, playback_enabled=playback_enabled, library_id=library_id, now=now)
    base = operations.lecture_detail(connection, lecture_id, library_id=library_id, language=language)
    discovery = connection.execute(DISCOVERY_DETAIL, (lecture_id,)).fetchone()
    match = connection.execute(MATCH_DETAIL, (lecture_id,)).fetchone()
    return {
        "row": row,
        "lms": {"match": dict(zip(("status", "rule_version", "requires_review", "last_evaluated_at"),
                                  [_iso(x) for x in match])) if match else None,
                "links": [dict(zip(("occurrence_id", "live_session_id", "module_catalogue_id", "session_number",
                                    "link_role", "is_current"), r))
                          for r in connection.execute(LINKS, (lecture_id,)).fetchall()]},
        "recording": {"discovery": {k: _iso(v) for k, v in zip(DISCOVERY_FIELDS, discovery)} if discovery else None,
                      "parts": [{"part_index": r[0], "locate_status": r[1], "classification": r[2],
                                 "recorded_start": _iso(r[3]), "recorded_end": _iso(r[4]),
                                 "duration_seconds": float(r[5]) if r[5] is not None else None,
                                 "transcript_paired": r[6]}
                                for r in connection.execute(SOURCES, (lecture_id,)).fetchall()]},
        "delivery": None if view is None else {
            "state": view["state"], "blocking": view["blocking"], "caption_status": view["caption_status"],
            "release": view["release"], "released_at": view["released_at"], "withdrawn_at": view["withdrawn_at"],
            "parts": [{k: v for k, v in p.items() if not k.startswith("_")} for p in view["parts"]],
            "excluded_parts": view["excluded_parts"]},
        "readiness": base["readiness"],
        "videos": base["videos"],
        "transcript": {"selection_status": fact["selection_status"], "status": row["transcript"]["status"]},
        "jobs": base["jobs"],
        "timeline": list(reversed(base["history"]))[:60],
        "library_id": library_id,
    }
