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


class ProviderError(RuntimeError):
    """A provider failure. Carries a code and an HTTP status, never a secret."""

    def __init__(self, code: str, message: str, *, http_status=None, attempts=1):
        self.code = code
        self.http_status = http_status
        self.attempts = attempts
        super().__init__(message)


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
            detail = ""
            try:
                detail = (json.loads(error.read().decode("utf-8"))
                          .get("error", {}).get("code") or "")
            except Exception:  # noqa: BLE001 - the error body is best-effort only
                detail = ""
            # The message carries a provider code, never the key or the prompt.
            raise ProviderError("provider_http_error",
                                f"QA model call failed with HTTP {error.code} {detail}".strip(),
                                http_status=error.code) from error
        except urllib.error.URLError as error:
            raise ProviderError("provider_unreachable", "QA model endpoint unreachable") from error
