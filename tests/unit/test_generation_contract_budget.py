"""
Phase 3C3D unit tests: the generation budget follows the generation CONTRACT.

Two things had to be true at once and were not:

  * three exhausted generations under the old plain-JSON contract must stay
    exhausted, so nothing can reset a working cap by re-running it;
  * changing WHO enforces the schema is a materially different contract, so it
    must open its own budget rather than inherit a spent one.

Both fall out of the same fact - the contract version is in the fingerprint,
and the cap is keyed on the fingerprint. And separately: the evaluation's
attempt aggregate must be the generation count, not the HTTP retry count of
the last call, which is the defect that made an exhausted lecture report 1.

No provider, no database.
"""
from datetime import date

import pytest

from app.qa import structured_output as contract
from app.qa.provider import ProviderError
from app.qa.service import (
    COMPLETED,
    INVALID_STRUCTURED_OUTPUT,
    MAX_GENERATIONS_EXHAUSTED,
    REVIEW_REQUIRED,
    QaInputError,
    ShadowQaService,
)
from app.qa.validation import validate_structured_output

from tests.unit.test_shadow_qa import (
    StubEvaluations,
    StubInputs,
    StubProvider,
    StubRuns,
    good_output,
    package,
)


TARGET_DATE = date(2026, 9, 4)


class StubAttempts:
    """The authoritative append-only attempts table, in memory."""

    def __init__(self, existing=None):
        self.rows = list(existing or [])

    def count(self, connection, source_fingerprint):
        return sum(1 for row in self.rows
                   if row["source_fingerprint"] == source_fingerprint)

    def record(self, connection, entry):
        number = self.count(connection, entry["source_fingerprint"]) + 1
        self.rows.append({**entry, "generation_number": number})
        return number


class Evaluations(StubEvaluations):
    """StubEvaluations plus the terminal-review transition the cap performs."""

    def mark_review_required(self, connection, evaluation_id, *, reason, attempts):
        for row in self.rows.values():
            if row["evaluation_id"] == evaluation_id:
                row["qa_status"] = "REVIEW_REQUIRED"
                row["evaluation"]["qa_status"] = "REVIEW_REQUIRED"
                row["evaluation"]["review_reason"] = reason
                row["evaluation"]["provider_attempts"] = max(
                    row["evaluation"].get("provider_attempts") or 0, attempts)


class ContractProvider(StubProvider):
    """A provider that declares which contract it was configured for."""

    def __init__(self, response_contract, output=None, error=None):
        super().__init__(output=output, error=error)
        self.response_contract = response_contract


def _service(*, contract_version, provider, attempts, evaluations=None, packages=None):
    return ShadowQaService(
        input_repository=StubInputs(packages or [package()]),
        evaluation_repository=evaluations or Evaluations(),
        run_repository=StubRuns(), provider=provider,
        attempt_repository=attempts,
        resolver_version="r1", role_algorithm_version="o1",
        engagement_algorithm_version="e1", model_name="gpt-5.2",
        provider_contract_version=contract_version)


def _run(service):
    return service.run_day(None, TARGET_DATE, execute=True)


def _fingerprint(contract_version):
    """
    The fingerprint the SERVICE would compute, not a re-derivation.

    A no-cost preview run is used deliberately: the service normalises the
    package (Item 2 bounds, contract version) before fingerprinting, so
    recomputing it here by hand would test the test rather than the service.
    """
    service = _service(contract_version=contract_version, provider=None,
                       attempts=StubAttempts())
    return service.run_day(None, TARGET_DATE, execute=False)["lectures"][0][
        "source_fingerprint"]


def _spent(contract_version, count=3):
    """A history of `count` invalid generations under one contract."""
    fingerprint = _fingerprint(contract_version)
    return StubAttempts([{"source_fingerprint": fingerprint,
                          "outcome": INVALID_STRUCTURED_OUTPUT}
                         for _ in range(count)])


# --- 1. the old budget stays spent ------------------------------------------

