"""
The transcript stage through the real ARTIFACTS statement (app/orchestration/state.py).

A failed refresh marks an artifact CONTENT_FETCH_ERROR although its content is still stored
(mark_content_failure). Every row below is synthetic (tests/integration/seeding.py).
"""
from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest

from tests.integration.seeding import seed_lecture
from app.config.settings import Settings
from app.orchestration.factory import build_resolver
from app.orchestration.stages import ACQUIRE_TRANSCRIPT, COMPLETE, FAILED, TRANSCRIPT


@pytest.fixture
def db():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    connection = psycopg.connect(url)
    try:
        name = connection.execute("SELECT current_database()").fetchone()[0]
        assert "test" in name, "refusing: not the isolated test database"
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _artifact(connection, artifact_id, *, status, content_version, created_at=None):
    stored = content_version > 0
    connection.execute("""
        UPDATE public.lecture_transcript_artifacts
           SET artifact_status = %(status)s, content_version = %(version)s,
               content_sha256 = %(sha)s, content_bytes = %(bytes)s,
               content_format = %(format)s, speaker_attribution = %(speakers)s,
               provider_created_at = coalesce(%(at)s::timestamptz, provider_created_at),
               provider_end_at = coalesce(%(at)s::timestamptz + interval '2 hours', provider_end_at)
         WHERE artifact_id = %(id)s""",
                       {"status": status, "version": content_version, "sha": "a" * 64 if stored else None,
                        "bytes": 1000 if stored else None, "format": "text/vtt" if stored else None,
                        "speakers": True if stored else None, "at": created_at, "id": artifact_id})


def _stage(connection, lecture):
    return build_resolver(probe=False).for_lecture(connection, str(lecture["lecture_id"]))["stages"][TRANSCRIPT]


def test_stored_content_with_a_failed_refresh_is_a_complete_transcript(db):
    lecture = seed_lecture(db)
    _artifact(db, lecture["artifact_ids"][0], status="CONTENT_FETCH_ERROR", content_version=1)
    assert _stage(db, lecture)["state"] == COMPLETE


def test_the_day_without_content_fails_although_an_earlier_week_is_stored(db):
    lecture = seed_lecture(db, artifacts=["week-before", "the-day"])
    earlier, today = lecture["artifact_ids"]
    _artifact(db, earlier, status="CONTENT_FETCH_ERROR", content_version=1,
              created_at=lecture["start"] - timedelta(days=7))
    _artifact(db, today, status="CONTENT_FETCH_ERROR", content_version=0)
    stage = _stage(db, lecture)
    assert (stage["state"], stage["action"]) == (FAILED, ACQUIRE_TRANSCRIPT)


def test_a_failed_fetch_with_nothing_stored_anywhere_still_fails(db):
    lecture = seed_lecture(db)
    _artifact(db, lecture["artifact_ids"][0], status="CONTENT_FETCH_ERROR", content_version=0)
    assert _stage(db, lecture)["state"] == FAILED
