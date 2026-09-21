from app.common.errors import (
    GRAPH_AUTH_ERROR,
    GRAPH_PERMISSION_ERROR,
    PlatformError,
)


def translate_graph_error(exc, default_code: str, operation: str) -> PlatformError:
    if exc.status == 401:
        code = GRAPH_AUTH_ERROR
    elif exc.status == 403:
        code = GRAPH_PERMISSION_ERROR
    else:
        code = default_code
    error = PlatformError(code, f"Microsoft Graph {operation} failed")
    error.http_status = exc.status
    error.provider_code = exc.code
    message = str(getattr(exc, "message", "")).casefold()
    if "application access policy" in message:
        error.diagnostic_reason = "APPLICATION_ACCESS_POLICY_DENIED"
    elif exc.status == 403:
        error.diagnostic_reason = "GRAPH_ACCESS_FORBIDDEN"
    return error
