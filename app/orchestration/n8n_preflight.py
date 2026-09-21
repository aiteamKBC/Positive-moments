"""
Phase 4A Part I: the read-only legacy QA safety precheck.

THE COEXISTENCE THIS PROTECTS
-----------------------------
Legacy QA is manually paused. QA Master Daily v8 is active, its recording
branch is enabled, Excel Sync is enabled, and exactly one node - `Execute QA
One Lecture` - is disabled. That single disabled node is the entire reason the
coded platform may write legacy QA rows without racing the legacy pipeline.

The ownership guard that would have made this robust is NOT deployed (the n8n
management API cannot carry the live workflow's `binaryMode` and
`availableInMCP` through `PUT`, and deploying via the UI was deferred). So the
coexistence rests on a manual configuration, and a manual configuration can be
changed by anyone at any time, including by accident, including by someone who
does not know this platform exists.

This module is the cheap insurance: before any production orchestration run,
ASK, and refuse to write if the answer has changed.

WHAT IT WILL NOT DO
-------------------
It issues exactly one HTTP GET. There is no PUT, PATCH, POST or DELETE in this
file, and no code path that constructs one. It cannot enable, disable, repair
or deploy anything - if the answer is wrong, the correct response is to stop
the coded run and tell a human, not to "fix" a live production workflow
unattended.

FAIL CLOSED
-----------
Unreachable, unauthorised, malformed, or timed out all mean the same thing: we
do not know. Not knowing is treated exactly like "the node is enabled", because
the cost of being wrong in the other direction is two systems writing the same
legacy QA row.
"""
import json
import logging
import os
import pathlib
import urllib.error
import urllib.request


# QA Master Daily - Safe Exact Recording v8. The workflow whose QA branch would
# race the coded platform.
QA_MASTER_WORKFLOW_ID = "8yeJigbj8BCNBMkU"
# The one node whose disabled state is the coexistence contract.
GUARDED_NODE_NAME = "Execute QA One Lecture"

PRECHECK_VERSION = "n8n_legacy_qa_precheck_v1"

# Outcomes. Only the first permits coded production writes.
LEGACY_QA_DISABLED = "LEGACY_QA_DISABLED"
LEGACY_QA_ENABLED = "LEGACY_QA_ENABLED"
PRECHECK_UNAVAILABLE = "PRECHECK_UNAVAILABLE"
PRECHECK_SKIPPED = "PRECHECK_SKIPPED"

ROOT = pathlib.Path(__file__).resolve().parents[2]


class N8nReadOnlyGateway:
    """
    One GET against the n8n management API, and nothing else.

    The API key is read from the environment, sent as a header, and never
    logged, returned, or included in any result - including error results,
    whose bodies are dropped rather than echoed, because an n8n error body can
    quote the request that produced it.
    """

    def __init__(self, *, base_url: str = "", api_key: str = "", timeout: int = 20):
        self.base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        self.timeout = timeout

    @classmethod
    def from_environment(cls, *, timeout: int = 20) -> "N8nReadOnlyGateway":
        config = {"N8N_BASE_URL": os.environ.get("N8N_BASE_URL", "").strip(),
                  "N8N_API_KEY": os.environ.get("N8N_API_KEY", "").strip()}
        if not all(config.values()):
            for candidate in ("backend/.env", ".env"):
                path = ROOT / candidate
                if not path.exists():
                    continue
                for line in path.read_text(encoding="utf-8",
                                           errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key in config and not config[key] and value:
                        config[key] = value
        return cls(base_url=config["N8N_BASE_URL"], api_key=config["N8N_API_KEY"],
                   timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self._api_key)

    def get_workflow(self, workflow_id: str) -> dict:
        """GET one workflow. The only request this class can make."""
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/workflows/{workflow_id}", method="GET")
        request.add_header("X-N8N-API-KEY", self._api_key)
        request.add_header("Accept", "application/json")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode() or "{}")


class LegacyQaPreflight:
    """
    Answer one question: is `Execute QA One Lecture` still disabled?

    `required=False` is for environments with no n8n at all (a developer
    machine, CI). It downgrades an unavailable precheck to SKIPPED, and it must
    never be set for a run that will perform production writes - which is why
    the orchestrator decides it from whether writes are enabled, not from
    configuration.
    """

    def __init__(self, *, gateway=None, workflow_id: str = QA_MASTER_WORKFLOW_ID,
                 node_name: str = GUARDED_NODE_NAME, required: bool = True):
        self.gateway = gateway
        self.workflow_id = workflow_id
        self.node_name = node_name
        self.required = required
        self.log = logging.getLogger(__name__)

    def check(self) -> dict:
        result = {"precheck_version": PRECHECK_VERSION,
                  "workflow_id": self.workflow_id, "node_name": self.node_name,
                  "read_only": True, "http_method": "GET", "n8n_modified": False,
                  "required": self.required}
        gateway = self.gateway
        if gateway is None or not getattr(gateway, "configured", False):
            return {**result, "status": self._unavailable(),
                    "legacy_qa_node_disabled": None,
                    "reason": "N8N_NOT_CONFIGURED",
                    "writes_permitted": not self.required}
        try:
            workflow = gateway.get_workflow(self.workflow_id)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                ValueError, TimeoutError) as exc:
            # The body is deliberately not captured: an n8n error can echo the
            # request, and the request carries the API key header.
            self.log.warning("n8n precheck unavailable: %s", type(exc).__name__)
            return {**result, "status": self._unavailable(),
                    "legacy_qa_node_disabled": None,
                    "reason": f"PRECHECK_{type(exc).__name__.upper()}",
                    "writes_permitted": not self.required}

        nodes = workflow.get("nodes") or []
        target = [node for node in nodes if node.get("name") == self.node_name]
        observed = {
            "workflow_active": bool(workflow.get("active")),
            "workflow_updated_at": workflow.get("updatedAt"),
            "node_count": len(nodes),
            "disabled_node_names": sorted(
                node.get("name") for node in nodes if node.get("disabled")),
        }
        if not target:
            # The node we rely on is gone. That is not "disabled"; it is a
            # workflow we no longer understand, and it fails closed.
            return {**result, **observed, "status": PRECHECK_UNAVAILABLE,
                    "legacy_qa_node_disabled": None,
                    "reason": "GUARDED_NODE_NOT_FOUND", "writes_permitted": False}

        disabled = bool(target[0].get("disabled"))
        return {**result, **observed,
                "status": LEGACY_QA_DISABLED if disabled else LEGACY_QA_ENABLED,
                "legacy_qa_node_disabled": disabled,
                "reason": ("LEGACY_QA_NODE_DISABLED" if disabled
                           else "LEGACY_QA_NODE_ENABLED"),
                "writes_permitted": disabled}

    def _unavailable(self) -> str:
        return PRECHECK_UNAVAILABLE if self.required else PRECHECK_SKIPPED
