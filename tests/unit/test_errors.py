from app.common.errors import GRAPH_AUTH_ERROR, GRAPH_PERMISSION_ERROR
from app.graph.errors import translate_graph_error
from automation.lecture_parts.graph_client import GraphError


def test_graph_auth_and_permission_failures_are_not_empty_results():
    auth = translate_graph_error(GraphError("Graph", 401, "x", "secret-free"), "calendar_query_error", "calendar query")
    permission = translate_graph_error(GraphError("Graph", 403, "x", "secret-free"), "calendar_query_error", "calendar query")
    assert auth.code == GRAPH_AUTH_ERROR
    assert permission.code == GRAPH_PERMISSION_ERROR
    assert auth.http_status == 401
    assert permission.provider_code == "x"


def test_access_policy_message_is_classified_without_echoing_it():
    error = translate_graph_error(
        GraphError("Graph", 403, "Forbidden", "No application access policy found for this app"),
        "online_meeting_query_error",
        "online meeting query",
    )
    assert error.diagnostic_reason == "APPLICATION_ACCESS_POLICY_DENIED"
    assert "No application" not in str(error)
