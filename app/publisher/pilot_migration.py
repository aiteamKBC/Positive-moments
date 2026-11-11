"""
Promote the successful single-video Bunny pilot's local manifest
(output/bunny_pilot/manifest.json) into migration 024's tables.

PREPARED, NOT EXECUTED AGAINST PRODUCTION IN P0-D. Safe by construction:

  * plan() is read-only and reports, per record: INSERT, SKIP (already present
    and identical) or CONFLICT (present and different - never overwritten);
  * apply() refuses when the plan has any CONFLICT, and writes everything in
    ONE transaction, so it is all-or-nothing;
  * idempotent: a second apply finds every record SKIP and writes nothing; the
    audit events are copied once, guarded by a PILOT_MANIFEST_MIGRATED marker
    carrying the manifest's sha256;
  * the manifest refuses URL-like values on load, and the old account's library
    is refused here as well.

The pilot video keeps its original title and record id (generation 1); the
publisher then sees a live row for that lecture part and never imports it again.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from psycopg.types.json import Jsonb

from app.bunny.identity import IDENTITY_PATTERN
from app.bunny.manifest import PilotManifest
from app.publisher.config import FORBIDDEN_LIBRARY_IDS

VIDEO_COLUMNS = (
    "bunny_video_record_id", "lecture_id", "recording_part_index", "recording_drive_id", "recording_item_id",
    "recording_identity", "bunny_library_id", "bunny_video_id", "video_title", "import_method", "import_status",
    "encoding_status", "bunny_status_code", "encode_progress", "source_file_name", "source_mime_type",
    "source_size_bytes", "source_duration_seconds", "bunny_length_seconds", "bunny_storage_size_bytes",
    "bunny_width", "bunny_height", "encoding_tier", "output_codecs", "available_resolutions", "media_verification",
    "import_submitted_at", "video_identified_at", "encoding_completed_at", "media_verified_at", "attempt_count",
    "last_error_code", "last_error", "metadata", "created_at", "updated_at")
CAPTION_COLUMNS = (
    "publication_id", "bunny_library_id", "bunny_video_id", "lecture_id", "language_code", "caption_label",
    "source_selection_id", "source_artifact_id", "source_content_version", "source_content_sha256", "caption_sha256",
    "caption_bytes", "transform_version", "alignment_version", "alignment_method", "applied_offset_ms",
    "source_cue_count", "published_cue_count", "removed_voice_tag_count", "publication_status", "attempt_count",
    "last_error_code", "last_error", "bunny_validation", "bunny_caption_version", "readback", "upload_started_at",
    "published_at", "verified_at", "created_at", "updated_at")
JSON_COLUMNS = {"media_verification", "metadata", "bunny_validation", "readback"}
MARKER = "PILOT_MANIFEST_MIGRATED"


def manifest_sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _same(existing, record, keys) -> bool:
    return all(str(existing[i] if existing[i] is not None else "").lower() ==
               str(record.get(k) if record.get(k) is not None else "").lower() for i, k in enumerate(keys))


def plan(connection, manifest_path, *, library_id: int) -> dict:
    data = PilotManifest(manifest_path).data
    sha = manifest_sha256(manifest_path)
    items, problems = [], []
    if int(library_id) in FORBIDDEN_LIBRARY_IDS:
        problems.append("the old account's library is never a destination")
    for record in data["videos"].values():
        key = ("lecture_id", "bunny_video_id", "recording_identity", "bunny_library_id")
        action, why = "INSERT", None
        if int(record["bunny_library_id"]) != int(library_id):
            action, why = "CONFLICT", "record belongs to another library"
        elif not IDENTITY_PATTERN.match(record.get("recording_identity") or "") \
                or record["recording_identity"] not in record.get("video_title", ""):
            action, why = "CONFLICT", "identity token missing or not in the title"
        elif connection.execute("SELECT 1 FROM public.lecture_sessions WHERE lecture_id = %s",
                                (record["lecture_id"],)).fetchone() is None:
            action, why = "CONFLICT", "lecture is not in this database"
        else:
            existing = connection.execute(
                "SELECT lecture_id, bunny_video_id, recording_identity, bunny_library_id FROM public.bunny_videos "
                "WHERE bunny_video_record_id = %s", (record["bunny_video_record_id"],)).fetchone()
            if existing is not None:
                action, why = ("SKIP", "already migrated") if _same(existing, record, key) else (
                    "CONFLICT", "a different row holds this record id")
            elif connection.execute(
                    "SELECT 1 FROM public.bunny_videos WHERE retired_at IS NULL AND bunny_library_id = %s "
                    "AND ((lecture_id = %s AND recording_part_index = %s) OR "
                    "(recording_drive_id = %s AND recording_item_id = %s) OR bunny_video_id = %s)",
                    (library_id, record["lecture_id"], record.get("recording_part_index") or 1,
                     record["recording_drive_id"], record["recording_item_id"],
                     record.get("bunny_video_id"))).fetchone():
                action, why = "CONFLICT", "another live row already holds this part, source or Bunny video"
        items.append({"table": "bunny_videos", "id": str(record["bunny_video_record_id"]),
                      "action": action, "why": why})
    for record in data["captions"].values():
        action, why = "INSERT", None
        existing = connection.execute("SELECT caption_sha256, bunny_video_id FROM public.bunny_caption_publications "
                                      "WHERE publication_id = %s", (record["publication_id"],)).fetchone()
        if existing is not None:
            action, why = ("SKIP", "already migrated") if _same(existing, record, ("caption_sha256", "bunny_video_id")) \
                else ("CONFLICT", "a different publication holds this id")
        elif connection.execute("SELECT 1 FROM public.lecture_transcript_artifact_contents WHERE artifact_id = %s "
                                "AND version_number = %s", (record["source_artifact_id"],
                                                            record["source_content_version"])).fetchone() is None:
            action, why = "CONFLICT", "the caption's source transcript version is not in this database"
        items.append({"table": "bunny_caption_publications", "id": str(record["publication_id"]),
                      "action": action, "why": why})
    for pointer in data["current_captions"].values():
        existing = connection.execute(
            "SELECT publication_id FROM public.bunny_caption_current WHERE bunny_library_id = %s "
            "AND bunny_video_id = %s AND language_code = %s",
            (pointer["bunny_library_id"], pointer["bunny_video_id"], pointer["language_code"])).fetchone()
        action = "INSERT" if existing is None else (
            "SKIP" if str(existing[0]) == str(pointer["publication_id"]) else "CONFLICT")
        items.append({"table": "bunny_caption_current", "id": str(pointer["publication_id"]),
                      "action": action, "why": None if action != "CONFLICT" else "another caption is current"})
    marked = connection.execute("SELECT 1 FROM public.bunny_publisher_events WHERE event = %s AND detail ->> 'sha256' = %s",
                                (MARKER, sha)).fetchone() is not None
    items.append({"table": "bunny_publisher_events", "id": sha[:16], "action": "SKIP" if marked else "INSERT",
                  "why": f"{len(data['events'])} audit events" if not marked else "already copied"})
    conflicts = [i for i in items if i["action"] == "CONFLICT"]
    return {"manifest_sha256": sha, "library_id": int(library_id), "items": items, "problems": problems,
            "conflicts": len(conflicts), "inserts": sum(1 for i in items if i["action"] == "INSERT"),
            "applicable": not conflicts and not problems}


def apply(connection, manifest_path, *, library_id: int, actor: str) -> dict:
    """All-or-nothing; refuses any CONFLICT. Returns the plan that was applied."""
    data = PilotManifest(manifest_path).data
    with connection.transaction():
        report = plan(connection, manifest_path, library_id=library_id)
        if not report["applicable"]:
            raise ValueError(f"pilot manifest refused: {report['conflicts']} conflict(s), problems={report['problems']}")
        todo = {(i["table"], i["id"]) for i in report["items"] if i["action"] == "INSERT"}
        for record in data["videos"].values():
            if ("bunny_videos", str(record["bunny_video_record_id"])) in todo:
                _insert(connection, "bunny_videos", VIDEO_COLUMNS,
                        {**record, "metadata": {**(record.get("metadata") or {}), "migrated_from": "pilot_manifest"}})
        for record in data["captions"].values():
            if ("bunny_caption_publications", str(record["publication_id"])) in todo:
                _insert(connection, "bunny_caption_publications", CAPTION_COLUMNS, record)
        for pointer in data["current_captions"].values():
            if ("bunny_caption_current", str(pointer["publication_id"])) in todo:
                connection.execute(
                    "INSERT INTO public.bunny_caption_current (bunny_library_id, bunny_video_id, language_code, "
                    "publication_id, made_current_at) VALUES (%s, %s, %s, %s, %s)",
                    (pointer["bunny_library_id"], pointer["bunny_video_id"], pointer["language_code"],
                     pointer["publication_id"], pointer.get("made_current_at")))
        if ("bunny_publisher_events", report["manifest_sha256"][:16]) in todo:
            for entry in data["events"]:
                detail = {k: v for k, v in entry.items() if k not in ("at", "event")}
                connection.execute("INSERT INTO public.bunny_publisher_events (event, actor, detail, at) VALUES (%s, %s, %s, %s)",
                                   (entry["event"], "pilot_manifest", Jsonb(detail), entry["at"]))
            connection.execute("INSERT INTO public.bunny_publisher_events (event, actor, detail) VALUES (%s, %s, %s)",
                               (MARKER, actor[:120], Jsonb({"sha256": report["manifest_sha256"],
                                                            "events": len(data["events"])})))
    return report


def _insert(connection, table, columns, record) -> None:
    present = [c for c in columns if c in record]
    values = [Jsonb(record[c]) if c in JSON_COLUMNS else record[c] for c in present]
    connection.execute(f"INSERT INTO public.{table} ({', '.join(present)}) VALUES ({', '.join(['%s'] * len(present))})",
                       values)


def dumps(report: dict) -> str:
    return json.dumps(report, indent=2, default=str)
