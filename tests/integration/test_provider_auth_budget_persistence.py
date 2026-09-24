"""
RELEASE GATE: the generation budget, read by the REAL SQL, against real
PostgreSQL.

The 2026-09-23 shape on synthetic evidence: two attempt rows written by the
old code for HTTP 401 refusals - `MODEL_ERROR` / `provider_http_error`, no
HTTP status, no budget flag - and one evaluation recording the last 401. The
repository's `budget()` and the recovery state the orchestrator resolves
stages from must both read that as zero generations spent, without a single
byte of either row changing. Real failures must still count, and the first
answer written afterwards must not resurrect the old refusals.
"""
from __future__ import annotations

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.qa_writer import GenerationAttemptRepository
from app.qa.recovery import recovery_state
from tests.integration.seeding import seed_evaluation, seed_lecture, sha


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


def _legacy_401_lecture(db, *, http_status=401, label="auth"):
    lecture = seed_lecture(db)
    fingerprint = sha(f"eval{lecture['lecture_id']}{label}")
    seed_evaluation(db, lecture, qa_status="MODEL_ERROR", label=label, ai_called=True,
                    error_code="provider_http_error", review_reason="PROVIDER_ERROR",
                    provider_attempts=2,
                    metadata={"http_status": http_status, "generation_number": 2,
                              "provider_error_code": "provider_http_error",
                              "provider_contract_version": "strict_json_schema_v1"})
    repository = GenerationAttemptRepository()
    for number in (1, 2):
        repository.record(db, {
            "lecture_id": lecture["lecture_id"], "source_fingerprint": fingerprint,
            "qa_engine_version": "engine", "prompt_version": "prompt",
            "model_name": "gpt-5.2", "outcome": "MODEL_ERROR",
            "error_code": "provider_http_error",
            "metadata": {"generation_number": number}})
    return lecture, fingerprint


def _history(db, fingerprint):
    return db.execute(
        "SELECT generation_number, md5(a::text) FROM public.lecture_qa_generation_attempts a "
        " WHERE source_fingerprint = %s ORDER BY generation_number",
        (fingerprint,)).fetchall()


def test_two_historical_401s_spend_no_budget_in_the_repository_or_the_resolver(db):
    lecture, fingerprint = _legacy_401_lecture(db)
    before = _history(db, fingerprint)

    budget = GenerationAttemptRepository().budget(db, fingerprint, max_generations=3)
    assert budget["attempt_records"] == 2
    assert budget["generation_budget_used"] == 0
    assert budget["attempts_remaining"] == 3 and not budget["exhausted"]

    current = recovery_state(db, lecture["lecture_id"])["current_evaluation"]
    assert current["attempts_used"] == 0 and current["attempts_remaining"] == 3
    assert current["attempt_records"] == 2 and current["configuration_failures"] == 2
    assert current["last_attempt_configuration_failure"] is True
    assert current["attempts_aggregate_agrees"] is True
    assert _history(db, fingerprint) == before


def test_a_new_refusal_and_then_an_answer_keep_the_history_free(db):
    lecture, fingerprint = _legacy_401_lecture(db)
    before = _history(db, fingerprint)
    repository = GenerationAttemptRepository()
    repository.record(db, {
        "lecture_id": lecture["lecture_id"], "source_fingerprint": fingerprint,
        "qa_engine_version": "engine", "prompt_version": "prompt", "model_name": "gpt-5.2",
        "outcome": "MODEL_ERROR", "error_code": "provider_configuration_error",
        "metadata": {"generation_number": 3, "consumes_generation_budget": False,
                     "generation_budget_used": 0, "http_status": 401,
                     "failure_class": "PROVIDER_CONFIGURATION_ERROR"}})
    assert repository.budget(db, fingerprint, max_generations=3)[
        "generation_budget_used"] == 0

    # The key is fixed: an answer overwrites the evaluation. The old refusals
    # were judged when the refusal above was written and stay judged.
    repository.record(db, {
        "lecture_id": lecture["lecture_id"], "source_fingerprint": fingerprint,
        "qa_engine_version": "engine", "prompt_version": "prompt", "model_name": "gpt-5.2",
        "outcome": "COMPLETED", "error_code": None,
        "metadata": {"generation_number": 4, "consumes_generation_budget": True,
                     "generation_budget_used": 1}})
    db.execute("UPDATE public.lecture_qa_evaluations SET qa_status = 'COMPLETED', "
               "       error_code = NULL, ai_raw_output = '{}'::jsonb, provider_attempts = 4 "
               " WHERE source_fingerprint = %s", (fingerprint,))
    budget = repository.budget(db, fingerprint, max_generations=3)
    assert budget["attempt_records"] == 4 and budget["generation_budget_used"] == 1
    current = recovery_state(db, lecture["lecture_id"])["current_evaluation"]
    assert current["attempts_used"] == 1 and current["attempts_aggregate_agrees"] is True
    assert _history(db, fingerprint)[:2] == before


def test_historical_transport_failures_without_401_evidence_still_count(db):
    lecture, fingerprint = _legacy_401_lecture(db, http_status=500, label="server")
    budget = GenerationAttemptRepository().budget(db, fingerprint, max_generations=3)
    assert budget["generation_budget_used"] == 2
    current = recovery_state(db, lecture["lecture_id"])["current_evaluation"]
    assert current["attempts_used"] == 2 and current["attempts_remaining"] == 1
    assert current["last_attempt_configuration_failure"] is False


def test_the_ledger_numbering_is_unchanged_and_append_only(db):
    """`count` stays the record count the unique generation number relies on."""
    lecture, fingerprint = _legacy_401_lecture(db)
    repository = GenerationAttemptRepository()
    assert repository.count(db, fingerprint) == 2
    assert repository.record(db, {
        "lecture_id": lecture["lecture_id"], "source_fingerprint": fingerprint,
        "qa_engine_version": "engine", "prompt_version": "prompt", "model_name": "gpt-5.2",
        "outcome": "MODEL_ERROR", "error_code": "provider_configuration_error",
        "metadata": {"consumes_generation_budget": False,
                     "generation_budget_used": 0}}) == 3
