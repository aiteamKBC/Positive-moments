"""
PROPOSED migration 024 (Bunny tracking) against the isolated test database.

The migration is applied INSIDE the test transaction (its own BEGIN/COMMIT
stripped) and rolled back afterwards, so the shared test database is left
exactly as tools/bootstrap_integration_db.py built it.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

import psycopg
import pytest

from app.bunny.identity import build_title, recording_identity, video_record_id
from app.config.settings import Settings
from tests.integration.publisher_db import reset_proposed_migrations
from tests.integration.seeding import insert, seed_lecture
from tools.integration_db_guard import assert_isolated

PROPOSED = Path(__file__).resolve().parents[2] / "app" / "db" / "migrations_proposed"
MIGRATION = PROPOSED / "024_create_bunny_stream_tables.sql"
ROLLBACK = PROPOSED / "024_create_bunny_stream_tables.rollback.sql"
LIBRARY = 786045


def _body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return re.sub(r"^\s*(BEGIN|COMMIT);\s*$", "", text, flags=re.MULTILINE)


@pytest.fixture
def db():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    reset_proposed_migrations(url)              # a killed earlier run must not fail this one
    connection = psycopg.connect(url)
    try:
        assert "test" in connection.execute("SELECT current_database()").fetchone()[0]
        # The live isolation check rolls back the open transaction the first
        # time it sees a connection; run it BEFORE the DDL so it is cached.
        assert_isolated(connection)
        exists = connection.execute("SELECT to_regclass('public.bunny_videos')").fetchone()[0]
        assert exists is None, "024 must not already be applied to the test database"
        connection.execute(_body(MIGRATION))
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _fails(connection, sql, args=()):
    with pytest.raises(psycopg.errors.IntegrityError):
        with connection.transaction():
            connection.execute(sql, args)


def _video(connection, lecture_id, *, drive="b!drive", item="ITEM1", part=1, video_id=None,
           status="PENDING", encoding="NOT_STARTED"):
    identity = recording_identity(drive, item)
    record = video_record_id(LIBRARY, drive, item)
    connection.execute("""
        INSERT INTO public.bunny_videos (bunny_video_record_id, lecture_id, recording_part_index,
            recording_drive_id, recording_item_id, recording_identity, bunny_library_id, bunny_video_id,
            video_title, import_status, encoding_status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (record, lecture_id, part, drive, item, identity, LIBRARY, video_id,
         build_title("Synthetic", "2026-10-16", str(lecture_id), identity), status, encoding))
    return record


def _content(connection, lecture):
    artifact = lecture["artifact_ids"][0]
    raw = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello\n"
    insert(connection, "lecture_transcript_artifact_contents", content_id=uuid.uuid4(), artifact_id=artifact,
           version_number=1, content_format="text/vtt", speaker_attribution=False, raw_content=raw,
           content_sha256=hashlib.sha256(raw.encode()).hexdigest(), content_bytes=len(raw))
    return artifact


def _publication(connection, lecture, video_id, artifact, *, sha="a" * 64, status="VERIFIED", language="en"):
    publication = uuid.uuid4()
    connection.execute("""
        INSERT INTO public.bunny_caption_publications (publication_id, bunny_library_id,
            bunny_video_id, lecture_id, language_code, caption_label, source_selection_id,
            source_artifact_id, source_content_version, source_content_sha256, caption_sha256,
            caption_bytes, transform_version, applied_offset_ms, source_cue_count,
            published_cue_count, publication_status, published_at)
        VALUES (%s,%s,%s,%s,%s,'English',%s,%s,1,%s,%s,100,'teams_vtt_strip_voice_v1',0,1,1,%s,now())""",
        (publication, LIBRARY, video_id, lecture["lecture_id"], language, lecture["selection_id"], artifact,
         "b" * 64, sha, status))
    return publication


def test_video_identity_constraints(db):
    lecture = seed_lecture(db)
    _video(db, lecture["lecture_id"])
    # same Microsoft recording twice in one library
    _fails(db, """INSERT INTO public.bunny_videos (bunny_video_record_id, lecture_id, recording_drive_id,
               recording_item_id, recording_identity, bunny_library_id, video_title, import_status)
               SELECT gen_random_uuid(), lecture_id, recording_drive_id, recording_item_id, 'rec-00000000',
                      bunny_library_id, 'x | rec-00000000', 'PENDING' FROM public.bunny_videos""")
    # identified without a Bunny id
    _fails(db, "UPDATE public.bunny_videos SET import_status='IDENTIFIED'")
    # encoding without a Bunny id
    _fails(db, "UPDATE public.bunny_videos SET encoding_status='IN_PROGRESS'")
    # a second recording of the same lecture needs a distinct part
    _fails(db, """INSERT INTO public.bunny_videos (bunny_video_record_id, lecture_id, recording_drive_id,
               recording_item_id, recording_identity, bunny_library_id, video_title, import_status)
               SELECT gen_random_uuid(), lecture_id, 'b!drive', 'ITEM2', 'rec-11111111', bunny_library_id,
                      'x | rec-11111111', 'PENDING' FROM public.bunny_videos""")
    _video(db, lecture["lecture_id"], item="ITEM2", part=2)
    # title must carry the identity token
    _fails(db, "UPDATE public.bunny_videos SET video_title='no token' WHERE recording_item_id='ITEM2'")
    # unknown lecture
    _fails(db, """INSERT INTO public.bunny_videos (bunny_video_record_id, lecture_id, recording_drive_id,
               recording_item_id, recording_identity, bunny_library_id, video_title, import_status)
               VALUES (gen_random_uuid(), gen_random_uuid(), 'd', 'i', 'rec-22222222', 1, 'x rec-22222222', 'PENDING')""")


