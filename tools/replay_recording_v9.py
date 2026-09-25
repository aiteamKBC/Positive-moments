"""
Replay the v9 recording branch offline against a saved n8n execution.

Inputs (both produced read-only; neither is committed - they hold production
identifiers):
  --execution  an n8n execution fetched with GET /api/v1/executions/{id}
               ?includeData=true for QA Master Daily v8 (8yeJigbj8BCNBMkU);
  --lectures   the v9 `Get Target Lectures` rows for the same lectures, as JSON.

What is replayed, with the v9 export's own code:
  Validate Run Settings -> Extract Exact Call IDs -> Graph Lookup Possible?
  -> Collect Lectures and Exact Graph Times -> Match Every Lecture Safely
  -> Write Mode Armed? -> Dry Run Results / Not Evaluated.

The Graph recordings response for a lecture is the one v8 recorded. A
recordings list is the same whichever mailbox route fetched it, so a v8 200 is
reused as-is. A v8 HTTP failure (the /me route cannot see meetings the n8n
user did not organize) is replayed as that failure and reported as
PENDING_ORGANIZER_LOOKUP: v9 would call /users/{organizer}/... instead, and
only a live run with the app-only credential can answer it.

The recording files are the tenant-search, channel-folder and OneDrive
listings the same execution collected. Nothing here calls n8n, Graph or a
database, and nothing writes.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

from tools import recording_v9 as v9


def _outputs(run_data, name):
    items = []
    for run in run_data.get(name) or []:
        for branch in (run.get("data") or {}).get("main") or []:
            items.extend(branch or [])
    return items


def replay(execution: dict, lectures: list[dict], *, date_from: str, date_to: str,
           max_lectures: int = 500) -> dict:
    workflow = v9.load_v9()
    code = lambda name: v9.node(workflow, name)["parameters"]["jsCode"]   # noqa: E731
    run_data = execution["data"]["resultData"]["runData"]

    settings = v9.run_code(code("Validate Run Settings"), v9.as_items([{
        "dry_run": True, "date_from": date_from, "date_to": date_to,
        "max_lectures": max_lectures}]))
    settings_nodes = {"Validate Run Settings": [v9.as_items(settings)]}
    in_range = [row for row in lectures if date_from <= row["lecture_date"] <= date_to]

    extracted = v9.run_code(code("Extract Exact Call IDs"), v9.as_items(in_range),
                            settings_nodes)
    gate = v9.node(workflow, "Graph Lookup Possible?")["parameters"]["conditions"]["conditions"][0]
    lookups = [row for row in extracted if v9.evaluate(gate["leftValue"], row) is True]
    skipped = [row for row in extracted if row not in lookups]

    recorded = {}
    for lecture, response in zip(_outputs(run_data, "Extract Exact Call IDs"),
                                 _outputs(run_data, "Get Meeting Recordings by Meeting ID")):
        recorded[lecture["json"]["session_id"]] = response["json"]
    missing = [row["session_id"] for row in lookups if row["session_id"] not in recorded]
    if missing:
        raise SystemExit(f"{len(missing)} lectures have no recorded Graph response")
    responses = [recorded[row["session_id"]] for row in lookups]

    matched = []
    if lookups:
        [collected] = v9.run_code(code("Collect Lectures and Exact Graph Times"),
                                  v9.as_items(responses),
                                  {"Graph Lookup Possible?": [v9.as_items(lookups)]})
        onedrive = [item["json"] for item in _outputs(run_data, "List Current User OneDrive Recordings")]
        matched = v9.run_code(code("Match Every Lecture Safely"), v9.as_items(onedrive), {
            "Collect Lectures and Exact Graph Times": [v9.as_items([collected])],
            "Collect Tenant Search Candidates": [
                [_outputs(run_data, "Collect Tenant Search Candidates")[0]]],
            "Collect All Channel Recording Files": [
                [_outputs(run_data, "Collect All Channel Recording Files")[0]]],
        })
        armed = v9.node(workflow, "Write Mode Armed?")["parameters"]["conditions"]["conditions"][0]
        if any(v9.evaluate(armed["leftValue"], row, settings_nodes) for row in matched):
            raise SystemExit("write gate opened during a dry-run replay")
        matched = v9.run_code(code("Dry Run Results - REVIEW THIS"), v9.as_items(matched))
    not_evaluated = v9.run_code(code("Not Evaluated - Never Update DB"), v9.as_items(skipped))

    rows = []
    for row in matched + not_evaluated:
        status = row["recording_link_status"]
        pending = (status == "GRAPH_LOOKUP_FAILED" and
                   row.get("graph_http_status") in (401, 403, 404))
        rows.append({
            "date": row["lecture_date"], "subject": row["subject"],
            "status": "PENDING_ORGANIZER_LOOKUP" if pending else status,
            "v9_status": status, "graph_http_status": row.get("graph_http_status"),
            "timestamp_difference_seconds": row.get("timestamp_difference_seconds"),
            "nearest_same_subject_lead_seconds": row.get("nearest_same_subject_lead_seconds"),
            "would_update_session": status == v9.MATCHED_STATUS,
            "would_update_perfect": (status == v9.MATCHED_STATUS and bool(row.get("lecture_key"))
                                     and bool(row.get("perfect_recording_url_empty"))),
            "call_id": (row.get("expected_call_id") or "")[:8],
        })
    rows.sort(key=lambda r: (r["date"], r["subject"]))
    counts = collections.Counter(r["status"] for r in rows)
    return {"date_from": date_from, "date_to": date_to, "dry_run": settings[0]["dry_run"],
            "lectures": len(rows), "counts": dict(sorted(counts.items())),
            "would_update_sessions": sum(r["would_update_session"] for r in rows),
            "would_update_perfect": sum(r["would_update_perfect"] for r in rows),
            "rows": rows}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline dry-run replay of recording v9")
    parser.add_argument("--execution", required=True, type=pathlib.Path)
    parser.add_argument("--lectures", required=True, type=pathlib.Path)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args(argv)
    report = replay(json.loads(args.execution.read_text(encoding="utf-8")),
                    json.loads(args.lectures.read_text(encoding="utf-8")),
                    date_from=args.date_from, date_to=args.date_to)
    if args.summary_only:
        report.pop("rows")
    sys.stdout.reconfigure(encoding="utf-8")
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
