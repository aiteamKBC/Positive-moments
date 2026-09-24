"""
A refused provider credential is not a generation.

On 2026-09-23 the provider answered HTTP 401 to ten calls in two scheduled
cycles: five lectures, one call each per cycle, because each lecture learned
separately that the key was wrong. Every one of those calls was charged to its
lecture's three-generation budget, so a third cycle with the same key would
have pushed all five into manual review without the model ever being asked.

Three things are proved here:

  * classification - 401, and a 403 whose STRUCTURED provider fields say
    authentication/authorization, are PROVIDER_CONFIGURATION_ERROR; any other
    403, a 5xx, and every answer the model actually gave keep their old
    meaning and keep spending the budget;
  * accounting - the refusal is recorded (attempt row, HTTP status, audit),
    spends nothing, and the two historical 401 rows of the 2026-09-23 shape
    are read the same way without being touched;
  * the circuit - one refusal per cycle, then zero calls for everybody else in
    that cycle, through the SAME orchestrator path the scheduler and the
    backfill both use, and a fresh circuit on the next cycle.

The whole chain is real from the orchestrator down to the QA service: only
the provider class, the settings and the repositories are stand-ins.
"""
import io
import json
import urllib.error
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.orchestration import runner as runner_module
from app.orchestration.orchestrator import PipelineOrchestrator
from app.orchestration.runner import StageRunner
from app.orchestration.scheduler import SchedulerConfig, SchedulerService
from app.orchestration.stages import (
    ITEM_BLOCKED,
    MANUAL_REVIEW_REQUIRED,
    NOTHING_TO_DO,
    PERFECT_ELIGIBILITY,
    QA_EVALUATION,
    QA_RENDER,
    REVIEW_REQUIRED,
    RUN_QA,
    RUN_TYPE_BACKFILL,
    RUN_TYPE_SCHEDULED,
    SYNC_LEGACY_QA,
    SYNC_PERFECT,
)
from app.qa import provider as provider_module
from app.qa.generation_budget import attempt_row, generation_budget
from app.qa.provider import (
    GENERATION_FAILURE,
    PROVIDER_CONFIGURATION_BLOCKED,
    PROVIDER_CONFIGURATION_ERROR,
    OpenAIChatProvider,
    ProviderError,
    classify_provider_failure,
)
from app.qa.service import (
    COMPLETED,
    INVALID_EVIDENCE,
    INVALID_STRUCTURED_OUTPUT,
    MAX_GENERATIONS_EXHAUSTED,
    MODEL_ERROR,
    ShadowQaService,
)
from tests.unit.test_generation_contract_budget import Evaluations
from tests.unit.test_shadow_qa import (
    StubInputs,
    StubProvider,
    StubRuns,
    good_output,
    package,
)


DAY = date(2026, 9, 23)
LECTURES = [uuid.UUID(int=value) for value in (101, 102, 103, 104, 105)]


def refused(status=401, code="invalid_api_key", kind="invalid_request_error"):
    return ProviderError("provider_http_error", f"QA model call failed with HTTP {status}",
                         http_status=status, provider_error_code=code,
                         provider_error_type=kind)


def invalid_structured():
    output = good_output()
    output["checklist_evaluation"] = output["checklist_evaluation"][:5]
    return output


def fabricated_evidence():
    output = good_output()
    output["checklist_evaluation"][3]["evidence_clips"] = [
        {"start": "09:59:00.000", "end": "09:59:30.000"}]
    return output


# --- the in-memory world ----------------------------------------------------

class Ledger:
    """
    `lecture_qa_generation_attempts`, in memory, read through the REAL shared
    budget policy - the same function the SQL repository and the recovery
    state call.
    """

    def __init__(self, evaluations):
        self.rows = []
        self.evaluations = evaluations

    def count(self, connection, source_fingerprint):
        return sum(1 for row in self.rows
                   if row["source_fingerprint"] == source_fingerprint)

    def record(self, connection, entry):
        number = self.count(connection, entry["source_fingerprint"]) + 1
        self.rows.append({**entry, "generation_number": number})
        return number

    def budget(self, connection, source_fingerprint, *, max_generations):
        rows = [attempt_row(row["outcome"], row.get("error_code"),
                            (row.get("metadata") or {}).get("consumes_generation_budget"),
                            (row.get("metadata") or {}).get("generation_budget_used"))
                for row in self.rows if row["source_fingerprint"] == source_fingerprint]
        stored = self.evaluations.find_by_fingerprint(connection, source_fingerprint)
        evidence = None
        if stored:
            evaluation = stored["evaluation"]
            evidence = {"error_code": evaluation.get("error_code"),
                        "http_status": (evaluation.get("metadata") or {}).get("http_status"),
                        "has_model_output": evaluation.get("ai_raw_output") is not None}
        return generation_budget(rows, evidence, max_generations=max_generations)

    def of(self, fingerprint):
        return [row for row in self.rows if row["source_fingerprint"] == fingerprint]


