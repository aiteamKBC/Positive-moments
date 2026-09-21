import json
import unittest
from unittest.mock import patch

from graph_client import GraphAppClient, GraphResponse


class GraphAppClientTests(unittest.TestCase):
    def client(self) -> GraphAppClient:
        return GraphAppClient(
            tenant_id="tenant",
            client_id="client",
            client_secret="secret",
            scope="https://graph.microsoft.com/.default",
            base_url="https://graph.microsoft.com/v1.0",
        )

    def test_token_is_cached_in_memory(self) -> None:
        client = self.client()
        token_response = GraphResponse(
            json.dumps({"access_token": "token-value", "expires_in": 3600}).encode(),
            "application/json",
            200,
        )
        with patch.object(client, "_open", return_value=token_response) as mocked_open:
            self.assertEqual(client._access_token(), "token-value")
            self.assertEqual(client._access_token(), "token-value")
        self.assertEqual(mocked_open.call_count, 1)

    def test_rejects_pagination_url_outside_configured_graph_base(self) -> None:
        client = self.client()
        with self.assertRaisesRegex(ValueError, "outside MICROSOFT_GRAPH_BASE_URL"):
            client.request("GET", "https://example.invalid/v1.0/users")

    def test_missing_environment_value_is_named_without_exposing_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "MICROSOFT_GRAPH_CLIENT_SECRET"):
            GraphAppClient.from_mapping(
                {
                    "MICROSOFT_GRAPH_TENANT_ID": "tenant",
                    "MICROSOFT_GRAPH_CLIENT_ID": "client",
                    "MICROSOFT_GRAPH_SCOPE": "scope",
                    "MICROSOFT_GRAPH_BASE_URL": "https://graph.microsoft.com/v1.0",
                }
            )


if __name__ == "__main__":
    unittest.main()