def test_three_spent_generations_under_the_same_contract_buy_nothing_more():
    """G2 Keith's real state: re-running must not call the model again."""
    provider = ContractProvider(contract.JSON_OBJECT, good_output())
    service = _service(contract_version=contract.JSON_OBJECT, provider=provider,
                       attempts=_spent(contract.JSON_OBJECT))
    summary = _run(service)
    lecture = summary["lectures"][0]
    assert provider.calls == 0
    assert lecture["qa_status"] == REVIEW_REQUIRED
    assert lecture["review_reason"] == MAX_GENERATIONS_EXHAUSTED
    assert lecture["attempts_remaining"] == 0


def test_the_old_attempt_history_is_never_rewritten_or_relabelled():
    attempts = _spent(contract.JSON_OBJECT)
    before = [dict(row) for row in attempts.rows]
    provider = ContractProvider(contract.STRICT_JSON_SCHEMA, good_output())
    _run(_service(contract_version=contract.STRICT_JSON_SCHEMA, provider=provider,
                  attempts=attempts))
    assert attempts.rows[:3] == before
    assert all(row["source_fingerprint"] == _fingerprint(contract.JSON_OBJECT)
               for row in before)


# --- 2. the new contract gets its own, equally bounded, budget --------------

def test_the_strict_contract_is_a_different_fingerprint_so_the_budget_is_fresh():
    provider = ContractProvider(contract.STRICT_JSON_SCHEMA, good_output())
    service = _service(contract_version=contract.STRICT_JSON_SCHEMA, provider=provider,
                       attempts=_spent(contract.JSON_OBJECT))
    lecture = _run(service)["lectures"][0]
    assert provider.calls == 1
    assert lecture["qa_status"] == COMPLETED
    assert lecture["generation_number"] == 1
    assert lecture["attempts_remaining"] == 2


def test_the_fresh_budget_is_still_three_and_then_stops():
    """A new contract is not a licence to spend without limit."""
    bad = good_output(ksbs_covered=[{"type": "K", "title": "t", "evidence_clips": []}])
    provider = ContractProvider(contract.STRICT_JSON_SCHEMA, bad)
    attempts = StubAttempts()
    evaluations = Evaluations()
    statuses = []
    for _ in range(4):
        service = _service(contract_version=contract.STRICT_JSON_SCHEMA,
                           provider=provider, attempts=attempts,
                           evaluations=evaluations)
        statuses.append(_run(service)["lectures"][0]["qa_status"])
    assert provider.calls == 3
    assert statuses[-1] == REVIEW_REQUIRED
    assert attempts.count(None, _fingerprint(contract.STRICT_JSON_SCHEMA)) == 3


def test_the_two_contracts_keep_completely_separate_histories():
    attempts = _spent(contract.JSON_OBJECT)
    provider = ContractProvider(contract.STRICT_JSON_SCHEMA, good_output())
    _run(_service(contract_version=contract.STRICT_JSON_SCHEMA, provider=provider,
                  attempts=attempts))
    assert attempts.count(None, _fingerprint(contract.JSON_OBJECT)) == 3
    assert attempts.count(None, _fingerprint(contract.STRICT_JSON_SCHEMA)) == 1


# --- 3. the local validator still runs under the strict contract ------------

def test_an_invalid_ksb_type_is_still_rejected_even_under_the_strict_contract():
    """
    Defence in depth. The provider should now make this impossible, but if it
    ever arrives the result is still INVALID_STRUCTURED_OUTPUT - never a
    silent rewrite to "Knowledge".
    """
    bad = good_output(ksbs_covered=[{"type": "K", "title": "t", "evidence_clips": []}])
    provider = ContractProvider(contract.STRICT_JSON_SCHEMA, bad)
    evaluations = Evaluations()
    service = _service(contract_version=contract.STRICT_JSON_SCHEMA, provider=provider,
                       attempts=StubAttempts(), evaluations=evaluations)
    lecture = _run(service)["lectures"][0]
    assert lecture["qa_status"] == INVALID_STRUCTURED_OUTPUT
    stored = list(evaluations.rows.values())[0]["evaluation"]
    assert stored["structured_output_error_count"] >= 1
    assert stored["ai_raw_output"]["ksbs_covered"][0]["type"] == "K"
    assert any(code.startswith("INVALID_KSB_TYPE")
               for code in validate_structured_output(bad))


