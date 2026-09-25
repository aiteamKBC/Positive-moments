# Recording links v9 (reference only)

> **Superseded by the coded `RECORDING_LINK` stage** (`app/recordings/`,
> wired through the existing orchestrator, scheduler and backfill). This n8n
> export is kept as migration evidence and as the specification the Python
> stage was ported from. Do not import or deploy it.


`../QA_Master_Daily_Safe_Exact_Recording_v9.json` is **generated**. Edit the
files here, then run:

```text
python -m tools.recording_v9 build     # regenerate the export
python -m tools.recording_v9 check     # fail if the export is stale
```

It is derived from the v8 production export (`../QA Master Daily — Safe Exact
Recording v8`, left unchanged) and changes only the recording branch. It is
exported **inactive**, with `Schedule Trigger` and `Execute QA One Lecture`
both disabled.

## What changed from v8

| Gap in v8 (audit, 2026-09-25) | v9 |
|---|---|
| `GET /me/onlineMeetings/{id}/recordings` fails with 403/404 when the n8n user did not organize the meeting (43 of 50 lectures) | `GET /users/{meeting_lookup_user_id}/onlineMeetings/{id}/recordings`, with the organizer taken from `lecture_sessions`, using an **app-only** credential |
| HTTP failures were reported as "recording not found" | `GRAPH_LOOKUP_FAILED` (with `graph_http_status`). `RECORDING_NOT_FOUND` only for a successful, complete response |
| The file timestamp had to be within ±15 s of Graph `createdDateTime`, which rejected 5 correct matches (17–73 s early) | The file may **lead** Graph by 0–120 s and may never trail it. The file must also be named for the lecture's own date |
| Call id came from plain base64 plus a regex, which fails on LZ4 back-referenced ids | A port of the QA Core RC4 decoder (`app/transcripts/identity.py`). It agrees on 693/693 production ids |
| `dry_run: false` was hard-coded in a Code node | A Set node: `dry_run` (default **true**), `date_from`, `date_to`, `max_lectures` |

Unchanged: the exact call-id link, exact normalized subject, exactly one
candidate, "ambiguous ⇒ no update", and the empty-`recording_url` guard.

## Microsoft Graph permissions

The **Get Organizer Meeting Recordings** node needs a separate n8n OAuth2
credential that uses the client-credentials grant, with scope
`https://graph.microsoft.com/.default`. The coded platform's app registration
(`MICROSOFT_GRAPH_CLIENT_ID`) already reads `/users/{u}/onlineMeetings/...`
with this route. The export carries a placeholder credential id, so n8n will
refuse to run the node until you choose one.

Per [List recordings](https://learn.microsoft.com/graph/api/onlinemeeting-list-recordings):

- Application permission **`OnlineMeetingRecording.Read.All`**, with admin consent.
- A Teams **application access policy** that grants the app access for each
  organizer user id in the request path (`New-CsApplicationAccessPolicy` and
  `Grant-CsApplicationAccessPolicy`). Without it, Graph returns 403/404, which
  v9 reports as `GRAPH_LOOKUP_FAILED`.
- The API only works for meetings that have not expired.

Every other node keeps its v8 **delegated** credential and permissions: tenant
search, joined teams/channels, channel `Recordings` folders, `/me/drive`, and
`createLink`. `createLink` is a write, a sharing link, and runs only on the
armed write path.

## Statuses (never written unless marked)

| Status | Meaning |
|---|---|
| `NO_RECORDING_EXPECTED_CANCELLED` | `cancelled_session = 'true'`. Not a defect. Not looked up |
| `ORGANIZER_LOOKUP_ID_MISSING` / `ORGANIZER_LOOKUP_ID_AMBIGUOUS` | No organizer in `lecture_sessions`, or more than one |
| `SESSION_CALL_ID_MISSING` | The session id does not decode to a `…-TranscriptV2` transcript id |
| `GRAPH_LOOKUP_FAILED` | HTTP/auth/access failure, or a malformed response |
| `GRAPH_RESULTS_INCOMPLETE` | Graph paged the results, so a second recording could be hidden |
| `RECORDING_NOT_FOUND` | Graph answered and has no recording for the call id |
| `AMBIGUOUS_GRAPH_RECORDINGS` | More than one recording for the call id |
| `TIMESTAMP_MISMATCH` / `SUBJECT_MISMATCH` / `RECORDING_FILE_NOT_FOUND` | No file passes every rule |
| `AMBIGUOUS_RECORDING_FILES` | More than one file passes every rule |
| `EXACT_RECORDING_FILE_MATCHED` | Exactly one. **Written only when armed**, as `organization_view_link_created_exact_match` or `drive_item_web_url_used_exact_match` |

## The write path

The **Update Both Recording Tables** node, and the `createLink` POST before
it, are reachable only when:

1. `Write Mode Armed?` finds `dry_run === false` on the lecture **and** in
   `Validate Run Settings`. Only a literal boolean `false` disarms dry run.
2. `Exactly One Safe File?` finds `EXACT_RECORDING_FILE_MATCHED`, item and
   drive ids present, and method `exact_call_id_subject_timestamp_v9`.
3. The SQL repeats these checks: `dry_run IS FALSE`, the v9 method, an exact
   link status, the `meeting_id` match, and an empty `recording_url`.

It writes `recording_url`, `recording_item_id`, `recording_drive_id`,
`recording_filename`, `recording_link_status` and `recording_link_updated_at`.
On `qa_perfect_lectures` it writes `recording_url`, plus `meeting_id` /
`session_id` only when they are NULL, and only if the session row was updated
in the same statement. No QA column is referenced.

## Offline replay

```text
python -m tools.replay_recording_v9 --execution exec.json --lectures lectures.json \
    --date-from 2026-09-01 --date-to 2026-09-30 --summary-only
```

`exec.json` is a read-only `GET /api/v1/executions/{id}?includeData=true` of
the v8 Master. `lectures.json` is this export's `Get Target Lectures` result
for the same lectures. Both contain production identifiers, so keep them out
of the repository.
