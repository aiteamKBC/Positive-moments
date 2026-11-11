"""
Repository-backed Bunny state for the continuous publisher (migration 024).

Replaces the pilot's local manifest: same records, same state machine, in
PostgreSQL. Every transition is compare-and-set on the row's current status, so
a worker acting on a stale view changes nothing. Nothing here calls Bunny or
Graph, and no URL is ever written (024's CHECKs refuse one in metadata).

    video   PENDING -> SUBMITTING -> SUBMITTED | SUBMISSION_UNCERTAIN -> IDENTIFIED
            encoding NOT_STARTED -> IN_PROGRESS -> FINISHED -> VERIFIED | FAILED | REVIEW_REQUIRED
    caption PREPARED -> UPLOADING | UPLOAD_UNCERTAIN -> PUBLISHED -> VERIFIED (current) [-> SUPERSEDED]

A confirmed failed import is RETIRED (retired_at + reason), never deleted: the
row keeps the deleted Bunny video id as audit evidence, and the re-import is a
new row of the next generation.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

VIDEO_FIELDS = (
    "bunny_video_record_id", "lecture_id", "recording_part_index", "recording_drive_id", "recording_item_id",
    "recording_identity", "bunny_library_id", "bunny_video_id", "video_title", "import_status", "encoding_status",
    "bunny_status_code", "encode_progress", "source_file_name", "source_size_bytes", "source_duration_seconds",
    "bunny_length_seconds", "import_submitted_at", "video_identified_at", "encoding_completed_at",
    "media_verified_at", "attempt_count", "last_error_code", "retired_at", "retire_reason", "visibility_state",
    "metadata", "created_at", "updated_at")
_V = ", ".join(VIDEO_FIELDS)

IN_FLIGHT = """
SELECT count(*) FROM public.bunny_videos
 WHERE bunny_library_id = %s AND retired_at IS NULL
   AND (import_status IN ('SUBMITTING', 'SUBMITTED', 'SUBMISSION_UNCERTAIN')
        OR (import_status = 'IDENTIFIED' AND encoding_status IN ('NOT_STARTED', 'IN_PROGRESS')))
"""
# One slot lock per library: counting and claiming a slot are one decision.
SLOT_LOCK_NAMESPACE = 0x4B42_5331        # "KBS1"

INSERT_VIDEO = f"""
INSERT INTO public.bunny_videos (
    bunny_video_record_id, lecture_id, recording_part_index, recording_drive_id, recording_item_id,
    recording_identity, bunny_library_id, video_title, import_status, encoding_status, source_file_name,
    source_size_bytes, source_duration_seconds, metadata)
VALUES (%(record_id)s, %(lecture_id)s, %(part)s, %(drive)s, %(item)s, %(identity)s, %(library)s, %(title)s,
        'PENDING', 'NOT_STARTED', %(file_name)s, %(size)s, %(duration)s, %(metadata)s)
