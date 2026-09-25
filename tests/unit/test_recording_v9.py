"""
Recording links v9: the workflow export, executed offline.

Every behavioural test here runs the jsCode / expressions exactly as they sit
in automation/legacy_n8n/QA_Master_Daily_Safe_Exact_Recording_v9.json, through
Node.js (tools/recording_v9.py). Nothing reaches n8n, Graph or a database: the
upstream node outputs are stubbed.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import uuid

import pytest

from app.transcripts.identity import canonical_transcript_identity
from tools import recording_v9 as v9


pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="Node.js is required to execute the n8n Code nodes offline")

SETTINGS_NODE = "Validate Run Settings"
ORGANIZER = "11111111-2222-3333-4444-555555555555"
MEETING = "MSoxMTExMTExMS0yMjIyLTMzMzMtNDQ0NC01NTU1NTU1NTU1NTUqMCoqMTk6bWVldGluZ0B0aHJlYWQudjI"


@pytest.fixture(scope="module")
def wf():
    return v9.load_v9()


def code(wf, name):
    return v9.node(wf, name)["parameters"]["jsCode"]


def settings_nodes(dry_run=True):
    return {SETTINGS_NODE: [[{"json": {"dry_run": dry_run, "date_from": "2026-09-01",
                                       "date_to": "2026-09-30", "max_lectures": 100}}]]}


# ---------------------------------------------------------------------------
# Graph transcript id encoders (MessagePack-CSharp LZ4 envelope)
# ---------------------------------------------------------------------------

def _mp_uint(n):
    if n < 0x80:
        return bytes([n])
    if n < 0x100:
        return b"\xcc" + bytes([n])
    if n < 0x10000:
        return b"\xcd" + n.to_bytes(2, "big")
    return b"\xce" + n.to_bytes(4, "big")


def _mp_str(text):
    raw = text.encode("utf-8")
    if len(raw) < 32:
        return bytes([0xA0 | len(raw)]) + raw
    if len(raw) < 256:
        return b"\xd9" + bytes([len(raw)]) + raw
    return b"\xda" + len(raw).to_bytes(2, "big") + raw


def _lz4_len(value):
    out = bytearray()
    while value >= 255:
        out.append(255)
        value -= 255
    out.append(value)
    return bytes(out)


def _lz4_sequence(literal, offset=None, match=None):
    lit_token = min(len(literal), 15)
    match_token = 0 if match is None else min(match - 4, 15)
    out = bytearray([(lit_token << 4) | match_token])
    if len(literal) >= 15:
        out += _lz4_len(len(literal) - 15)
    out += literal
    if match is not None:
        out += offset.to_bytes(2, "little")
        if match - 4 >= 15:
            out += _lz4_len(match - 4 - 15)
    return bytes(out)


def lz4_literal_block(payload):
    """What Graph usually emits: one literals-only sequence."""
    return _lz4_sequence(payload)


def lz4_compressed_block(payload):
    """A greedy LZ4 compressor: emits real back-references (matches)."""
    out, table, i, anchor, n = bytearray(), {}, 0, 0, len(payload)
    while i < n - 12:
        key = payload[i:i + 4]
        candidate = table.get(key)
        table[key] = i
        if candidate is not None and i - candidate <= 65535:
            length = 4
            while i + length < n - 5 and payload[candidate + length] == payload[i + length]:
                length += 1
            out += _lz4_sequence(payload[anchor:i], i - candidate, length)
            i += length
            anchor = i
        else:
            i += 1
    out += _lz4_sequence(payload[anchor:])
    return bytes(out)


def graph_id(thread, meeting_ts, transcript, *, trailing_nil=False, compress=False):
    fields = [_mp_uint(4), _mp_str(thread), _mp_str(meeting_ts), _mp_str(transcript)]
    if trailing_nil:
        fields.append(b"\xc0")
    payload = bytes([0x90 | len(fields)]) + b"".join(fields)
    block = (lz4_compressed_block if compress else lz4_literal_block)(payload)
    ext = _mp_uint(len(payload))
    header = {1: b"\xd4", 2: b"\xd5", 4: b"\xd6"}.get(len(ext), b"\xc7" + bytes([len(ext)]))
    envelope = b"\x92" + header + bytes([98]) + ext + b"\xc6" + len(block).to_bytes(4, "big") + block
    return base64.urlsafe_b64encode(envelope).decode("ascii")


# Two real production ids of one transcript (both serializations), already
# public in tests/unit/test_safety_fixes_f01_f02_f03.py.
REAL_CANONICAL = (
    "ktVizIDGAAAAgvBxlATZMDE5OjYxMjU3NDVhMTc0NjRjYTA4MDFiMTM5NzY4MTZiZGUxQHRocmVh"
    "ZC50YWN2Mq0xNzgxODc5ODEzMTUy2TwzMjVhNWVhNy1jMzVmLTRlODQtOWQ2MS04M2RlOTEwNTBm"
    "NjItMTc5MDA2NTY1MS1UcmFuc2NyaXB0VjI=")
REAL_VARIANT = (
    "ktVizIHGAAAAg_BylQTZMDE5OjYxMjU3NDVhMTc0NjRjYTA4MDFiMTM5NzY4MTZiZGUxQHRocmVh"
    "ZC50YWN2Mq0xNzgxODc5ODEzMTUy2TwzMjVhNWVhNy1jMzVmLTRlODQtOWQ2MS04M2RlOTEwNTBm"
    "NjItMTc5MDA2NTY1MS1UcmFuc2NyaXB0VjLA")
REAL_CALL_ID = "325a5ea7-c35f-4e84-9d61-83de91050f62"

# The thread id shares a 24-character run with the call id, so the compressor
# encodes the call id as a back-reference - the shape of the production row
# (2026-08-20) that v8 reported as SESSION_CALL_ID_MISSING.
LZ4_CALL_ID = "9fec4226-6f0d-4c4e-8a51-0d2f6e1a7b3c"
LZ4_TRIPLE = (f"19:meeting_{LZ4_CALL_ID[:24]}x@thread.v2", "1781879813152",
              f"{LZ4_CALL_ID}-1787212469-TranscriptV2")


def lecture(**overrides):
    row = {"lecture_key": None, "lecture_date": "2026-09-11", "date": "2026-09-11",
           "session_date": "2026-09-11", "subject": "Femi-Commercial Intelligence-Oct 25",
           "trainer": "Trainer", "meeting_id": MEETING, "session_id": REAL_CANONICAL,
           "recording_url": None, "recording_link_status": None,
           "cancelled_session": "false", "meeting_lookup_user_id": ORGANIZER,
           "organizer_count": 1}
    row.update(overrides)
    return row


def extract(wf, rows, dry_run=True):
    return v9.run_code(code(wf, "Extract Exact Call IDs"), v9.as_items(rows),
                       settings_nodes(dry_run))


# ---------------------------------------------------------------------------
# 1. the export itself
# ---------------------------------------------------------------------------

def test_the_committed_export_is_exactly_what_the_sources_build():
    assert v9.V9_MASTER.read_text(encoding="utf-8") == v9.render(v9.build_workflow())


def test_the_v8_production_export_is_untouched_by_the_builder(wf):
    v8 = v9.V8_MASTER.read_text(encoding="utf-8")
    assert "Get Meeting Recordings by Meeting ID" in v8
    assert "exact_call_id_subject_timestamp_v9" not in v8


def test_qa_child_stays_disabled_in_the_v9_export(wf):
    child = v9.node(wf, "Execute QA One Lecture")
    assert child["disabled"] is True
    assert wf["active"] is False
    assert v9.node(wf, "Schedule Trigger")["disabled"] is True


def test_dry_run_defaults_true_in_the_export(wf):
    assignments = v9.node(wf, "Run Settings - EDIT HERE")["parameters"]["assignments"]["assignments"]
    values = {a["name"]: (a["value"], a["type"]) for a in assignments}
    assert values["dry_run"] == (True, "boolean")
    assert values["date_from"] == ("2026-09-01", "string")
    assert values["date_to"] == ("2026-09-30", "string")
    assert values["max_lectures"][1] == "number"


@pytest.mark.parametrize("raw_dry_run, expected", [
    (True, True), (None, True), ("false", True), (0, True), ("", True), (False, False),
])
def test_only_a_literal_boolean_false_disarms_dry_run(wf, raw_dry_run, expected):
    raw = {"date_from": "2026-09-01", "date_to": "2026-09-30", "max_lectures": 50}
    if raw_dry_run is not None:
        raw["dry_run"] = raw_dry_run
    out = v9.run_code(code(wf, SETTINGS_NODE), v9.as_items([raw]))
    assert out == [{"settings_version": "recording_links_v9", "dry_run": expected,
                    "date_from": "2026-09-01", "date_to": "2026-09-30", "max_lectures": 50}]


@pytest.mark.parametrize("raw", [
    {"date_from": "2026-09-30", "date_to": "2026-09-01"},
    {"date_from": "2026-02-30", "date_to": "2026-03-01"},
    {"date_from": "", "date_to": "2026-09-30"},
    {"date_from": "2026-09-01", "date_to": "2026-09-30", "max_lectures": 0},
    {"date_from": "2026-09-01", "date_to": "2026-09-30", "max_lectures": 1.5},
    {"date_from": "2025-01-01", "date_to": "2026-09-30"},
])
def test_invalid_run_settings_fail_the_run(wf, raw):
    with pytest.raises(v9.HarnessError, match="Run Settings"):
        v9.run_code(code(wf, SETTINGS_NODE), v9.as_items([raw]))


def test_target_query_is_bound_to_the_validated_date_range(wf):
    target = v9.node(wf, "Get Target Lectures")
    params = v9.evaluate(target["parameters"]["options"]["queryReplacement"],
                         {"max_lectures": 100, "date_from": "2026-09-01",
                          "date_to": "2026-09-30"})
    assert params == [100, "2026-09-01", "2026-09-30"]
    sql = target["parameters"]["query"]
    assert "s.date::date BETWEEN $2::date AND $3::date" in sql
    assert "NULLIF(BTRIM(s.recording_url), '') IS NULL" in sql
    assert "meeting_lookup_user_id" in sql
    assert "LIMIT $1::integer" in sql


# ---------------------------------------------------------------------------
# 2. the write path is unreachable in dry run
# ---------------------------------------------------------------------------

def _paths_to(wf, target):
    """Every trigger-to-target path, as a list of (node, output_index) hops."""
    edges = {}
    for source, outputs in wf["connections"].items():
        for index, targets in enumerate(outputs.get("main", [])):
            for hop in targets or []:
                edges.setdefault(source, []).append((index, hop["node"]))
    triggers = [n["name"] for n in wf["nodes"] if n["type"].endswith("Trigger")]
    found = []

    def walk(name, path):
        if name == target:
            found.append(path)
            return
        for index, nxt in edges.get(name, []):
            if nxt not in [p[0] for p in path]:
                walk(nxt, path + [(name, index)])

    for trigger in triggers:
        walk(trigger, [])
    return found


@pytest.mark.parametrize("writer", [v9.UPDATE_NODE, "Create Organization View Link"])
def test_every_path_to_a_write_passes_the_armed_gate_on_its_true_output(wf, writer):
    paths = _paths_to(wf, writer)
    assert paths, f"{writer} is not wired"
    for path in paths:
        hops = dict(path)
        assert hops.get("Write Mode Armed?") == 0
        assert hops.get("Exactly One Safe File?") == 0
        assert "Dry Run Results - REVIEW THIS" not in hops


@pytest.mark.parametrize("item_dry, settings_dry, armed", [
    (True, True, False), (False, True, False), (True, False, False),
    ("false", False, False), (None, False, False), (False, False, True),
])
def test_the_write_gate_opens_only_when_both_dry_run_values_are_false(
        wf, item_dry, settings_dry, armed):
    gate = v9.node(wf, "Write Mode Armed?")["parameters"]["conditions"]["conditions"]
    assert len(gate) == 1 and gate[0]["operator"]["operation"] == "true"
    assert v9.evaluate(gate[0]["leftValue"], {"dry_run": item_dry},
                       settings_nodes(settings_dry)) is armed


def test_update_sql_refuses_in_dry_run_even_if_reached(wf):
    update = v9.node(wf, v9.UPDATE_NODE)
    params = v9.evaluate(update["parameters"]["options"]["queryReplacement"],
                         {"session_id": "s", "recording_url": "u",
                          "recording_match_method": v9.MATCH_METHOD},
                         settings_nodes(True))
    assert params[-1] == "true"
    sql = update["parameters"]["query"]
    assert "WHERE i.dry_run IS FALSE" in sql
    assert f"i.recording_match_method = '{v9.MATCH_METHOD}'" in sql


# ---------------------------------------------------------------------------
# 3. RC4 / LZ4 call-id extraction agrees with QA Core
# ---------------------------------------------------------------------------

def _qa_core_call_id(raw):
    identity = canonical_transcript_identity(raw)
    if not identity.decoded:
        return None
    match = re.fullmatch(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
                         r"-\d+-TranscriptV2", identity.transcript_id, re.I)
    return match.group(1).lower() if match else None


@pytest.mark.parametrize("raw, expected", [
    (REAL_CANONICAL, REAL_CALL_ID),
    (REAL_VARIANT, REAL_CALL_ID),
    (graph_id(*LZ4_TRIPLE, compress=True), LZ4_CALL_ID),
    (graph_id(*LZ4_TRIPLE, compress=True, trailing_nil=True), LZ4_CALL_ID),
], ids=["rc4-canonical", "rc4-trailing-nil", "lz4-back-reference", "lz4-trailing-nil"])
def test_v9_call_id_matches_the_qa_core_rc4_decoder(wf, raw, expected):
    assert _qa_core_call_id(raw) == expected
    [out] = extract(wf, [lecture(session_id=raw)])
    assert out["expected_call_id"] == expected
    assert out["transcript_identity_source"] == "DECODED"
    assert out["call_id_source"] == "rc4_transcript_identity_v1"
    assert out["precheck_status"] is None


def test_the_lz4_fixture_really_back_references_the_call_id():
    raw = graph_id(*LZ4_TRIPLE, compress=True)
    decoded_bytes = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    assert LZ4_CALL_ID.encode() not in decoded_bytes      # v8's plain decode cannot see it
    assert canonical_transcript_identity(raw).transcript_id == LZ4_TRIPLE[2]


def test_v8_extraction_fails_on_the_lz4_id_and_v9_does_not(wf):
    v8 = v9.node(json.loads(v9.V8_MASTER.read_text(encoding="utf-8")), "Extract Exact Call IDs")
    raw = graph_id(*LZ4_TRIPLE, compress=True)
    [old] = v9.run_code(v8["parameters"]["jsCode"], v9.as_items([{"session_id": raw}]),
                        {"Run Settings - EDIT HERE": [[{"json": {"dry_run": True}}]]})
    assert old["expected_call_id"] is None
    [new] = extract(wf, [lecture(session_id=raw)])
    assert new["expected_call_id"] == LZ4_CALL_ID


def test_many_synthetic_ids_agree_with_qa_core_in_every_serialization(wf):
    rows, expected = [], []
    for i in range(60):
        call = str(uuid.uuid5(uuid.NAMESPACE_URL, f"call-{i}"))
        triple = (f"19:{uuid.uuid5(uuid.NAMESPACE_URL, f't{i}').hex}@thread.tacv2",
                  str(1781879813152 + i), f"{call}-{1790000000 + i}-TranscriptV2")
        for kwargs in ({}, {"trailing_nil": True}, {"compress": True},
                       {"compress": True, "trailing_nil": True}):
            raw = graph_id(*triple, **kwargs)
            rows.append(lecture(session_id=raw))
            expected.append(_qa_core_call_id(raw))
    assert all(expected)
    assert [o["expected_call_id"] for o in extract(wf, rows)] == expected


@pytest.mark.parametrize("raw", [
    "", "not a transcript id", "!!!***", "ktVizIDG",
    base64.urlsafe_b64encode(b"\x92\xd5\x62\xcc\x80").decode(),
    base64.urlsafe_b64encode(base64.urlsafe_b64decode(REAL_CANONICAL) + b"\x00").decode(),
])
def test_undecodable_ids_yield_no_call_id_and_are_never_looked_up(wf, raw):
    assert _qa_core_call_id(raw) is None
    [out] = extract(wf, [lecture(session_id=raw or "x")])
    assert out["expected_call_id"] is None
    assert out["precheck_status"] == "SESSION_CALL_ID_MISSING"
    assert out["graph_lookup_url"] is None


# ---------------------------------------------------------------------------
# 4. organizer-aware lookup and the precheck
# ---------------------------------------------------------------------------

def test_the_lookup_uses_the_organizer_mailbox_and_an_app_only_credential(wf):
    [out] = extract(wf, [lecture()])
    assert out["graph_lookup_url"] == (
        f"https://graph.microsoft.com/v1.0/users/{ORGANIZER}/onlineMeetings/{MEETING}"
        "/recordings?$top=100")
    http = v9.node(wf, "Get Organizer Meeting Recordings")
    assert http["parameters"]["url"] == "={{ $json.graph_lookup_url }}"
    assert http["credentials"] == v9.APP_GRAPH_CREDENTIAL
    assert http["onError"] == "continueRegularOutput"
    gate = v9.node(wf, "Graph Lookup Possible?")["parameters"]["conditions"]["conditions"][0]
    assert v9.evaluate(gate["leftValue"], out) is True
    assert "/me/" not in json.dumps(http)


@pytest.mark.parametrize("overrides, status", [
    ({"meeting_lookup_user_id": None, "organizer_count": 0}, "ORGANIZER_LOOKUP_ID_MISSING"),
    ({"meeting_lookup_user_id": "  ", "organizer_count": 0}, "ORGANIZER_LOOKUP_ID_MISSING"),
    ({"organizer_count": 2}, "ORGANIZER_LOOKUP_ID_AMBIGUOUS"),
    ({"cancelled_session": "true"}, "NO_RECORDING_EXPECTED_CANCELLED"),
    ({"cancelled_session": "true", "meeting_lookup_user_id": None},
     "NO_RECORDING_EXPECTED_CANCELLED"),
])
def test_lectures_that_cannot_be_looked_up_never_reach_graph(wf, overrides, status):
    [out] = extract(wf, [lecture(**overrides)])
    assert out["precheck_status"] == status
    assert out["graph_lookup_url"] is None
    gate = v9.node(wf, "Graph Lookup Possible?")["parameters"]["conditions"]["conditions"][0]
    assert v9.evaluate(gate["leftValue"], out) is False
    [reported] = v9.run_code(code(wf, "Not Evaluated - Never Update DB"), v9.as_items([out]))
    assert reported["recording_link_status"] == status
    assert reported["execution_result"] == "NO_DATABASE_UPDATE_NOT_EVALUATED"


def collect(wf, responses, lectures_):
    [out] = v9.run_code(code(wf, "Collect Lectures and Exact Graph Times"),
                        v9.as_items(responses),
                        {"Graph Lookup Possible?": [v9.as_items(lectures_)]})
    return out["lectures"]


def _axios_error(status, code_):
    return {"error": {"message": f'{status} - "{{\\"error\\":{{\\"code\\":\\"{code_}\\"}}}}"',
                      "name": "AxiosError"}}


@pytest.mark.parametrize("status, code_", [(401, "InvalidAuthenticationToken"),
                                            (403, "Forbidden"), (404, "NotFound")])
def test_http_failures_are_graph_lookup_failed_not_recording_not_found(wf, status, code_):
    [out] = extract(wf, [lecture()])
    [result] = collect(wf, [_axios_error(status, code_)], [out])
    assert result["graph_status"] == "GRAPH_LOOKUP_FAILED"
    assert result["graph_http_status"] == status


def test_a_successful_empty_graph_response_is_recording_not_found(wf):
    [out] = extract(wf, [lecture()])
    assert collect(wf, [{"value": []}], [out])[0]["graph_status"] == "RECORDING_NOT_FOUND"
    other = {"value": [{"callId": str(uuid.uuid4()), "createdDateTime": "2026-09-11T08:00:00Z"}]}
    assert collect(wf, [other], [out])[0]["graph_status"] == "RECORDING_NOT_FOUND"


def test_graph_edge_cases_never_look_like_a_clean_answer(wf):
    [out] = extract(wf, [lecture()])
    one = {"callId": REAL_CALL_ID, "createdDateTime": "2026-09-11T07:57:05.0872461Z"}
    assert collect(wf, [{"value": [one, dict(one, createdDateTime="2026-09-11T10:00:00Z")]}],
                   [out])[0]["graph_status"] == "AMBIGUOUS_GRAPH_RECORDINGS"
    assert collect(wf, [{"value": [one], "@odata.nextLink": "https://next"}],
                   [out])[0]["graph_status"] == "GRAPH_RESULTS_INCOMPLETE"
    assert collect(wf, [{"unexpected": True}], [out])[0]["graph_status"] == "GRAPH_LOOKUP_FAILED"
    exact = collect(wf, [{"value": [one]}], [out])[0]
    assert exact["graph_status"] == "EXACT_GRAPH_RECORDING_FOUND"
    assert exact["graph_created_at"] == one["createdDateTime"]


def test_a_response_count_mismatch_fails_the_run(wf):
    [out] = extract(wf, [lecture()])
    with pytest.raises(v9.HarnessError, match="responses"):
        collect(wf, [{"value": []}, {"value": []}], [out])


# ---------------------------------------------------------------------------
# 5. the timestamp rule and exactly-one matching
# ---------------------------------------------------------------------------

GRAPH_CREATED = "2026-09-11T07:57:05.0000000Z"


def graph_lecture(**overrides):
    row = dict(lecture(), expected_call_id=REAL_CALL_ID, graph_status="EXACT_GRAPH_RECORDING_FOUND",
               graph_created_at=GRAPH_CREATED, dry_run=True)
    row.update(overrides)
    return row


def recording_file(seconds_before_graph, *, subject="Femi-Commercial Intelligence-Oct 25",
                   n=0, utc=True):
    from datetime import datetime, timedelta, timezone
    graph = datetime(2026, 9, 11, 7, 57, 5, tzinfo=timezone.utc)
    stamp = graph - timedelta(seconds=seconds_before_graph)
    name = f"{subject}-{stamp:%Y%m%d_%H%M%S}{'UTC' if utc else ''}-Meeting Recording.mp4"
    return {"id": f"item-{n}-{seconds_before_graph}", "driveId": f"drive-{n}", "name": name,
            "webUrl": f"https://tenant.sharepoint.com/r/{n}.mp4", "source": "tenant_graph_search"}


def match(wf, lectures_, files):
    return v9.run_code(
        code(wf, "Match Every Lecture Safely"), v9.as_items([{"value": []}]),
        {"Collect Lectures and Exact Graph Times": [[{"json": {"lectures": lectures_}}]],
         "Collect Tenant Search Candidates": [[{"json": {"tenant_search_files": files}}]],
         "Collect All Channel Recording Files": [[{"json": {"channel_recording_files": []}}]]})


@pytest.mark.parametrize("lead", [17, 24, 27, 72, 73, 0, 120])
def test_the_audited_early_file_timestamps_now_match(wf, lead):
    [out] = match(wf, [graph_lecture()], [recording_file(lead)])
    assert out["recording_link_status"] == "EXACT_RECORDING_FILE_MATCHED"
    assert out["timestamp_difference_seconds"] == lead
    assert out["recording_match_method"] == "exact_call_id_subject_timestamp_v9"
    assert out["recording_item_id"] and out["recording_drive_id"]


@pytest.mark.parametrize("lead", [121, 180, 3600])
def test_a_file_more_than_120_seconds_early_does_not_match(wf, lead):
    [out] = match(wf, [graph_lecture()], [recording_file(lead)])
    assert out["recording_link_status"] == "TIMESTAMP_MISMATCH"
    assert out["recording_item_id"] is None and out["recording_match_method"] is None


@pytest.mark.parametrize("lag", [1, 5, 60])
def test_a_file_after_the_graph_timestamp_does_not_match(wf, lag):
    [out] = match(wf, [graph_lecture()], [recording_file(-lag)])
    assert out["recording_link_status"] == "TIMESTAMP_MISMATCH"
    assert out["recording_item_id"] is None


def test_subject_and_date_are_still_exact(wf):
    [wrong_subject] = match(wf, [graph_lecture()], [recording_file(20, subject="Martech - Fri")])
    assert wrong_subject["recording_link_status"] == "SUBJECT_MISMATCH"
    [dr_prefix] = match(wf, [graph_lecture()],
                        [recording_file(20, subject="Dr Femi-Commercial Intelligence-Oct 25")])
    assert dr_prefix["recording_link_status"] == "EXACT_RECORDING_FILE_MATCHED"
    [other_day] = match(wf, [graph_lecture(lecture_date="2026-09-12", date="2026-09-12")],
                        [recording_file(20)])
    assert other_day["recording_link_status"] == "RECORDING_FILE_NOT_FOUND"
    [nothing] = match(wf, [graph_lecture()], [])
    assert nothing["recording_link_status"] == "RECORDING_FILE_NOT_FOUND"


def test_two_qualifying_files_are_ambiguous_and_carry_no_file(wf):
    [out] = match(wf, [graph_lecture()], [recording_file(20, n=1), recording_file(40, n=2)])
    assert out["recording_link_status"] == "AMBIGUOUS_RECORDING_FILES"
    assert out["exact_file_matches"] == 2
    assert out["recording_item_id"] is None and out["recording_match_method"] is None


@pytest.mark.parametrize("graph_status", ["GRAPH_LOOKUP_FAILED", "RECORDING_NOT_FOUND",
                                          "AMBIGUOUS_GRAPH_RECORDINGS",
                                          "GRAPH_RESULTS_INCOMPLETE"])
def test_no_file_is_considered_without_an_exact_graph_recording(wf, graph_status):
    [out] = match(wf, [graph_lecture(graph_status=graph_status)], [recording_file(20)])
    assert out["recording_link_status"] == graph_status
    assert out["recording_item_id"] is None


def _safe_file_gate(wf, item):
    conditions = v9.node(wf, "Exactly One Safe File?")["parameters"]["conditions"]["conditions"]
    for condition in conditions:
        value = v9.evaluate(condition["leftValue"], item)
        operation = condition["operator"]["operation"]
        if operation == "equals" and value != condition["rightValue"]:
            return False
        if operation == "notEmpty" and not value:
            return False
    return True


def test_only_an_exact_v9_match_passes_the_safe_file_gate(wf):
    [exact] = match(wf, [graph_lecture()], [recording_file(20)])
    [ambiguous] = match(wf, [graph_lecture()], [recording_file(20, n=1), recording_file(30, n=2)])
    [late] = match(wf, [graph_lecture()], [recording_file(-3)])
    assert _safe_file_gate(wf, exact) is True
    assert _safe_file_gate(wf, ambiguous) is False
    assert _safe_file_gate(wf, late) is False
    assert _safe_file_gate(wf, dict(exact, recording_match_method="exact_call_id_subject_and_timestamp_v8")) is False


def test_dry_run_report_marks_results_without_writing(wf):
    [exact] = match(wf, [graph_lecture()], [recording_file(20)])
    [report] = v9.run_code(code(wf, "Dry Run Results - REVIEW THIS"), v9.as_items([exact]))
    assert report["execution_result"] == "DRY_RUN_EXACT_MATCH_NO_DATABASE_UPDATE"


def test_finalize_never_persists_a_list_form_url(wf):
    [exact] = match(wf, [graph_lecture()], [recording_file(20)])
    nodes = {"Exactly One Safe File?": [[{"json": dict(
        exact, recording_web_url="https://t.sharepoint.com/Forms/DispForm.aspx?ID=1")}]]}
    [out] = v9.run_code(code(wf, "Finalize Recording URL"),
                        [{"json": {"link": {"webUrl": "https://t.sharepoint.com/Forms/DispForm.aspx?ID=2"}},
                          "pairedItem": {"item": 0}}], nodes)
    assert out["recording_url"] is None
    assert out["recording_link_status"] == "create_link_failed_no_update"
    [ok] = v9.run_code(code(wf, "Finalize Recording URL"),
                       [{"json": {"link": {"webUrl": "https://t.sharepoint.com/:v:/s/x"}},
                         "pairedItem": {"item": 0}}], nodes)
    assert ok["recording_url"] == "https://t.sharepoint.com/:v:/s/x"
    assert ok["recording_link_status"] == "organization_view_link_created_exact_match"


# ---------------------------------------------------------------------------
# 6. recording-owned columns only
# ---------------------------------------------------------------------------

QA_COLUMNS = ("met_count", "partial_count", "not_met_count", "Engagement", "engagement",
              "strengths", "areas_for_development", "overall_judgement",
              "teaching_quality_rating", "teaching_quality_comments", "ksb_coverage",
              "duration_score", "engagement_score", "lms_students", "attended_count",
              "cancelled_session", "trainer", "checklist", "lecture_qa_legacy_writes")


def _set_clauses(sql):
    return re.findall(r"^\s{4}(\w+)\s*=", sql, re.M)


def test_the_update_writes_only_recording_owned_columns(wf):
    sql = v9.node(wf, v9.UPDATE_NODE)["parameters"]["query"]
    session_sql, perfect_sql = sql.split("perfect_update AS")
    assert _set_clauses(session_sql.split("session_update AS")[1]) == [
        "recording_url", "recording_item_id", "recording_drive_id", "recording_filename",
        "recording_link_status", "recording_link_updated_at"]
    assert _set_clauses(perfect_sql) == ["recording_url", "meeting_id", "session_id"]
    assert "COALESCE(p.meeting_id, i.meeting_id)" in perfect_sql
    assert "COALESCE(p.session_id, i.session_id)" in perfect_sql
    for column in QA_COLUMNS:
        assert not re.search(rf"\b{column}\b", sql), column


def test_an_existing_recording_url_is_never_overwritten(wf):
    sql = v9.node(wf, v9.UPDATE_NODE)["parameters"]["query"]
    session_sql, perfect_sql = sql.split("perfect_update AS")
    assert "AND NULLIF(BTRIM(s.recording_url), '') IS NULL" in session_sql
    assert "AND NULLIF(BTRIM(p.recording_url), '') IS NULL" in perfect_sql
    assert "COALESCE(i.recording_url" not in sql
