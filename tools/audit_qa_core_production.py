"""
QA Core production data-quality audit. READ ONLY.

WHY THIS FILE EXISTS
--------------------
An audit that cannot be re-run is an anecdote. This script is the evidence
behind docs/audits/QA_CORE_PRODUCTION_DATA_AUDIT_*.md: every number in that
report comes from a query in here, so a reviewer can re-derive any claim and a
future audit can be diffed against this one.

SAFETY CONTRACT
---------------
Three independent reasons this cannot write, which is the right number for a
tool pointed at a production database shared with live company systems:

  1. the connection is opened with `-c default_transaction_read_only=on`;
  2. that setting is READ BACK and asserted before any query runs, and the
     script exits non-zero if it is not `on`;
  3. a write is attempted on purpose at startup and MUST fail. A connection
     that lets the probe through is refused rather than trusted.

There is no INSERT, UPDATE, DELETE, UPSERT, CREATE, ALTER, DROP, TRUNCATE,
COPY or GRANT anywhere in this file, and `test_no_mutation_sql_in_the_auditor`
asserts that structurally. The only exception is the write probe itself, which
exists to be refused.

The script calls no external API: no Microsoft Graph, no OpenAI, no n8n, no
SharePoint, no media worker. It reads PostgreSQL and nothing else.

It never prints a connection string, a credential or a token. Learner names and
transcript text are never selected - the audit asks about structure, ownership
and provenance, and none of those questions need the content.

USAGE
-----
    python tools/audit_qa_core_production.py --from 2026-09-01 --to 2026-09-22

DATABASE_URL is read from the environment, falling back to backend/.env, which
is git-ignored and must stay that way.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

AUDIT_VERSION = "qa_core_production_audit_v1"
RC_COMMIT = "f85932e45a4ecb413cb198cae57b8e4b81cfcd1e"
RC_TAG = "qa-core-rc3"

# The 22 columns the coded writer owns in the legacy compatibility table.
# Everything else on that table belongs to another workflow and is audited
# separately, never as a coded-platform defect.
OWNED_LEGACY_COLUMNS = (
    "session_id", "meeting_id", "trainer", "duration", "met_count",
    "partial_count", "not_met_count", "date", "subject", "Engagement",
    "lms_module", "lms_students", "lms_students_count", "duration_score",
    "engagement_score", "ksb_coverage", "strengths", "areas_for_development",
    "overall_judgement", "teaching_quality_rating", "teaching_quality_comments",
    "cancelled_session",
)
CHECKLIST_TOTAL = 11

CRITICAL, HIGH, MEDIUM, LOW, INFO = "CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"


@dataclass
class Finding:
    finding_id: str
    severity: str
    title: str
    tables: list = field(default_factory=list)
    lecture_ids: list = field(default_factory=list)
    dates: list = field(default_factory=list)
    evidence_query: str = ""
    detail: str = ""
    # The distinction that decides whether anyone has to touch production.
    data_is_wrong: bool = False
    telemetry_or_ui_only: bool = False
    recommended_fix: str = ""


class ReadOnlyViolation(RuntimeError):
    """The connection could not be proven read-only. Fail closed."""


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------

def database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        try:
            from dotenv import load_dotenv
            load_dotenv(REPO_ROOT / "backend" / ".env")
        except ImportError:
            pass
        url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is not set (checked env, then backend/.env)")
    return url


def connect_read_only():
    """
    Open the audit connection and PROVE it cannot write.

    The proof is deliberately positive: we ask PostgreSQL what the session is,
    and then we try to write and require a refusal. A guard that is never
    exercised is a guard nobody has checked.
    """
    import psycopg

    conn = psycopg.connect(database_url(), autocommit=True,
                           options="-c default_transaction_read_only=on")
    setting = conn.execute("SHOW transaction_read_only").fetchone()[0]
    if setting != "on":
        conn.close()
        raise ReadOnlyViolation(
            f"transaction_read_only is {setting!r}, refusing to audit production")
    try:
        # The one write in this file. It exists to be refused.
        conn.execute("CREATE TEMP TABLE _qa_core_audit_write_probe (x int)")
    except psycopg.errors.ReadOnlySqlTransaction:
        return conn, setting
    except Exception as exc:                                     # noqa: BLE001
        conn.close()
        raise ReadOnlyViolation(
            f"write probe failed for an unexpected reason: {type(exc).__name__}") from exc
    conn.close()
    raise ReadOnlyViolation("the write probe SUCCEEDED; this connection is not read-only")


def rows(conn, sql, args=()):
    cur = conn.execute(sql, args)
    names = [d.name for d in cur.description] if cur.description else []
    return [dict(zip(names, r)) for r in cur.fetchall()]


def one(conn, sql, args=()):
    result = conn.execute(sql, args).fetchone()
    return result[0] if result else None


def jsonable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# PHASE 1 / 2 - contract and schema
# ---------------------------------------------------------------------------

def migration_tables() -> list:
    """The authoritative table list, derived from the migrations themselves."""
    import re

    found = set()
    for path in sorted((REPO_ROOT / "app" / "db" / "migrations").glob("*.sql")):
        text = path.read_text(encoding="utf-8", errors="replace")
        found.update(re.findall(
            r"CREATE TABLE (?:IF NOT EXISTS )?public\.([a-z_]+)", text))
    return sorted(found)


# Tables QA Core reads or projects into but does NOT own. Their absence of a
# coded ownership record is normal, not corruption.
FOREIGN_TABLES = {
    "qa_doctors_sessions": "legacy QA compatibility projection target; clips/recording/transcript columns owned by other workflows",
    "qa_doctors_checklist_items": "legacy checklist compatibility target",
    "qa_perfect_lectures": "legacy Perfect Lecture target; recording_url/recap_url/excel_synced_at owned by other workflows",
    "aptem_auto_extracting": "READ ONLY source of active Aptem groups",
    "kbc_attendance": "READ ONLY authoritative attendance source",
    "kbc_users_data": "READ ONLY learner/LMS source",
    "qa_doctors_transcripts": "legacy transcript store, read for comparison only",
}


def phase_schema(conn, findings) -> dict:
    expected = migration_tables()
    live = {r["table_name"] for r in rows(conn, """
        SELECT table_name FROM information_schema.tables
         WHERE table_schema='public' AND table_type='BASE TABLE'""")}
    missing = [t for t in expected if t not in live]
    if missing:
        findings.append(Finding(
            "SCHEMA-MISSING-TABLE", CRITICAL,
            "A table created by a migration is absent from production",
            tables=missing, data_is_wrong=True,
            evidence_query="information_schema.tables vs app/db/migrations/*.sql",
            recommended_fix="Run the missing migration before enabling the scheduler."))

    checks = []

    def check(name, ok, detail="", severity=HIGH, **kw):
        checks.append({"contract": name, "result": "PASS" if ok else "FAIL",
                       "detail": detail})
        if not ok:
            findings.append(Finding(f"SCHEMA-{name}", severity,
                                    f"Schema contract failed: {name}",
                                    detail=detail, data_is_wrong=True, **kw))

    check("all_migration_tables_exist", not missing,
          f"{len(expected)} expected, {len(missing)} missing")

    # Migration 020 - the backfill schema RC2/RC3 depend on.
    for table in ("backfill_runs", "backfill_run_days"):
        check(f"migration_020_{table}", table in live, f"{table} present")

    run_type = one(conn, """
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
         WHERE conrelid='public.lecture_pipeline_runs'::regclass
           AND pg_get_constraintdef(oid) LIKE '%%run_type%%'""")
    check("backfill_allowed_in_run_type", bool(run_type and "BACKFILL" in run_type),
          run_type or "constraint not found",
          recommended_fix="Backfill audit rows cannot be written without this.")

    # The ownership key that makes two concurrent writers safe.
    owner = one(conn, """
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
         WHERE conrelid='public.lecture_qa_legacy_writes'::regclass
           AND contype='u'""")
    check("legacy_write_ownership_unique",
          bool(owner and "legacy_session_id" in owner and "writer_version" in owner),
          owner or "missing",
          recommended_fix="Without it two writers could both claim one legacy row.")

    item_key = one(conn, """
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
         WHERE conrelid='public.lecture_pipeline_run_items'::regclass AND contype='u'""")
    check("run_item_unique_per_lecture",
          bool(item_key and "run_id" in item_key and "lecture_id" in item_key),
          item_key or "missing")

    legacy_pk = one(conn, """
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
         WHERE conrelid='public.qa_doctors_sessions'::regclass AND contype='p'""")
    check("legacy_session_primary_key",
          bool(legacy_pk and "session_id" in legacy_pk), legacy_pk or "missing",
          recommended_fix="The compatibility upsert depends on this key.")

    # Every owned legacy column must still exist, or the projection is broken.
    legacy_cols = {r["column_name"] for r in rows(conn, """
        SELECT column_name FROM information_schema.columns
         WHERE table_schema='public' AND table_name='qa_doctors_sessions'""")}
    absent = [c for c in OWNED_LEGACY_COLUMNS if c not in legacy_cols]
    check("legacy_owned_columns_present", not absent,
          f"{len(OWNED_LEGACY_COLUMNS)} owned columns, missing: {absent}",
          severity=CRITICAL)

    cancelled_type = one(conn, """
        SELECT data_type FROM information_schema.columns
         WHERE table_schema='public' AND table_name='qa_doctors_sessions'
           AND column_name='cancelled_session'""")
    check("cancelled_session_is_legacy_text", cancelled_type == "text",
          f"type={cancelled_type} (legacy TEXT 'true'/'false' by design)",
          severity=MEDIUM)

    index_count = one(conn, """
        SELECT count(*) FROM pg_indexes WHERE schemaname='public'
           AND tablename = ANY(%s)""", (expected,))

    return {"expected_tables": expected, "live_table_count": len(live),
            "missing_tables": missing, "foreign_tables": FOREIGN_TABLES,
            "indexes_on_qa_core_tables": index_count, "checks": checks}


def phase_row_counts(conn, tables) -> dict:
    counts = {}
    for table in tables:
        try:
            counts[table] = one(conn, f'SELECT count(*) FROM public."{table}"')
        except Exception:                                        # noqa: BLE001
            counts[table] = None
    return counts


# ---------------------------------------------------------------------------
# PHASE 3 - backfill accounting
# ---------------------------------------------------------------------------

def phase_backfill(conn, run_id, findings) -> dict:
    run = rows(conn, "SELECT * FROM public.backfill_runs WHERE backfill_run_id=%s",
               (run_id,))
    if not run:
        findings.append(Finding("BACKFILL-RUN-MISSING", CRITICAL,
                                "The audited backfill run does not exist",
                                tables=["backfill_runs"], data_is_wrong=True))
        return {"run": None}
    run = run[0]

    days = rows(conn, """
        SELECT business_date, status, pipeline_run_id, calendar_events_considered,
               matched_lectures, newly_discovered, already_complete, processed,
               waiting, review_required, failed, suppressed, graph_calls,
               provider_calls, duration_ms
          FROM public.backfill_run_days WHERE backfill_run_id=%s
         ORDER BY business_date""", (run_id,))

    day_sum = {k: sum(int(d[k] or 0) for d in days) for k in (
        "calendar_events_considered", "matched_lectures", "newly_discovered",
        "already_complete", "processed", "waiting", "review_required", "failed",
        "suppressed", "graph_calls", "provider_calls")}

    pipeline = rows(conn, """
        SELECT r.target_date, r.status, r.lectures_seen, r.completed_count,
               r.waiting_count, r.review_count, r.failed_count, r.skipped_count,
               r.graph_calls, r.provider_calls, r.legacy_rows_written
          FROM public.lecture_pipeline_runs r
          JOIN public.backfill_run_days d ON d.pipeline_run_id = r.run_id
         WHERE d.backfill_run_id=%s ORDER BY r.target_date""", (run_id,))
    pipe_sum = {k: sum(int(p[k] or 0) for p in pipeline) for k in (
        "lectures_seen", "completed_count", "waiting_count", "review_count",
        "failed_count", "skipped_count", "graph_calls", "provider_calls",
        "legacy_rows_written")}

    items = rows(conn, """
        SELECT i.lecture_id, i.status, i.initial_state, i.final_state, i.action,
               i.graph_calls, i.provider_calls, i.error_code, d.business_date
          FROM public.lecture_pipeline_run_items i
          JOIN public.backfill_run_days d ON d.pipeline_run_id = i.run_id
         WHERE d.backfill_run_id=%s""", (run_id,))
    item_status = Counter(i["status"] for i in items)

    # The real suppressions: an action actually executed, not a bucket label.
    real_suppressions = [i for i in items
                         if "SUPPRESS_DUPLICATE_EVENT" in (i["action"] or "")]

    # Lectures the run left short of settling.
    unsettled = [i for i in items
                 if i["final_state"] not in (None, "NOTHING_TO_DO")
                 and i["status"] == "SUCCEEDED"]

    discovery = rows(conn, """
        SELECT target_date, calendar_events_found, active_group_matches,
               unmatched_count, error_count, status
          FROM public.lecture_discovery_runs
         WHERE target_date BETWEEN %s AND %s AND started_at >= %s
         ORDER BY target_date, started_at""",
        (run["requested_from"], run["requested_to"], run["started_at"]))
    discovered_events = sum(int(d["calendar_events_found"] or 0) for d in discovery)

    # --- telemetry defect 1: calendar_events_considered ---------------------
    if day_sum["calendar_events_considered"] == 0 and discovered_events > 0:
        findings.append(Finding(
            "TELEMETRY-CALENDAR-EVENTS-ZERO", MEDIUM,
            "backfill_run_days.calendar_events_considered is 0 for every EXECUTE day "
            "although discovery examined calendar events",
            tables=["backfill_run_days", "lecture_discovery_runs"],
            dates=[str(run["requested_from"]), str(run["requested_to"])],
            evidence_query=(
                "SELECT sum(calendar_events_considered) FROM backfill_run_days "
                "WHERE backfill_run_id=... -> 0; "
                "SELECT sum(calendar_events_found) FROM lecture_discovery_runs "
                f"over the same window -> {discovered_events}"),
            detail=(
                f"{discovered_events} calendar events were examined across "
                f"{len(discovery)} discovery runs, and every EXECUTE day recorded 0. "
                "app/orchestration/backfill.py:_record builds its default day_counts "
                "with a hardcoded 'calendar_events_considered': 0, and _counts_from "
                "never reads calendar_events_found from the orchestrator summary. The "
                "PREVIEW path (_preview_day) does populate it, which is why only "
                "EXECUTE runs look empty. No lecture data is affected."),
            telemetry_or_ui_only=True,
            recommended_fix=(
                "Have _counts_from carry calendar_events_found out of "
                "summary['discovery'] the way _preview_day already does, and pass it "
                "to _record as day_counts.")))

    # --- telemetry defect 2: suppressed_count -------------------------------
    reported_suppressed = int(run["suppressed_count"] or 0)
    if reported_suppressed != len(real_suppressions) or any(
            d["suppressed"] and d["business_date"] not in
            {i["business_date"] for i in real_suppressions} for d in days):
        findings.append(Finding(
            "TELEMETRY-SUPPRESSED-MISLABELLED", HIGH,
            "The 'suppressed' counter does not count duplicate suppressions",
            tables=["backfill_runs", "backfill_run_days", "lecture_pipeline_runs",
                    "lecture_pipeline_run_items"],
            lecture_ids=[str(i["lecture_id"]) for i in unsettled],
            dates=sorted({str(i["business_date"]) for i in unsettled}),
            evidence_query=(
                "SELECT action FROM lecture_pipeline_run_items WHERE action LIKE "
                "'%SUPPRESS_DUPLICATE_EVENT%' vs "
                "SELECT business_date, suppressed FROM backfill_run_days"),
            detail=(
                f"backfill_run_days reports suppressed on the days "
                f"{sorted({str(d['business_date']) for d in days if d['suppressed']})}, "
                f"but SUPPRESS_DUPLICATE_EVENT actually executed on "
                f"{sorted({str(i['business_date']) for i in real_suppressions})}. "
                "app/orchestration/backfill.py:_counts_from maps 'suppressed' to "
                "counts['skipped_count'], and _counts() in the orchestrator uses "
                "skipped_count as its RESIDUAL bucket - anything not failed, not "
                "review, not waiting and not settled at NOTHING_TO_DO. A genuinely "
                "suppressed duplicate settles at NOTHING_TO_DO and is therefore "
                "counted as COMPLETE, while unsettled lectures are reported to the "
                "operator as 'suppressed'. The two numbers coincided at 2 by chance."),
            telemetry_or_ui_only=True,
            recommended_fix=(
                "Count SUPPRESS_DUPLICATE_EVENT actions (or duplicate_suppression "
                "annotations) for the suppressed counter, and surface skipped_count "
                "under a name that says what it is, e.g. 'unsettled'.")))

    # --- the thing the mislabel was hiding ----------------------------------
    if unsettled:
        findings.append(Finding(
            "BACKFILL-UNSETTLED-LECTURES", MEDIUM,
            "Lectures finished the backfill without reaching a settled state",
            tables=["lecture_pipeline_run_items"],
            lecture_ids=[str(i["lecture_id"]) for i in unsettled],
            dates=sorted({str(i["business_date"]) for i in unsettled}),
            evidence_query=(
                "SELECT lecture_id, final_state FROM lecture_pipeline_run_items "
                "WHERE status='SUCCEEDED' AND final_state <> 'NOTHING_TO_DO'"),
            detail="; ".join(
                f"{i['business_date']} final_state={i['final_state']}"
                for i in unsettled),
            data_is_wrong=False,
            recommended_fix=(
                "Re-run these lectures through the normal orchestrator (RETRY). "
                "Confirm current state first - the resolver may already report them "
                "settled if a later cycle finished them.")))

    # "already_complete" is completed_count, i.e. complete at the END.
    newly = day_sum["newly_discovered"]
    already = day_sum["already_complete"]
    if newly and already and already > (day_sum["matched_lectures"] - newly):
        findings.append(Finding(
            "TELEMETRY-ALREADY-COMPLETE-SEMANTICS", LOW,
            "'already complete' counts lectures complete AFTER the run, not before",
            tables=["backfill_run_days"],
            evidence_query=(
                "backfill_run_days for 2026-09-02: newly_discovered=8, "
                "already_complete=5, matched=8 - all eight lectures were created by "
                "this run, so none can have been complete beforehand"),
            detail=(
                "_counts_from maps already_complete to counts['completed_count'], "
                "which _counts() derives from the state at the END of the cycle. The "
                "label reads as 'was already done', which for a day of newly "
                "discovered lectures is impossible and misleading."),
            telemetry_or_ui_only=True,
            recommended_fix="Rename to complete_at_end_of_day, or compute a true "
                            "before-count from the pre-run state snapshot."))

    reconciliation = {
        "run_row": {k: jsonable(run[k]) for k in (
            "status", "mode", "requested_from", "requested_to", "total_days",
            "completed_days", "discovered_count", "matched_count", "processed_count",
            "already_complete_count", "waiting_count", "review_required_count",
            "failed_count", "suppressed_count")},
        "recomputed_from_days": day_sum,
        "recomputed_from_pipeline_runs": pipe_sum,
        "run_items_by_status": dict(item_status),
        "run_items_total": len(items),
        "real_suppression_actions": [
            {"business_date": str(i["business_date"]),
             "lecture_id": str(i["lecture_id"])} for i in real_suppressions],
        "unsettled_lectures": [
            {"business_date": str(i["business_date"]),
             "lecture_id": str(i["lecture_id"]),
             "final_state": i["final_state"]} for i in unsettled],
        "discovery_calendar_events_found": discovered_events,
        "days": [jsonable(d) for d in days],
    }

    # Cross-table arithmetic. These MUST agree or the audit trail is unusable.
    agreements = {
        "days_sum_matched == run.matched_count":
            day_sum["matched_lectures"] == int(run["matched_count"] or 0),
        "days_sum_newly == run.discovered_count":
            day_sum["newly_discovered"] == int(run["discovered_count"] or 0),
        "days_sum_waiting == run.waiting_count":
            day_sum["waiting"] == int(run["waiting_count"] or 0),
        "pipeline_lectures_seen == days_matched":
            pipe_sum["lectures_seen"] == day_sum["matched_lectures"],
        "run_items_total == pipeline_lectures_seen":
            len(items) == pipe_sum["lectures_seen"],
        "item_buckets_partition":
            sum(item_status.values()) == len(items),
    }
    for name, ok in agreements.items():
        if not ok:
            findings.append(Finding(
                f"BACKFILL-RECONCILE-{abs(hash(name)) % 10000}", HIGH,
                f"Backfill accounting disagreement: {name}",
                tables=["backfill_runs", "backfill_run_days",
                        "lecture_pipeline_runs", "lecture_pipeline_run_items"],
                data_is_wrong=True, evidence_query=name))
    reconciliation["agreements"] = agreements
    return reconciliation


# ---------------------------------------------------------------------------
# PHASE 4 - lecture-by-lecture, via the platform's OWN resolver
# ---------------------------------------------------------------------------

def phase_lectures(conn, start, end, run_id, findings) -> list:
    """
    One row per canonical lecture, with the state the platform itself reports.

    The resolver is used rather than re-derived: a second implementation of the
    state model would be auditing my arithmetic, not production.
    """
    from app.orchestration.factory import build_resolver
    from app.orchestration.stages import OBSERVED_ONLY_STAGES

    resolver = build_resolver(probe=False)

    lectures = rows(conn, """
        SELECT lecture_id, session_date, subject, module, normalized_subject,
               scheduled_start, scheduled_end, meeting_id, calendar_event_id,
               calendar_mapping_status, group_match_status, discovery_status,
               downstream_ready, is_cancelled,
               (metadata->'duplicate_suppression') AS suppression
          FROM public.lecture_sessions
         WHERE session_date BETWEEN %s AND %s
         ORDER BY session_date, scheduled_start, subject""", (start, end))

    items = {str(r["lecture_id"]): r for r in rows(conn, """
        SELECT i.lecture_id, i.status, i.initial_state, i.final_state, i.action,
               i.graph_calls, i.provider_calls, i.error_code
          FROM public.lecture_pipeline_run_items i
          JOIN public.backfill_run_days d ON d.pipeline_run_id = i.run_id
         WHERE d.backfill_run_id=%s""", (run_id,))}

    out = []
    for lecture in lectures:
        lid = str(lecture["lecture_id"])
        state = resolver.for_lecture(conn, lid)
        stages = state["stages"]
        item = items.get(lid, {})

        suppressed = lecture["suppression"] is not None
        legacy = stages.get("LEGACY_QA_SYNC", {})
        perfect = stages.get("PERFECT_SYNC", {})
        recording = stages.get("RECORDING_LINK", {})

        # Classification, in the order that makes each label truthful.
        #
        # `state["is_waiting"]` is deliberately NOT the waiting test: it is also
        # true for WAIT_FOR_RECORDING and WAIT_FOR_EXCEL_SYNC, which are
        # observed-only stages owned by other workflows. A lecture whose only
        # outstanding stage is one of those has finished everything QA Core is
        # responsible for, and calling it "waiting" would hide that.
        attendance_waiting = (stages.get("ATTENDANCE", {}).get("state") == "WAITING")
        outstanding = [name for name, item in stages.items()
                       if name not in OBSERVED_ONLY_STAGES
                       and item["state"] not in ("COMPLETE", "NOT_APPLICABLE")]
        explained = None
        if suppressed:
            classification = "DUPLICATE_SUPPRESSED"
        elif any(item["state"] == "FAILED" for item in stages.values()):
            classification = "FAILED"
        elif state["requires_review"]:
            classification = "REVIEW_REQUIRED"
        elif attendance_waiting:
            classification = "WAITING_EXPECTED"
        elif not outstanding:
            classification = "COMPLETE"
        else:
            # A stage the resolver can NAME a reason for is not unexplained.
            # The commonest here is a selection superseded by transcript content
            # that arrived after the selection was made.
            reasons = [stages[n].get("reason") for n in outstanding
                       if stages[n].get("reason")]
            explained = "; ".join(
                f"{n}={stages[n]['state']}"
                + (f"/{stages[n]['reason']}" if stages[n].get("reason") else "")
                for n in outstanding)
            classification = ("INCOMPLETE_EXPLAINED" if reasons
                              else "INCOMPLETE_UNEXPLAINED")

        row = {
            "session_date": str(lecture["session_date"]),
            "lecture_id": lid,
            "subject": lecture["subject"],
            "module": lecture["module"],
            "scheduled_start": jsonable(lecture["scheduled_start"]),
            "scheduled_end": jsonable(lecture["scheduled_end"]),
            "meeting_id_present": bool(lecture["meeting_id"]),
            "calendar_event_id_present": bool(lecture["calendar_event_id"]),
            "calendar_mapping_status": lecture["calendar_mapping_status"],
            "group_match_status": lecture["group_match_status"],
            "discovery_status": lecture["discovery_status"],
            "downstream_ready": lecture["downstream_ready"],
            "is_cancelled": lecture["is_cancelled"],
            "duplicate_suppressed": suppressed,
            "backfill_initial_state": item.get("initial_state"),
            "backfill_actions": item.get("action"),
            "backfill_final_state": item.get("final_state"),
            "backfill_item_status": item.get("status"),
            "graph_calls": int(item.get("graph_calls") or 0),
            "provider_calls": int(item.get("provider_calls") or 0),
            "next_action": state["next_action"],
            "next_executable_action": state["next_executable_action"],
            "blocking_stage": state["blocking_stage"],
            "is_waiting": state["is_waiting"],
            "requires_review": state["requires_review"],
            "is_complete": state["is_complete"],
            "attendance_state": stages.get("ATTENDANCE", {}).get("state"),
            "attendance_coverage_status": state.get("attendance_coverage_status"),
            "qa_evaluation_state": stages.get("QA_EVALUATION", {}).get("state"),
            "qa_render_state": stages.get("QA_RENDER", {}).get("state"),
            "legacy_projection_state": legacy.get("state"),
            "legacy_projection_reason": legacy.get("reason"),
            "legacy_session_id": legacy.get("legacy_session_id"),
            "legacy_coded_owned": legacy.get("coded_owned"),
            "perfect_eligibility_state":
                stages.get("PERFECT_ELIGIBILITY", {}).get("state"),
            "perfect_sync_state": perfect.get("state"),
            "recording_link_state": recording.get("state"),
            "classification": classification,
            "outstanding_stages": outstanding,
            "incomplete_explanation": explained,
            "stages": {k: v.get("state") for k, v in stages.items()},
        }
        out.append(row)

        if classification == "INCOMPLETE_UNEXPLAINED":
            findings.append(Finding(
                "LECTURE-INCOMPLETE-UNEXPLAINED", HIGH,
                "A lecture is neither complete, waiting, suppressed nor in review, "
                "and the resolver offers no reason",
                tables=["lecture_sessions"], lecture_ids=[lid],
                dates=[str(lecture["session_date"])],
                evidence_query="PipelineStateResolver.for_lecture(lecture_id)",
                detail=f"{lecture['subject']}: outstanding={outstanding}",
                data_is_wrong=False,
                recommended_fix="RETRY through the orchestrator; investigate the "
                                "blocking stage first."))

    # Grouped, because it is one condition rather than N independent ones.
    stale = [l for l in out if l["classification"] == "INCOMPLETE_EXPLAINED"]
    if stale:
        findings.append(Finding(
            "LECTURE-STALE-LINEAGE", MEDIUM,
            "Lectures whose QA is finished carry a stage the resolver reports STALE",
            tables=["lecture_transcript_selections", "lecture_sessions"],
            lecture_ids=[l["lecture_id"] for l in stale],
            dates=sorted({l["session_date"] for l in stale}),
            evidence_query="PipelineStateResolver.for_lecture -> a STALE stage with a "
                           "named reason while QA_EVALUATION and QA_RENDER are COMPLETE",
            detail="; ".join(sorted({l["incomplete_explanation"] for l in stale})),
            data_is_wrong=False,
            recommended_fix=(
                "Decide deliberately rather than reflexively re-running. "
                "SELECT_TRANSCRIPT would re-stale everything downstream and could "
                "turn settled compatibility rows into WOULD_UPDATE decisions that "
                "need an operator. The QA answers are not wrong; only the lineage "
                "marker is behind the transcript content.")))
    return out


# ---------------------------------------------------------------------------
# PHASE 5 - transcript / cue integrity
# ---------------------------------------------------------------------------

def phase_transcripts(conn, start, end, findings) -> dict:
    def scalar(label, sql, severity, title, fix, tables, telemetry=False):
        bad = rows(conn, sql, (start, end))
        if bad:
            findings.append(Finding(
                f"TRANSCRIPT-{label}", severity, title, tables=tables,
                lecture_ids=[str(b.get("lecture_id")) for b in bad
                             if b.get("lecture_id")][:50],
                evidence_query=" ".join(sql.split()),
                detail=f"{len(bad)} row(s)", data_is_wrong=not telemetry,
                recommended_fix=fix))
        return len(bad)

    result = {}
    result["selections"] = one(conn, """
        SELECT count(*) FROM public.lecture_transcript_selections s
          JOIN public.lecture_sessions l ON l.lecture_id=s.lecture_id
         WHERE l.session_date BETWEEN %s AND %s""", (start, end))
    result["documents"] = one(conn, """
        SELECT count(*) FROM public.lecture_transcript_documents d
          JOIN public.lecture_sessions l ON l.lecture_id=d.lecture_id
         WHERE l.session_date BETWEEN %s AND %s""", (start, end))
    result["cues"] = one(conn, """
        SELECT count(*) FROM public.lecture_transcript_cues c
          JOIN public.lecture_transcript_documents d ON d.document_id=c.document_id
          JOIN public.lecture_sessions l ON l.lecture_id=d.lecture_id
         WHERE l.session_date BETWEEN %s AND %s""", (start, end))

    result["orphan_selections"] = scalar(
        "ORPHAN-SELECTION", """
        SELECT s.selection_id, s.lecture_id FROM public.lecture_transcript_selections s
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_sessions l
                            WHERE l.lecture_id=s.lecture_id)
           AND %s::date IS NOT NULL AND %s::date IS NOT NULL""",
        HIGH, "Transcript selection references a lecture that does not exist",
        "Investigate before deleting; the lecture row may have been removed.",
        ["lecture_transcript_selections"])

    result["selection_missing_primary"] = scalar(
        "SELECTION-NO-PRIMARY", """
        SELECT s.selection_id, s.lecture_id FROM public.lecture_transcript_selections s
          JOIN public.lecture_sessions l ON l.lecture_id=s.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND s.selection_status='SELECTED'
           AND (s.primary_provider_transcript_id IS NULL
                OR s.primary_artifact_id IS NULL)""",
        CRITICAL, "A SELECTED transcript selection has no primary part",
        "The legacy session_id is derived from this; re-run SELECT_TRANSCRIPT.",
        ["lecture_transcript_selections"])

    result["selection_part_count_mismatch"] = scalar(
        "SELECTION-PART-COUNT", """
        SELECT s.selection_id, s.lecture_id FROM public.lecture_transcript_selections s
          JOIN public.lecture_sessions l ON l.lecture_id=s.lecture_id
         WHERE l.session_date BETWEEN %s AND %s AND s.selection_status='SELECTED'
           AND s.selected_part_count <> (
               SELECT count(*) FROM public.lecture_transcript_selection_parts p
                WHERE p.selection_id=s.selection_id)""",
        HIGH, "selected_part_count disagrees with the stored parts",
        "Re-run SELECT_TRANSCRIPT for the affected lectures.",
        ["lecture_transcript_selections", "lecture_transcript_selection_parts"])

    result["multipart_primary_violation"] = scalar(
        "SELECTION-PRIMARY-COUNT", """
        SELECT s.selection_id, s.lecture_id FROM public.lecture_transcript_selections s
          JOIN public.lecture_sessions l ON l.lecture_id=s.lecture_id
         WHERE l.session_date BETWEEN %s AND %s AND s.selection_status='SELECTED'
           AND (SELECT count(*) FROM public.lecture_transcript_selection_parts p
                 WHERE p.selection_id=s.selection_id AND p.is_primary) <> 1""",
        CRITICAL, "A selection does not have exactly one primary part",
        "Legacy session identity is ambiguous; re-run SELECT_TRANSCRIPT.",
        ["lecture_transcript_selection_parts"])

    result["selection_part_orphan_artifact"] = scalar(
        "PART-ORPHAN-ARTIFACT", """
        SELECT p.selection_id, s.lecture_id
          FROM public.lecture_transcript_selection_parts p
          JOIN public.lecture_transcript_selections s ON s.selection_id=p.selection_id
          JOIN public.lecture_sessions l ON l.lecture_id=s.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND NOT EXISTS (SELECT 1 FROM public.lecture_transcript_artifacts a
                            WHERE a.artifact_id=p.artifact_id)""",
        HIGH, "A selected transcript part references a missing artifact",
        "Re-acquire the transcript artifact.",
        ["lecture_transcript_selection_parts", "lecture_transcript_artifacts"])

    result["negative_or_inverted_cues"] = scalar(
        "CUE-INVERTED", """
        SELECT c.cue_id, d.lecture_id FROM public.lecture_transcript_cues c
          JOIN public.lecture_transcript_documents d ON d.document_id=c.document_id
          JOIN public.lecture_sessions l ON l.lecture_id=d.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND (c.start_ms < 0 OR c.end_ms < c.start_ms)""",
        HIGH, "Cue has a negative or inverted timestamp range",
        "Re-parse the canonical transcript (BUILD_CANONICAL_CUES).",
        ["lecture_transcript_cues"])

    result["non_monotonic_cue_starts"] = scalar(
        "CUE-NON-MONOTONIC", """
        SELECT d.document_id, d.lecture_id FROM public.lecture_transcript_documents d
          JOIN public.lecture_sessions l ON l.lecture_id=d.lecture_id
         WHERE l.session_date BETWEEN %s AND %s AND EXISTS (
             SELECT 1 FROM (
               SELECT start_ms, lag(start_ms) OVER (ORDER BY cue_index) prev
                 FROM public.lecture_transcript_cues WHERE document_id=d.document_id) t
              WHERE t.prev IS NOT NULL AND t.start_ms < t.prev)""",
        INFO, "Cue start times are not monotonic by cue_index",
        "None. Overlapping speech is real; the parser already records it as "
        "overlapping_cue_count and marks the document PARSED_WITH_WARNINGS.",
        ["lecture_transcript_cues"], telemetry=True)

    result["cue_count_mismatch"] = scalar(
        "DOC-CUE-COUNT", """
        SELECT d.document_id, d.lecture_id FROM public.lecture_transcript_documents d
          JOIN public.lecture_sessions l ON l.lecture_id=d.lecture_id
         WHERE l.session_date BETWEEN %s AND %s AND d.parse_status='PARSED'
           AND d.cue_count <> (SELECT count(*) FROM public.lecture_transcript_cues c
                                WHERE c.document_id=d.document_id)""",
        HIGH, "Document cue_count disagrees with the stored cues",
        "Re-parse the document.",
        ["lecture_transcript_documents", "lecture_transcript_cues"])

    result["orphan_cues"] = scalar(
        "CUE-ORPHAN", """
        SELECT c.cue_id FROM public.lecture_transcript_cues c
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_transcript_documents d
                            WHERE d.document_id=c.document_id)
           AND %s::date IS NOT NULL AND %s::date IS NOT NULL""",
        HIGH, "Cue rows with no parent document", "Investigate before deleting.",
        ["lecture_transcript_cues"])

    result["speakers_orphan_document"] = scalar(
        "SPEAKER-ORPHAN", """
        SELECT s.speaker_id FROM public.lecture_transcript_speakers s
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_transcript_documents d
                            WHERE d.document_id=s.document_id)
           AND %s::date IS NOT NULL AND %s::date IS NOT NULL""",
        HIGH, "Speaker inventory rows with no parent document",
        "Investigate before deleting.", ["lecture_transcript_speakers"])

    result["speaker_cue_index_out_of_range"] = scalar(
        "SPEAKER-CUE-RANGE", """
        SELECT s.speaker_id, d.lecture_id FROM public.lecture_transcript_speakers s
          JOIN public.lecture_transcript_documents d ON d.document_id=s.document_id
          JOIN public.lecture_sessions l ON l.lecture_id=d.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND (s.first_cue_index < 1 OR s.last_cue_index > d.cue_count
                OR s.last_cue_index < s.first_cue_index)""",
        MEDIUM, "Speaker cue index range falls outside its document",
        "Re-run RESOLVE_SPEAKERS.", ["lecture_transcript_speakers"])

    # Shared transcript reuse: one artifact legitimately serves several
    # occurrences, but the SAME primary transcript id on two lectures with
    # DIFFERENT dates would mean a wrong occurrence was selected.
    shared = rows(conn, """
        SELECT s.primary_provider_transcript_id AS tid,
               count(DISTINCT l.lecture_id) AS lectures,
               count(DISTINCT l.session_date) AS dates,
               min(l.session_date) AS first_date, max(l.session_date) AS last_date
          FROM public.lecture_transcript_selections s
          JOIN public.lecture_sessions l ON l.lecture_id=s.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND s.primary_provider_transcript_id IS NOT NULL
         GROUP BY 1 HAVING count(DISTINCT l.session_date) > 1""", (start, end))
    result["shared_primary_across_dates"] = len(shared)
    if shared:
        findings.append(Finding(
            "TRANSCRIPT-SHARED-ACROSS-DATES", CRITICAL,
            "One primary transcript id is the selection for lectures on different dates",
            tables=["lecture_transcript_selections"],
            dates=sorted({str(s["first_date"]) for s in shared}),
            evidence_query="GROUP BY primary_provider_transcript_id "
                           "HAVING count(DISTINCT session_date) > 1",
            detail=f"{len(shared)} transcript id(s); this also collides the legacy "
                   "session_id, because that key IS the primary transcript id",
            data_is_wrong=True,
            recommended_fix="Re-run SELECT_TRANSCRIPT for the affected occurrences."))
    return result


# ---------------------------------------------------------------------------
# PHASE 6 - attendance / engagement
# ---------------------------------------------------------------------------

def phase_attendance(conn, start, end, lectures, findings) -> dict:
    result = {}
    result["snapshots"] = one(conn, """
        SELECT count(*) FROM public.lecture_attendance_snapshots s
          JOIN public.lecture_sessions l ON l.lecture_id=s.lecture_id
         WHERE l.session_date BETWEEN %s AND %s""", (start, end))
    result["engagement_rows"] = one(conn, """
        SELECT count(*) FROM public.lecture_engagement_metrics e
          JOIN public.lecture_sessions l ON l.lecture_id=e.lecture_id
         WHERE l.session_date BETWEEN %s AND %s""", (start, end))

    bad_pct = rows(conn, """
        SELECT e.engagement_id, e.lecture_id, e.engagement_percentage, e.engagement_score
          FROM public.lecture_engagement_metrics e
          JOIN public.lecture_sessions l ON l.lecture_id=e.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND (e.engagement_percentage < 0 OR e.engagement_percentage > 100
                OR e.engagement_score < 0 OR e.engagement_score > 5)""", (start, end))
    result["engagement_out_of_range"] = len(bad_pct)
    if bad_pct:
        findings.append(Finding(
            "ENGAGEMENT-OUT-OF-RANGE", HIGH,
            "Engagement percentage or score outside its valid domain",
            tables=["lecture_engagement_metrics"],
            lecture_ids=[str(b["lecture_id"]) for b in bad_pct],
            evidence_query="engagement_percentage NOT BETWEEN 0 AND 100 "
                           "OR engagement_score NOT BETWEEN 0 AND 5",
            data_is_wrong=True,
            recommended_fix="Recalculate engagement for the affected lectures."))

    bad_counts = rows(conn, """
        SELECT e.engagement_id, e.lecture_id, e.attended_count, e.spoke_count,
               e.silent_count
          FROM public.lecture_engagement_metrics e
          JOIN public.lecture_sessions l ON l.lecture_id=e.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND (e.attended_count < 0 OR e.spoke_count < 0 OR e.silent_count < 0
                OR e.spoke_count > e.attended_count)""", (start, end))
    result["engagement_count_violations"] = len(bad_counts)
    if bad_counts:
        findings.append(Finding(
            "ENGAGEMENT-COUNT-VIOLATION", HIGH,
            "Negative count, or more speakers than attendees",
            tables=["lecture_engagement_metrics"],
            lecture_ids=[str(b["lecture_id"]) for b in bad_counts],
            evidence_query="spoke_count > attended_count OR any count < 0",
            data_is_wrong=True, recommended_fix="Recalculate engagement."))

    # The product contract: a lecture must not reach a FINALIZED QA answer on
    # an EMPTY attendance snapshot. A genuine 0% engagement with real attendees
    # is legitimate and is deliberately not flagged - the defect shape is an
    # empty source being treated as an answer.
    fabricated = rows(conn, """
        SELECT e.lecture_id, l.session_date, l.subject, e.attended_count,
               e.engagement_percentage, q.qa_status, q.met_count,
               s.source_row_count, s.present_row_count, s.effective_member_count
          FROM public.lecture_engagement_metrics e
          JOIN public.lecture_sessions l ON l.lecture_id=e.lecture_id
          JOIN public.lecture_qa_evaluations q ON q.engagement_id=e.engagement_id
          JOIN public.lecture_attendance_snapshots s
               ON s.snapshot_id=e.attendance_snapshot_id
         WHERE l.session_date BETWEEN %s AND %s
           AND q.qa_status IN ('COMPLETED','NON_DELIVERED')
           AND s.source_row_count = 0 AND s.effective_member_count = 0""",
        (start, end))
    result["qa_on_empty_attendance_snapshot"] = [
        {"lecture_id": str(f["lecture_id"]), "session_date": str(f["session_date"]),
         "subject": f["subject"], "met_count": f["met_count"],
         "qa_status": f["qa_status"]} for f in fabricated]
    if fabricated:
        findings.append(Finding(
            "ATTENDANCE-EMPTY-SNAPSHOT-REACHED-QA", HIGH,
            "A finalized QA evaluation rests on an EMPTY attendance snapshot",
            tables=["lecture_attendance_snapshots", "lecture_engagement_metrics",
                    "lecture_qa_evaluations"],
            lecture_ids=[str(f["lecture_id"]) for f in fabricated],
            dates=sorted({str(f["session_date"]) for f in fabricated}),
            evidence_query="attendance snapshot source_row_count=0 AND "
                           "effective_member_count=0 AND qa_status IN "
                           "('COMPLETED','NON_DELIVERED')",
            detail="; ".join(
                f"{f['session_date']} {f['subject']} met={f['met_count']}/11, "
                f"engagement={f['engagement_percentage']}%" for f in fabricated),
            data_is_wrong=True,
            recommended_fix=(
                "Do not repair by hand. These evaluations were produced before the "
                "attendance-required policy; the resolver now reports the lectures "
                "WAITING and refuses to advance them. Recover attendance, then let "
                "the normal pipeline supersede the evaluation.")))

    engagement_orphans = one(conn, """
        SELECT count(*) FROM public.lecture_engagement_metrics e
         WHERE e.attendance_snapshot_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM public.lecture_attendance_snapshots s
                            WHERE s.snapshot_id=e.attendance_snapshot_id)""")
    result["engagement_orphan_snapshot"] = engagement_orphans
    if engagement_orphans:
        findings.append(Finding(
            "ENGAGEMENT-ORPHAN-SNAPSHOT", HIGH,
            "Engagement row references a missing attendance snapshot",
            tables=["lecture_engagement_metrics", "lecture_attendance_snapshots"],
            data_is_wrong=True, recommended_fix="Recalculate engagement."))

    waiting = [l for l in lectures if l["classification"] == "WAITING_EXPECTED"]
    result["waiting"] = [{
        "session_date": w["session_date"], "lecture_id": w["lecture_id"],
        "subject": w["subject"], "attendance_state": w["attendance_state"],
        "attendance_coverage_status": w["attendance_coverage_status"],
        "blocking_stage": w["blocking_stage"],
        "next_executable_action": w["next_executable_action"],
        "qa_evaluation_state": w["qa_evaluation_state"],
        "legacy_projection_state": w["legacy_projection_state"],
        "perfect_sync_state": w["perfect_sync_state"],
    } for w in waiting]
    return result


# ---------------------------------------------------------------------------
# PHASE 7 - QA evaluation integrity
# ---------------------------------------------------------------------------

def phase_qa(conn, start, end, findings) -> dict:
    result = {}
    result["evaluations"] = one(conn, """
        SELECT count(*) FROM public.lecture_qa_evaluations q
          JOIN public.lecture_sessions l ON l.lecture_id=q.lecture_id
         WHERE l.session_date BETWEEN %s AND %s""", (start, end))
    result["rendered_sessions"] = one(conn, """
        SELECT count(*) FROM public.lecture_qa_rendered_sessions r
          JOIN public.lecture_sessions l ON l.lecture_id=r.lecture_id
         WHERE l.session_date BETWEEN %s AND %s""", (start, end))

    def flag(key, sql, severity, title, fix, tables, args=(start, end)):
        bad = rows(conn, sql, args)
        result[key] = len(bad)
        if bad:
            findings.append(Finding(
                f"QA-{key.upper().replace('_','-')}", severity, title, tables=tables,
                lecture_ids=[str(b.get("lecture_id")) for b in bad
                             if b.get("lecture_id")][:50],
                evidence_query=" ".join(sql.split()), detail=f"{len(bad)} row(s)",
                data_is_wrong=True, recommended_fix=fix))
        return bad

    flag("checklist_row_count_wrong", """
        SELECT q.evaluation_id, q.lecture_id FROM public.lecture_qa_evaluations q
          JOIN public.lecture_sessions l ON l.lecture_id=q.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND q.qa_status IN ('COMPLETED','NON_DELIVERED')
           AND (SELECT count(*) FROM public.lecture_qa_checklist_items c
                 WHERE c.evaluation_id=q.evaluation_id) <> 11""",
        CRITICAL, "A finalized evaluation does not have exactly 11 checklist rows",
        "Re-render/re-evaluate; the legacy checklist invariant depends on this.",
        ["lecture_qa_checklist_items"])

    flag("checklist_order_gap", """
        SELECT q.evaluation_id, q.lecture_id FROM public.lecture_qa_evaluations q
          JOIN public.lecture_sessions l ON l.lecture_id=q.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND q.qa_status IN ('COMPLETED','NON_DELIVERED')
           AND (SELECT count(DISTINCT c.checklist_order)
                  FROM public.lecture_qa_checklist_items c
                 WHERE c.evaluation_id=q.evaluation_id
                   AND c.checklist_order BETWEEN 1 AND 11) <> 11""",
        CRITICAL, "Checklist orders are not exactly 1..11 with no gaps or duplicates",
        "Re-evaluate the affected lectures.", ["lecture_qa_checklist_items"])

    flag("counts_do_not_reconcile", """
        SELECT q.evaluation_id, q.lecture_id FROM public.lecture_qa_evaluations q
          JOIN public.lecture_sessions l ON l.lecture_id=q.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND q.qa_status IN ('COMPLETED','NON_DELIVERED')
           AND coalesce(q.met_count,0)+coalesce(q.partial_count,0)
               +coalesce(q.not_met_count,0) <> 11""",
        CRITICAL, "met + partial + not_met does not equal the checklist total of 11",
        "Re-evaluate; the legacy projection copies these counts verbatim.",
        ["lecture_qa_evaluations"])

    flag("orphan_checklist_rows", """
        SELECT c.checklist_row_id FROM public.lecture_qa_checklist_items c
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_qa_evaluations q
                            WHERE q.evaluation_id=c.evaluation_id)
           AND %s::date IS NOT NULL AND %s::date IS NOT NULL""",
        HIGH, "Checklist rows with no parent evaluation", "Investigate before deleting.",
        ["lecture_qa_checklist_items"])

    flag("orphan_evidence_clips", """
        SELECT e.clip_id FROM public.lecture_qa_evidence_clips e
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_qa_evaluations q
                            WHERE q.evaluation_id=e.evaluation_id)
           AND %s::date IS NOT NULL AND %s::date IS NOT NULL""",
        HIGH, "Evidence clips with no parent evaluation", "Investigate before deleting.",
        ["lecture_qa_evidence_clips"])

    flag("rendered_orphan_evaluation", """
        SELECT r.rendered_session_id, r.lecture_id
          FROM public.lecture_qa_rendered_sessions r
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_qa_evaluations q
                            WHERE q.evaluation_id=r.evaluation_id)
           AND %s::date IS NOT NULL AND %s::date IS NOT NULL""",
        HIGH, "Rendered session references a missing evaluation",
        "Re-render.", ["lecture_qa_rendered_sessions"])

    flag("rendered_lecture_mismatch", """
        SELECT r.rendered_session_id, r.lecture_id
          FROM public.lecture_qa_rendered_sessions r
          JOIN public.lecture_qa_evaluations q ON q.evaluation_id=r.evaluation_id
          JOIN public.lecture_sessions l ON l.lecture_id=r.lecture_id
         WHERE l.session_date BETWEEN %s AND %s AND q.lecture_id <> r.lecture_id""",
        CRITICAL, "A rendered session belongs to a different lecture than its evaluation",
        "Re-render; this would project one lecture's QA onto another.",
        ["lecture_qa_rendered_sessions", "lecture_qa_evaluations"])

    flag("missing_provenance", """
        SELECT q.evaluation_id, q.lecture_id FROM public.lecture_qa_evaluations q
          JOIN public.lecture_sessions l ON l.lecture_id=q.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND q.qa_status IN ('COMPLETED','NON_DELIVERED')
           AND (q.source_fingerprint IS NULL OR q.source_fingerprint=''
                OR q.qa_engine_version IS NULL OR q.prompt_version IS NULL)""",
        HIGH, "A finalized evaluation is missing provenance",
        "Re-evaluate; provenance is what makes reuse and staleness decidable.",
        ["lecture_qa_evaluations"])

    flag("ai_called_without_model", """
        SELECT q.evaluation_id, q.lecture_id FROM public.lecture_qa_evaluations q
          JOIN public.lecture_sessions l ON l.lecture_id=q.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND q.ai_called IS TRUE AND (q.model_name IS NULL OR q.model_name='')""",
        MEDIUM, "A generation was bought but no model is recorded",
        "Provenance gap; investigate the QA service logging.",
        ["lecture_qa_evaluations"])

    flag("rating_out_of_range", """
        SELECT q.evaluation_id, q.lecture_id FROM public.lecture_qa_evaluations q
          JOIN public.lecture_sessions l ON l.lecture_id=q.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND q.teaching_quality_rating IS NOT NULL
           AND (q.teaching_quality_rating < 1 OR q.teaching_quality_rating > 5)""",
        MEDIUM, "teaching_quality_rating outside 1..5",
        "Re-evaluate.", ["lecture_qa_evaluations"])

    flag("duration_score_out_of_range", """
        SELECT q.evaluation_id, q.lecture_id FROM public.lecture_qa_evaluations q
          JOIN public.lecture_sessions l ON l.lecture_id=q.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND q.duration_score IS NOT NULL
           AND (q.duration_score < 0 OR q.duration_score > 5)""",
        MEDIUM, "duration_score outside 0..5", "Re-evaluate.",
        ["lecture_qa_evaluations"])

    # Two live evaluations for the same lecture AND engine version is the
    # shape that makes "which answer is current?" undecidable.
    flag("duplicate_active_evaluation", """
        SELECT q.lecture_id FROM public.lecture_qa_evaluations q
          JOIN public.lecture_sessions l ON l.lecture_id=q.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND q.qa_status IN ('COMPLETED','NON_DELIVERED')
         GROUP BY q.lecture_id, q.qa_engine_version, q.source_fingerprint
        HAVING count(*) > 1""",
        HIGH, "More than one finalized evaluation for the same lecture/version/source",
        "Investigate which is current; the resolver picks by lineage, not recency.",
        ["lecture_qa_evaluations"])

    # Evidence clips must point at real cue territory in the document.
    flag("evidence_outside_document", """
        SELECT e.clip_id, q.lecture_id FROM public.lecture_qa_evidence_clips e
          JOIN public.lecture_qa_evaluations q ON q.evaluation_id=e.evaluation_id
          JOIN public.lecture_sessions l ON l.lecture_id=q.lecture_id
          JOIN public.lecture_transcript_documents d ON d.document_id=q.document_id
         WHERE l.session_date BETWEEN %s AND %s
           AND e.validation_status='VALID'
           AND (e.start_ms < 0 OR e.end_ms < e.start_ms
                OR e.start_ms > coalesce(d.last_cue_end_ms, e.start_ms))""",
        HIGH, "A VALID evidence clip falls outside its transcript document",
        "Re-validate evidence (REVALIDATE_EVIDENCE).",
        ["lecture_qa_evidence_clips", "lecture_transcript_documents"])

    result["evidence_clip_validation"] = {
        r["validation_status"]: r["n"] for r in rows(conn, """
        SELECT e.validation_status, count(*) AS n
          FROM public.lecture_qa_evidence_clips e
          JOIN public.lecture_qa_evaluations q ON q.evaluation_id=e.evaluation_id
          JOIN public.lecture_sessions l ON l.lecture_id=q.lecture_id
         WHERE l.session_date BETWEEN %s AND %s GROUP BY 1""", (start, end))}
    result["qa_status_distribution"] = {
        r["qa_status"]: r["n"] for r in rows(conn, """
        SELECT q.qa_status, count(*) AS n FROM public.lecture_qa_evaluations q
          JOIN public.lecture_sessions l ON l.lecture_id=q.lecture_id
         WHERE l.session_date BETWEEN %s AND %s GROUP BY 1""", (start, end))}
    return result


# ---------------------------------------------------------------------------
# PHASE 8 / 13 - legacy compatibility and foreign rows
# ---------------------------------------------------------------------------

def phase_legacy(conn, start, end, lectures, findings) -> dict:
    result = {}

    owned = rows(conn, """
        SELECT w.legacy_session_id, w.lecture_id, w.writer_version, w.write_status,
               w.source_fingerprint, w.rendered_session_id
          FROM public.lecture_qa_legacy_writes w
          JOIN public.lecture_sessions l ON l.lecture_id=w.lecture_id
         WHERE l.session_date BETWEEN %s AND %s""", (start, end))
    owned_ids = {o["legacy_session_id"] for o in owned}

    window_rows = rows(conn, f"""
        SELECT {", ".join(f'q."{c}"' for c in OWNED_LEGACY_COLUMNS)},
               (w.legacy_session_id IS NOT NULL) AS coded_owned
          FROM public.qa_doctors_sessions q
          LEFT JOIN public.lecture_qa_legacy_writes w
                 ON w.legacy_session_id = q.session_id
         WHERE q.date BETWEEN %s AND %s""", (start, end))

    result["legacy_rows_in_window"] = len(window_rows)
    result["coded_owned_rows_in_window"] = sum(
        1 for r in window_rows if r["coded_owned"])
    result["foreign_rows_in_window"] = sum(
        1 for r in window_rows if not r["coded_owned"])
    result["ownership_records_in_window"] = len(owned)

    result["coded_owned_total"] = one(conn, """
        SELECT count(*) FROM public.qa_doctors_sessions q
         WHERE EXISTS (SELECT 1 FROM public.lecture_qa_legacy_writes w
                        WHERE w.legacy_session_id=q.session_id)""")
    result["foreign_total"] = one(conn, """
        SELECT count(*) FROM public.qa_doctors_sessions q
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_qa_legacy_writes w
                            WHERE w.legacy_session_id=q.session_id)""")

    # An ownership record whose target vanished is a genuine integrity defect.
    dangling = rows(conn, """
        SELECT w.legacy_session_id, w.lecture_id FROM public.lecture_qa_legacy_writes w
         WHERE w.write_status <> 'ROLLED_BACK'
           AND NOT EXISTS (SELECT 1 FROM public.qa_doctors_sessions q
                            WHERE q.session_id=w.legacy_session_id)""")
    result["ownership_without_target"] = len(dangling)
    if dangling:
        findings.append(Finding(
            "LEGACY-OWNERSHIP-NO-TARGET", CRITICAL,
            "A coded ownership record points at a qa_doctors_sessions row that is gone",
            tables=["lecture_qa_legacy_writes", "qa_doctors_sessions"],
            lecture_ids=[str(d["lecture_id"]) for d in dangling],
            evidence_query="lecture_qa_legacy_writes LEFT JOIN qa_doctors_sessions "
                           "WHERE q.session_id IS NULL AND write_status<>'ROLLED_BACK'",
            data_is_wrong=True,
            recommended_fix="Determine whether the legacy row was deleted externally; "
                            "re-sync through LEGACY_QA_SYNC, not by hand."))

    # Two ownership records for one legacy row at the same writer version.
    dup_owner = rows(conn, """
        SELECT legacy_session_id, writer_version, count(*) AS n
          FROM public.lecture_qa_legacy_writes
         GROUP BY 1,2 HAVING count(*) > 1""")
    result["duplicate_ownership"] = len(dup_owner)
    if dup_owner:
        findings.append(Finding(
            "LEGACY-DUPLICATE-OWNERSHIP", CRITICAL,
            "Two ownership records claim one legacy session at one writer version",
            tables=["lecture_qa_legacy_writes"], data_is_wrong=True,
            recommended_fix="The UNIQUE constraint should prevent this; investigate."))

    # Classification A/B/C/D per canonical lecture.
    buckets = {"A_coded_owned": [], "B_foreign": [], "C_no_projection_expected": [],
               "D_missing_required_projection": []}
    field_mismatches = []

    by_session = {r["session_id"]: r for r in window_rows}
    for lecture in lectures:
        lid = lecture["lecture_id"]
        state = lecture["legacy_projection_state"]
        sid = lecture["legacy_session_id"]
        if lecture["duplicate_suppressed"]:
            buckets["C_no_projection_expected"].append(
                {"lecture_id": lid, "reason": "DUPLICATE_SUPPRESSED"})
            continue
        if state == "COMPLETE" and lecture["legacy_coded_owned"]:
            buckets["A_coded_owned"].append({"lecture_id": lid, "session_id": sid})
        elif state == "NOT_APPLICABLE":
            buckets["B_foreign"].append(
                {"lecture_id": lid, "session_id": sid,
                 "reason": lecture["legacy_projection_reason"]})
        elif state in ("MISSING", "STALE") and \
                lecture["qa_render_state"] == "COMPLETE":
            buckets["D_missing_required_projection"].append(
                {"lecture_id": lid, "session_date": lecture["session_date"],
                 "subject": lecture["subject"], "state": state,
                 "reason": lecture["legacy_projection_reason"]})
        else:
            buckets["C_no_projection_expected"].append(
                {"lecture_id": lid, "reason": lecture["legacy_projection_reason"]
                 or f"render_state={lecture['qa_render_state']}"})

    if buckets["D_missing_required_projection"]:
        findings.append(Finding(
            "LEGACY-MISSING-REQUIRED-PROJECTION", HIGH,
            "A rendered lecture has no legacy compatibility row and none is protected",
            tables=["qa_doctors_sessions", "lecture_qa_legacy_writes"],
            lecture_ids=[d["lecture_id"]
                         for d in buckets["D_missing_required_projection"]],
            dates=sorted({d["session_date"]
                          for d in buckets["D_missing_required_projection"]}),
            evidence_query="PipelineStateResolver LEGACY_QA_SYNC state in "
                           "(MISSING, STALE) while QA_RENDER is COMPLETE",
            detail="Downstream consumers (positive-clips producer, dashboard) cannot "
                   "see these lectures.",
            data_is_wrong=True,
            recommended_fix="Run SYNC_LEGACY_QA through the orchestrator (RETRY). "
                            "Do not write the row by hand."))

    # Field-level check of the 22 owned columns against the rendered payload.
    projected = rows(conn, f"""
        SELECT w.lecture_id, w.legacy_session_id,
               {", ".join(f'q."{c}" AS "legacy_{c}"' for c in OWNED_LEGACY_COLUMNS)},
               r.meeting_id AS r_meeting_id, r.subject AS r_subject,
               r.trainer AS r_trainer, r.legacy_date AS r_date,
               r.duration AS r_duration, r.duration_score AS r_duration_score,
               r.engagement AS r_engagement, r.engagement_score AS r_engagement_score,
               r.met_count AS r_met, r.partial_count AS r_partial,
               r.not_met_count AS r_not_met, r.lms_module AS r_lms_module,
               r.lms_students_count AS r_lms_students_count,
               r.overall_judgement AS r_overall_judgement,
               r.teaching_quality_rating AS r_tq_rating,
               r.teaching_quality_comments AS r_tq_comments,
               r.cancelled_session AS r_cancelled, l.session_date
          FROM public.lecture_qa_legacy_writes w
          JOIN public.qa_doctors_sessions q ON q.session_id=w.legacy_session_id
          JOIN public.lecture_qa_rendered_sessions r
               ON r.rendered_session_id=w.rendered_session_id
          JOIN public.lecture_sessions l ON l.lecture_id=w.lecture_id
         WHERE l.session_date BETWEEN %s AND %s""", (start, end))

    def differs(a, b):
        if a is None and b is None:
            return False
        if isinstance(a, Decimal) or isinstance(b, Decimal):
            try:
                return Decimal(str(a)) != Decimal(str(b))
            except Exception:                                    # noqa: BLE001
                return str(a) != str(b)
        if isinstance(a, date) or isinstance(b, date):
            return str(a) != str(b)
        return str(a) if a is not None else None, str(b) if b is not None else None

    pairs = [("meeting_id", "r_meeting_id"), ("subject", "r_subject"),
             ("trainer", "r_trainer"), ("duration", "r_duration"),
             ("duration_score", "r_duration_score"),
             ("engagement_score", "r_engagement_score"),
             ("met_count", "r_met"), ("partial_count", "r_partial"),
             ("not_met_count", "r_not_met"), ("lms_module", "r_lms_module"),
             ("lms_students_count", "r_lms_students_count"),
             ("overall_judgement", "r_overall_judgement"),
             ("teaching_quality_rating", "r_tq_rating"),
             ("teaching_quality_comments", "r_tq_comments")]
    for row in projected:
        for legacy_col, rendered_col in pairs:
            lv, rv = row[f"legacy_{legacy_col}"], row[rendered_col]
            if lv is None and rv is None:
                continue
            if str(lv) != str(rv):
                field_mismatches.append({
                    "lecture_id": str(row["lecture_id"]),
                    "session_id": row["legacy_session_id"],
                    "session_date": str(row["session_date"]),
                    "field": legacy_col, "legacy": jsonable(lv),
                    "rendered": jsonable(rv)})
        # Engagement is numeric on both sides with different scales.
        le, re_ = row["legacy_Engagement"], row["r_engagement"]
        if le is not None and re_ is not None and Decimal(str(le)) != Decimal(str(re_)):
            field_mismatches.append({
                "lecture_id": str(row["lecture_id"]),
                "session_id": row["legacy_session_id"],
                "session_date": str(row["session_date"]),
                "field": "Engagement", "legacy": float(le), "rendered": float(re_)})
        # Legacy date semantics: UTC date of the transcript start.
        if str(row["legacy_date"]) != str(row["r_date"]):
            field_mismatches.append({
                "lecture_id": str(row["lecture_id"]),
                "session_id": row["legacy_session_id"],
                "session_date": str(row["session_date"]),
                "field": "date", "legacy": str(row["legacy_date"]),
                "rendered": str(row["r_date"])})

    result["coded_owned_field_mismatches"] = field_mismatches
    if field_mismatches:
        findings.append(Finding(
            "LEGACY-FIELD-MISMATCH", HIGH,
            "A coded-owned legacy row disagrees with the rendered payload it came from",
            tables=["qa_doctors_sessions", "lecture_qa_rendered_sessions"],
            lecture_ids=sorted({m["lecture_id"] for m in field_mismatches}),
            dates=sorted({m["session_date"] for m in field_mismatches}),
            evidence_query="qa_doctors_sessions JOIN lecture_qa_legacy_writes JOIN "
                           "lecture_qa_rendered_sessions, comparing the 22 owned columns",
            detail="; ".join(f"{m['field']} on {m['session_date']}"
                             for m in field_mismatches[:20]),
            data_is_wrong=True,
            recommended_fix="Re-sync via SYNC_LEGACY_QA; a WOULD_UPDATE decision "
                            "needs an operator, by design."))

    # cancelled_session must be exactly the legacy TEXT booleans, on OWNED rows.
    bad_cancelled = rows(conn, """
        SELECT q.session_id, w.lecture_id, q.cancelled_session
          FROM public.qa_doctors_sessions q
          JOIN public.lecture_qa_legacy_writes w ON w.legacy_session_id=q.session_id
         WHERE q.date BETWEEN %s AND %s
           AND (q.cancelled_session IS NULL
                OR q.cancelled_session NOT IN ('true','false'))""", (start, end))
    result["cancelled_session_invalid_owned"] = len(bad_cancelled)
    if bad_cancelled:
        findings.append(Finding(
            "LEGACY-CANCELLED-SESSION-DOMAIN", MEDIUM,
            "A coded-owned legacy row has cancelled_session outside 'true'/'false'",
            tables=["qa_doctors_sessions"],
            lecture_ids=[str(b["lecture_id"]) for b in bad_cancelled],
            evidence_query="cancelled_session NOT IN ('true','false') on owned rows",
            data_is_wrong=True, recommended_fix="Re-sync via SYNC_LEGACY_QA."))

    # Did anything coded ever overwrite a row it did not create? The ownership
    # ledger is the proof: an UPDATE can only ever be reached for a row this
    # writer already owned, so a zero here means no foreign history was touched.
    result["coded_updates_ever"] = one(conn, """
        SELECT count(*) FROM public.lecture_qa_legacy_writes
         WHERE write_status = 'UPDATED'""")
    result["coded_inserts_ever"] = one(conn, """
        SELECT count(*) FROM public.lecture_qa_legacy_writes
         WHERE write_status = 'WRITTEN'""")
    result["coded_rolled_back_ever"] = one(conn, """
        SELECT count(*) FROM public.lecture_qa_legacy_writes
         WHERE write_status = 'ROLLED_BACK'""")

    # A foreign row sitting on a session_id a canonical lecture needs.
    blocked = rows(conn, """
        SELECT r.session_id, r.lecture_id, l.session_date, l.subject
          FROM public.lecture_qa_rendered_sessions r
          JOIN public.lecture_sessions l ON l.lecture_id=r.lecture_id
          JOIN public.qa_doctors_sessions q ON q.session_id=r.session_id
         WHERE l.session_date BETWEEN %s AND %s
           AND NOT EXISTS (SELECT 1 FROM public.lecture_qa_legacy_writes w
                            WHERE w.legacy_session_id=r.session_id)""", (start, end))
    result["foreign_rows_blocking_projection"] = [
        {"lecture_id": str(b["lecture_id"]), "session_date": str(b["session_date"]),
         "subject": b["subject"]} for b in blocked]
    if blocked:
        findings.append(Finding(
            "LEGACY-FOREIGN-ROW-PROTECTED", INFO,
            "A pre-existing n8n row occupies the session_id a coded lecture would use",
            tables=["qa_doctors_sessions"],
            lecture_ids=[str(b["lecture_id"]) for b in blocked],
            dates=sorted({str(b["session_date"]) for b in blocked}),
            evidence_query="rendered session_id EXISTS in qa_doctors_sessions with no "
                           "lecture_qa_legacy_writes ownership row",
            detail="This is the ownership model working as designed: the writer "
                   "returns PROTECTED_EXISTING_LEGACY_ROW and the stage reports "
                   "NOT_APPLICABLE rather than asking anyone to overwrite history.",
            data_is_wrong=False,
            recommended_fix="None. Do not overwrite. If the coded answer must "
                            "supersede n8n's, that is an explicit operator decision."))

    result["classification"] = {k: len(v) for k, v in buckets.items()}
    result["classification_detail"] = buckets
    return result


# ---------------------------------------------------------------------------
# PHASE 9 - Perfect Lecture
# ---------------------------------------------------------------------------

def phase_perfect(conn, start, end, findings) -> dict:
    from app.qa.perfect import DEFAULT_PERFECT_ELIGIBILITY_VERSION as VERSION

    result = {"eligibility_version": VERSION}
    result["results"] = one(conn, """
        SELECT count(*) FROM public.lecture_perfect_lecture_results p
          JOIN public.lecture_sessions l ON l.lecture_id=p.lecture_id
         WHERE l.session_date BETWEEN %s AND %s""", (start, end))
    result["is_perfect"] = one(conn, """
        SELECT count(*) FROM public.lecture_perfect_lecture_results p
          JOIN public.lecture_sessions l ON l.lecture_id=p.lecture_id
         WHERE l.session_date BETWEEN %s AND %s AND p.is_perfect""", (start, end))
    result["legacy_perfect_writes"] = one(conn, """
        SELECT count(*) FROM public.lecture_perfect_lecture_legacy_writes w
          JOIN public.lecture_sessions l ON l.lecture_id=w.lecture_id
         WHERE l.session_date BETWEEN %s AND %s""", (start, end))

    wrong_version = rows(conn, """
        SELECT p.result_id, p.lecture_id, p.eligibility_version
          FROM public.lecture_perfect_lecture_results p
          JOIN public.lecture_sessions l ON l.lecture_id=p.lecture_id
         WHERE l.session_date BETWEEN %s AND %s AND p.eligibility_version <> %s""",
        (start, end, VERSION))
    result["wrong_eligibility_version"] = len(wrong_version)
    if wrong_version:
        findings.append(Finding(
            "PERFECT-STALE-VERSION", MEDIUM,
            "Perfect eligibility computed under a superseded version",
            tables=["lecture_perfect_lecture_results"],
            lecture_ids=[str(w["lecture_id"]) for w in wrong_version],
            evidence_query=f"eligibility_version <> {VERSION}",
            data_is_wrong=False,
            recommended_fix="Re-run EVALUATE_PERFECT; the resolver already treats "
                            "these as not-current."))

    bad_rule = rows(conn, """
        SELECT p.result_id, p.lecture_id FROM public.lecture_perfect_lecture_results p
          JOIN public.lecture_sessions l ON l.lecture_id=p.lecture_id
         WHERE l.session_date BETWEEN %s AND %s AND p.is_perfect
           AND (p.met_count <> 11 OR coalesce(p.partial_count,0) <> 0
                OR coalesce(p.not_met_count,0) <> 0
                OR p.checklist_row_count <> 11 OR p.distinct_order_count <> 11)""",
        (start, end))
    result["perfect_rule_violations"] = len(bad_rule)
    if bad_rule:
        findings.append(Finding(
            "PERFECT-RULE-VIOLATION", CRITICAL,
            "A lecture marked perfect does not satisfy the 11/0/0 checklist rule",
            tables=["lecture_perfect_lecture_results"],
            lecture_ids=[str(b["lecture_id"]) for b in bad_rule],
            evidence_query="is_perfect AND NOT (met=11 AND partial=0 AND not_met=0 "
                           "AND checklist_row_count=11 AND distinct_order_count=11)",
            data_is_wrong=True, recommended_fix="Re-run EVALUATE_PERFECT."))

    # A perfect verdict resting on attendance that never arrived.
    perfect_waiting = rows(conn, """
        SELECT p.lecture_id, l.session_date FROM public.lecture_perfect_lecture_results p
          JOIN public.lecture_sessions l ON l.lecture_id=p.lecture_id
         WHERE l.session_date BETWEEN %s AND %s AND p.is_perfect
           AND NOT EXISTS (SELECT 1 FROM public.lecture_attendance_snapshots s
                            WHERE s.lecture_id=p.lecture_id)""", (start, end))
    result["perfect_without_attendance"] = len(perfect_waiting)
    if perfect_waiting:
        findings.append(Finding(
            "PERFECT-WITHOUT-ATTENDANCE", CRITICAL,
            "A Perfect Lecture verdict exists with no attendance snapshot",
            tables=["lecture_perfect_lecture_results",
                    "lecture_attendance_snapshots"],
            lecture_ids=[str(p["lecture_id"]) for p in perfect_waiting],
            dates=sorted({str(p["session_date"]) for p in perfect_waiting}),
            evidence_query="is_perfect AND no lecture_attendance_snapshots row",
            data_is_wrong=True,
            recommended_fix="PERFECT_PENDING_ATTENDANCE_DATA should have blocked this."))

    # A Perfect row PUBLISHED under an older eligibility version that the
    # CURRENT version now refuses. Nothing retracts it: the writer's
    # PERFECT_SUPERSEDED_NOT_PERFECT decision is operator-only by design,
    # because the Excel Sync workflow may already have taken it downstream.
    superseded = rows(conn, """
        SELECT p.lecture_key, p.session_date, p.subject, p.engagement,
               p.attended_count, p.met_count, current.reason AS current_reason,
               current.eligibility_version AS current_version
          FROM public.qa_perfect_lectures p
          JOIN public.lecture_perfect_lecture_legacy_writes w
               ON w.legacy_lecture_key = p.lecture_key
          JOIN public.lecture_perfect_lecture_results current
               ON current.lecture_id = w.lecture_id
              AND current.eligibility_version = %s
         WHERE p.session_date BETWEEN %s AND %s
           AND current.is_perfect IS FALSE""", (VERSION, start, end))
    result["published_perfect_now_not_eligible"] = [
        {"lecture_key": r["lecture_key"], "session_date": str(r["session_date"]),
         "engagement": jsonable(r["engagement"]),
         "attended_count": r["attended_count"],
         "current_reason": r["current_reason"]} for r in superseded]
    if superseded:
        pending = [r for r in superseded
                   if r["current_reason"] == "PENDING_ATTENDANCE_DATA"]
        findings.append(Finding(
            "PERFECT-PUBLISHED-THEN-SUPERSEDED", HIGH,
            "A published Perfect Lecture row is no longer eligible under the "
            "current policy and has not been retracted",
            tables=["qa_perfect_lectures", "lecture_perfect_lecture_results",
                    "lecture_perfect_lecture_legacy_writes"],
            dates=sorted({str(r["session_date"]) for r in superseded}),
            evidence_query=(
                "qa_perfect_lectures JOIN lecture_perfect_lecture_legacy_writes "
                f"JOIN lecture_perfect_lecture_results (version={VERSION}) "
                "WHERE is_perfect IS FALSE"),
            detail="; ".join(
                f"{r['session_date']} {r['subject']} -> {r['current_reason']} "
                f"(published with engagement={r['engagement']}%, "
                f"attended={r['attended_count']})" for r in superseded),
            data_is_wrong=True,
            recommended_fix=(
                "Operator decision, not an automated one. "
                + ("At least one was published on an EMPTY attendance snapshot and "
                   "is visible to downstream consumers as a Perfect Lecture with 0 "
                   "attendees; treat that as the priority. " if pending else "")
                + "Retracting a published Perfect row has outward consequences "
                  "because the Excel Sync workflow may already have exported it.")))

    dangling = one(conn, """
        SELECT count(*) FROM public.lecture_perfect_lecture_legacy_writes w
         WHERE NOT EXISTS (SELECT 1 FROM public.qa_perfect_lectures p
                            WHERE p.lecture_key = w.legacy_lecture_key)""")
    result["perfect_ownership_without_target"] = dangling
    if dangling:
        findings.append(Finding(
            "PERFECT-OWNERSHIP-NO-TARGET", HIGH,
            "Perfect ownership record with no qa_perfect_lectures target",
            tables=["lecture_perfect_lecture_legacy_writes", "qa_perfect_lectures"],
            data_is_wrong=True, recommended_fix="Investigate external deletion."))

    dup = rows(conn, """
        SELECT lecture_key, count(*) AS n FROM public.qa_perfect_lectures
         WHERE session_date BETWEEN %s AND %s GROUP BY 1 HAVING count(*) > 1""",
        (start, end))
    result["duplicate_perfect_rows"] = len(dup)
    if dup:
        findings.append(Finding(
            "PERFECT-DUPLICATE-ROW", HIGH,
            "Duplicate qa_perfect_lectures rows on one lecture_key",
            tables=["qa_perfect_lectures"], data_is_wrong=True,
            recommended_fix="Investigate; the key collision hazard is known."))

    result["foreign_perfect_rows"] = one(conn, """
        SELECT count(*) FROM public.qa_perfect_lectures p
         WHERE p.session_date BETWEEN %s AND %s
           AND NOT EXISTS (SELECT 1 FROM public.lecture_perfect_lecture_legacy_writes w
                            WHERE w.legacy_lecture_key=p.lecture_key)""", (start, end))
    # recording_url / recap_url / excel_synced_at belong to other workflows.
    result["foreign_enrichment_present"] = one(conn, """
        SELECT count(*) FROM public.qa_perfect_lectures p
         WHERE p.session_date BETWEEN %s AND %s
           AND (p.recording_url IS NOT NULL OR p.recap_url IS NOT NULL
                OR p.excel_synced_at IS NOT NULL)""", (start, end))
    return result


# ---------------------------------------------------------------------------
# PHASE 10 - recording link
# ---------------------------------------------------------------------------

def phase_recording(conn, start, end, lectures, findings) -> dict:
    result = Counter(l["recording_link_state"] for l in lectures)
    out = {"states": dict(result)}

    mismatched = rows(conn, """
        SELECT p.recording_part_id, p.lecture_id, p.legacy_session_id, p.meeting_id,
               l.session_date, l.meeting_id AS lecture_meeting_id
          FROM public.lecture_recording_parts p
          JOIN public.lecture_sessions l ON l.lecture_id=p.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND p.meeting_id IS NOT NULL AND l.meeting_id IS NOT NULL
           AND p.meeting_id <> l.meeting_id""", (start, end))
    out["meeting_id_mismatch"] = len(mismatched)
    if mismatched:
        findings.append(Finding(
            "RECORDING-MEETING-MISMATCH", HIGH,
            "A recording part's meeting_id differs from its lecture's",
            tables=["lecture_recording_parts", "lecture_sessions"],
            lecture_ids=[str(m["lecture_id"]) for m in mismatched],
            evidence_query="lecture_recording_parts.meeting_id <> "
                           "lecture_sessions.meeting_id",
            data_is_wrong=True,
            recommended_fix="Re-resolve recording coordinates for these lectures."))

    wrong_session = rows(conn, """
        SELECT p.recording_part_id, p.lecture_id, p.legacy_session_id
          FROM public.lecture_recording_parts p
          JOIN public.lecture_sessions l ON l.lecture_id=p.lecture_id
         WHERE l.session_date BETWEEN %s AND %s AND p.legacy_session_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM public.lecture_qa_rendered_sessions r
                WHERE r.lecture_id=p.lecture_id
                  AND r.session_id=p.legacy_session_id)""", (start, end))
    out["legacy_session_mismatch"] = len(wrong_session)
    if wrong_session:
        findings.append(Finding(
            "RECORDING-SESSION-MISMATCH", MEDIUM,
            "A recording part's legacy_session_id is not this lecture's rendered id",
            tables=["lecture_recording_parts", "lecture_qa_rendered_sessions"],
            lecture_ids=[str(w["lecture_id"]) for w in wrong_session],
            evidence_query="lecture_recording_parts.legacy_session_id not found among "
                           "that lecture's rendered session ids",
            data_is_wrong=True,
            recommended_fix="Re-resolve; a wrong link would attach media to the "
                            "wrong lecture."))

    out["recording_parts"] = one(conn, """
        SELECT count(*) FROM public.lecture_recording_parts p
          JOIN public.lecture_sessions l ON l.lecture_id=p.lecture_id
         WHERE l.session_date BETWEEN %s AND %s""", (start, end))
    out["legacy_rows_with_recording_link"] = one(conn, """
        SELECT count(*) FROM public.qa_doctors_sessions
         WHERE date BETWEEN %s AND %s AND coalesce(recording_url,'') <> ''""",
        (start, end))
    return out


# ---------------------------------------------------------------------------
# PHASE 11 / 12 - referential integrity and field quality
# ---------------------------------------------------------------------------

ORPHAN_CHECKS = [
    ("lecture_pipeline_run_items", "run", """
        SELECT count(*) FROM public.lecture_pipeline_run_items i
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_pipeline_runs r
                            WHERE r.run_id=i.run_id)"""),
    ("lecture_pipeline_run_items", "lecture", """
        SELECT count(*) FROM public.lecture_pipeline_run_items i
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_sessions l
                            WHERE l.lecture_id=i.lecture_id)"""),
    ("lecture_qa_evaluations", "lecture", """
        SELECT count(*) FROM public.lecture_qa_evaluations q
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_sessions l
                            WHERE l.lecture_id=q.lecture_id)"""),
    ("lecture_qa_checklist_items", "evaluation", """
        SELECT count(*) FROM public.lecture_qa_checklist_items c
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_qa_evaluations q
                            WHERE q.evaluation_id=c.evaluation_id)"""),
    ("lecture_qa_evidence_clips", "evaluation", """
        SELECT count(*) FROM public.lecture_qa_evidence_clips e
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_qa_evaluations q
                            WHERE q.evaluation_id=e.evaluation_id)"""),
    ("lecture_qa_rendered_checklist_items", "rendered_session", """
        SELECT count(*) FROM public.lecture_qa_rendered_checklist_items i
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_qa_rendered_sessions r
                            WHERE r.rendered_session_id=i.rendered_session_id)"""),
    ("lecture_transcript_selections", "lecture", """
        SELECT count(*) FROM public.lecture_transcript_selections s
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_sessions l
                            WHERE l.lecture_id=s.lecture_id)"""),
    ("lecture_transcript_cues", "document", """
        SELECT count(*) FROM public.lecture_transcript_cues c
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_transcript_documents d
                            WHERE d.document_id=c.document_id)"""),
    ("lecture_attendance_snapshots", "lecture", """
        SELECT count(*) FROM public.lecture_attendance_snapshots s
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_sessions l
                            WHERE l.lecture_id=s.lecture_id)"""),
    ("lecture_engagement_metrics", "lecture", """
        SELECT count(*) FROM public.lecture_engagement_metrics e
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_sessions l
                            WHERE l.lecture_id=e.lecture_id)"""),
    ("lecture_qa_legacy_writes", "lecture", """
        SELECT count(*) FROM public.lecture_qa_legacy_writes w
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_sessions l
                            WHERE l.lecture_id=w.lecture_id)"""),
    ("lecture_perfect_lecture_results", "lecture", """
        SELECT count(*) FROM public.lecture_perfect_lecture_results p
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_sessions l
                            WHERE l.lecture_id=p.lecture_id)"""),
    ("backfill_run_days", "backfill_run", """
        SELECT count(*) FROM public.backfill_run_days d
         WHERE NOT EXISTS (SELECT 1 FROM public.backfill_runs r
                            WHERE r.backfill_run_id=d.backfill_run_id)"""),
]


def phase_integrity(conn, start, end, findings) -> dict:
    orphans = {}
    for table, parent, sql in ORPHAN_CHECKS:
        count = one(conn, sql)
        orphans[f"{table}->{parent}"] = count
        if count:
            findings.append(Finding(
                f"ORPHAN-{table}-{parent}".upper(), HIGH,
                f"{count} orphan row(s) in {table} with no {parent}",
                tables=[table], evidence_query=" ".join(sql.split()),
                data_is_wrong=True,
                recommended_fix="Investigate the cause before removing anything."))

    dup_identity = rows(conn, """
        SELECT source_system, calendar_user_upn, calendar_event_id, count(*) AS n
          FROM public.lecture_sessions
         WHERE session_date BETWEEN %s AND %s
         GROUP BY 1,2,3 HAVING count(*) > 1""", (start, end))
    if dup_identity:
        findings.append(Finding(
            "DUPLICATE-CANONICAL-IDENTITY", CRITICAL,
            "Two canonical lectures share one calendar identity",
            tables=["lecture_sessions"], data_is_wrong=True,
            evidence_query="GROUP BY source_system, calendar_user_upn, "
                           "calendar_event_id HAVING count(*) > 1",
            recommended_fix="The UNIQUE constraint should prevent this; investigate."))

    conflicting_meeting = rows(conn, """
        SELECT meeting_id, session_date, count(*) AS n
          FROM public.lecture_sessions
         WHERE session_date BETWEEN %s AND %s AND meeting_id IS NOT NULL
           AND metadata->'duplicate_suppression' IS NULL
         GROUP BY 1,2 HAVING count(*) > 1""", (start, end))
    if conflicting_meeting:
        findings.append(Finding(
            "MEETING-ID-SHARED-SAME-DAY", MEDIUM,
            "Two live lectures share a meeting_id on the same date",
            tables=["lecture_sessions"],
            dates=sorted({str(c["session_date"]) for c in conflicting_meeting}),
            evidence_query="GROUP BY meeting_id, session_date HAVING count(*) > 1 "
                           "excluding suppressed duplicates",
            detail=f"{len(conflicting_meeting)} pair(s). A recurring series can "
                   "legitimately reuse one meeting_id across dates; the same date is "
                   "what makes this worth reading.",
            data_is_wrong=False,
            recommended_fix="Confirm duplicate resolution handled these."))

    dup_projection = rows(conn, """
        SELECT r.session_id, count(DISTINCT r.lecture_id) AS n
          FROM public.lecture_qa_rendered_sessions r
          JOIN public.lecture_sessions l ON l.lecture_id=r.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
         GROUP BY 1 HAVING count(DISTINCT r.lecture_id) > 1""", (start, end))
    if dup_projection:
        findings.append(Finding(
            "DUPLICATE-COMPATIBILITY-PROJECTION", CRITICAL,
            "One legacy session_id would be projected from two different lectures",
            tables=["lecture_qa_rendered_sessions", "qa_doctors_sessions"],
            evidence_query="GROUP BY rendered session_id HAVING "
                           "count(DISTINCT lecture_id) > 1",
            data_is_wrong=True,
            recommended_fix="Re-run SELECT_TRANSCRIPT; the primary transcript id "
                            "is the legacy key and must be unique per occurrence."))

    impossible = one(conn, """
        SELECT count(*) FROM public.lecture_sessions
         WHERE session_date BETWEEN %s AND %s
           AND (scheduled_end <= scheduled_start
                OR session_date <> (scheduled_start AT TIME ZONE 'UTC')::date)""",
        (start, end))

    return {"orphans": orphans,
            "duplicate_canonical_identity": len(dup_identity),
            "meeting_id_shared_same_day": len(conflicting_meeting),
            "duplicate_compatibility_projection": len(dup_projection),
            "impossible_date_or_range": impossible}


def phase_field_quality(conn, start, end, findings) -> dict:
    """
    NULL checks driven by the CODE contract, not by column nullability.

    A nullable column is not a defect. These are the fields the platform's own
    invariants require, which is a different and much shorter list.
    """
    checks = []

    def check(table, rule, sql, severity=HIGH, fix="", args=(start, end)):
        count = one(conn, sql, args)
        checks.append({"table": table, "rule": rule, "violations": count})
        if count:
            findings.append(Finding(
                f"QUALITY-{table}-{rule}".upper().replace("_", "-"), severity,
                f"{count} row(s) violate: {rule}", tables=[table],
                evidence_query=" ".join(sql.split()), data_is_wrong=True,
                recommended_fix=fix))
        return count

    check("lecture_sessions", "downstream_ready_requires_meeting_id", """
        SELECT count(*) FROM public.lecture_sessions
         WHERE session_date BETWEEN %s AND %s
           AND downstream_ready IS TRUE AND meeting_id IS NULL""",
        CRITICAL, "A lecture cannot be downstream-ready without a meeting.")

    check("lecture_sessions", "subject_not_blank", """
        SELECT count(*) FROM public.lecture_sessions
         WHERE session_date BETWEEN %s AND %s
           AND (subject IS NULL OR btrim(subject) = '')""",
        HIGH, "Subject is projected into the legacy row.")

    check("qa_doctors_sessions", "owned_row_trainer_not_null", """
        SELECT count(*) FROM public.qa_doctors_sessions q
          JOIN public.lecture_qa_legacy_writes w ON w.legacy_session_id=q.session_id
         WHERE q.date BETWEEN %s AND %s
           AND (q.trainer IS NULL OR btrim(q.trainer) = '')""",
        CRITICAL, "trainer is NOT NULL in the legacy contract.")

    check("qa_doctors_sessions", "owned_row_counts_sum_to_11", """
        SELECT count(*) FROM public.qa_doctors_sessions q
          JOIN public.lecture_qa_legacy_writes w ON w.legacy_session_id=q.session_id
         WHERE q.date BETWEEN %s AND %s
           AND coalesce(q.met_count,0)+coalesce(q.partial_count,0)
               +coalesce(q.not_met_count,0) <> 11""",
        CRITICAL, "Re-sync via SYNC_LEGACY_QA.")

    check("qa_doctors_sessions", "owned_row_engagement_in_range", """
        SELECT count(*) FROM public.qa_doctors_sessions q
          JOIN public.lecture_qa_legacy_writes w ON w.legacy_session_id=q.session_id
         WHERE q.date BETWEEN %s AND %s AND q."Engagement" IS NOT NULL
           AND (q."Engagement" < 0 OR q."Engagement" > 100)""",
        HIGH, "Re-sync via SYNC_LEGACY_QA.")

    check("qa_doctors_sessions", "owned_row_scores_in_range", """
        SELECT count(*) FROM public.qa_doctors_sessions q
          JOIN public.lecture_qa_legacy_writes w ON w.legacy_session_id=q.session_id
         WHERE q.date BETWEEN %s AND %s
           AND ((q.duration_score IS NOT NULL
                 AND (q.duration_score < 0 OR q.duration_score > 5))
             OR (q.engagement_score IS NOT NULL
                 AND (q.engagement_score < 0 OR q.engagement_score > 5))
             OR (q.teaching_quality_rating IS NOT NULL
                 AND (q.teaching_quality_rating < 1 OR q.teaching_quality_rating > 5)))""",
        HIGH, "Re-sync via SYNC_LEGACY_QA.")

    check("qa_doctors_sessions", "owned_row_lms_students_is_json_object", """
        SELECT count(*) FROM public.qa_doctors_sessions q
          JOIN public.lecture_qa_legacy_writes w ON w.legacy_session_id=q.session_id
         WHERE q.date BETWEEN %s AND %s AND q.lms_students IS NOT NULL
           AND jsonb_typeof(q.lms_students) NOT IN ('object','array')""",
        MEDIUM, "Re-sync via SYNC_LEGACY_QA.")

    check("qa_doctors_sessions", "owned_row_lms_count_matches_payload", """
        SELECT count(*) FROM public.qa_doctors_sessions q
          JOIN public.lecture_qa_legacy_writes w ON w.legacy_session_id=q.session_id
         WHERE q.date BETWEEN %s AND %s
           AND q.lms_students IS NOT NULL AND q.lms_students_count IS NOT NULL
           AND jsonb_typeof(q.lms_students) = 'object'
           AND jsonb_typeof(q.lms_students->'students') = 'array'
           AND jsonb_array_length(q.lms_students->'students') <> q.lms_students_count""",
        MEDIUM, "Re-sync via SYNC_LEGACY_QA.")

    check("lecture_qa_evaluations", "finalized_has_judgement", """
        SELECT count(*) FROM public.lecture_qa_evaluations q
          JOIN public.lecture_sessions l ON l.lecture_id=q.lecture_id
         WHERE l.session_date BETWEEN %s AND %s AND q.qa_status='COMPLETED'
           AND (q.overall_judgement IS NULL OR btrim(q.overall_judgement)='')""",
        HIGH, "Re-evaluate.")

    check("lecture_transcript_documents", "parsed_has_cues", """
        SELECT count(*) FROM public.lecture_transcript_documents d
          JOIN public.lecture_sessions l ON l.lecture_id=d.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND d.parse_status='PARSED' AND coalesce(d.cue_count,0)=0""",
        HIGH, "Re-parse.")

    check("lecture_attendance_snapshots", "non_negative_counts", """
        SELECT count(*) FROM public.lecture_attendance_snapshots s
          JOIN public.lecture_sessions l ON l.lecture_id=s.lecture_id
         WHERE l.session_date BETWEEN %s AND %s
           AND (s.source_row_count < 0 OR s.present_row_count < 0
                OR s.effective_member_count < 0)""",
        HIGH, "Re-resolve attendance.")

    check("lecture_pipeline_run_items", "status_in_domain", """
        SELECT count(*) FROM public.lecture_pipeline_run_items
         WHERE status NOT IN ('SUCCEEDED','SKIPPED','WAITING','REVIEW_REQUIRED',
                              'FAILED','BLOCKED','LOCKED')
           AND %s::date IS NOT NULL AND %s::date IS NOT NULL""",
        HIGH, "Investigate; the CHECK constraint should prevent this.")

    return {"checks": checks,
            "total_violations": sum(c["violations"] or 0 for c in checks)}


# ---------------------------------------------------------------------------
# dedicated day section
# ---------------------------------------------------------------------------

def phase_single_day(conn, day, run_id, lectures) -> dict:
    day_str = str(day)
    todays = [l for l in lectures if l["session_date"] == day_str]

    run = rows(conn, """
        SELECT r.run_id, r.status, r.lectures_seen, r.completed_count,
               r.waiting_count, r.skipped_count, r.graph_calls, r.provider_calls,
               r.legacy_rows_written, r.started_at, r.finished_at,
               d.duration_ms, d.newly_discovered, d.matched_lectures
          FROM public.backfill_run_days d
          JOIN public.lecture_pipeline_runs r ON r.run_id=d.pipeline_run_id
         WHERE d.backfill_run_id=%s AND d.business_date=%s""", (run_id, day))

    prior = rows(conn, """
        SELECT l.lecture_id, l.subject,
               (SELECT count(*) FROM public.lecture_qa_evaluations q
                 WHERE q.lecture_id=l.lecture_id AND q.created_at < %s) AS evals_before,
               (SELECT count(*) FROM public.lecture_qa_rendered_sessions r
                 WHERE r.lecture_id=l.lecture_id AND r.created_at < %s) AS renders_before,
               (SELECT count(*) FROM public.lecture_qa_legacy_writes w
                 WHERE w.lecture_id=l.lecture_id AND w.created_at < %s) AS legacy_before,
               l.first_discovered_at
          FROM public.lecture_sessions l WHERE l.session_date=%s
         ORDER BY l.scheduled_start""",
        (run[0]["started_at"], run[0]["started_at"], run[0]["started_at"], day)
        if run else (None, None, None, day))

    prior_by_id = {str(p["lecture_id"]): p for p in prior}
    detail = []
    for lecture in todays:
        p = prior_by_id.get(lecture["lecture_id"], {})
        detail.append({
            "lecture_id": lecture["lecture_id"], "subject": lecture["subject"],
            "first_discovered_at": jsonable(p.get("first_discovered_at")),
            "evaluations_before_backfill": p.get("evals_before"),
            "renders_before_backfill": p.get("renders_before"),
            "legacy_writes_before_backfill": p.get("legacy_before"),
            "backfill_action": lecture["backfill_actions"],
            "backfill_item_status": lecture["backfill_item_status"],
            "backfill_final_state": lecture["backfill_final_state"],
            "provider_calls": lecture["provider_calls"],
            "graph_calls": lecture["graph_calls"],
            "qa_evaluation_state": lecture["qa_evaluation_state"],
            "legacy_projection_state": lecture["legacy_projection_state"],
            "legacy_projection_reason": lecture["legacy_projection_reason"],
            "legacy_session_id": lecture["legacy_session_id"],
            "classification": lecture["classification"],
        })
    return {"date": day_str, "lecture_count": len(todays),
            "pipeline_run": jsonable(run[0]) if run else None,
            "lectures": detail}


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def overall_status(findings) -> str:
    if any(f.severity == CRITICAL and f.data_is_wrong for f in findings):
        return "FAIL"
    if any(f.severity == HIGH and f.data_is_wrong for f in findings):
        return "FAIL"
    if findings:
        return "PASS WITH WARNINGS"
    return "PASS"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", default="2026-09-01")
    parser.add_argument("--to", dest="end", default="2026-09-22")
    parser.add_argument("--backfill-run-id",
                        default="14e7ee8b-2fe5-4fe8-bccf-074fd61b795b")
    parser.add_argument("--focus-day", default="2026-09-16")
    parser.add_argument("--json-out", default="docs/audits/"
                        "QA_CORE_PRODUCTION_DATA_AUDIT_2026-09-22.json")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    focus = date.fromisoformat(args.focus_day)

    conn, ro_setting = connect_read_only()
    print(f"read-only proven: transaction_read_only={ro_setting}, write probe refused")

    findings: list = []
    report = {
        "audit_version": AUDIT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release_tag": RC_TAG, "release_commit": RC_COMMIT,
        "window": {"from": str(start), "to": str(end)},
        "backfill_run_id": args.backfill_run_id,
        "connection": {"read_only": True, "transaction_read_only": ro_setting,
                       "write_probe": "REFUSED"},
    }

    schema = phase_schema(conn, findings)
    report["schema"] = schema
    report["tables_audited"] = schema["expected_tables"] + sorted(FOREIGN_TABLES)
    report["row_counts"] = phase_row_counts(conn, report["tables_audited"])
    report["backfill"] = phase_backfill(conn, args.backfill_run_id, findings)
    lectures = phase_lectures(conn, start, end, args.backfill_run_id, findings)
    report["lectures"] = lectures
    report["lecture_classification"] = dict(
        Counter(l["classification"] for l in lectures))
    report["transcripts"] = phase_transcripts(conn, start, end, findings)
    attendance = phase_attendance(conn, start, end, lectures, findings)
    report["attendance_engagement"] = attendance
    report["waiting"] = attendance.pop("waiting", [])
    report["qa"] = phase_qa(conn, start, end, findings)
    report["legacy_projection"] = phase_legacy(conn, start, end, lectures, findings)
    report["perfect"] = phase_perfect(conn, start, end, findings)
    report["recording"] = phase_recording(conn, start, end, lectures, findings)
    report["integrity"] = phase_integrity(conn, start, end, findings)
    report["data_quality"] = phase_field_quality(conn, start, end, findings)
    report["focus_day"] = phase_single_day(conn, focus, args.backfill_run_id, lectures)
    report["duplicates"] = [
        {"lecture_id": l["lecture_id"], "session_date": l["session_date"],
         "subject": l["subject"]}
        for l in lectures if l["duplicate_suppressed"]]

    report["telemetry_findings"] = [asdict(f) for f in findings
                                    if f.telemetry_or_ui_only]
    report["findings"] = [asdict(f) for f in findings]
    report["overall_status"] = overall_status(findings)

    out = REPO_ROOT / args.json_out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(jsonable(report), indent=2, sort_keys=False),
                   encoding="utf-8")
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    print(f"overall_status = {report['overall_status']}")
    print(f"findings: {len(findings)} "
          f"({sum(1 for f in findings if f.data_is_wrong)} data, "
          f"{sum(1 for f in findings if f.telemetry_or_ui_only)} telemetry)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
