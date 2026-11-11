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

v3: a row is ALSO free when its own recorded evidence (HTTP status and the
provider's structured code/type) classifies as an account refusal under the
current provider policy, whatever the flag it was written with. That is how
the 429 `project_spend_limit_exceeded` refusals of 2026-10-12..14, recorded
as spent under provider policy v1, stop counting. A recorded total is still
the anchor; it is only corrected by the rows it counted that the evidence now
shows were refusals. No row is rewritten.
"""
from app.qa.provider import (
    PROVIDER_CONFIGURATION_ERROR,
    PROVIDER_CONFIGURATION_ERROR_CODE,
    classify_provider_failure,
)


GENERATION_BUDGET_POLICY_VERSION = "generation_budget_v3_recorded_refusals_free"

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


def recorded_refusal(row) -> bool:
    """Does this attempt's own evidence show the provider refused the account?"""
    try:
        status = int(row.get("http_status"))
    except (TypeError, ValueError):
        return False
    return classify_provider_failure(
        http_status=status, provider_error_code=row.get("provider_error_code"),
        provider_error_type=row.get("provider_error_type")) == PROVIDER_CONFIGURATION_ERROR


def _free(row) -> bool:
    return row.get("consumes_generation_budget") is False or recorded_refusal(row)


def generation_budget(attempts, evaluation=None, *, max_generations: int) -> dict:
    """
    `attempts`: this fingerprint's rows in generation order, each with
    `outcome`, `error_code`, `consumes_generation_budget` (True, False, or None
    for a row recorded before this policy), `generation_budget_used` (the
    total that row recorded, or None) and, when recorded, the provider's
    `http_status`, `provider_error_code` and `provider_error_type`.
    `evaluation`: `error_code`, `http_status`, `has_model_output` of the
    fingerprint's evaluation row, or None.
    """
    rows = list(attempts)
    anchor = next((index for index in range(len(rows) - 1, -1, -1)
                   if rows[index].get("generation_budget_used") is not None), None)
    if anchor is not None:
        # Everything up to the anchor was already judged when it was written.
        # A total written before v3 still counts the account refusals it was
        # told were generations; one written under v3 already excludes them.
        miscounted = 0
        if rows[anchor].get("generation_budget_policy_version") != GENERATION_BUDGET_POLICY_VERSION:
            miscounted = sum(1 for row in rows[:anchor + 1]
                             if row.get("consumes_generation_budget") is True
                             and recorded_refusal(row))
        tail = rows[anchor + 1:]
        used = max(int(rows[anchor]["generation_budget_used"]) - miscounted, 0) + sum(
            1 for row in tail if not _free(row))
        return _summary(rows, used, max_generations, inferred=0,
                        last_free=_free(rows[-1]))

    free = [_free(row) for row in rows]
    inferred = 0
    if evaluation_proves_configuration_failure(evaluation):
        for index in range(len(rows) - 1, -1, -1):
            row = rows[index]
            if free[index]:
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


def attempt_row(outcome, error_code, consumes, total, http_status=None,
                provider_error_code=None, provider_error_type=None,
                budget_policy_version=None) -> dict:
    """One attempt, as the policy reads it, from its stored columns."""
    return {"outcome": outcome, "error_code": error_code or None,
            "consumes_generation_budget": consumes_flag(consumes),
            "generation_budget_used": recorded_total(total),
            "http_status": http_status or None,
            "provider_error_code": provider_error_code or None,
            "provider_error_type": provider_error_type or None,
            "generation_budget_policy_version": budget_policy_version or None}
