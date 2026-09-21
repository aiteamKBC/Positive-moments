"""
Phase 6A/6B: persistence for the measured recording timeline.

One row per (owner, transcript part), where the owner is EITHER a canonical
lecture or a legacy analysed session - see migration 018. Both live in one
table and are read through one repository, because "where does canonical time
T live in media" must have exactly one answer and exactly one way to obtain it.

The identity is deterministic, so re-measuring updates the row the previous
measurement wrote instead of growing a second opinion about where part 2 begins.
"""
import uuid

from app.common.errors import DATABASE_ERROR, PlatformError

# Same namespace convention as the rest of the platform's deterministic ids.
RECORDING_PART_NAMESPACE = uuid.UUID("6f2a1f2e-0b6d-5a2f-9f4a-2f5f0a9c7d31")

COLUMNS = """
    recording_part_id, lecture_id, legacy_session_id, part_index,
    canonical_offset_ms, selection_id, user_id, meeting_id, recording_id,
    content_correlation_id, media_duration_seconds, media_start_time_seconds,
    media_duration_source, container, video_codec, audio_codec, size_bytes,
    provider_created_at, provider_end_at, media_coordinate_version"""

VALUES = """
    %(recording_part_id)s, %(lecture_id)s, %(legacy_session_id)s,
    %(part_index)s, %(canonical_offset_ms)s, %(selection_id)s, %(user_id)s,
    %(meeting_id)s, %(recording_id)s, %(content_correlation_id)s,
    %(media_duration_seconds)s, %(media_start_time_seconds)s,
    %(media_duration_source)s, %(container)s, %(video_codec)s,
    %(audio_codec)s, %(size_bytes)s, %(provider_created_at)s,
    %(provider_end_at)s, %(media_coordinate_version)s"""

UPDATES = """
    canonical_offset_ms      = EXCLUDED.canonical_offset_ms,
    selection_id             = EXCLUDED.selection_id,
    user_id                  = EXCLUDED.user_id,
    meeting_id               = EXCLUDED.meeting_id,
    recording_id             = EXCLUDED.recording_id,
    content_correlation_id   = EXCLUDED.content_correlation_id,
    media_duration_seconds   = EXCLUDED.media_duration_seconds,
    media_start_time_seconds = EXCLUDED.media_start_time_seconds,
    media_duration_source    = EXCLUDED.media_duration_source,
    container                = EXCLUDED.container,
    video_codec              = EXCLUDED.video_codec,
    audio_codec              = EXCLUDED.audio_codec,
    size_bytes               = EXCLUDED.size_bytes,
    provider_created_at      = EXCLUDED.provider_created_at,
    provider_end_at          = EXCLUDED.provider_end_at,
    media_coordinate_version = EXCLUDED.media_coordinate_version,
    measured_at              = NOW(),
    updated_at               = NOW()"""

# Two statements rather than one parameterised target: a conflict target is SQL,
# not a value, and building it by interpolation is how a query string becomes
# an injection point. The partial unique indexes come from migration 018.
UPSERT_CANONICAL = f"""
INSERT INTO public.lecture_recording_parts ({COLUMNS})
VALUES ({VALUES})
ON CONFLICT (lecture_id, part_index) WHERE lecture_id IS NOT NULL
DO UPDATE SET {UPDATES}
RETURNING recording_part_id
"""

UPSERT_LEGACY = f"""
INSERT INTO public.lecture_recording_parts ({COLUMNS})
VALUES ({VALUES})
ON CONFLICT (legacy_session_id, part_index) WHERE legacy_session_id IS NOT NULL
DO UPDATE SET {UPDATES}
RETURNING recording_part_id
"""

SELECT_COLUMNS = """
SELECT part_index, canonical_offset_ms, user_id, meeting_id, recording_id,
       content_correlation_id, media_duration_seconds,
       media_start_time_seconds, media_duration_source, container,
       video_codec, audio_codec, size_bytes, measured_at,
       media_coordinate_version
  FROM public.lecture_recording_parts"""

FOR_LECTURE = SELECT_COLUMNS + "\n WHERE lecture_id = %s\n ORDER BY part_index"
FOR_LEGACY = SELECT_COLUMNS + "\n WHERE legacy_session_id = %s\n ORDER BY part_index"

FIELDS = ("part_index", "canonical_offset_ms", "user_id", "meeting_id",
          "recording_id", "content_correlation_id", "media_duration_seconds",
          "media_start_time_seconds", "media_duration_source", "container",
          "video_codec", "audio_codec", "size_bytes", "measured_at",
          "media_coordinate_version")


def recording_part_id(owner_id, part_index: int) -> uuid.UUID:
    return uuid.uuid5(RECORDING_PART_NAMESPACE, f"{owner_id}:{part_index}")


def _payload(part: dict, *, lecture_id, legacy_session_id) -> dict:
    owner = lecture_id if lecture_id is not None else legacy_session_id
    return {
        "recording_part_id": recording_part_id(owner, part["part_index"]),
        "lecture_id": str(lecture_id) if lecture_id is not None else None,
        "legacy_session_id": legacy_session_id,
        "part_index": part["part_index"],
        "canonical_offset_ms": part["canonical_offset_ms"],
        "selection_id": part.get("selection_id"),
        "user_id": part["user_id"],
        "meeting_id": part["meeting_id"],
        "recording_id": part["recording_id"],
        "content_correlation_id": part.get("content_correlation_id"),
        "media_duration_seconds": part["media_duration_seconds"],
        "media_start_time_seconds": part.get("media_start_time_seconds"),
        "media_duration_source": part["media_duration_source"],
        "container": part.get("container"),
        "video_codec": part.get("video_codec"),
        "audio_codec": part.get("audio_codec"),
        "size_bytes": part.get("size_bytes"),
        "provider_created_at": part.get("provider_created_at"),
        "provider_end_at": part.get("provider_end_at"),
        "media_coordinate_version": part["media_coordinate_version"],
    }


class RecordingPartRepository:
    """Read and write the measured media timeline. No Graph calls here."""

    def for_lecture(self, connection, lecture_id) -> list[dict]:
        return self._read(connection, FOR_LECTURE, str(lecture_id))

    def for_legacy_session(self, connection, session_id: str) -> list[dict]:
        return self._read(connection, FOR_LEGACY, session_id)

    def _read(self, connection, statement, key) -> list[dict]:
        try:
            rows = connection.execute(statement, (key,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR,
                                "recording part read failed") from exc
        return [dict(zip(FIELDS, row)) for row in rows]

    def upsert(self, connection, lecture_id, part: dict) -> uuid.UUID:
        return self._write(
            connection, UPSERT_CANONICAL,
            _payload(part, lecture_id=lecture_id, legacy_session_id=None))

    def upsert_legacy(self, connection, session_id: str, part: dict) -> uuid.UUID:
        return self._write(
            connection, UPSERT_LEGACY,
            _payload(part, lecture_id=None, legacy_session_id=session_id))

    def _write(self, connection, statement, payload) -> uuid.UUID:
        try:
            row = connection.execute(statement, payload).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR,
                                "recording part write failed") from exc
        return row[0]
