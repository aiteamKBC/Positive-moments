"""
Recording links v9: build the n8n export from versioned sources, and run its
Code nodes offline.

WHY A BUILDER
-------------
The v9 node code lives in automation/legacy_n8n/recording_v9/{src,sql} as
reviewable files. This module derives the importable workflow JSON from the v8
production export plus those files, deterministically, so the export can never
drift from the reviewed source (tests/unit/test_recording_v9.py checks that the
committed JSON equals a fresh build).

WHAT IT CHANGES RELATIVE TO v8 (recording branch only)
------------------------------------------------------
* Run Settings becomes a Set node: dry_run (default true), date_from, date_to,
  max_lectures - editable without touching code - then validated fail-closed.
* The Graph recordings lookup uses the organizer mailbox from
  lecture_sessions.meeting_lookup_user_id with an APP-ONLY credential, instead
  of /me with the delegated n8n user.
* Call ids come from a port of the QA Core RC4 transcript-identity decoder.
* HTTP failures are GRAPH_LOOKUP_FAILED, never "not found".
* The timestamp rule is asymmetric: the file may lead Graph by <= 120 s.
* The write path is reachable only when dry_run is exactly false, and the
  UPDATE repeats every guard in SQL.

WHAT IT NEVER DOES
------------------
`Execute QA One Lecture` is disabled in the export, and so is the schedule:
v9 is imported inactive and run by hand until it is promoted. Nothing here
talks to n8n, Graph or a database.
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import shutil
import subprocess
import sys
import uuid


ROOT = pathlib.Path(__file__).resolve().parents[1]
LEGACY_DIR = ROOT / "automation" / "legacy_n8n"
V8_MASTER = LEGACY_DIR / "QA Master Daily — Safe Exact Recording v8"
V9_MASTER = LEGACY_DIR / "QA_Master_Daily_Safe_Exact_Recording_v9.json"
SOURCE_DIR = LEGACY_DIR / "recording_v9"
HARNESS = SOURCE_DIR / "harness" / "run_n8n_code.js"

WORKFLOW_NAME = "QA Master Daily — Safe Exact Recording v9"
MATCH_METHOD = "exact_call_id_subject_timestamp_v9"
MATCHED_STATUS = "EXACT_RECORDING_FILE_MATCHED"
QA_CHILD_NODE = "Execute QA One Lecture"
UPDATE_NODE = "Update Both Recording Tables"
DEFAULT_SETTINGS = {"dry_run": True, "date_from": "2026-09-01",
                    "date_to": "2026-09-30", "max_lectures": 100}

# The app-only credential must be chosen explicitly after import. The id is a
# placeholder on purpose: n8n reports it missing, so the node cannot run with
# the delegated credential by accident.
APP_GRAPH_CREDENTIAL = {"oAuth2Api": {
    "id": "SET_APP_ONLY_GRAPH_CREDENTIAL_BEFORE_USE",
    "name": "Microsoft Graph APP-ONLY (client credentials) - OnlineMeetingRecording.Read.All",
}}

_NAMESPACE = uuid.UUID("2f1f4b8e-6a53-4c1e-9a7e-3d2a5b0c9e11")


def _source(relative: str) -> str:
    return (SOURCE_DIR / relative).read_text(encoding="utf-8")


def _node_id(name: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, name))


def _if_node(name: str, expression: str, position) -> dict:
    return {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "",
                            "typeValidation": "strict", "version": 2},
                "conditions": [{
                    "id": _node_id(name + ":condition"),
                    "leftValue": expression,
                    "rightValue": "",
                    "operator": {"type": "boolean", "operation": "true",
                                 "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        },
        "type": "n8n-nodes-base.if", "typeVersion": 2.2,
        "position": position, "id": _node_id(name), "name": name,
    }


def _code_node(name: str, code: str, position) -> dict:
    return {"parameters": {"jsCode": code}, "type": "n8n-nodes-base.code",
            "typeVersion": 2, "position": position, "id": _node_id(name),
            "name": name}


def _run_settings_node(position) -> dict:
    types = {"dry_run": "boolean", "date_from": "string", "date_to": "string",
             "max_lectures": "number"}
    return {
        "parameters": {
            "assignments": {"assignments": [
                {"id": _node_id("Run Settings:" + key), "name": key,
                 "value": value, "type": types[key]}
                for key, value in DEFAULT_SETTINGS.items()]},
            "options": {},
        },
        "type": "n8n-nodes-base.set", "typeVersion": 3.4, "position": position,
        "id": _node_id("Run Settings - EDIT HERE"),
        "name": "Run Settings - EDIT HERE",
    }


def _connect(*targets):
    return {"main": [[{"node": node, "type": "main", "index": 0} for node in output]
                     for output in targets]}


def build_workflow() -> dict:
    v8 = json.loads(V8_MASTER.read_text(encoding="utf-8"))
    nodes = {node["name"]: copy.deepcopy(node) for node in v8["nodes"]}

    def at(name, dx=0, dy=0):
        x, y = nodes[name]["position"]
        return [x + dx, y + dy]

    # --- guarded, unchanged-except-disabled ---------------------------------
    nodes[QA_CHILD_NODE]["disabled"] = True
    nodes["Schedule Trigger"]["disabled"] = True

    # --- settings -------------------------------------------------------------
    settings_position = at("Run Settings - EDIT HERE")
    nodes["Run Settings - EDIT HERE"] = _run_settings_node(settings_position)
    nodes["Validate Run Settings"] = _code_node(
        "Validate Run Settings", _source("src/validate_run_settings.js"),
        [settings_position[0] + 110, settings_position[1] + 160])

    target = nodes["Get Target Lectures"]
    target["parameters"]["query"] = _source("sql/get_target_lectures.sql")
    target["parameters"]["options"]["queryReplacement"] = (
        "={{ [ $json.max_lectures, $json.date_from, $json.date_to ] }}")

    nodes["Extract Exact Call IDs"]["parameters"]["jsCode"] = _source(
        "src/extract_exact_call_ids.js")

    # --- organizer-aware Graph lookup ---------------------------------------
    old_lookup = nodes.pop("Get Meeting Recordings by Meeting ID")
    lookup_position = old_lookup["position"]
    nodes["Graph Lookup Possible?"] = _if_node(
        "Graph Lookup Possible?",
        "={{ $json.precheck_status === null && typeof $json.graph_lookup_url === 'string' "
        "&& $json.graph_lookup_url.startsWith('https://graph.microsoft.com/v1.0/users/') }}",
        [lookup_position[0], lookup_position[1] - 180])
    lookup = copy.deepcopy(old_lookup)
    lookup.update({"name": "Get Organizer Meeting Recordings",
                   "id": _node_id("Get Organizer Meeting Recordings"),
                   "credentials": copy.deepcopy(APP_GRAPH_CREDENTIAL),
                   "onError": "continueRegularOutput"})
    lookup["parameters"]["url"] = "={{ $json.graph_lookup_url }}"
    nodes["Get Organizer Meeting Recordings"] = lookup
    nodes["Not Evaluated - Never Update DB"] = _code_node(
        "Not Evaluated - Never Update DB", _source("src/not_evaluated.js"),
        [lookup_position[0] + 220, lookup_position[1] - 360])

    nodes["Collect Lectures and Exact Graph Times"]["parameters"]["jsCode"] = _source(
        "src/collect_graph_recordings.js")
    nodes["Match Every Lecture Safely"]["parameters"]["jsCode"] = _source(
        "src/match_every_lecture_safely.js")

    # --- dry-run gate: the write path needs dry_run === false, twice ----------
    old_gate = nodes.pop("Dry Run?")
    nodes["Write Mode Armed?"] = _if_node(
        "Write Mode Armed?",
        "={{ $json.dry_run === false && "
        "$items('Validate Run Settings')[0].json.dry_run === false }}",
        old_gate["position"])
    nodes["Dry Run Results - REVIEW THIS"]["parameters"]["jsCode"] = _source(
        "src/dry_run_results.js")

    safe = nodes["Exactly One Safe File?"]["parameters"]["conditions"]["conditions"]
    status_condition = next(c for c in safe
                            if c["leftValue"] == "={{ $json.recording_link_status }}")
    status_condition["rightValue"] = MATCHED_STATUS
    safe.append({
        "id": _node_id("Exactly One Safe File?:method"),
        "leftValue": "={{ $json.recording_match_method }}",
        "rightValue": MATCH_METHOD,
        "operator": {"type": "string", "operation": "equals"},
    })

    nodes["Finalize Recording URL"]["parameters"]["jsCode"] = _source(
        "src/finalize_recording_url.js")
    update = nodes[UPDATE_NODE]
    update["parameters"]["query"] = _source("sql/update_both_recording_tables.sql")
    update["parameters"]["options"]["queryReplacement"] = (
        "={{ [\n  $json.session_id,\n  $json.recording_url,\n  $json.recording_item_id,\n"
        "  $json.recording_drive_id,\n  $json.recording_filename,\n"
        "  $json.recording_link_status,\n  $json.lecture_key,\n  $json.meeting_id,\n"
        "  $json.recording_match_method,\n"
        "  String($items('Validate Run Settings')[0].json.dry_run !== false)\n] }}")

    connections = copy.deepcopy(v8["connections"])
    for removed in ("Get Meeting Recordings by Meeting ID", "Dry Run?"):
        connections.pop(removed, None)
    connections.update({
        "Run Settings - EDIT HERE": _connect(["Validate Run Settings"]),
        "Validate Run Settings": _connect(["Get Target Lectures"]),
        "Extract Exact Call IDs": _connect(["Graph Lookup Possible?"]),
        "Graph Lookup Possible?": _connect(["Get Organizer Meeting Recordings"],
                                           ["Not Evaluated - Never Update DB"]),
        "Get Organizer Meeting Recordings": _connect(
            ["Collect Lectures and Exact Graph Times"]),
        "Match Every Lecture Safely": _connect(["Write Mode Armed?"]),
        "Write Mode Armed?": _connect(["Exactly One Safe File?"],
                                      ["Dry Run Results - REVIEW THIS"]),
    })

    ordered = [nodes.pop(node["name"]) for node in v8["nodes"] if node["name"] in nodes]
    ordered += [nodes[name] for name in sorted(nodes)]
    return {
        "name": WORKFLOW_NAME,
        "active": False,
        "nodes": ordered,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
        "pinData": {},
        "meta": v8.get("meta", {}),
        "tags": [],
    }


def render(workflow: dict) -> str:
    return json.dumps(workflow, indent=2, ensure_ascii=False) + "\n"


def load_v9() -> dict:
    return json.loads(V9_MASTER.read_text(encoding="utf-8"))


def node(workflow: dict, name: str) -> dict:
    for candidate in workflow["nodes"]:
        if candidate["name"] == name:
            return candidate
    raise KeyError(name)


# ---------------------------------------------------------------------------
# offline execution
# ---------------------------------------------------------------------------

class HarnessError(RuntimeError):
    """The node code threw, or the harness could not run it."""


def _node_binary() -> str:
    binary = shutil.which("node")
    if not binary:
        raise HarnessError("Node.js is required to execute the v9 Code nodes offline")
    return binary


def _harness(request: dict):
    completed = subprocess.run([_node_binary(), str(HARNESS)],
                               input=json.dumps(request).encode("utf-8"),
                               capture_output=True, timeout=120, check=False)
    try:
        payload = json.loads(completed.stdout.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise HarnessError(completed.stderr.decode("utf-8", "replace")[-2000:]) from exc
    if "error" in payload:
        raise HarnessError(payload["error"])
    return payload.get("result")


def as_items(rows) -> list:
    return [{"json": row} for row in rows]


def run_code(code: str, items=(), nodes=None) -> list:
    """Run a Code node's jsCode. Returns the list of output json objects."""
    result = _harness({"mode": "code", "code": code, "items": list(items),
                       "nodes": nodes or {}})
    return [entry.get("json") for entry in (result or [])]


def evaluate(expression: str, json_value: dict, nodes=None):
    """Evaluate one `={{ ... }}` n8n expression against $json and $items."""
    return _harness({"mode": "expression", "expression": expression,
                     "json": json_value, "nodes": nodes or {}})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("command", choices=("build", "check"))
    args = parser.parse_args(argv)
    rendered = render(build_workflow())
    if args.command == "build":
        V9_MASTER.write_text(rendered, encoding="utf-8", newline="\n")
        print(f"wrote {V9_MASTER.relative_to(ROOT)}")
        return 0
    current = V9_MASTER.read_text(encoding="utf-8") if V9_MASTER.exists() else ""
    if current != rendered:
        print(f"{V9_MASTER.relative_to(ROOT)} is stale: run python -m tools.recording_v9 build")
        return 1
    print("v9 export matches its sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
