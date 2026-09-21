"""
Phase 3C2.2: static validation of the legacy QA One Lecture ownership guard.

The legacy child workflow upserts the SAME 22 session and 6 checklist columns
the coded writer owns, keyed on the same `session_id`, and has no ownership
awareness of its own. These tests assert - from the workflow JSON alone, with
no n8n, no database and no model - that a session recorded as coded-owned in
`public.lecture_qa_legacy_writes` cannot reach either upsert.

They are deliberately structural: they prove the GRAPH cannot route an owned
session to a write, rather than trusting any node's runtime behaviour.
"""
import json
import pathlib

import pytest

from app.writer.mapping import CHECKLIST_COLUMNS, SESSION_COLUMNS


ROOT = pathlib.Path(__file__).resolve().parents[2]
CHILD = ROOT / "automation" / "legacy_n8n" / "QA_One_Lecture_Safe_Exact_Recording_v8.json"
MASTER = ROOT / "automation" / "legacy_n8n" / "QA Master Daily — Safe Exact Recording v8"
BASELINE = (ROOT / "docs" / "migration" / "rollback"
            / "QA_One_Lecture_Safe_Exact_Recording_v8.pre-3c2.2.json")

CHECK = "Check Coded Ownership"
GATE = "Coded Owned?"
SKIP = "Protected - Coded Platform Owns Session"
RESUME = "Resume QA Write Rows"
GUARD_NODES = {CHECK, GATE, SKIP, RESUME}
UPSERTS = {"Upsert QA Session", "Upsert QA Checklist Items"}


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def child():
    return _load(CHILD)


@pytest.fixture(scope="module")
def baseline():
    if not BASELINE.exists():
        pytest.skip("pre-change rollback artifact is not present")
    return _load(BASELINE)


def _nodes(wf):
    return {n["name"]: n for n in wf["nodes"]}


def _targets(wf, name, branch=None):
    spec = wf["connections"].get(name, {})
    branches = spec.get("main") or []
    if branch is not None:
        branches = [branches[branch]] if branch < len(branches) else []
    return [link["node"] for b in branches for link in (b or [])]


def _reachable(wf, start, branch=None):
    """Every node reachable downstream of `start` (optionally one output branch)."""
    seen, stack = set(), list(_targets(wf, start, branch))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(_targets(wf, node))
    return seen


# --- 1-2. the guard exists and is well formed ------------------------------------------

def test_the_workflow_is_valid_json_with_unique_node_ids_and_names(child):
    names = [n["name"] for n in child["nodes"]]
    ids = [n["id"] for n in child["nodes"]]
    assert len(names) == len(set(names)), "duplicate node names"
    assert len(ids) == len(set(ids)), "duplicate node ids"


def test_every_connection_target_exists(child):
    known = {n["name"] for n in child["nodes"]}
    for source, spec in child["connections"].items():
        assert source in known, f"connection from unknown node {source}"
        for branch in spec.get("main") or []:
            for link in branch or []:
                assert link["node"] in known, f"{source} -> unknown {link['node']}"


def test_the_four_guard_nodes_are_present(child):
    nodes = _nodes(child)
    assert GUARD_NODES <= set(nodes)
    assert nodes[CHECK]["type"] == "n8n-nodes-base.postgres"
    assert nodes[GATE]["type"] == "n8n-nodes-base.if"
    assert nodes[SKIP]["type"] == "n8n-nodes-base.code"
    assert nodes[RESUME]["type"] == "n8n-nodes-base.code"


# --- 3-5. both upserts sit behind ONE decision -----------------------------------------

def test_both_upserts_are_reachable_only_through_the_not_owned_branch(child):
    # Nothing but the resume node may feed either upsert.
    feeders = {
        upsert: [src for src, spec in child["connections"].items()
                 for branch in (spec.get("main") or [])
                 for link in (branch or []) if link["node"] == upsert]
        for upsert in UPSERTS
    }
    for upsert, sources in feeders.items():
        assert sources == [RESUME], f"{upsert} is fed by {sources}, expected only {RESUME}"


def test_the_owned_branch_cannot_reach_either_upsert(child):
    owned = _reachable(child, GATE, branch=0)
    assert UPSERTS.isdisjoint(owned), f"owned branch reaches {UPSERTS & owned}"
    assert owned == {SKIP}, f"owned branch should terminate at the no-op, got {owned}"


