"""
OpenAI chat provider for the shadow QA engine.

Uses `urllib.request` like the rest of the platform's HTTP code, so no new
dependency is introduced. Retries are bounded and mirror the legacy agent node
(retryOnFail, 5 s between tries, n8n's default of 3 attempts). There is no
infinite retry and no fallback to a different model: substituting a model
would silently destroy prompt parity, so a missing or unavailable model is an
error the caller must see.

Never logs or stores the API key, the Authorization header, or transcript text.
"""
import json
import logging
import re
import time
import urllib.error
import urllib.request

from app.qa.prompt import LEGACY_RETRY
from app.qa.structured_output import (
    DEFAULT_PROVIDER_CONTRACT,
    STRICT_JSON_SCHEMA,
    contract_provenance,
    drop_null_optionals,
    response_format,
    validate_contract,
)


QA_PROVIDER = "openai"
DEFAULT_BASE_URL = "https://api.openai.com/v1"

# What KIND of failure a provider error is, versioned because it decides
# whether a lecture's bounded generation budget is spent.
#
# A configuration failure is the provider refusing the CALLER - the key, the
# organisation, the account - before any generation happened. It says nothing
# about the lecture, it will repeat identically for every lecture until an
# operator fixes the credential, and charging it to the lecture's budget is
# how one rotated key walked five lectures towards manual review.
PROVIDER_FAILURE_POLICY_VERSION = "provider_failure_classification_v1"
PROVIDER_CONFIGURATION_ERROR = "PROVIDER_CONFIGURATION_ERROR"
GENERATION_FAILURE = "GENERATION_FAILURE"
# Not a failure of this lecture: the cycle's provider circuit was already open,
# so no call was made at all.
PROVIDER_CONFIGURATION_BLOCKED = "PROVIDER_CONFIGURATION_BLOCKED"
PROVIDER_CONFIGURATION_ERROR_CODE = "provider_configuration_error"

# HTTP 403 is also returned for reasons that are about the REQUEST (content,
# region, a model this project may not use for this input), so a bare 403 is
# never taken as a credential failure. Only the provider's own structured
# error code or type can say so. Structured fields only: the free-text message
# is not parsed, because it is not a contract and it can echo part of the key.
AUTH_FAILURE_ERROR_CODES = frozenset({
    "invalid_api_key", "invalid_organization", "insufficient_permissions"})
AUTH_FAILURE_ERROR_TYPES = frozenset({"authentication_error", "permission_error"})

_STRUCTURED_TOKEN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def classify_provider_failure(*, http_status=None, provider_error_code=None,
                              provider_error_type=None) -> str:
    """PROVIDER_CONFIGURATION_ERROR or GENERATION_FAILURE, from structured fields only."""
    if http_status == 401:
        return PROVIDER_CONFIGURATION_ERROR
    if http_status == 403 and (provider_error_code in AUTH_FAILURE_ERROR_CODES
                               or provider_error_type in AUTH_FAILURE_ERROR_TYPES):
        return PROVIDER_CONFIGURATION_ERROR
    return GENERATION_FAILURE


def _structured_token(value):
    """A provider code/type is kept only if it looks like an identifier."""
    if isinstance(value, str) and _STRUCTURED_TOKEN.match(value):
        return value
    return None


class ProviderError(RuntimeError):
    """A provider failure. Carries a code and an HTTP status, never a secret."""

    def __init__(self, code: str, message: str, *, http_status=None, attempts=1,
                 provider_error_code=None, provider_error_type=None):
        self.http_status = http_status
        self.attempts = attempts
        self.provider_error_code = _structured_token(provider_error_code)
        self.provider_error_type = _structured_token(provider_error_type)
        self.failure_class = classify_provider_failure(
            http_status=http_status, provider_error_code=self.provider_error_code,
            provider_error_type=self.provider_error_type)
        # The generic transport code stays for every other failure; a
        # credential rejection gets its own, so it can be found without
        # decoding metadata.
        self.code = (PROVIDER_CONFIGURATION_ERROR_CODE
                     if self.failure_class == PROVIDER_CONFIGURATION_ERROR else code)
        super().__init__(message)

    @property
    def is_configuration_failure(self) -> bool:
        return self.failure_class == PROVIDER_CONFIGURATION_ERROR

    def diagnostics(self) -> dict:
        """Everything recorded about this failure. No message, no key."""
        return {"failure_class": self.failure_class,
                "http_status": self.http_status,
                "provider_error_code": self.provider_error_code,
                "provider_error_type": self.provider_error_type,
                "provider_failure_policy_version": PROVIDER_FAILURE_POLICY_VERSION}


