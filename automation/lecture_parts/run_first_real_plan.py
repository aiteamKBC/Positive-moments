#!/usr/bin/env python
"""Controlled real-data runner for Lecture Split Planner v1.

The command has two phases:

* prepare: fetch real Graph metadata/transcript and emit only bounded AI windows
* commit:  repeat the fetch, validate supplied AI JSON, save one idempotent plan,
           and print exactly three future-job previews (never inserts jobs)

No token, credential, Authorization header, transcript body, recording URL, or
DriveItem identifier is printed.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row

from graph_client import GraphAppClient, GraphError
from planner import (
    SPLIT_VERSION,
    build_ai_request,
    build_candidate_windows,
    lecture_timeline,
    parse_transcript,
    preview_media_jobs,
    split_plan_key,
    validate_ai_output,
)


REPO = Path(__file__).resolve().parents[2]
ENV_FILE = REPO / "backend" / ".env"
MIN_PLAUSIBLE_DURATION_SECONDS = 60.0
MAX_PLAUSIBLE_DURATION_SECONDS = 24 * 60 * 60.0


def load_config() -> dict[str, str]:
    values = {key: value for key, value in dotenv_values(ENV_FILE).items() if value}
    if not values.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL not found in backend/.env")
    return values


def select_session(conn: psycopg.Connection, subject: str, lecture_date: date) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.date, s.subject, s.trainer, s.session_id, s.meeting_id,
                   s.recording_drive_id, s.recording_item_id,
                   s.recording_duration_seconds, s.clips_media_origin
            FROM public.qa_doctors_sessions s
            WHERE s.subject = %s
              AND s.date = %s
              AND nullif(s.session_id, '') IS NOT NULL
              AND nullif(s.meeting_id, '') IS NOT NULL
              AND nullif(s.recording_drive_id, '') IS NOT NULL
              AND nullif(s.recording_item_id, '') IS NOT NULL
              AND lower(coalesce(s.cancelled_session, '')) NOT IN
                    ('yes', 'true', '1', 'cancelled', 'canceled')
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.qa_lecture_split_plans p
                  WHERE p.session_id = s.session_id
                    AND p.split_version = %s
              )
            """,
            (subject, lecture_date, SPLIT_VERSION),
        )
        rows = cur.fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            f"expected exactly one eligible lecture for subject/date, found {len(rows)}"
        )
    return dict(rows[0])


def _user_lookup_path(trainer: str) -> str:
    escaped = trainer.replace("'", "''")
    query = urllib.parse.urlencode(
        {"$filter": f"displayName eq '{escaped}'", "$select": "id,displayName,userPrincipalName"}
    )
    return "/users?" + query


