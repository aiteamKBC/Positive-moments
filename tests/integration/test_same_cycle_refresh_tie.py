"""
RELEASE GATE: an evaluation and its deterministic refresh written in the SAME
transaction share one updated_at, and the refresh must still be the answer.

Production shape: Andrew - Scheduling Professional, 2026-09-23. One cycle on
2026-09-24 ran QA while attendance was pending AND, after attendance landed,
the free deterministic refresh - both rows carry that transaction's now(). The
"strictly newer wins" rule kept both renders as payloads, the automated sync
saw two writer plans for one lecture, and the lecture stayed "in progress".
The refresh names its origin (deterministic_refresh.origin_evaluation_id), so
the tie is broken by that fact, never by a uuid.

Every connection is rolled back.
"""
import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.qa_writer import LegacyQaTargetRepository
from app.qa.recovery import recovery_state
from tests.integration.seeding import DAY, seed_evaluation, seed_lecture, seed_render
from tests.integration.test_pipeline_contracts import _writer
from tools.integration_db_guard import assert_isolated


@pytest.fixture
def db():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    connection = psycopg.connect(url)
    try:
        assert_isolated(connection)
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _tied_pair(db):
    lecture = seed_lecture(db)
    origin = seed_evaluation(db, lecture, label="origin")
    refresh = seed_evaluation(db, lecture, label="refresh", metadata={
        "deterministic_refresh": {"origin_evaluation_id": str(origin),
                                  "deterministic_refresh_version": "qa_deterministic_refresh_v1"}})
    # Same transaction, so both carry the same now() - as on 2026-09-24.
    tied = db.execute("SELECT updated_at FROM public.lecture_qa_evaluations"
                      " WHERE evaluation_id = %s", (origin,)).fetchone()[0]
    assert db.execute("SELECT updated_at FROM public.lecture_qa_evaluations"
                      " WHERE evaluation_id = %s", (refresh,)).fetchone()[0] == tied
    old = seed_render(db, lecture, evaluation_id=origin, label="r-origin", attended_count=None)
    new = seed_render(db, lecture, evaluation_id=refresh, label="r-refresh", attended_count=6)
    return lecture, origin, refresh, old, new


def test_the_writer_plans_only_the_refresh(db):
    lecture, _origin, refresh, _old, _new = _tied_pair(db)
    plan = _writer().plan_day(db, DAY)
    rows = [row for row in plan["lectures"]
            if row["lecture_id"] == str(lecture["lecture_id"])]
    assert len(rows) == 1, rows


def test_every_reader_agrees_the_refresh_is_current(db):
    lecture, _origin, refresh, _old, new = _tied_pair(db)
    state = recovery_state(db, lecture["lecture_id"])
    assert str(state["current_evaluation"]["evaluation_id"]) == str(refresh)
    assert str(state["rendered"]["rendered_session_id"]) == str(new["rendered_session_id"])
    current = LegacyQaTargetRepository().current_evaluation(db, lecture["lecture_id"])
    assert current["evaluation_id"] == str(refresh)
    assert current["ambiguous"] is False