class ProviderCircuit:
    """
    One invalid credential, one provider call - per cycle.

    Opened by the first configuration failure; while open, no further
    generation is attempted by whoever holds it. Deliberately in memory and
    owned by a single cycle: the next cycle starts closed, so a repaired
    credential is used at once and nothing has to be unlocked by hand.
    """

    def __init__(self):
        self.opened_by = None

    @property
    def is_open(self) -> bool:
        return self.opened_by is not None

    def open(self, *, lecture_id=None, **diagnostics) -> None:
        if self.opened_by is None:
            self.opened_by = {"lecture_id": str(lecture_id) if lecture_id else None,
                              **diagnostics}

    def report(self, *, blocked=()) -> dict:
        return {"state": "OPEN" if self.is_open else "CLOSED",
                "opened_by": self.opened_by,
                "blocked_lecture_ids": list(blocked),
                "blocked_reason": PROVIDER_CONFIGURATION_BLOCKED if self.is_open else None,
                "provider_failure_policy_version": PROVIDER_FAILURE_POLICY_VERSION}


class OpenAIChatProvider:
    def __init__(self, *, api_key: str, model: str, base_url: str = DEFAULT_BASE_URL,
                 timeout: int = 300, max_tries: int = LEGACY_RETRY["max_tries"],
                 wait_between_tries_ms: int = LEGACY_RETRY["wait_between_tries_ms"],
                 response_contract: str = DEFAULT_PROVIDER_CONTRACT,
                 sleep=time.sleep):
        if not api_key:
            raise ProviderError("provider_not_configured", "QA model API key is not configured")
        if not model:
            raise ProviderError("provider_not_configured", "QA model name is not configured")
        self._api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        # Phase 3C3D. The contract the PROVIDER is asked to enforce, named so
        # an old evaluation stays reproducible under the contract that bought
        # it instead of being retroactively reinterpreted.
        self.response_contract = validate_contract(response_contract)
        self.timeout = timeout
        self.max_tries = max(1, int(max_tries))
        self.wait_between_tries_ms = wait_between_tries_ms
        self._sleep = sleep
        self.log = logging.getLogger(__name__)

    def complete_json(self, *, system_message: str, user_message: str) -> dict:
        """
        One structured QA call. Returns the parsed object plus response
        provenance (model actually used, response id, token usage, attempts).
        """
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system_message},
                         {"role": "user", "content": user_message}],
            # Under strict_json_schema_v1 this carries the schema itself, so
            # the KSB enum is refused by the provider rather than paid for and
            # then rejected locally.
            "response_format": response_format(self.response_contract),
        }).encode("utf-8")

        last: ProviderError | None = None
        for attempt in range(1, self.max_tries + 1):
            try:
                payload = self._post("/chat/completions", body)
            except ProviderError as error:
                last = error
                # 4xx other than rate limiting will not succeed on a retry.
                retryable = error.http_status is None or error.http_status in (408, 409, 429) \
                    or (error.http_status or 0) >= 500
                if not retryable or attempt == self.max_tries:
                    error.attempts = attempt
                    raise
                self._sleep(self.wait_between_tries_ms / 1000)
                continue

            choices = payload.get("choices") or []
            content = (choices[0].get("message", {}).get("content")
                       if choices and isinstance(choices[0], dict) else None)
            if not content:
                last = ProviderError("empty_model_response", "model returned no content",
                                     attempts=attempt)
                if attempt == self.max_tries:
                    raise last
                self._sleep(self.wait_between_tries_ms / 1000)
                continue
            try:
                parsed = json.loads(content)
            except ValueError as error:
                # Not retried: a structurally invalid answer is a result the
                # caller records as INVALID_STRUCTURED_OUTPUT, not a transport
                # failure. The legacy auto-fixing model is not reimplemented.
                raise ProviderError("model_output_not_json",
                                    "model response was not valid JSON",
                                    attempts=attempt) from error
            # Strict mode cannot express an optional property, so absent
            # `speaker` / `reasoning` arrive as explicit nulls. Only those
            # nulls are removed; nothing else is touched.
            null_optionals_dropped = 0
            if self.response_contract == STRICT_JSON_SCHEMA:
                parsed, null_optionals_dropped = drop_null_optionals(parsed)
            return {
                "output": parsed,
                "provider": QA_PROVIDER,
                "response_contract": self.response_contract,
                "contract_provenance": contract_provenance(self.response_contract),
                "null_optionals_dropped": null_optionals_dropped,
                "model_requested": self.model,
                "model_reported": payload.get("model"),
                "response_id": payload.get("id"),
                "usage": payload.get("usage") or {},
                "attempts": attempt,
            }
        raise last or ProviderError("provider_error", "model call failed")

    def _post(self, path: str, body: bytes) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._api_key}"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            code = kind = None
            try:
                body = json.loads(error.read().decode("utf-8")).get("error") or {}
                if isinstance(body, dict):
                    code = _structured_token(body.get("code"))
                    kind = _structured_token(body.get("type"))
            except Exception:  # noqa: BLE001 - the error body is best-effort only
                code = kind = None
            # The message carries a provider code, never the key or the prompt.
            raise ProviderError("provider_http_error",
                                f"QA model call failed with HTTP {error.code} {code or ''}".strip(),
                                http_status=error.code, provider_error_code=code,
                                provider_error_type=kind) from error
        except urllib.error.URLError as error:
            raise ProviderError("provider_unreachable", "QA model endpoint unreachable") from error