def test_bunny_video_id_is_unique_per_library(db):
    lecture = seed_lecture(db)
    vid = uuid.uuid4()
    _video(db, lecture["lecture_id"], video_id=vid, status="IDENTIFIED", encoding="IN_PROGRESS")
    _fails(db, """INSERT INTO public.bunny_videos (bunny_video_record_id, lecture_id, recording_part_index,
               recording_drive_id, recording_item_id, recording_identity, bunny_library_id, bunny_video_id,
               video_title, import_status, encoding_status)
               VALUES (gen_random_uuid(), %s, 2, 'd2', 'i2', 'rec-33333333', %s, %s, 'x | rec-33333333',
                       'IDENTIFIED', 'IN_PROGRESS')""", (lecture["lecture_id"], LIBRARY, vid))


def test_caption_publications_history_and_current_pointer(db):
    lecture = seed_lecture(db)
    vid = uuid.uuid4()
    _video(db, lecture["lecture_id"], video_id=vid, status="IDENTIFIED", encoding="VERIFIED")
    artifact = _content(db, lecture)
    first = _publication(db, lecture, vid, artifact, sha="a" * 64)
    second = _publication(db, lecture, vid, artifact, sha="c" * 64, status="PUBLISHED")
    # the same bytes + transform for the same video+language is one publication
    _fails(db, """INSERT INTO public.bunny_caption_publications SELECT * FROM
               public.bunny_caption_publications WHERE publication_id = %s""", (first,))
    # caption for a video that is not tracked
    _fails(db, """UPDATE public.bunny_caption_publications SET bunny_video_id = gen_random_uuid()
                  WHERE publication_id = %s""", (second,))
    # source version that does not exist
    _fails(db, """UPDATE public.bunny_caption_publications SET source_content_version = 9
                  WHERE publication_id = %s""", (second,))
    # published without a timestamp
    _fails(db, """UPDATE public.bunny_caption_publications SET published_at = NULL
                  WHERE publication_id = %s""", (second,))

    db.execute("""INSERT INTO public.bunny_caption_current
                  (bunny_library_id, bunny_video_id, language_code, publication_id) VALUES (%s,%s,'en',%s)""",
               (LIBRARY, vid, first))
    # only one current per video+language
    _fails(db, """INSERT INTO public.bunny_caption_current
                  (bunny_library_id, bunny_video_id, language_code, publication_id) VALUES (%s,%s,'en',%s)""",
           (LIBRARY, vid, second))
    # pointer cannot name a publication of another language
    _fails(db, """INSERT INTO public.bunny_caption_current
                  (bunny_library_id, bunny_video_id, language_code, publication_id) VALUES (%s,%s,'fr',%s)""",
           (LIBRARY, vid, second))
    # switching the current version is one UPDATE
    db.execute("UPDATE public.bunny_caption_current SET publication_id = %s", (second,))
    assert db.execute("SELECT count(*) FROM public.bunny_caption_publications").fetchone()[0] == 2
    # a referenced video cannot be deleted
    _fails(db, "DELETE FROM public.bunny_videos")


def test_rollback_refuses_with_rows_and_drops_when_empty(db):
    lecture = seed_lecture(db)
    _video(db, lecture["lecture_id"])
    with pytest.raises(psycopg.errors.RaiseException):
        with db.transaction():
            db.execute(_body(ROLLBACK))
    db.execute("DELETE FROM public.bunny_videos")
    db.execute(_body(ROLLBACK))
    assert db.execute("SELECT to_regclass('public.bunny_videos')").fetchone()[0] is None
    # re-apply is clean (IF NOT EXISTS + additive)
    db.execute(_body(MIGRATION))
    db.execute(_body(MIGRATION))
    assert db.execute("SELECT to_regclass('public.bunny_caption_current')").fetchone()[0] is not None


def test_migration_touches_no_existing_table():
    text = MIGRATION.read_text(encoding="utf-8")
    code = re.sub(r"\bON (DELETE|UPDATE)\b", "", re.sub(r"--[^\n]*", "", text))
    assert not re.search(r"\b(ALTER|DROP|TRUNCATE|DELETE|UPDATE|INSERT)\b", code)
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS public\.([a-z_]+)", text))
    assert created == {"bunny_videos", "bunny_caption_publications",
                       "bunny_caption_current"}
