"""
Against real PostgreSQL: the 2026-10-12..14 spend-limit history, read by the
REAL SQL of both budget readers.

Each seeded lecture carries three attempt rows shaped exactly like the
production rows - written under provider policy v1 as spent (consumes true,
totals 1/2/3), with the provider's own evidence 429
`project_spend_limit_exceeded` / `insufficient_quota` - and an evaluation
already flipped to REVIEW_REQUIRED / MAX_GENERATIONS_EXHAUSTED. Both readers
must give all three attempts back, without a byte of history changing.
"""
from __future__ import annotations

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.qa_writer import GenerationAttemptRepository
from app.qa.recovery import recovery_state
from tests.integration.seeding import seed_evaluation, seed_lecture, sha

SPEND_LIMIT = "project_spend_limit_exceeded"


@pytest.fixture
def db():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    connection = psycopg.connect(url)
    try:
        assert "test" in connection.execute("SELECT current_database()").fetchone()[0]
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _stranded_lecture(db, *, label, second_status=429):
    lecture = seed_lecture(db)
    fingerprint = sha(f"eval{lecture['lecture_id']}{label}")
    seed_evaluation(db, lecture, qa_status="REVIEW_REQUIRED", label=label, ai_called=True,
                    error_code="provider_http_error",
                    review_reason="MAX_GENERATIONS_EXHAUSTED", provider_attempts=3,
                    metadata={"http_status": 429, "generation_number": 3,
                              "provider_error_code": "provider_http_error",
                              "provider_contract_version": "strict_json_schema_v1"})
    repository = GenerationAttemptRepository()
    for number in (1, 2, 3):
        status = second_status if number == 2 else 429
        evidence = ({"provider_error_code": SPEND_LIMIT,
                     "provider_error_type": "insufficient_quota"} if status == 429 else {})
        repository.record(db, {
            "lecture_id": lecture["lecture_id"], "source_fingerprint": fingerprint,
            "qa_engine_version": "engine", "prompt_version": "prompt",
            "model_name": "gpt-5.2", "outcome": "MODEL_ERROR",
            "error_code": "provider_http_error",
            "metadata": {"generation_number": number, "consumes_generation_budget": True,
                         "generation_budget_used": number, "http_status": status,
                         "failure_class": "GENERATION_FAILURE",
                         "provider_failure_policy_version":
                             "provider_failure_classification_v1",
                         **evidence}})
    return lecture, fingerprint


def _history(db, fingerprint):
    return db.execute(
        "SELECT generation_number, md5(a::text) FROM public.lecture_qa_generation_attempts a "
        " WHERE source_fingerprint = %s ORDER BY generation_number",
        (fingerprint,)).fetchall()


def test_three_spend_limit_refusals_release_the_lecture_in_both_readers(db):
    lecture, fingerprint = _stranded_lecture(db, label="spend")
    before = _history(db, fingerprint)

    budget = GenerationAttemptRepository().budget(db, fingerprint, max_generations=3)
    assert budget["attempt_records"] == 3
    assert budget["generation_budget_used"] == 0
    assert budget["attempts_remaining"] == 3 and not budget["exhausted"]

    current = recovery_state(db, lecture["lecture_id"])["current_evaluation"]
    assert current["attempts_used"] == 0 and current["attempts_remaining"] == 3
    assert current["configuration_failures"] == 3
    assert current["last_attempt_configuration_failure"] is True
    assert _history(db, fingerprint) == before


def test_a_real_failure_among_the_refusals_still_counts(db):
    lecture, fingerprint = _stranded_lecture(db, label="mixed", second_status=500)
    budget = GenerationAttemptRepository().budget(db, fingerprint, max_generations=3)
    assert budget["generation_budget_used"] == 1
    current = recovery_state(db, lecture["lecture_id"])["current_evaluation"]
    assert current["attempts_used"] == 1 and current["attempts_remaining"] == 2
