"""Small production Microsoft Graph app-only client.

RELOCATED. This module used to live at `automation/lecture_parts/graph_client`,
which made the QA pipeline depend on a media-automation directory. The
scheduler image copies only `app/`, so its entrypoint could not import - the
container would have crashed on startup. It is the platform's shared Graph
transport and now lives with the rest of `app.graph`.

`automation/lecture_parts/graph_client.py` re-exports from here so the Phase 6
media scripts keep working unchanged.

Credentials are accepted from environment values only. Access tokens are kept
in memory, refreshed before expiry, and are never included in exceptions or
logs emitted by this module.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping


class GraphError(RuntimeError):
    """A sanitized Microsoft identity platform or Graph API failure."""

    def __init__(self, service: str, status: int | None, code: str, message: str):
        self.service = service
        self.status = status
        self.code = code
        self.message = message
        status_text = f" HTTP {status}" if status is not None else ""
        super().__init__(f"{service}{status_text} [{code}]: {message}")


@dataclass(frozen=True)
class GraphResponse:
    body: bytes
    content_type: str
    status: int

    def json(self) -> dict[str, Any]:
        try:
            value = json.loads(self.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GraphError("Microsoft Graph", self.status, "invalid_json", str(exc)) from exc
        if not isinstance(value, dict):
            raise GraphError("Microsoft Graph", self.status, "invalid_json", "expected an object")
        return value


class GraphAppClient:
    """Thread-safe client-credentials client with an in-memory token cache."""

    REQUIRED_ENV = (
        "MICROSOFT_GRAPH_TENANT_ID",
        "MICROSOFT_GRAPH_CLIENT_ID",
        "MICROSOFT_GRAPH_CLIENT_SECRET",
        "MICROSOFT_GRAPH_SCOPE",
        "MICROSOFT_GRAPH_BASE_URL",
    )

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        scope: str,
        base_url: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        values = {
            "tenant_id": tenant_id,
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
            "base_url": base_url,
        }
        missing = [key for key, value in values.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(f"missing Graph configuration: {', '.join(missing)}")
        self._tenant_id = tenant_id.strip()
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._scope = scope.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_mapping(cls, values: Mapping[str, str | None]) -> "GraphAppClient":
        missing = [name for name in cls.REQUIRED_ENV if not values.get(name)]
        if missing:
            raise ValueError(f"missing Graph environment variables: {', '.join(missing)}")
        return cls(
            tenant_id=str(values["MICROSOFT_GRAPH_TENANT_ID"]),
            client_id=str(values["MICROSOFT_GRAPH_CLIENT_ID"]),
            client_secret=str(values["MICROSOFT_GRAPH_CLIENT_SECRET"]),
            scope=str(values["MICROSOFT_GRAPH_SCOPE"]),
            base_url=str(values["MICROSOFT_GRAPH_BASE_URL"]),
        )

    def _access_token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        with self._lock:
            now = time.monotonic()
            if self._token and now < self._token_expires_at - 60:
                return self._token
            token_url = (
                "https://login.microsoftonline.com/"
                + urllib.parse.quote(self._tenant_id, safe="")
                + "/oauth2/v2.0/token"
            )
            form = urllib.parse.urlencode(
                {
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": self._scope,
                    "grant_type": "client_credentials",
                }
            ).encode()
            request = urllib.request.Request(
                token_url,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            response = self._open(request, "Microsoft identity platform")
            payload = response.json()
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise GraphError(
                    "Microsoft identity platform",
                    response.status,
                    "missing_access_token",
                    "token response did not contain an access token",
                )
            try:
                expires_in = max(60, int(payload.get("expires_in", 3600)))
            except (TypeError, ValueError):
                expires_in = 3600
            self._token = token
            self._token_expires_at = time.monotonic() + expires_in
            return token

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        accept: str = "application/json",
    ) -> GraphResponse:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
            if not url.startswith(self.base_url + "/"):
                raise ValueError("refusing a Graph pagination URL outside MICROSOFT_GRAPH_BASE_URL")
        else:
            url = self.base_url + "/" + path_or_url.lstrip("/")
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": "Bearer " + self._access_token(),
                "Accept": accept,
            },
            method=method,
        )
        return self._open(request, "Microsoft Graph")

    def get_json(self, path_or_url: str) -> dict[str, Any]:
        return self.request("GET", path_or_url).json()

    def get_collection(self, path: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        next_url: str | None = path
        while next_url:
            payload = self.get_json(next_url)
            values = payload.get("value", [])
            if not isinstance(values, list):
                raise GraphError("Microsoft Graph", 200, "invalid_collection", "value was not a list")
            rows.extend(row for row in values if isinstance(row, dict))
            candidate = payload.get("@odata.nextLink")
            next_url = candidate if isinstance(candidate, str) and candidate else None
        return rows

    def _open(self, request: urllib.request.Request, service: str) -> GraphResponse:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return GraphResponse(
                    response.read(),
                    response.headers.get_content_type(),
                    response.status,
                )
        except urllib.error.HTTPError as exc:
            body = exc.read()
            code, message = "request_failed", "request was rejected"
            try:
                payload = json.loads(body)
                error = payload.get("error", payload)
                code = str(error.get("code", code))
                message = str(error.get("message", message))
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                pass
            raise GraphError(service, exc.code, code, message) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            reason_name = type(reason).__name__ if reason else "network_error"
            raise GraphError(service, None, reason_name, "network request failed") from None
