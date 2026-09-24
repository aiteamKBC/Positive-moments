"""
How much of a QA fingerprint's bounded generation budget has been spent.

The attempts table is append-only and records every provider call, including
the ones the provider refused before any generation happened. Those refusals
stay on record - they are audit history - but they are not generations, so
they do not count against MAX_MODEL_GENERATIONS. Everything the model actually
answered (valid, invalid structured output, INVALID_EVIDENCE) and every other
provider failure (5xx, timeouts, a non-credential 403) keeps counting exactly
as before.

ONE interpretation, used by both readers: the QA service deciding whether it
may call the model, and the recovery state the orchestrator resolves stages
from. Two copies of this rule would disagree about whether a lecture is
exhausted, which is the one question they must never disagree on.

Attempts recorded since this policy carry the answer themselves: whether the
call spent budget (`consumes_generation_budget`) and the running total after
it (`generation_budget_used`). The newest recorded total is the anchor, and
only calls after it are added.

Attempts recorded before the policy carry neither, and no HTTP status either,
only `provider_http_error`. For those the evaluation row is the evidence:
every generation for a fingerprint overwrites that one row, so an evaluation
that still records a credential rejection and no model answer proves the LAST
call was refused. The unbroken run of transport failures leading up to it is
read the same way. That is an inference, made ONCE: the first call under this
policy records the total it implies, so a later answer overwriting the
evaluation cannot un-make it. Its worst case is bounded - a legacy transport
failure that was really a 5xx followed by 401s gets one extra generation,
never an unbounded number - and nothing is rewritten.
"""
from app.qa.provider import (
    PROVIDER_CONFIGURATION_ERROR,
    PROVIDER_CONFIGURATION_ERROR_CODE,
    classify_provider_failure,
)


GENERATION_BUDGET_POLICY_VERSION = "generation_budget_v2_configuration_failures_free"

MODEL_ERROR = "MODEL_ERROR"
LEGACY_TRANSPORT_ERROR_CODE = "provider_http_error"


def evaluation_proves_configuration_failure(evaluation) -> bool:
    """Does the fingerprint's evaluation record a credential rejection, with no answer?"""
    if not evaluation or evaluation.get("has_model_output"):
        return False
    if evaluation.get("error_code") not in (LEGACY_TRANSPORT_ERROR_CODE,
                                            PROVIDER_CONFIGURATION_ERROR_CODE):
        return False
    try:
        status = int(evaluation.get("http_status"))
    except (TypeError, ValueError):
        return False
    return classify_provider_failure(http_status=status) == PROVIDER_CONFIGURATION_ERROR


def generation_budget(attempts, evaluation=None, *, max_generations: int) -> dict:
    """
    `attempts`: this fingerprint's rows in generation order, each with
    `outcome`, `error_code`, `consumes_generation_budget` (True, False, or None
    for a row recorded before this policy) and `generation_budget_used` (the
    total that row recorded, or None).
    `evaluation`: `error_code`, `http_status`, `has_model_output` of the
    fingerprint's evaluation row, or None.
    """
    rows = list(attempts)
    anchor = next((index for index in range(len(rows) - 1, -1, -1)
                   if rows[index].get("generation_budget_used") is not None), None)
    if anchor is not None:
        # Everything up to the anchor was already judged when it was written.
        tail = rows[anchor + 1:]
        used = int(rows[anchor]["generation_budget_used"]) + sum(
            1 for row in tail if row.get("consumes_generation_budget") is not False)
        return _summary(rows, used, max_generations, inferred=0,
                        last_free=rows[-1].get("consumes_generation_budget") is False)

    free = [row.get("consumes_generation_budget") is False for row in rows]
    inferred = 0
    if evaluation_proves_configuration_failure(evaluation):
        for index in range(len(rows) - 1, -1, -1):
            row = rows[index]
            if row.get("consumes_generation_budget") is False:
                continue            # a recorded refusal keeps the run unbroken
            if (row.get("consumes_generation_budget") is None
                    and row.get("outcome") == MODEL_ERROR
                    and row.get("error_code") == LEGACY_TRANSPORT_ERROR_CODE):
                free[index] = True
                inferred += 1
                continue
            break
    return _summary(rows, len(rows) - sum(free), max_generations, inferred=inferred,
                    last_free=bool(free and free[-1]))


def _summary(rows, used, max_generations, *, inferred, last_free) -> dict:
    return {
        "attempt_records": len(rows),
        "generation_budget_used": used,
        "configuration_failures": len(rows) - used,
        # Whether the MOST RECENT call was a refused credential - what the
        # lecture is waiting on right now, as opposed to its history.
        "last_attempt_configuration_failure": last_free,
        "legacy_configuration_failures_inferred": inferred,
        "max_model_generations": max_generations,
        "attempts_remaining": max(max_generations - used, 0),
        "exhausted": used >= max_generations,
        "generation_budget_policy_version": GENERATION_BUDGET_POLICY_VERSION,
    }


def consumes_flag(value):
    """The stored flag, read from `metadata ->> 'consumes_generation_budget'`."""
    if value in (True, "true"):
        return True
    if value in (False, "false"):
        return False
    return None


def recorded_total(value):
    """The stored running total, read from `metadata ->> 'generation_budget_used'`."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def attempt_row(outcome, error_code, consumes, total) -> dict:
    """One attempt, as the policy reads it, from its stored columns."""
    return {"outcome": outcome, "error_code": error_code or None,
            "consumes_generation_budget": consumes_flag(consumes),
            "generation_budget_used": recorded_total(total)}