def qa_service(*, provider, packages, evaluations, ledger, lecture_ids=None):
    return ShadowQaService(
        input_repository=StubInputs(packages), evaluation_repository=evaluations,
        run_repository=StubRuns(), provider=provider, attempt_repository=ledger,
        resolver_version="r1", role_algorithm_version="o1",
        engagement_algorithm_version="e1", model_name="gpt-5.2",
        lecture_ids=lecture_ids)


def packages_for(lecture_ids):
    return [package(lecture_id=lecture_id, session_date=DAY.isoformat())
            for lecture_id in lecture_ids]


def fingerprint_of(lecture_id):
    """The fingerprint the SERVICE computes, from a no-cost preview."""
    preview = qa_service(provider=None, packages=packages_for([lecture_id]),
                         evaluations=Evaluations(), ledger=None).run_day(
        None, DAY, execute=False)
    return preview["lectures"][0]["source_fingerprint"]


def seed_historical_401s(evaluations, ledger, lecture_id, count=2):
    """
    The exact 2026-09-23 shape, as the OLD code wrote it: MODEL_ERROR attempt
    rows carrying only `provider_http_error` and a generation number, and one
    evaluation overwritten by each attempt, last recording HTTP 401.
    """
    fingerprint = fingerprint_of(lecture_id)
    for number in range(1, count + 1):
        ledger.rows.append({
            "lecture_id": lecture_id, "source_fingerprint": fingerprint,
            "outcome": MODEL_ERROR, "error_code": "provider_http_error",
            "generation_number": number, "forced": False,
            "metadata": {"generation_number": number}})
    evaluations.rows[fingerprint] = {
        "evaluation_id": uuid.uuid4(), "qa_status": MODEL_ERROR, "ai_called": True,
        "evaluation": {"qa_status": MODEL_ERROR, "error_code": "provider_http_error",
                       "review_reason": "PROVIDER_ERROR", "ai_raw_output": None,
                       "provider_attempts": count,
                       "metadata": {"generation_number": count, "http_status": 401,
                                    "provider_error_code": "provider_http_error",
                                    "provider_transport_attempts": 1}},
        "checklist": [], "clips": []}
    return fingerprint


# --- 1. HTTP 401 --------------------------------------------------------------

def test_1_a_401_is_classified_as_a_configuration_failure():
    assert classify_provider_failure(http_status=401) == PROVIDER_CONFIGURATION_ERROR
    error = refused(401)
    assert error.is_configuration_failure
    assert error.code == "provider_configuration_error"
    assert error.diagnostics()["http_status"] == 401


def test_1_a_401_is_recorded_spends_no_budget_and_opens_the_circuit():
    evaluations = Evaluations()
    ledger = Ledger(evaluations)
    provider = StubProvider(error=refused(401))
    summary = qa_service(provider=provider, packages=packages_for(LECTURES[:1]),
                         evaluations=evaluations, ledger=ledger).run_day(
        None, DAY, execute=True)
    (lecture,) = summary["lectures"]
    assert provider.calls == 1
    assert lecture["qa_status"] == MODEL_ERROR
    assert lecture["review_reason"] == PROVIDER_CONFIGURATION_ERROR
    assert lecture["http_status"] == 401
    assert lecture["generation_budget_used"] == 0
    assert lecture["attempts_remaining"] == 3

    (row,) = ledger.rows
    assert row["outcome"] == MODEL_ERROR
    assert row["error_code"] == "provider_configuration_error"
    assert row["metadata"]["consumes_generation_budget"] is False
    assert row["metadata"]["http_status"] == 401
    assert row["metadata"]["failure_class"] == PROVIDER_CONFIGURATION_ERROR
    assert row["metadata"]["provider_error_code"] == "invalid_api_key"

    stored = evaluations.find_by_fingerprint(None, lecture["source_fingerprint"])
    assert stored["evaluation"]["metadata"]["http_status"] == 401
    assert stored["evaluation"]["review_reason"] == PROVIDER_CONFIGURATION_ERROR
    assert summary["provider_circuit"]["state"] == "OPEN"
    assert summary["provider_circuit"]["opened_by"]["http_status"] == 401
    assert summary["provider_configuration_failures"] == 1


