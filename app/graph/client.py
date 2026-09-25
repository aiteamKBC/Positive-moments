import json
import urllib.request

from app.graph.transport import GraphAppClient, GraphError, GraphResponse


class Phase1GraphClient(GraphAppClient):
    """Graph client extension supporting safe request headers for discovery."""

    def post_json(self, path: str, body: dict) -> dict:
        """
        POST a JSON body under MICROSOFT_GRAPH_BASE_URL and return the JSON reply.

        Used by recording links for `/search/query` (read-only despite the verb)
        and `createLink`. Same token handling, same base-URL guard, same
        sanitized errors as every GET.
        """
        if path.startswith(("http://", "https://")):
            raise ValueError("post_json takes a Graph path, not an absolute URL")
        request = urllib.request.Request(
            self.base_url + "/" + path.lstrip("/"),
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + self._access_token(),
                     "Accept": "application/json",
                     "Content-Type": "application/json"},
            method="POST")
        return self._open(request, "Microsoft Graph").json()

    def request(self, method: str, path_or_url: str, *, accept: str = "application/json", headers=None) -> GraphResponse:
        if path_or_url.startswith(("http://", "https://")):
            url = path_or_url
            if not url.startswith(self.base_url + "/"):
                raise ValueError("refusing a Graph pagination URL outside MICROSOFT_GRAPH_BASE_URL")
        else:
            url = self.base_url + "/" + path_or_url.lstrip("/")
        safe_headers = dict(headers or {})
        if any(name.casefold() == "authorization" for name in safe_headers):
            raise ValueError("Authorization header is managed internally")
        request = urllib.request.Request(
            url,
            headers={"Authorization": "Bearer " + self._access_token(), "Accept": accept, **safe_headers},
            method=method,
        )
        return self._open(request, "Microsoft Graph")

    def get_json(self, path_or_url: str, *, headers=None):
        return self.request("GET", path_or_url, headers=headers).json()

    def get_collection(self, path: str, *, headers=None) -> list[dict]:
        rows: list[dict] = []
        next_url = path
        while next_url:
            payload = self.get_json(next_url, headers=headers)
            values = payload.get("value", [])
            if not isinstance(values, list):
                raise GraphError("Microsoft Graph", 200, "invalid_collection", "value was not a list")
            rows.extend(row for row in values if isinstance(row, dict))
            candidate = payload.get("@odata.nextLink")
            next_url = candidate if isinstance(candidate, str) and candidate else None
        return rows

__all__ = ["GraphAppClient", "Phase1GraphClient", "GraphError", "GraphResponse"]