def test_a_valid_response_under_the_strict_contract_completes_normally():
    provider = ContractProvider(contract.STRICT_JSON_SCHEMA, good_output(
        ksbs_covered=[{"type": "Behaviour", "title": "t", "evidence_clips": []}]))
    lecture = _run(_service(contract_version=contract.STRICT_JSON_SCHEMA,
                            provider=provider, attempts=StubAttempts()))["lectures"][0]
    assert lecture["qa_status"] == COMPLETED


# --- 4. the service and the provider cannot disagree about the contract ------

def test_a_provider_configured_for_a_different_contract_is_refused():
    """Two names for one contract is how provenance starts lying."""
    with pytest.raises(QaInputError):
        _service(contract_version=contract.STRICT_JSON_SCHEMA,
                 provider=ContractProvider(contract.JSON_OBJECT, good_output()),
                 attempts=StubAttempts())


def test_the_declared_contract_appears_in_the_version_set():
    service = _service(contract_version=contract.STRICT_JSON_SCHEMA,
                       provider=None, attempts=StubAttempts())
    assert service.versions["provider_contract_version"] == contract.STRICT_JSON_SCHEMA


def test_an_unknown_contract_cannot_be_declared():
    with pytest.raises(Exception):
        _service(contract_version="whatever_v1", provider=None,
                 attempts=StubAttempts())


# --- 5. the attempt aggregate is the generation count -----------------------

def test_the_stored_aggregate_is_the_generation_number_not_the_http_retry_count():
    """
    The exact defect: `provider_attempts` used to be `response["attempts"]`,
    the transport retry counter, which is 1 whenever the HTTP call succeeds
    first time - so three failed generations all stored 1.
    """
    bad = good_output(ksbs_covered=[{"type": "S", "title": "t", "evidence_clips": []}])
    provider = ContractProvider(contract.STRICT_JSON_SCHEMA, bad)
    attempts, evaluations = StubAttempts(), Evaluations()
    stored = []
    for _ in range(3):
        _run(_service(contract_version=contract.STRICT_JSON_SCHEMA, provider=provider,
                      attempts=attempts, evaluations=evaluations))
        stored.append(list(evaluations.rows.values())[0]["evaluation"]["provider_attempts"])
    # The provider reported attempts == 1 every single time.
    assert stored == [1, 2, 3]


def test_the_transport_retry_count_is_kept_but_kept_separate():
    provider = ContractProvider(contract.STRICT_JSON_SCHEMA, good_output())
    evaluations = Evaluations()
    _run(_service(contract_version=contract.STRICT_JSON_SCHEMA, provider=provider,
                  attempts=StubAttempts(), evaluations=evaluations))
    metadata = list(evaluations.rows.values())[0]["evaluation"]["metadata"]
    assert metadata["provider_transport_attempts"] == 1
    assert metadata["generation_number"] == 1
    assert metadata["max_model_generations"] == 3
    assert metadata["provider_contract_version"] == contract.STRICT_JSON_SCHEMA
    assert metadata["provider_enforced_schema"] is True


def test_a_provider_error_also_records_the_generation_number():
    provider = ContractProvider(
        contract.STRICT_JSON_SCHEMA,
        error=ProviderError("provider_http_error", "boom", http_status=500, attempts=3))
    evaluations = Evaluations()
    _run(_service(contract_version=contract.STRICT_JSON_SCHEMA, provider=provider,
                  attempts=StubAttempts(), evaluations=evaluations))
    evaluation = list(evaluations.rows.values())[0]["evaluation"]
    assert evaluation["provider_attempts"] == 1
    assert evaluation["metadata"]["provider_transport_attempts"] == 3


def test_a_second_identical_run_neither_calls_the_model_nor_adds_an_attempt():
    provider = ContractProvider(contract.STRICT_JSON_SCHEMA, good_output())
    attempts, evaluations = StubAttempts(), Evaluations()
    for _ in range(2):
        _run(_service(contract_version=contract.STRICT_JSON_SCHEMA, provider=provider,
                      attempts=attempts, evaluations=evaluations))
    assert provider.calls == 1
    assert len(attempts.rows) == 1