def resolve_transcript_context(
    graph: GraphAppClient, session: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Resolve the one explicit user context containing session_id as transcript id."""
    users = sorted(
        graph.get_collection(_user_lookup_path(session["trainer"])),
        key=lambda row: (str(row.get("userPrincipalName", "")).lower(), str(row.get("id", ""))),
    )
    matches: list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = []
    errors: list[dict[str, Any]] = []
    meeting = urllib.parse.quote(session["meeting_id"], safe="")
    for user in users:
        user_id = user.get("id")
        if not user_id:
            continue
        context = urllib.parse.quote(str(user_id), safe="")
        try:
            artifacts = graph.get_collection(
                f"/users/{context}/onlineMeetings/{meeting}/transcripts"
            )
        except GraphError as exc:
            errors.append(
                {
                    "context": user.get("displayName"),
                    "status": exc.status,
                    "code": exc.code,
                }
            )
            continue
        exact = [row for row in artifacts if row.get("id") == session["session_id"]]
        if len(exact) == 1:
            matches.append((user, artifacts, exact[0]))
        elif len(exact) > 1:
            raise RuntimeError("duplicate transcript artifact IDs returned by Graph")
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one explicit user/transcript match, found {len(matches)}; errors={errors}"
        )
    user, artifacts, selected = matches[0]
    return user, artifacts, selected, errors


def _parse_graph_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Graph {field} is missing")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def validate_duration(seconds: Any) -> float:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float, Decimal)):
        raise ValueError("recording duration is not numeric")
    result = round(float(seconds), 3)
    if result <= 0:
        raise ValueError("recording duration must be greater than zero")
    if not MIN_PLAUSIBLE_DURATION_SECONDS <= result <= MAX_PLAUSIBLE_DURATION_SECONDS:
        raise ValueError(
            f"recording duration {result:.3f}s is outside the plausible 60s-24h range"
        )
    return result


def get_recording_duration(
    graph: GraphAppClient,
    session: dict[str, Any],
    user: dict[str, Any],
    transcript: dict[str, Any],
    *,
    allow_artifact_fallback: bool,
) -> tuple[float, dict[str, Any], dict[str, Any] | None]:
    drive_path = (
        "/drives/"
        + urllib.parse.quote(session["recording_drive_id"], safe="")
        + "/items/"
        + urllib.parse.quote(session["recording_item_id"], safe="")
        + "?$select=id,name,size,video"
    )
    drive_error: dict[str, Any] | None = None
    try:
        item = graph.get_json(drive_path)
        raw_duration = (item.get("video") or {}).get("duration")
        if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)):
            raise ValueError("DriveItem video.duration is missing or non-numeric")
        duration = validate_duration(float(raw_duration) / 1000.0)
        return duration, {
            "endpoint": "/drives/{drive-id}/items/{item-id}?$select=id,name,size,video",
            "raw_field": "driveItem.video.duration",
            "raw_value": raw_duration,
            "raw_unit": "milliseconds",
            "normalization": "video.duration / 1000",
        }, None
    except GraphError as exc:
        drive_error = {"status": exc.status, "code": exc.code, "message": exc.message}
        if not allow_artifact_fallback:
            raise

    correlation = transcript.get("contentCorrelationId")
    if not isinstance(correlation, str) or not correlation:
        raise RuntimeError("selected transcript has no contentCorrelationId")
    context = urllib.parse.quote(str(user["id"]), safe="")
    meeting = urllib.parse.quote(session["meeting_id"], safe="")
    query = urllib.parse.urlencode({"$filter": f"contentCorrelationId eq '{correlation}'"})
    recordings = graph.get_collection(
        f"/users/{context}/onlineMeetings/{meeting}/recordings?{query}"
    )
    if len(recordings) != 1:
        raise RuntimeError(
            f"expected one recording matching transcript contentCorrelationId, found {len(recordings)}"
        )
    recording = recordings[0]
    started = _parse_graph_datetime(recording.get("createdDateTime"), "createdDateTime")
    ended = _parse_graph_datetime(recording.get("endDateTime"), "endDateTime")
    duration = validate_duration((ended - started).total_seconds())
    return duration, {
        "endpoint": "/users/{userId}/onlineMeetings/{meetingId}/recordings"
                    "?$filter=contentCorrelationId eq {transcriptCorrelationId}",
        "raw_field": "callRecording.createdDateTime -> callRecording.endDateTime",
        "raw_value": {
            "createdDateTime": recording.get("createdDateTime"),
            "endDateTime": recording.get("endDateTime"),
        },
        "raw_unit": "ISO 8601 UTC timestamps",
        "normalization": "endDateTime - createdDateTime in seconds",
    }, drive_error


def fetch_transcript_content(
    graph: GraphAppClient,
    session: dict[str, Any],
    user: dict[str, Any],
    transcript: dict[str, Any],
) -> tuple[str, str]:
    context = urllib.parse.quote(str(user["id"]), safe="")
    meeting = urllib.parse.quote(session["meeting_id"], safe="")
    transcript_id = urllib.parse.quote(str(transcript["id"]), safe="")
    response = graph.request(
        "GET",
        f"/users/{context}/onlineMeetings/{meeting}/transcripts/{transcript_id}/content",
        accept="text/vtt",
    )
    try:
        content = response.body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Graph transcript content was not valid UTF-8") from exc
    return content, response.content_type


def inferred_media_origin(session: dict[str, Any]) -> str:
    stored = session.get("clips_media_origin")
    if stored in ("history", "live"):
        return stored
    return "history" if session["date"] < date.today() else "live"


def collect_real_inputs(
    graph: GraphAppClient,
    session: dict[str, Any],
    *,
    allow_artifact_fallback: bool,
) -> dict[str, Any]:
    user, artifacts, selected_transcript, context_errors = resolve_transcript_context(graph, session)
    duration, duration_metadata, drive_error = get_recording_duration(
        graph,
        session,
        user,
        selected_transcript,
        allow_artifact_fallback=allow_artifact_fallback,
    )
    transcript_content, content_type = fetch_transcript_content(
        graph, session, user, selected_transcript
    )
    cues = parse_transcript(transcript_content, content_type)
    # The old guard compared the last cue against a single recording's length
    # and rejected every multipart lecture, where the transcript legitimately
    # runs past the end of the FIRST recording. Whether a canonical time exists
    # in media is now app.media.coordinates' question, answered against every
    # recording rather than one of them.
    timeline = lecture_timeline(cues)
    try:
        windows = build_candidate_windows(cues, timeline)
    except ValueError as exc:
        raise ValueError(
            f"{exc}; parsed_cues={len(cues)}, first_start={cues[0].start_seconds:.3f}, "
            f"last_end={cues[-1].end_seconds:.3f}"
        ) from exc
    return {
        "user": user,
        "artifacts": artifacts,
        "selected_transcript": selected_transcript,
        "context_errors": context_errors,
        "duration": duration,
        "duration_metadata": duration_metadata,
        "drive_error": drive_error,
        "content_type": content_type,
        "cues": cues,
        "timeline": timeline,
        "windows": windows,
    }


def plan_row(plan: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    parts = {part["part_number"]: part for part in plan.parts}
    return {
        "split_plan_key": split_plan_key(plan.session_id, plan.split_version),
        "session_id": plan.session_id,
        "split_version": plan.split_version,
        "media_origin": plan.media_origin,
        "recording_duration_seconds": plan.recording_duration_seconds,
        "cut_1_cue": plan.cut_1_cue,
        "cut_1_seconds": plan.cut_1_seconds,
        "cut_1_reason": plan.cut_1_reason,
        "cut_2_cue": plan.cut_2_cue,
        "cut_2_seconds": plan.cut_2_seconds,
        "cut_2_reason": plan.cut_2_reason,
        "part_1_title": parts.get(1, {}).get("title"),
        "part_1_summary": parts.get(1, {}).get("summary"),
        "part_2_title": parts.get(2, {}).get("title"),
        "part_2_summary": parts.get(2, {}).get("summary"),
        "part_3_title": parts.get(3, {}).get("title"),
        "part_3_summary": parts.get(3, {}).get("summary"),
        "planner_status": plan.planner_status,
        "planner_model": plan.planner_model,
        "planner_confidence": plan.planner_confidence,
        "planner_metadata": json.dumps(
            {
                "errors": plan.errors,
                "duration_source": inputs["duration_metadata"],
                "drive_item_error": inputs["drive_error"],
                "transcript_artifact_count": len(inputs["artifacts"]),
                "selected_transcript_rule": "artifact.id == qa_doctors_sessions.session_id",
                "parsed_cue_count": len(inputs["cues"]),
                "candidate_windows": {
                    name: {
                        "target_seconds": window["target_seconds"],
                        "zone_start_seconds": window["zone_start_seconds"],
                        "zone_end_seconds": window["zone_end_seconds"],
                        "candidate_cue_count": len(window["cues"]),
                    }
                    for name, window in inputs["windows"].items()
                },
            }
        ),
    }


def save_plan(
    conn: psycopg.Connection,
    session: dict[str, Any],
    inputs: dict[str, Any],
    plan: Any,
) -> dict[str, Any]:
    row = plan_row(plan, inputs)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (session["session_id"] + ":" + SPLIT_VERSION,),
        )
        existing = session.get("recording_duration_seconds")
        if existing is None or float(existing) <= 0:
            cur.execute(
                """
                UPDATE public.qa_doctors_sessions
                SET recording_duration_seconds = %s
                WHERE session_id = %s
                  AND (recording_duration_seconds IS NULL OR recording_duration_seconds <= 0)
                """,
                (inputs["duration"], session["session_id"]),
            )
            duration_action = "updated" if cur.rowcount == 1 else "unchanged"
        else:
            duration_action = "preserved_valid_existing"

        cur.execute(
            """
            INSERT INTO public.qa_lecture_split_plans (
                split_plan_key, session_id, split_version, media_origin,
                recording_duration_seconds,
                cut_1_cue, cut_1_seconds, cut_1_reason,
                cut_2_cue, cut_2_seconds, cut_2_reason,
                part_1_title, part_1_summary, part_2_title, part_2_summary,
                part_3_title, part_3_summary,
                planner_status, planner_model, planner_confidence, planner_metadata
            ) VALUES (
                %(split_plan_key)s, %(session_id)s, %(split_version)s, %(media_origin)s,
                %(recording_duration_seconds)s,
                %(cut_1_cue)s, %(cut_1_seconds)s, %(cut_1_reason)s,
                %(cut_2_cue)s, %(cut_2_seconds)s, %(cut_2_reason)s,
                %(part_1_title)s, %(part_1_summary)s, %(part_2_title)s, %(part_2_summary)s,
                %(part_3_title)s, %(part_3_summary)s,
                %(planner_status)s, %(planner_model)s, %(planner_confidence)s,
                %(planner_metadata)s::jsonb
            )
            ON CONFLICT (session_id, split_version) DO UPDATE SET
                split_plan_key = EXCLUDED.split_plan_key,
                media_origin = EXCLUDED.media_origin,
                recording_duration_seconds = EXCLUDED.recording_duration_seconds,
                cut_1_cue = EXCLUDED.cut_1_cue,
                cut_1_seconds = EXCLUDED.cut_1_seconds,
                cut_1_reason = EXCLUDED.cut_1_reason,
                cut_2_cue = EXCLUDED.cut_2_cue,
                cut_2_seconds = EXCLUDED.cut_2_seconds,
                cut_2_reason = EXCLUDED.cut_2_reason,
                part_1_title = EXCLUDED.part_1_title,
                part_1_summary = EXCLUDED.part_1_summary,
                part_2_title = EXCLUDED.part_2_title,
                part_2_summary = EXCLUDED.part_2_summary,
                part_3_title = EXCLUDED.part_3_title,
                part_3_summary = EXCLUDED.part_3_summary,
                planner_status = EXCLUDED.planner_status,
                planner_model = EXCLUDED.planner_model,
                planner_confidence = EXCLUDED.planner_confidence,
                planner_metadata = EXCLUDED.planner_metadata,
                updated_at = NOW()
            RETURNING split_plan_id, (xmax = 0) AS created, planner_status
            """,
            row,
        )
        saved = dict(cur.fetchone())
    conn.commit()
    saved["duration_action"] = duration_action
    return saved


def public_report(
    session: dict[str, Any],
    inputs: dict[str, Any],
    *,
    plan: Any | None = None,
    saved: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "selected_lecture": {
            "date": str(session["date"]),
            "subject": session["subject"],
            "trainer": session["trainer"],
            "session_id": session["session_id"],
        },
        "graph_authentication": "OAuth 2.0 client credentials; in-memory cached app token",
        "graph_user_context": {
            "display_name": inputs["user"].get("displayName"),
            "principal_name": inputs["user"].get("userPrincipalName"),
            "endpoint_shape": "/users/{userId}/onlineMeetings/{meetingId}",
        },
        "recording_metadata": inputs["duration_metadata"],
        "recording_duration_seconds": inputs["duration"],
        "drive_item_issue": inputs["drive_error"],
        "transcript_retrieval": "real Graph WebVTT retrieved",
        "transcript_artifacts_found": len(inputs["artifacts"]),
        "transcript_selection_rule": "exact artifact ID match to session_id",
        "parsed_cues": len(inputs["cues"]),
        "cut_1_region": {
            key: inputs["windows"]["cut_1"][key]
            for key in ("target_seconds", "zone_start_seconds", "zone_end_seconds")
        },
        "cut_2_region": {
            key: inputs["windows"]["cut_2"][key]
            for key in ("target_seconds", "zone_start_seconds", "zone_end_seconds")
        },
        "graph_context_attempt_errors": inputs["context_errors"],
    }
    if plan is not None:
        previews = preview_media_jobs(plan, session)
        report.update(
            {
                "plan": plan.as_dict(),
                "validator_result": "accepted" if plan.planner_status == "planned" else "rejected",
                "split_plan_db": saved,
                "media_job_previews": [
                    {
                        key: job[key]
                        for key in (
                            "part_number", "start_seconds", "end_seconds", "duration_seconds",
                            "job_key", "output_filename", "priority", "job_type", "cut_mode",
                        )
                    }
                    | {"media_origin": plan.media_origin}
                    for job in previews
                ],
                "media_jobs_inserted": 0,
            }
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", required=True)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--ai-output", type=Path)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--windows-only", action="store_true")
    parser.add_argument("--window", choices=("cut_1", "cut_2"))
    parser.add_argument("--allow-recording-artifact-duration", action="store_true")
    args = parser.parse_args()
    if args.commit and not args.ai_output:
        parser.error("--commit requires --ai-output")
    if args.ai_output and not args.commit:
        parser.error("--ai-output is only valid with --commit")

    config = load_config()
    graph = GraphAppClient.from_mapping(config)
    with psycopg.connect(config["DATABASE_URL"], connect_timeout=20, row_factory=dict_row) as conn:
        session = select_session(conn, args.subject, args.date)
        session["clips_media_origin"] = inferred_media_origin(session)
        inputs = collect_real_inputs(
            graph,
            session,
            allow_artifact_fallback=args.allow_recording_artifact_duration,
        )
        if not args.commit:
            if args.window:
                print(json.dumps(inputs["windows"][args.window], indent=2))
                return
            if args.windows_only:
                print(json.dumps(inputs["windows"], indent=2))
                return
            print(json.dumps(public_report(session, inputs), indent=2, default=str))
            print("AI_REQUEST")
            print(json.dumps(build_ai_request(session, inputs["windows"], inputs["timeline"]), indent=2))
            return

        ai_output = json.loads(args.ai_output.read_text(encoding="utf-8"))
        plan = validate_ai_output(
            ai_output,
            inputs["cues"],
            inputs["windows"],
            inputs["timeline"],
            session["session_id"],
            session["clips_media_origin"],
            model="codex-gpt-5",
            recording_duration_seconds=inputs["duration"],
        )
        saved = save_plan(conn, session, inputs, plan)
        print(json.dumps(public_report(session, inputs, plan=plan, saved=saved), indent=2, default=str))


if __name__ == "__main__":
    try:
        main()
    except (GraphError, RuntimeError, ValueError, psycopg.Error) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