def test_the_not_owned_branch_reaches_both_upserts(child):
    not_owned = _reachable(child, GATE, branch=1)
    assert UPSERTS <= not_owned


def test_both_merges_route_into_the_guard_and_nowhere_else(child):
    for merge in ("Merge1", "Merge2"):
        assert _targets(child, merge) == [CHECK], f"{merge} bypasses the guard"


def test_no_path_from_either_merge_reaches_an_upsert_without_the_guard(child):
    # Remove the guard chain and the upserts must become unreachable.
    pruned = json.loads(json.dumps(child))
    for node in GUARD_NODES:
        pruned["connections"].pop(node, None)
    for merge in ("Merge1", "Merge2"):
        assert UPSERTS.isdisjoint(_reachable(pruned, merge))


# --- 6-9. the ownership contract --------------------------------------------------------

def test_the_ownership_query_is_parameterised_and_uses_the_written_status(child):
    sql = _nodes(child)[CHECK]["parameters"]["query"]
    assert "lecture_qa_legacy_writes" in sql
    assert "legacy_session_id = $1" in sql, "session id must be a bound parameter"
    assert "write_status = 'WRITTEN'" in sql
    assert "coded_owned" in sql
    # No interpolation of the identifier into SQL text.
    assert "{{" not in sql and "$json" not in sql


def test_the_ownership_key_is_the_session_id_and_not_a_coded_identifier(child):
    node = _nodes(child)[CHECK]
    replacement = node["parameters"]["options"]["queryReplacement"]
    assert replacement == "={{ $json.session_id }}"
    blob = json.dumps(node)
    for forbidden in ("lecture_id", "meeting_id", "targetDate", "subject"):
        assert forbidden not in blob, f"{forbidden} must not be an ownership key"


def test_rolled_back_ownership_does_not_protect(child):
    # Ownership means a live WRITTEN row. A rolled-back write released the row,
    # so legacy n8n is free to own it again.
    sql = _nodes(child)[CHECK]["parameters"]["query"]
    assert "ROLLED_BACK" not in sql
    assert "write_status = 'WRITTEN'" in sql


def test_the_ownership_lookup_runs_once_per_lecture_not_once_per_item(child):
    assert _nodes(child)[CHECK].get("executeOnce") is True


# --- 10-11. fail closed ------------------------------------------------------------------

def test_the_ownership_check_stops_the_workflow_on_error(child):
    node = _nodes(child)[CHECK]
    assert node.get("onError") == "stopWorkflow"
    # alwaysOutputData would manufacture an empty item out of a failed lookup.
    assert node.get("alwaysOutputData") in (False, None)


def test_the_gate_uses_strict_validation_so_a_non_boolean_cannot_pass(child):
    conditions = _nodes(child)[GATE]["parameters"]["conditions"]
    assert conditions["options"]["typeValidation"] == "strict"
    only = conditions["conditions"][0]
    assert only["operator"] == {"type": "boolean", "operation": "true", "singleValue": True}
    assert only["leftValue"] == "={{ $json.coded_owned }}"


def test_the_resume_node_refuses_anything_that_is_not_exactly_false(child):
    code = _nodes(child)[RESUME]["parameters"]["jsCode"]
    # A second, independent assertion immediately before the writes.
    assert "owned !== false" in code
    assert "throw new Error" in code
    assert "no QA rows available to write" in code


# --- 12-13. item cardinality --------------------------------------------------------------

def test_the_resume_node_restores_the_rows_both_merges_produce(child):
    code = _nodes(child)[RESUME]["parameters"]["jsCode"]
    assert "'Merge1'" in code and "'Merge2'" in code
    assert "$(nodeName).all()" in code, (
        "all eleven checklist rows must be re-emitted, not just the first")


def test_the_session_upsert_still_executes_once_while_the_checklist_does_not(child):
    nodes = _nodes(child)
    # Eleven rows carry the same session payload; only the checklist upsert
    # may fan out across them.
    assert nodes["Upsert QA Session"].get("executeOnce") is True
    assert nodes["Upsert QA Checklist Items"].get("executeOnce") in (False, None)


def test_the_guard_does_not_deduplicate_or_filter_rows(child):
    code = _nodes(child)[RESUME]["parameters"]["jsCode"]
    for forbidden in (".filter(", ".slice(", "new Set(", ".shift(", ".pop("):
        assert forbidden not in code, f"{forbidden} could change item cardinality"


# --- 14-16. nothing else changed -----------------------------------------------------------