def test_1_the_401_is_parsed_from_structured_fields_and_never_echoes_the_key(monkeypatch):
    body = json.dumps({"error": {
        "message": "Incorrect API key provided: sk-test-SECRETVALUE",
        "type": "invalid_request_error", "code": "invalid_api_key"}}).encode()
    sleeps = []

    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {},
                                     io.BytesIO(body))

    monkeypatch.setattr(provider_module.urllib.request, "urlopen", urlopen)
    client = OpenAIChatProvider(api_key="sk-test-SECRETVALUE", model="gpt-5.2",
                                sleep=sleeps.append)
    with pytest.raises(ProviderError) as caught:
        client.complete_json(system_message="s", user_message="u")
    error = caught.value
    assert error.is_configuration_failure
    assert (error.http_status, error.provider_error_code,
            error.provider_error_type) == (401, "invalid_api_key", "invalid_request_error")
    assert error.attempts == 1 and sleeps == []          # never retried
    assert "SECRET" not in str(error)
    assert "SECRET" not in json.dumps(error.diagnostics())


# --- 2. the rest of the run gets nothing -------------------------------------

def test_2_other_lectures_in_the_same_run_receive_zero_calls_and_zero_attempts():
    evaluations = Evaluations()
    ledger = Ledger(evaluations)
    provider = StubProvider(error=refused(401))
    summary = qa_service(provider=provider, packages=packages_for(LECTURES),
                         evaluations=evaluations, ledger=ledger).run_day(
        None, DAY, execute=True)
    assert provider.calls == 1
    assert len(ledger.rows) == 1
    blocked = [row for row in summary["lectures"]
               if row["qa_status"] == PROVIDER_CONFIGURATION_BLOCKED]
    assert len(blocked) == 4
    for row in blocked:
        assert row["provider_calls"] == 0 and row["persisted"] is False
        assert row["attempts_remaining"] == 3
        # Nothing written: the lecture is exactly as retryable as before.
        assert evaluations.find_by_fingerprint(None, row["source_fingerprint"]) is None
    assert summary["provider_calls"] == 1
    assert summary["provider_circuit"]["blocked_lecture_ids"] == [
        row["lecture_id"] for row in blocked]


# --- 3/4. HTTP 403 --------------------------------------------------------------

@pytest.mark.parametrize("code,kind", [
    ("invalid_api_key", None),
    ("insufficient_permissions", "invalid_request_error"),
    (None, "authentication_error"),
    (None, "permission_error"),
])
def test_3_a_403_the_provider_marks_as_an_auth_rejection_is_a_configuration_failure(
        code, kind):
    assert classify_provider_failure(http_status=403, provider_error_code=code,
                                     provider_error_type=kind) \
        == PROVIDER_CONFIGURATION_ERROR
    evaluations = Evaluations()
    ledger = Ledger(evaluations)
    provider = StubProvider(error=refused(403, code, kind))
    summary = qa_service(provider=provider, packages=packages_for(LECTURES[:3]),
                         evaluations=evaluations, ledger=ledger).run_day(
        None, DAY, execute=True)
    assert provider.calls == 1
    assert [row["metadata"]["consumes_generation_budget"] for row in ledger.rows] == [False]
    assert summary["provider_configuration_blocked_count"] == 2


@pytest.mark.parametrize("code,kind", [
    (None, None),
    ("unsupported_country_region_territory", "request_forbidden"),
    ("model_not_found", "invalid_request_error"),
])
def test_4_any_other_403_is_not_silently_treated_as_a_credential_failure(code, kind):
    assert classify_provider_failure(http_status=403, provider_error_code=code,
                                     provider_error_type=kind) == GENERATION_FAILURE
    evaluations = Evaluations()
    ledger = Ledger(evaluations)
    provider = StubProvider(error=refused(403, code, kind))
    summary = qa_service(provider=provider, packages=packages_for(LECTURES[:3]),
                         evaluations=evaluations, ledger=ledger).run_day(
        None, DAY, execute=True)
    # Each lecture is still tried, and each try spends budget, as before.
    assert provider.calls == 3
    assert [row["error_code"] for row in ledger.rows] == ["provider_http_error"] * 3
    assert all(row["metadata"]["consumes_generation_budget"] for row in ledger.rows)
    assert summary["provider_circuit"]["state"] == "CLOSED"
    assert all(row["review_reason"] == "PROVIDER_ERROR" for row in summary["lectures"])


