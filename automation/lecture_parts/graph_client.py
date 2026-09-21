"""
Compatibility shim. The implementation moved to `app.graph.transport`.

WHY IT MOVED
------------
Five QA-pipeline modules imported this file, which made the nightly QA
scheduler depend on a media-automation directory. The scheduler image copies
only `app/`, so `python -m app.cli.main` could not import inside the container
and the service would have crashed on startup. It is the platform's shared
Microsoft Graph transport, so it belongs in `app.graph`.

This shim exists so the Phase 6 media scripts in this directory - which import
`graph_client` by its bare module name, sys.path-relative - keep working with
no change while that work continues.
"""
import os
import sys

# These scripts run with this directory on sys.path rather than the repository
# root, so `app` may not be importable yet.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.graph.transport import (            # noqa: E402,F401
    GraphAppClient,
    GraphError,
    GraphResponse,
)

__all__ = ["GraphAppClient", "GraphError", "GraphResponse"]