def test_the_upsert_column_mappings_are_untouched(child, baseline):
    now, was = _nodes(child), _nodes(baseline)
    for upsert in UPSERTS:
        assert now[upsert]["parameters"] == was[upsert]["parameters"]
        assert now[upsert].get("credentials") == was[upsert].get("credentials")


def test_the_legacy_columns_still_match_the_coded_writer_exactly(child):
    nodes = _nodes(child)
    session = set(nodes["Upsert QA Session"]["parameters"]["columns"]["value"])
    checklist = set(nodes["Upsert QA Checklist Items"]["parameters"]["columns"]["value"])
    assert session == set(SESSION_COLUMNS)
    assert checklist == set(CHECKLIST_COLUMNS)


def test_only_the_guard_nodes_were_added_and_no_node_was_removed(child, baseline):
    now = {n["name"] for n in child["nodes"]}
    was = {n["name"] for n in baseline["nodes"]}
    assert now - was == GUARD_NODES
    assert was - now == set(), "the guard must not remove any legacy node"


def test_no_unrelated_node_parameters_changed(child, baseline):
    now, was = _nodes(child), _nodes(baseline)
    changed = [name for name in was
               if json.dumps(now[name], sort_keys=True) != json.dumps(was[name], sort_keys=True)]
    assert changed == [], f"unexpected changes to {changed}"


def test_the_model_and_credential_references_are_unchanged(child, baseline):
    def creds(wf):
        return {(n["name"], json.dumps(n.get("credentials"), sort_keys=True))
                for n in wf["nodes"] if n.get("credentials")}
    added = creds(child) - creds(baseline)
    assert {name for name, _ in added} == {CHECK}, "no other credential reference may change"
    assert creds(baseline) - creds(child) == set(), "no credential reference may be removed"
    # The new node reuses the existing Postgres credential; it introduces none.
    assert _nodes(child)[CHECK]["credentials"] == _nodes(baseline)["Upsert QA Session"]["credentials"]


def test_only_the_expected_connections_changed(child, baseline):
    now, was = child["connections"], baseline["connections"]
    changed = {k for k in set(now) | set(was)
               if json.dumps(now.get(k), sort_keys=True) != json.dumps(was.get(k), sort_keys=True)}
    assert changed == {"Merge1", "Merge2"} | GUARD_NODES - {SKIP}, changed


# --- 17-18. the caller contract and the Master ----------------------------------------------

def test_the_child_input_contract_is_unchanged(child, baseline):
    now, was = _nodes(child), _nodes(baseline)
    assert now["Lecture Input"]["parameters"] == was["Lecture Input"]["parameters"]
    trigger = [n for n in child["nodes"] if "rigger" in n["type"]]
    assert len(trigger) == 1
    assert trigger[0]["parameters"] == {"inputSource": "passthrough"}


def test_the_master_still_invokes_this_child_and_was_not_modified():
    master = _load(MASTER)
    invoke = next(n for n in master["nodes"] if n["name"] == "Execute QA One Lecture")
    assert invoke["parameters"]["workflowId"]["value"] == "LW0lYn7XXJ9JTaYB"
    assert invoke["parameters"]["options"] == {"waitForSubWorkflow": True}
    # The recording branch must stay exactly as it was: it is allowed to set
    # recording_* on a coded-owned session.
    recording = next(n for n in master["nodes"] if n["name"] == "Update Both Recording Tables")
    sql = recording["parameters"]["query"]
    assert "qa_doctors_sessions" in sql and "recording_url" in sql
    assert "lecture_qa_legacy_writes" not in sql, "the recording branch must not be guarded"
    for column in SESSION_COLUMNS:
        if column not in ("session_id", "meeting_id"):
            assert f"{column} =" not in sql, f"recording branch must not write {column}"


def test_direct_child_invocation_is_structurally_protected(child):
    """
    The Phase 3C2.1 audit proved direct invocation was the remaining overwrite
    path. Protection must be structural: from the child's own trigger, every
    route to an upsert passes through the gate.
    """
    trigger = next(n["name"] for n in child["nodes"] if "rigger" in n["type"])
    assert UPSERTS <= _reachable(child, trigger), "the write path must still exist"
    pruned = json.loads(json.dumps(child))
    pruned["connections"].pop(GATE, None)
    assert UPSERTS.isdisjoint(_reachable(pruned, trigger)), (
        "an upsert is reachable from the trigger without passing the gate")