def test_4_free_text_is_never_read_so_a_message_cannot_classify_a_403():
    error = ProviderError("provider_http_error",
                          "invalid_api_key authentication_error permission_error",
                          http_status=403)
    assert error.failure_class == GENERATION_FAILURE


def test_4_an_unstructured_code_is_dropped_rather_than_trusted():
    error = refused(403, code="invalid api key: sk-SECRET", kind="x" * 200)
    assert error.provider_error_code is None and error.provider_error_type is None
    assert error.failure_class == GENERATION_FAILURE


# --- 5. HTTP 500 ----------------------------------------------------------------

def test_5_a_500_keeps_the_existing_transport_retry(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(request.full_url, 500, "Server Error", {},
                                     io.BytesIO(b"{}"))

    monkeypatch.setattr(provider_module.urllib.request, "urlopen", urlopen)
    sleeps = []
    client = OpenAIChatProvider(api_key="k", model="gpt-5.2", sleep=sleeps.append)
    with pytest.raises(ProviderError) as caught:
        client.complete_json(system_message="s", user_message="u")
    assert len(calls) == client.max_tries and len(sleeps) == client.max_tries - 1
    assert caught.value.failure_class == GENERATION_FAILURE
    assert caught.value.code == "provider_http_error"


def test_5_a_500_still_spends_a_generation_and_does_not_open_the_circuit():
    evaluations = Evaluations()
    ledger = Ledger(evaluations)
    provider = StubProvider(error=refused(500, code=None, kind="server_error"))
    summary = qa_service(provider=provider, packages=packages_for(LECTURES[:2]),
                         evaluations=evaluations, ledger=ledger).run_day(
        None, DAY, execute=True)
    assert provider.calls == 2
    assert all(row["metadata"]["consumes_generation_budget"] for row in ledger.rows)
    assert all(row["metadata"]["http_status"] == 500 for row in ledger.rows)
    assert all(row["generation_budget_used"] == 1 for row in summary["lectures"])
    assert summary["provider_circuit"]["state"] == "CLOSED"


# --- 6/7. genuine generations -------------------------------------------------

def test_6_invalid_structured_output_still_spends_a_real_generation():
    evaluations = Evaluations()
    ledger = Ledger(evaluations)
    summary = qa_service(provider=StubProvider(invalid_structured()),
                         packages=packages_for(LECTURES[:1]),
                         evaluations=evaluations, ledger=ledger).run_day(
        None, DAY, execute=True)
    (lecture,) = summary["lectures"]
    assert lecture["qa_status"] == INVALID_STRUCTURED_OUTPUT
    (row,) = ledger.rows
    assert row["metadata"]["consumes_generation_budget"] is True
    assert "failure_class" not in row["metadata"]
    assert lecture["generation_budget_used"] == 1


def test_7_invalid_evidence_keeps_its_existing_attempt_semantics():
    evaluations = Evaluations()
    ledger = Ledger(evaluations)
    fingerprint = fingerprint_of(LECTURES[0])
    ledger.rows.extend({"source_fingerprint": fingerprint, "outcome": INVALID_EVIDENCE,
                        "error_code": None, "metadata": {"generation_number": number}}
                       for number in (1, 2))
    provider = StubProvider(fabricated_evidence())
    summary = qa_service(provider=provider, packages=packages_for(LECTURES[:1]),
                         evaluations=evaluations, ledger=ledger).run_day(
        None, DAY, execute=True)
    (lecture,) = summary["lectures"]
    # Third real generation: the budget closes exactly as it always did.
    assert provider.calls == 1
    assert ledger.rows[-1]["outcome"] == INVALID_EVIDENCE
    assert lecture["review_reason"] == MAX_GENERATIONS_EXHAUSTED


# --- 8. the 2026-09-23 history --------------------------------------------------

def test_8_two_historical_401s_are_read_as_zero_budget_without_being_touched():
    evaluations = Evaluations()
    ledger = Ledger(evaluations)
    fingerprint = seed_historical_401s(evaluations, ledger, LECTURES[0])
    before = json.dumps(ledger.of(fingerprint), default=str, sort_keys=True)

    budget = ledger.budget(None, fingerprint, max_generations=3)
    assert budget["attempt_records"] == 2
    assert budget["generation_budget_used"] == 0
    assert budget["legacy_configuration_failures_inferred"] == 2
    assert budget["attempts_remaining"] == 3 and not budget["exhausted"]
    assert json.dumps(ledger.of(fingerprint), default=str, sort_keys=True) == before


def test_8_the_old_rule_would_have_exhausted_the_same_lecture_on_the_third_401():
    """The defect, pinned: a ledger that can only count rows spends every 401."""

    class CountOnly(Ledger):
        budget = None

    evaluations = Evaluations()
    ledger = CountOnly(evaluations)
    seed_historical_401s(evaluations, ledger, LECTURES[0])
    service = qa_service(provider=StubProvider(error=refused(401)),
                         packages=packages_for(LECTURES[:1]),
                         evaluations=evaluations, ledger=ledger)
    service.attempt_repository.budget = None
    (lecture,) = service.run_day(None, DAY, execute=True)["lectures"]
    assert lecture["generation_budget_used"] == 2       # counted as if it were real


def test_8_two_historical_401s_plus_another_401_is_still_not_exhausted():
    evaluations = Evaluations()
    ledger = Ledger(evaluations)
    fingerprint = seed_historical_401s(evaluations, ledger, LECTURES[0])
    history = json.dumps(ledger.of(fingerprint)[:2], default=str, sort_keys=True)
    provider = StubProvider(error=refused(401))
    (lecture,) = qa_service(provider=provider, packages=packages_for(LECTURES[:1]),
                            evaluations=evaluations, ledger=ledger).run_day(
        None, DAY, execute=True)["lectures"]
    assert provider.calls == 1
    assert lecture["qa_status"] == MODEL_ERROR
    assert lecture["review_reason"] == PROVIDER_CONFIGURATION_ERROR
    assert lecture["generation_budget_used"] == 0
    assert lecture["attempts_remaining"] == 3
    rows = ledger.of(fingerprint)
    assert [row["generation_number"] for row in rows] == [1, 2, 3]
    # The two historical rows are byte-for-byte what they were.
    assert json.dumps(rows[:2], default=str, sort_keys=True) == history
    assert ledger.budget(None, fingerprint, max_generations=3)["generation_budget_used"] == 0


def test_8_the_historical_lecture_retries_normally_once_the_key_works():
    evaluations = Evaluations()
    ledger = Ledger(evaluations)
    fingerprint = seed_historical_401s(evaluations, ledger, LECTURES[0])
    provider = StubProvider(good_output())
    (lecture,) = qa_service(provider=provider, packages=packages_for(LECTURES[:1]),
                            evaluations=evaluations, ledger=ledger).run_day(
        None, DAY, execute=True)["lectures"]
    assert provider.calls == 1
    assert lecture["qa_status"] == COMPLETED
    assert ledger.budget(None, fingerprint, max_generations=3)[
        "generation_budget_used"] == 1


def test_8_a_legacy_transport_failure_before_a_real_answer_still_counts():
    """The inference only reaches back through an UNBROKEN run of refusals."""
    budget = generation_budget(
        [{"outcome": MODEL_ERROR, "error_code": "provider_http_error",
          "consumes_generation_budget": None},
         {"outcome": INVALID_STRUCTURED_OUTPUT, "error_code": None,
          "consumes_generation_budget": None},
         {"outcome": MODEL_ERROR, "error_code": "provider_http_error",
          "consumes_generation_budget": None}],
        {"error_code": "provider_http_error", "http_status": "401",
         "has_model_output": False}, max_generations=3)
    assert budget["generation_budget_used"] == 2
    assert budget["legacy_configuration_failures_inferred"] == 1


def test_8_legacy_transport_failures_without_401_evidence_keep_counting():
    rows = [{"outcome": MODEL_ERROR, "error_code": "provider_http_error",
             "consumes_generation_budget": None}] * 2
    for evidence in (None,
                     {"error_code": "provider_http_error", "http_status": "500",
                      "has_model_output": False},
                     {"error_code": "provider_http_error", "http_status": "403",
                      "has_model_output": False}):
        assert generation_budget(rows, evidence, max_generations=3)[
            "generation_budget_used"] == 2


def test_8_the_resolver_hands_the_2026_09_23_shape_back_as_retryable_work():
    from test_pipeline_state import FINGERPRINT, complete_rows, resolve

    from tests.unit.test_qa_document_lineage import evaluation_row

    rows = complete_rows(
        evaluations=[evaluation_row(status=MODEL_ERROR, review="PROVIDER_ERROR")],
        attempts=[(FINGERPRINT, 2, 2, datetime(2026, 9, 23, 18, tzinfo=timezone.utc),
                   datetime(2026, 9, 23, 20, tzinfo=timezone.utc),
                   [MODEL_ERROR] * 2, ["provider_http_error"] * 2, [""] * 2, [""] * 2,
                   {"error_code": "provider_http_error", "http_status": "401",
                    "has_model_output": False})],
        rendered=[], qa_writes=[], legacy_writes_state=[], perfect_results=[],
        perfect_writes=[], perfect_writes_state=[])
    state = resolve(rows)
    qa = state["stages"][QA_EVALUATION]
    assert qa["state"] == REVIEW_REQUIRED and qa["action"] == RUN_QA
    assert qa["reason"] == PROVIDER_CONFIGURATION_ERROR
    assert qa["attempts_used"] == 0 and qa["attempts_remaining"] == 3
    assert qa["attempt_records"] == 2 and qa["configuration_failures"] == 2
    assert state["next_executable_action"] == RUN_QA
    # 12/13: no render, so neither the legacy writer nor Perfect has anything.
    assert state["stages"][QA_RENDER]["action"] != SYNC_LEGACY_QA
    assert state["stages"][PERFECT_ELIGIBILITY]["action"] != SYNC_PERFECT


def test_8_three_real_failures_are_still_exhausted_in_the_resolver():
    from test_pipeline_state import FINGERPRINT, complete_rows, resolve

    from tests.unit.test_qa_document_lineage import evaluation_row

    rows = complete_rows(
        evaluations=[evaluation_row(status=MODEL_ERROR, review="PROVIDER_ERROR")],
        attempts=[(FINGERPRINT, 3, 3, datetime(2026, 9, 23, tzinfo=timezone.utc),
                   datetime(2026, 9, 23, tzinfo=timezone.utc),
                   [MODEL_ERROR] * 3, ["provider_http_error"] * 3, [""] * 3, [""] * 3,
                   {"error_code": "provider_http_error", "http_status": "500",
                    "has_model_output": False})],
        rendered=[], qa_writes=[], legacy_writes_state=[])
    qa = resolve(rows)["stages"][QA_EVALUATION]
    assert qa["action"] == MANUAL_REVIEW_REQUIRED
    assert qa["reason"] == "GENERATION_BUDGET_EXHAUSTED"


# --- 9-13. the shared orchestrator path ---------------------------------------

class World:
    """Five 2026-09-23 lectures, their evaluations, their ledger, one key."""

    def __init__(self, *, historical_401s=True):
        self.evaluations = Evaluations()
        self.ledger = Ledger(self.evaluations)
        self.packages = packages_for(LECTURES)
        self.fingerprints = {str(lecture_id): fingerprint_of(lecture_id)
                             for lecture_id in LECTURES}
        if historical_401s:
            for lecture_id in LECTURES:
                seed_historical_401s(self.evaluations, self.ledger, lecture_id)
        self.key = "rotated-away"
        self.calls = []

    def provider_class(self):
        world = self

        class Provider:
            response_contract = None

            def __init__(self, *, api_key, model, base_url=None, **_kwargs):
                self.api_key = api_key

            def complete_json(self, *, system_message, user_message):
                world.calls.append(self.api_key)
                if self.api_key != "valid":
                    raise refused(401)
                return {"output": good_output(), "provider": "openai",
                        "model_reported": "gpt-5.2", "response_id": "r", "usage": {},
                        "attempts": 1}

        return Provider

    def status(self, lecture_id):
        stored = self.evaluations.find_by_fingerprint(None, self.fingerprints[lecture_id])
        return stored["qa_status"] if stored else None

    def budget(self, lecture_id):
        return self.ledger.budget(None, self.fingerprints[lecture_id], max_generations=3)


class WorldResolver:
    """Next action from the world, through the same budget rule."""

    def __init__(self, world):
        self.world = world

    def for_day(self, connection, day):
        if day != DAY:
            return []
        states = []
        for lecture_id in (str(value) for value in LECTURES):
            if self.world.status(lecture_id) == COMPLETED:
                action = NOTHING_TO_DO
            elif self.world.budget(lecture_id)["exhausted"]:
                action = MANUAL_REVIEW_REQUIRED
            else:
                action = RUN_QA
            states.append({
                "lecture_id": lecture_id, "subject": "Lecture", "session_date": DAY.isoformat(),
                "next_executable_action": action,
                "blocking_stage": None if action == NOTHING_TO_DO else QA_EVALUATION,
                "stages": {"ATTENDANCE": {"state": "COMPLETE"}},
                "operator_actions": [], "requires_review": action != NOTHING_TO_DO,
                "is_waiting": False})
        return states


class Settings:
    def __init__(self, world):
        self.world = world
        self.qa_model_name = "gpt-5.2"
        self.qa_model_base_url = "https://provider.invalid/v1"

    @property
    def qa_model_api_key(self):
        return self.world.key

    def require_qa_model(self):
        return None


class WorldStageRunner(StageRunner):
    """The REAL StageRunner._run_qa; only the QA service's storage is in memory."""

    def __init__(self, world):
        super().__init__(settings=Settings(world), allow_graph=False)
        self.world = world
        self.actions = []

    def execute(self, connection, action, *, session_date, lecture_id=None):
        self.actions.append(action)
        return super().execute(connection, action, session_date=session_date,
                               lecture_id=lecture_id)

    def _qa_service(self, *, provider, lecture_ids=None):
        return qa_service(provider=provider, packages=self.world.packages,
                          evaluations=self.world.evaluations, ledger=self.world.ledger,
                          lecture_ids=lecture_ids)


class StubPreflight:
    def check(self):
        return {"status": "LEGACY_QA_DISABLED", "writes_permitted": True}


@pytest.fixture
def world(monkeypatch):
    world = World()
    monkeypatch.setattr(runner_module, "OpenAIChatProvider", world.provider_class())
    return world


def orchestrator_for(world):
    runner = WorldStageRunner(world)
    return PipelineOrchestrator(resolver=WorldResolver(world), runner=runner,
                                preflight=StubPreflight()), runner


def test_9_one_refusal_per_cycle_then_zero_calls_for_everyone_else(world):
    orchestrator, runner = orchestrator_for(world)
    summary = orchestrator.run_window(None, DAY, run_type=RUN_TYPE_SCHEDULED)
    assert len(world.calls) == 1
    assert summary["provider_calls"] == 1
    assert summary["provider_circuit"]["state"] == "OPEN"
    assert summary["provider_circuit"]["opened_by"]["http_status"] == 401
    by_status = {}
    for item in summary["lectures"]:
        by_status.setdefault(item["status"], []).append(item)
    blocked = by_status[ITEM_BLOCKED]
    assert len(blocked) == 4
    assert all(item["error_code"] == PROVIDER_CONFIGURATION_BLOCKED
               and item["retryable"] and item["provider_calls"] == 0
               for item in blocked)
    assert sorted(summary["provider_circuit"]["blocked_lecture_ids"]) == sorted(
        item["lecture_id"] for item in blocked)
    (refusing,) = [item for item in summary["lectures"]
                   if item["error_code"] == PROVIDER_CONFIGURATION_ERROR]
    assert refusing["retryable"] and refusing["provider_calls"] == 1
    # Budgets: nobody spent anything, including the lecture that was refused.
    assert all(world.budget(str(lecture_id))["generation_budget_used"] == 0
               for lecture_id in LECTURES)
    assert len(world.ledger.rows) == 5 * 2 + 1
    # 12/13: nothing reached rendering, the legacy writer or Perfect.
    assert set(runner.actions) == {RUN_QA}
    assert summary["legacy_rows_written"] == 0


def test_9_the_circuit_resets_and_the_next_healthy_cycle_completes_everything(world):
    orchestrator, _runner = orchestrator_for(world)
    orchestrator.run_window(None, DAY, run_type=RUN_TYPE_SCHEDULED)
    # Third cycle with the bad key: under the old rule this one exhausted all
    # five lectures. Now it costs one refused call and nothing else.
    orchestrator.run_window(None, DAY, run_type=RUN_TYPE_SCHEDULED)
    assert len(world.calls) == 2
    assert all(not world.budget(str(lecture_id))["exhausted"] for lecture_id in LECTURES)

    world.key = "valid"
    healthy = orchestrator.run_window(None, DAY, run_type=RUN_TYPE_SCHEDULED)
    assert healthy["provider_circuit"]["state"] == "CLOSED"
    assert healthy["provider_calls"] == 5
    assert all(world.status(str(lecture_id)) == COMPLETED for lecture_id in LECTURES)
    assert all(world.budget(str(lecture_id))["generation_budget_used"] == 1
               for lecture_id in LECTURES)
    assert all(item["final_action"] == NOTHING_TO_DO for item in healthy["lectures"])


def test_9_a_missing_key_opens_the_circuit_without_any_call(world):
    world.key = ""

    class Unconfigured(Settings):
        def require_qa_model(self):
            raise ValueError("QA_MODEL_API_KEY is required")

    orchestrator, runner = orchestrator_for(world)
    runner.settings = Unconfigured(world)
    summary = orchestrator.run_window(None, DAY, run_type=RUN_TYPE_SCHEDULED)
    assert world.calls == []
    assert summary["provider_circuit"]["state"] == "OPEN"
    codes = [item["error_code"] for item in summary["lectures"]]
    assert codes.count("provider_configuration_error") == 1
    assert codes.count(PROVIDER_CONFIGURATION_BLOCKED) == 4


def test_10_the_scheduler_gets_the_circuit_from_the_shared_orchestrator(world):
    orchestrator, _runner = orchestrator_for(world)
    scheduler = SchedulerService(orchestrator=orchestrator,
                                 config=SchedulerConfig(enabled=True, lookback_days=3))
    now = datetime(2026, 9, 24, 18, tzinfo=timezone.utc)
    first = scheduler.run_cycle(None, now=now)
    assert first["cycle_target_date"] == "2026-09-24"
    assert DAY.isoformat() in first["window_days"]
    assert len(world.calls) == 1 and first["provider_circuit"]["state"] == "OPEN"

    world.key = "valid"
    second = scheduler.run_cycle(None, now=now + timedelta(hours=2))
    assert second["provider_circuit"]["state"] == "CLOSED"
    assert all(world.status(str(lecture_id)) == COMPLETED for lecture_id in LECTURES)


def test_11_the_backfill_gets_the_same_circuit_from_the_same_orchestrator(world):
    from tests.unit.test_backfill import FakeConnection, FakeRepository, a_run

    from app.orchestration.backfill import BackfillRunner

    orchestrator, _runner = orchestrator_for(world)
    repository = FakeRepository(a_run(requested_from=DAY, requested_to=DAY))
    backfill = BackfillRunner(orchestrator=orchestrator, connection_factory=FakeConnection,
                              readonly_connection_factory=FakeConnection,
                              repository=repository)
    result = backfill.run_claimed("bf-1")
    assert result["outcome"] == "COMPLETED"
    assert len(world.calls) == 1
    (day,) = repository.days
    assert day["provider_calls"] == 1
    assert all(world.budget(str(lecture_id))["generation_budget_used"] == 0
               for lecture_id in LECTURES)

    world.key = "valid"
    BackfillRunner(orchestrator=orchestrator, connection_factory=FakeConnection,
                   readonly_connection_factory=FakeConnection,
                   repository=FakeRepository(a_run(requested_from=DAY, requested_to=DAY))
                   ).run_claimed("bf-2")
    assert all(world.status(str(lecture_id)) == COMPLETED for lecture_id in LECTURES)


def test_10_11_neither_entry_point_holds_a_private_provider_rule():
    import inspect

    from app.orchestration import backfill, scheduler
    for module in (backfill, scheduler):
        source = inspect.getsource(module)
        for private in ("ProviderCircuit", "PROVIDER_CONFIGURATION",
                        "classify_provider_failure", "generation_budget"):
            assert private not in source, (module.__name__, private)
        assert "run_window" in source


# --- 12/13. writer and Perfect are untouched ----------------------------------

def test_12_13_no_legacy_writer_or_perfect_module_knows_about_the_provider():
    import inspect

    from app.qa import perfect
    from app.writer import service as writer_service
    for module in (writer_service, perfect):
        source = inspect.getsource(module)
        for name in ("ProviderCircuit", "PROVIDER_CONFIGURATION", "generation_budget",
                     "consumes_generation_budget"):
            assert name not in source, (module.__name__, name)


def test_12_a_blocked_or_refused_lecture_never_writes_anything_downstream(world):
    orchestrator, runner = orchestrator_for(world)
    summary = orchestrator.run_window(None, DAY, run_type=RUN_TYPE_BACKFILL)
    assert set(runner.actions) == {RUN_QA}
    assert summary["legacy_rows_written"] == 0
    assert not any(evaluation["qa_status"] == COMPLETED
                   for evaluation in world.evaluations.rows.values())
