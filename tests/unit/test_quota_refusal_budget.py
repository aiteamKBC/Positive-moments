"""
An account out of money is refused, not evaluated.

2026-10-12..14: the key's OpenAI project was over its spend limit and every
QA call came back 429 `project_spend_limit_exceeded` (type
`insufficient_quota`). Provider policy v1 counted each refusal as a
generation, and twelve of them exhausted four lectures no model ever saw.
"""
import pytest

from app.qa.generation_budget import (
    GENERATION_BUDGET_POLICY_VERSION,
    attempt_row,
    generation_budget,
)
from app.qa.provider import (
    GENERATION_FAILURE,
    PROVIDER_CONFIGURATION_ERROR,
    PROVIDER_CONFIGURATION_ERROR_CODE,
    OpenAIChatProvider,
    ProviderError,
    classify_provider_failure,
)

SPEND_LIMIT = "project_spend_limit_exceeded"
OLD_POLICY = "generation_budget_v2_configuration_failures_free"


# --- classification -----------------------------------------------------------

@pytest.mark.parametrize("code, kind", [
    (SPEND_LIMIT, "insufficient_quota"),      # exactly what production received
    ("insufficient_quota", None),
    (None, "insufficient_quota"),
    ("billing_hard_limit_reached", None),
])
def test_a_quota_429_is_an_account_refusal(code, kind):
    assert classify_provider_failure(http_status=429, provider_error_code=code,
                                     provider_error_type=kind) == PROVIDER_CONFIGURATION_ERROR


@pytest.mark.parametrize("code, kind", [
    ("rate_limit_exceeded", "requests"),
    (None, None),
])
def test_ordinary_rate_limiting_still_counts_as_a_failed_generation(code, kind):
    assert classify_provider_failure(http_status=429, provider_error_code=code,
                                     provider_error_type=kind) == GENERATION_FAILURE


def test_a_quota_code_on_another_status_is_not_trusted():
    assert classify_provider_failure(http_status=400, provider_error_code=SPEND_LIMIT
                                     ) == GENERATION_FAILURE


def test_the_refusal_gets_the_configuration_error_code():
    error = ProviderError("provider_http_error", "x", http_status=429,
                          provider_error_code=SPEND_LIMIT,
                          provider_error_type="insufficient_quota")
    assert error.is_configuration_failure
    assert error.code == PROVIDER_CONFIGURATION_ERROR_CODE


# --- no pointless retries inside one call -------------------------------------

class ScriptedProvider(OpenAIChatProvider):
    def __init__(self, error, **kwargs):
        super().__init__(api_key="k", model="gpt-5.2", sleep=lambda _: None, **kwargs)
        self.error = error
        self.posts = 0

    def _post(self, path, body):
        self.posts += 1
        raise ProviderError("provider_http_error", "refused", **self.error)


def test_a_quota_refusal_is_not_retried():
    provider = ScriptedProvider({"http_status": 429, "provider_error_code": SPEND_LIMIT,
                                 "provider_error_type": "insufficient_quota"})
    with pytest.raises(ProviderError) as raised:
        provider.complete_json(system_message="s", user_message="u")
    assert provider.posts == 1
    assert raised.value.is_configuration_failure


def test_ordinary_rate_limiting_is_still_retried():
    provider = ScriptedProvider({"http_status": 429,
                                 "provider_error_code": "rate_limit_exceeded"})
    with pytest.raises(ProviderError):
        provider.complete_json(system_message="s", user_message="u")
    assert provider.posts == provider.max_tries


# --- the budget, read from what production actually stored ------------------

def refused(total, consumes="true", policy=None):
    """An attempt row as recorded on 2026-10-12..14 under provider policy v1."""
    return attempt_row("MODEL_ERROR", "provider_http_error", consumes, str(total),
                       "429", SPEND_LIMIT, "insufficient_quota", policy)


def real_failure(total, policy=None):
    return attempt_row("MODEL_ERROR", "provider_http_error", "true", str(total),
                       "500", None, None, policy)


def test_the_four_stranded_lectures_get_their_three_attempts_back():
    budget = generation_budget([refused(1), refused(2), refused(3)], max_generations=3)
    assert budget["generation_budget_used"] == 0
    assert budget["attempts_remaining"] == 3
    assert not budget["exhausted"]
    assert budget["configuration_failures"] == 3
    assert budget["last_attempt_configuration_failure"]
    assert budget["generation_budget_policy_version"] == GENERATION_BUDGET_POLICY_VERSION


def test_real_failures_between_refusals_still_count():
    budget = generation_budget([real_failure(1), refused(2), refused(3)], max_generations=3)
    assert budget["generation_budget_used"] == 1
    assert budget["attempts_remaining"] == 2


def test_three_real_failures_are_still_exhausted():
    budget = generation_budget([real_failure(1), real_failure(2), real_failure(3)],
                               max_generations=3)
    assert budget["exhausted"]


def test_a_total_written_under_v3_is_not_corrected_twice():
    # 500, then a refusal counted under v1 (total 2), then the first call
    # under v3: a real failure whose total (2) already excludes the refusal.
    rows = [real_failure(1), refused(2),
            real_failure(2, policy=GENERATION_BUDGET_POLICY_VERSION)]
    budget = generation_budget(rows, max_generations=3)
    assert budget["generation_budget_used"] == 2
    assert budget["attempts_remaining"] == 1


def test_a_refusal_recorded_as_free_is_not_subtracted_again():
    # Written by v3 code: consumes false, total unchanged.
    rows = [real_failure(1), refused(1, consumes="false",
                                     policy=GENERATION_BUDGET_POLICY_VERSION)]
    assert generation_budget(rows, max_generations=3)["generation_budget_used"] == 1


def test_a_pre_v3_total_from_policy_v2_is_corrected():
    rows = [refused(1, policy=OLD_POLICY), refused(2, policy=OLD_POLICY)]
    assert generation_budget(rows, max_generations=3)["generation_budget_used"] == 0


def test_rows_without_evidence_are_read_exactly_as_before():
    rows = [attempt_row("MODEL_ERROR", "provider_http_error", "true", "1"),
            attempt_row("MODEL_ERROR", "provider_http_error", "true", "2")]
    assert generation_budget(rows, max_generations=3)["generation_budget_used"] == 2
