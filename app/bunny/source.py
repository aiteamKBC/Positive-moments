"""
Authoritative source facts for one lecture's Bunny import, READ-ONLY.

Database: the connection must report `transaction_read_only = on`; anything
else is refused before a single query runs. Rows come from the verified chain
  lecture_sessions
  lecture_recording_links  (+ qa_doctors_sessions, which must agree)
  lecture_transcript_selections (current SELECTION_VERSION)
    -> lecture_transcript_selection_parts
    -> lecture_transcript_artifacts -> lecture_transcript_artifact_contents
lecture_combined_transcripts and qa_doctors_transcripts are deliberately not
read. The raw transcript is held in memory only and never logged.

Graph: DriveItem METADATA only - no download URL is requested here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import quote

from app.transcripts.selection import SELECTION_VERSION

DRIVE_ITEM_FIELDS = "id,name,size,file,video,parentReference,createdDateTime"


class SourceError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


@dataclass(frozen=True)
class TranscriptSource:
    selection_id: str
    selection_version: str
    selected_part_count: int
    part_index: int
    part_offset_ms: int
    artifact_id: str
    provider_transcript_id: str
    provider_created_at: datetime
    provider_end_at: datetime
    content_correlation_id: str | None
    content_version: int
    stored_content_sha256: str
    raw_content: str = field(repr=False)

    @property
    def window_seconds(self) -> float:
        return (self.provider_end_at - self.provider_created_at).total_seconds()


@dataclass(frozen=True)
class LectureSource:
    lecture_id: str
    subject: str
    session_date: date
    is_cancelled: bool
    recording_drive_id: str | None
    recording_item_id: str | None
    recording_filename: str | None
    link_stage_state: str | None
    link_status: str | None
    link_metadata: dict
    legacy_drive_id: str | None
    legacy_item_id: str | None
    recording_part_rows: int
    transcript: TranscriptSource | None
    selections_seen: list


def _require_read_only(connection) -> None:
    value = connection.execute("SHOW transaction_read_only").fetchone()[0]
    if value != "on":
        raise SourceError("CONNECTION_NOT_READ_ONLY",
                          "the source connection must be read-only (transaction_read_only=on)")


def _rows(connection, sql: str, args: tuple) -> list:
    cursor = connection.cursor()
    cursor.execute(sql, args)
    return cursor.fetchall()


def load_lecture_source(connection, lecture_id: str) -> LectureSource:
    _require_read_only(connection)
    lecture = _rows(connection, """
        SELECT lecture_id::text, subject, session_date, is_cancelled
          FROM public.lecture_sessions WHERE lecture_id = %s""", (lecture_id,))
    if not lecture:
        raise SourceError("LECTURE_NOT_FOUND", "no lecture_sessions row for this lecture_id")
    _, subject, session_date, cancelled = lecture[0]

    link = _rows(connection, """
        SELECT recording_drive_id, recording_item_id, recording_filename, stage_state, status,
               metadata, legacy_session_id
          FROM public.lecture_recording_links WHERE lecture_id = %s""", (lecture_id,))
    drive = item = filename = state = status = legacy_id = None
    metadata: dict = {}
    if link:
        drive, item, filename, state, status, metadata, legacy_id = link[0]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

    selections = _rows(connection, """
        SELECT selection_id::text, selection_version, selection_status, selected_part_count
          FROM public.lecture_transcript_selections WHERE lecture_id = %s""", (lecture_id,))
    current = [row for row in selections if row[1] == SELECTION_VERSION]
    transcript = None
    if len(current) == 1 and current[0][2] == "SELECTED":
        selection_id, version, _, part_count = current[0]
        parts = _rows(connection, """
            SELECT sp.part_index, sp.part_offset_ms, sp.artifact_id::text, sp.provider_transcript_id,
                   sp.provider_created_at, sp.provider_end_at, a.content_correlation_id,
                   a.content_version, a.content_sha256
              FROM public.lecture_transcript_selection_parts sp
              JOIN public.lecture_transcript_artifacts a USING (artifact_id)
             WHERE sp.selection_id = %s ORDER BY sp.part_index""", (selection_id,))
        if len(parts) == 1:
            (part_index, offset_ms, artifact_id, transcript_id, created, ended, correlation,
             artifact_version, artifact_sha) = parts[0]
            content = _rows(connection, """
                SELECT version_number, raw_content, content_sha256, content_format
                  FROM public.lecture_transcript_artifact_contents
                 WHERE artifact_id = %s ORDER BY version_number DESC LIMIT 1""", (artifact_id,))
            if content:
                version_number, raw, content_sha, content_format = content[0]
                if content_format != "text/vtt":
                    raise SourceError("TRANSCRIPT_NOT_VTT", "stored transcript is not text/vtt")
                if version_number != artifact_version or content_sha != artifact_sha:
                    raise SourceError("TRANSCRIPT_POINTER_MISMATCH",
                                      "artifact current-version pointer disagrees with its latest content row")
                transcript = TranscriptSource(
                    selection_id=selection_id, selection_version=version,
                    selected_part_count=part_count, part_index=part_index,
                    part_offset_ms=int(offset_ms), artifact_id=artifact_id,
                    provider_transcript_id=transcript_id, provider_created_at=created,
                    provider_end_at=ended, content_correlation_id=correlation,
                    content_version=version_number, stored_content_sha256=content_sha,
                    raw_content=raw)
        elif parts:
            transcript = None                        # multipart: refused by preflight

    legacy_drive = legacy_item = None
    lookup_id = legacy_id or (transcript.provider_transcript_id if transcript else None)
    if lookup_id:
        legacy = _rows(connection, """
            SELECT recording_drive_id, recording_item_id FROM public.qa_doctors_sessions
             WHERE session_id = %s""", (lookup_id,))
        if legacy:
            legacy_drive, legacy_item = legacy[0]

    part_rows = _rows(connection, """
        SELECT count(*) FROM public.lecture_recording_parts WHERE lecture_id = %s""", (lecture_id,))[0][0]

    return LectureSource(
        lecture_id=str(lecture_id), subject=subject, session_date=session_date,
        is_cancelled=bool(cancelled), recording_drive_id=drive, recording_item_id=item,
        recording_filename=filename, link_stage_state=state, link_status=status,
        link_metadata=metadata or {}, legacy_drive_id=legacy_drive, legacy_item_id=legacy_item,
        recording_part_rows=int(part_rows), transcript=transcript,
        selections_seen=[{"selection_version": r[1], "status": r[2], "parts": r[3]} for r in selections])


@dataclass(frozen=True)
class DriveItemFacts:
    item_id: str
    drive_id: str | None
    name: str | None
    mime_type: str | None
    size_bytes: int | None
    duration_seconds: float | None
    parent_id: str | None
    created_at: str | None
    video: dict


def drive_item_facts(graph, drive_id: str, item_id: str) -> DriveItemFacts:
    """GET /drives/{d}/items/{i}?$select=... - metadata only, no download URL."""
    item = graph.get_json(f"/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}"
                          f"?$select={DRIVE_ITEM_FIELDS}")
    item.pop("@microsoft.graph.downloadUrl", None)
    parent = item.get("parentReference") or {}
    video = item.get("video") or {}
    duration = video.get("duration")
    return DriveItemFacts(
        item_id=str(item.get("id") or ""), drive_id=parent.get("driveId"), name=item.get("name"),
        mime_type=(item.get("file") or {}).get("mimeType"), size_bytes=item.get("size"),
        duration_seconds=int(duration) / 1000.0 if duration else None,
        parent_id=parent.get("id"), created_at=item.get("createdDateTime"),
        video={k: v for k, v in video.items() if k != "duration"})