ON CONFLICT DO NOTHING
RETURNING {_V}
"""


def _video(row):
    return dict(zip(VIDEO_FIELDS, row)) if row else None


class BunnyVideoRepository:

    # -- reads --------------------------------------------------------------------------
    def live_for_part(self, connection, library_id, lecture_id, part_index) -> dict | None:
        return _video(connection.execute(
            f"SELECT {_V} FROM public.bunny_videos WHERE bunny_library_id = %s AND lecture_id = %s "
            "AND recording_part_index = %s AND retired_at IS NULL", (library_id, lecture_id, part_index)).fetchone())

    def live_for_source(self, connection, library_id, drive_id, item_id) -> dict | None:
        return _video(connection.execute(
            f"SELECT {_V} FROM public.bunny_videos WHERE bunny_library_id = %s AND recording_drive_id = %s "
            "AND recording_item_id = %s AND retired_at IS NULL", (library_id, drive_id, item_id)).fetchone())

    def by_record(self, connection, record_id) -> dict | None:
        return _video(connection.execute(
            f"SELECT {_V} FROM public.bunny_videos WHERE bunny_video_record_id = %s", (record_id,)).fetchone())

    def for_lecture(self, connection, lecture_id, library_id=None) -> list[dict]:
        rows = connection.execute(
            f"SELECT {_V} FROM public.bunny_videos WHERE lecture_id = %s "
            "AND (%s::bigint IS NULL OR bunny_library_id = %s) ORDER BY recording_part_index, created_at",
            (lecture_id, library_id, library_id)).fetchall()
        return [_video(r) for r in rows]

    def for_library(self, connection, library_id) -> list[dict]:
        rows = connection.execute(f"SELECT {_V} FROM public.bunny_videos WHERE bunny_library_id = %s",
                                  (library_id,)).fetchall()
        return [_video(r) for r in rows]

    def generations(self, connection, library_id, drive_id, item_id) -> int:
        """Rows ever created for this source in this library (retired ones included)."""
        return connection.execute(
            "SELECT count(*) FROM public.bunny_videos WHERE bunny_library_id = %s "
            "AND recording_drive_id = %s AND recording_item_id = %s", (library_id, drive_id, item_id)).fetchone()[0]

    def retired_video_ids(self, connection, library_id, identity) -> set[str]:
        rows = connection.execute(
            "SELECT bunny_video_id FROM public.bunny_videos WHERE bunny_library_id = %s "
            "AND recording_identity = %s AND retired_at IS NOT NULL AND bunny_video_id IS NOT NULL",
            (library_id, identity)).fetchall()
        return {str(r[0]).lower() for r in rows}

    def in_flight(self, connection, library_id) -> int:
        return connection.execute(IN_FLIGHT, (library_id,)).fetchone()[0]

    def lock_slots(self, connection, library_id) -> None:
        """Transaction-scoped: serializes 'count in-flight, then claim a slot' per library."""
        connection.execute("SELECT pg_advisory_xact_lock(%s, %s)", (SLOT_LOCK_NAMESPACE, int(library_id) % 2_000_000_000))

    # -- writes (compare-and-set) -------------------------------------------------------
    def create(self, connection, **values) -> dict | None:
        """The persisted INTENT. None when a live row already holds this part, source or token."""
        values["metadata"] = Jsonb(values.get("metadata") or {})
        return _video(connection.execute(INSERT_VIDEO, values).fetchone())

    def transition(self, connection, record_id, *, expect: dict, **changes) -> dict | None:
        """
        UPDATE ... WHERE the row still has the `expect`ed column values. Returns
        the new row, or None when someone else changed it first.
        `metadata` changes are merged (jsonb ||); `history` appends one entry.
        """
        sets, args = ["updated_at = now()"], []
        history = changes.pop("history", None)
        metadata = changes.pop("metadata", None)
        for column, value in changes.items():
            if column not in VIDEO_FIELDS and column not in (
                    "encode_progress", "bunny_width", "bunny_height", "output_codecs", "available_resolutions",
                    "bunny_storage_size_bytes", "encoding_tier", "media_verification", "next_attempt_after"):
                raise ValueError(f"unknown column {column}")
            if value == "now()":
                sets.append(f"{column} = now()")
                continue
            sets.append(f"{column} = %s")
            args.append(Jsonb(value) if column == "media_verification" else value)
        if metadata:
            sets.append("metadata = metadata || %s")
            args.append(Jsonb(metadata))
        if history:
            sets.append("bunny_status_history = bunny_status_history || %s")
            args.append(Jsonb([history]))
        where, wargs = ["bunny_video_record_id = %s"], [record_id]
        for column, value in expect.items():
            if value is None:
                where.append(f"{column} IS NULL")
            elif isinstance(value, (tuple, list, frozenset, set)):
                where.append(f"{column} = ANY(%s)")
                wargs.append(list(value))
            else:
                where.append(f"{column} = %s")
                wargs.append(value)
        row = connection.execute(f"UPDATE public.bunny_videos SET {', '.join(sets)} "
                                 f"WHERE {' AND '.join(where)} RETURNING {_V}", args + wargs).fetchone()
        return _video(row)


PUBLICATION_FIELDS = (
    "publication_id", "bunny_library_id", "bunny_video_id", "lecture_id", "language_code", "caption_label",
    "source_selection_id", "source_artifact_id", "source_content_version", "source_content_sha256", "caption_sha256",
    "transform_version", "applied_offset_ms", "publication_status", "attempt_count", "last_error_code",
    "published_at", "verified_at", "superseded_at", "created_at", "updated_at")
_P = ", ".join(PUBLICATION_FIELDS)


def _publication(row):
    return dict(zip(PUBLICATION_FIELDS, row)) if row else None


class BunnyCaptionRepository:

    def publication(self, connection, publication_id) -> dict | None:
        return _publication(connection.execute(
            f"SELECT {_P} FROM public.bunny_caption_publications WHERE publication_id = %s",
            (publication_id,)).fetchone())

    def current(self, connection, library_id, video_id, language) -> dict | None:
        return _publication(connection.execute(
            f"SELECT {', '.join('p.' + f for f in PUBLICATION_FIELDS)} FROM public.bunny_caption_current c "
            "JOIN public.bunny_caption_publications p ON p.publication_id = c.publication_id "
            "WHERE c.bunny_library_id = %s AND c.bunny_video_id = %s AND c.language_code = %s",
            (library_id, video_id, language)).fetchone())

    def for_video(self, connection, library_id, video_id) -> list[dict]:
        rows = connection.execute(
            f"SELECT {_P} FROM public.bunny_caption_publications WHERE bunny_library_id = %s "
            "AND bunny_video_id = %s ORDER BY created_at", (library_id, video_id)).fetchall()
        return [_publication(r) for r in rows]

    def prepare(self, connection, **values) -> dict:
        """PREPARED publication (idempotent on the deterministic publication id)."""
        values = {**values, "bunny_validation": Jsonb({}), "readback": Jsonb({}),
                  "local_validation": Jsonb(values.get("local_validation") or {})}
        connection.execute("""
            INSERT INTO public.bunny_caption_publications (
                publication_id, bunny_library_id, bunny_video_id, lecture_id, language_code, caption_label,
                source_selection_id, source_artifact_id, source_content_version, source_content_sha256,
                caption_sha256, caption_bytes, transform_version, alignment_version, alignment_method,
                applied_offset_ms, source_cue_count, published_cue_count, removed_voice_tag_count,
                publication_status, bunny_validation, readback)
            VALUES (%(publication_id)s, %(library_id)s, %(video_id)s, %(lecture_id)s, %(language)s, %(label)s,
                    %(selection_id)s, %(artifact_id)s, %(content_version)s, %(content_sha256)s, %(caption_sha256)s,
                    %(caption_bytes)s, %(transform_version)s, %(alignment_version)s, %(alignment_method)s,
                    %(offset_ms)s, %(source_cues)s, %(published_cues)s, %(removed_voice_tags)s, 'PREPARED',
                    %(bunny_validation)s, %(readback)s)
            ON CONFLICT (publication_id) DO NOTHING""", values)
        return self.publication(connection, values["publication_id"])

    def transition(self, connection, publication_id, *, expect, **changes) -> dict | None:
        sets, args = ["updated_at = now()"], []
        for column, value in changes.items():
            if value == "now()":
                sets.append(f"{column} = now()")
            elif column == "attempt_count" and value == "+1":
                sets.append("attempt_count = attempt_count + 1")
            else:
                sets.append(f"{column} = %s")
                args.append(Jsonb(value) if column in ("bunny_validation", "readback") else value)
        statuses = list(expect) if isinstance(expect, (tuple, list, set, frozenset)) else [expect]
        row = connection.execute(
            f"UPDATE public.bunny_caption_publications SET {', '.join(sets)} "
            f"WHERE publication_id = %s AND publication_status = ANY(%s) RETURNING {_P}",
            args + [publication_id, statuses]).fetchone()
        return _publication(row)

    def make_current(self, connection, publication: dict) -> str | None:
        """Point the video+language at this VERIFIED publication; the previous one is SUPERSEDED. Returns it."""
        key = (publication["bunny_library_id"], publication["bunny_video_id"], publication["language_code"])
        previous = connection.execute(
            "SELECT publication_id FROM public.bunny_caption_current WHERE bunny_library_id = %s "
            "AND bunny_video_id = %s AND language_code = %s FOR UPDATE", key).fetchone()
        connection.execute("""
            INSERT INTO public.bunny_caption_current (bunny_library_id, bunny_video_id, language_code,
                                                              publication_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (bunny_library_id, bunny_video_id, language_code)
            DO UPDATE SET publication_id = EXCLUDED.publication_id, made_current_at = now()""",
                           key + (publication["publication_id"],))
        if previous and str(previous[0]) != str(publication["publication_id"]):
            connection.execute("UPDATE public.bunny_caption_publications SET publication_status = 'SUPERSEDED', "
                               "superseded_at = now(), updated_at = now() WHERE publication_id = %s", (previous[0],))
            return str(previous[0])
        return None
