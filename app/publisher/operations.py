"""
Operations reads and the two operator actions, shared by the CLI and the
dashboard backend (backend/operations/publisher_views.py).

Reads are plain SELECTs (callers use a READ ONLY transaction). Responses carry
identities, statuses, codes and counts only - never a URL, key, file content
or transcript text. Operator actions require an actor and a reason and are
audited in bunny_publisher_events.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.db.repositories.bunny_publisher import BunnyVideoRepository
from app.db.repositories.publisher_ops import (
    PartDecisionRepository, PublisherAlertRepository, PublisherScheduleRepository,
)
from app.db.repositories.publisher_queue import JOB_FIELDS, PublisherQueueRepository
from app.publisher.progress import load_facts, readiness

# What an operator retry asks each worker to do differently.
RETRY_PAYLOAD = {"RESOLVE_RECORDING": {"rediscover": True}}
SAFE_JOB_FIELDS = ("job_id", "job_kind", "subject_key", "lecture_id", "status", "attempt_count", "max_attempts",
                   "next_attempt_at", "locked_by", "last_error_code", "created_at", "updated_at", "finished_at")


class OperatorActionRefused(Exception):
    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f"[{code}] {detail}")


def _job(row: dict) -> dict:
    return {k: (str(row[k]) if k in ("job_id", "lecture_id") and row[k] is not None else row[k])
            for k in SAFE_JOB_FIELDS}


def status_summary(connection, *, library_id=None, heartbeat_stale_seconds: int = 300, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    jobs = connection.execute("SELECT job_kind, status, count(*) FROM public.bunny_publisher_jobs GROUP BY 1, 2").fetchall()
    by_kind: dict = {}
    for kind, status, count in jobs:
        by_kind.setdefault(kind, {})[status] = count
    heartbeats = [{"worker_id": w, "seen_at": seen, "age_seconds": round((now - seen).total_seconds()),
                   "stale": (now - seen).total_seconds() > heartbeat_stale_seconds and not (detail or {}).get("stopped"),
                   "stopped": bool((detail or {}).get("stopped")), "current_job_id": str(job) if job else None,
                   "version": version}
                  for w, seen, version, job, detail in connection.execute(
                      "SELECT worker_id, seen_at, version, current_job_id, detail FROM public.bunny_publisher_heartbeats "
                      "ORDER BY worker_id").fetchall()]
    alerts = connection.execute("SELECT severity, count(*) FROM public.bunny_publisher_alerts WHERE status = 'OPEN' "
                                "GROUP BY 1").fetchall()
    tracks = connection.execute("""
        SELECT lms_status, recording_status, transcript_selection_status, count(*)
          FROM public.bunny_publisher_lecture_coverage_v
         WHERE NOT is_cancelled AND NOT duplicate_suppressed GROUP BY 1, 2, 3""").fetchall()
    videos = connection.execute(
        "SELECT import_status, encoding_status, count(*) FROM public.bunny_videos "
        "WHERE retired_at IS NULL AND (%s::bigint IS NULL OR bunny_library_id = %s) GROUP BY 1, 2",
        (library_id, library_id)).fetchall()
    ready = connection.execute("SELECT count(DISTINCT lecture_id) FROM public.bunny_publisher_events "
                               "WHERE event = 'LECTURE_FULLY_READY' AND (%s::text IS NULL OR detail ->> 'library_id' = %s)",
                               (str(library_id) if library_id else None, str(library_id) if library_id else None)
                               ).fetchone()[0]
    return {"jobs": by_kind, "workers": heartbeats,
            "workers_healthy": bool(heartbeats) and not any(h["stale"] for h in heartbeats),
            "open_alerts": {s: n for s, n in alerts},
            "lectures_by_track": [{"lms": a, "recording": b, "transcript": c, "count": n} for a, b, c, n in tracks],
            "videos": [{"import": a, "encoding": b, "count": n} for a, b, n in videos],
            "lectures_fully_ready": ready,
            "schedule": [{k: v for k, v in r.items() if k != "last_detail"}
                         for r in PublisherScheduleRepository().all(connection)]}


def lecture_detail(connection, lecture_id, *, library_id=None, language: str = "en") -> dict:
    facts = load_facts(connection, lecture_id)
    if not facts.exists:
        raise OperatorActionRefused("LECTURE_NOT_FOUND", "no such lecture")
    state = readiness(connection, facts, library_id, language)
    videos = [{k: (str(v[k]) if k in ("bunny_video_record_id", "bunny_video_id", "lecture_id") and v[k] else v[k])
               for k in ("bunny_video_record_id", "recording_part_index", "bunny_library_id", "bunny_video_id",
                         "import_status", "encoding_status", "last_error_code", "attempt_count", "retired_at",
                         "retire_reason", "visibility_state")}
              for v in BunnyVideoRepository().for_lecture(connection, facts.lecture_id, library_id)]
    jobs = [_job(dict(zip(JOB_FIELDS, r))) for r in connection.execute(
        f"SELECT {', '.join(JOB_FIELDS)} FROM public.bunny_publisher_jobs WHERE lecture_id = %s ORDER BY created_at",
        (facts.lecture_id,)).fetchall()]
    history = PublisherQueueRepository().history(connection, lecture_id=facts.lecture_id)
    return {
        "lecture_id": facts.lecture_id, "session_date": facts.session_date, "subject": facts.subject,
        "lms": {"status": facts.lms_status, "occurrence_id": facts.lms_occurrence_id},
        "transcript": {"selection_status": facts.transcript_status},
        "recording": {"status": facts.recording_status, "reason": facts.recording_reason,
                      "expected_part_count": facts.expected_part_count},
        "readiness": state.as_dict(), "videos": videos, "jobs": jobs,
        "history": [{k: (str(e[k]) if k in ("job_id", "lecture_id") and e[k] else e[k])
                     for k in ("event_id", "job_id", "event", "actor", "detail", "at")} for e in history]}


def pending_work(connection, *, limit: int = 200) -> list[dict]:
    rows = connection.execute(
        f"SELECT {', '.join(JOB_FIELDS)} FROM public.bunny_publisher_jobs WHERE status IN ('QUEUED', 'RUNNING', 'RETRY_WAIT') "
        "ORDER BY priority DESC, next_attempt_at NULLS FIRST, created_at LIMIT %s", (limit,)).fetchall()
    return [_job(dict(zip(JOB_FIELDS, r))) for r in rows]


def exceptions(connection, *, limit: int = 500) -> list[dict]:
    fields = ("source", "lecture_id", "item", "status", "reason", "at", "reference")
    rows = connection.execute(f"SELECT {', '.join(fields)} FROM public.bunny_publisher_exceptions_v "
                              "ORDER BY at DESC NULLS LAST LIMIT %s", (limit,)).fetchall()
    return [{k: (str(v) if k == "lecture_id" and v else v) for k, v in zip(fields, r)} for r in rows]


def job_history(connection, job_id) -> dict:
    queue = PublisherQueueRepository()
    job = queue.job(connection, job_id)
    if job is None:
        raise OperatorActionRefused("JOB_NOT_FOUND", "no such job")
    return {"job": _job(job), "history": [{k: (str(e[k]) if k in ("job_id", "lecture_id") and e[k] else e[k])
                                           for k in ("event_id", "event", "actor", "detail", "at")}
                                          for e in queue.history(connection, job_id=job_id)]}


def operator_retry(connection, job_id, *, actor: str, reason: str) -> dict:
    """Re-open one FAILED / REVIEW_REQUIRED job. Audited; refuses anything else."""
    if not str(actor or "").strip() or not str(reason or "").strip():
        raise OperatorActionRefused("ACTOR_AND_REASON_REQUIRED", "an operator retry needs an actor and a reason")
    queue = PublisherQueueRepository()
    with connection.transaction():
        job = queue.job(connection, job_id)
        if job is None:
            raise OperatorActionRefused("JOB_NOT_FOUND", "no such job")
        if job["status"] not in ("FAILED", "REVIEW_REQUIRED"):
            raise OperatorActionRefused("JOB_NOT_RETRYABLE", f"job is {job['status']}; only FAILED or "
                                                             "REVIEW_REQUIRED jobs can be retried")
        reopened = queue.operator_retry(connection, job_id, actor=actor.strip()[:120], reason=reason.strip(),
                                        payload=RETRY_PAYLOAD.get(job["job_kind"], {}))
        if reopened is None:
            raise OperatorActionRefused("LIVE_JOB_EXISTS", "another live job already covers this subject")
    return _job(reopened)


def decide_part(connection, lecture_id, part_index: int, *, decision: str, actor: str, reason: str) -> dict:
    """INCLUDE / EXCLUDE one recording part (the fragment policy never decides this alone). Audited."""
    decision = str(decision or "").upper()
    if decision not in ("INCLUDE", "EXCLUDE"):
        raise OperatorActionRefused("INVALID_DECISION", "decision must be INCLUDE or EXCLUDE")
    if not str(actor or "").strip() or not str(reason or "").strip():
        raise OperatorActionRefused("ACTOR_AND_REASON_REQUIRED", "a part decision needs an actor and a reason")
    with connection.transaction():
        facts = load_facts(connection, lecture_id)
        if facts.part(int(part_index)) is None:
            raise OperatorActionRefused("NO_SUCH_PART", "the lecture has no such recording part")
        PartDecisionRepository().decide(connection, facts.lecture_id, int(part_index), decision=decision,
                                        reason=reason.strip()[:500], actor=actor.strip()[:120])
        PublisherQueueRepository().event(connection, "OPERATOR_PART_DECISION", lecture_id=facts.lecture_id,
                                         actor=actor.strip()[:120],
                                         detail={"part": int(part_index), "decision": decision,
                                                 "reason": reason.strip()[:300]})
    return {"lecture_id": facts.lecture_id, "part": int(part_index), "decision": decision}


def open_alerts(connection, *, severity=None) -> list[dict]:
    return [{k: (str(v) if k in ("alert_id", "lecture_id") and v else v) for k, v in a.items()}
            for a in PublisherAlertRepository().open_alerts(connection, severity=severity)]
