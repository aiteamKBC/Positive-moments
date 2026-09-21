# Phase 2A — raw transcript acquisition and artifact registry

Status: **PHASE_2A_RAW_TRANSCRIPTS_READY**

Validation date: 2026-09-16. Target Cairo business date: 2026-09-04.

Phase 2A builds the central raw transcript acquisition layer and stops there.
No transcript was selected, combined, re-timed, or parsed; no QA, AI, Positive
Clips, Lecture Split, media job, FFmpeg, SharePoint upload, scheduler, or n8n
change occurred.

## Pipeline

```
public.lecture_sessions            (read-only Phase 1 input, downstream_ready)
  -> Microsoft Graph v1.0 transcript listing, in the persisted user context
  -> public.lecture_transcript_artifacts           (one row per artifact)
  -> public.lecture_transcript_artifact_contents   (raw bytes + SHA-256)
  -> public.lecture_transcript_acquisition_runs    (audit)
```

## The central finding: series scope

`GET /users/{userId}/onlineMeetings/{meetingId}/transcripts` returns the
transcripts of **every occurrence of the recurring series**, because a canonical
lecture's `meeting_id` is the series onlineMeeting. For 2026-09-04 the seven
downstream-ready lectures returned **79 artifacts**, of which only 7 belong to
that date.

This shaped two deliberate decisions:

- `lecture_transcript_artifacts.lecture_id` records **which canonical lecture's
  meeting exposed the artifact**. It is discovery linkage, not a claim that the
  transcript belongs to that occurrence. Occurrence attribution is Phase 2B.
- Artifact identity includes the lecture, so the same provider transcript
  reachable from two lectures stays two honest, separately-audited rows.

## Artifact identity strategy

```
artifact_id = uuidv5(TRANSCRIPT_ARTIFACT_NAMESPACE,
                     "{provider}\0transcript:{provider_transcript_id}\0lecture:{lecture_id}")
```

enforced by `UNIQUE (provider, provider_transcript_id, lecture_id)`.

`provider_transcript_id` alone is the provider's artifact identity; the lecture
is included for the series reason above. The identity is deterministic, so
re-running acquisition updates the existing row instead of creating a new one —
proven by the second run (79 discovered, **0 created**, 79 existing).

## Content versioning strategy

The simplest robust design: one append-only companion table keyed by content
hash.

- Compute SHA-256 over the exact provider bytes.
- If `(artifact_id, content_sha256)` already exists, reuse that version and only
  touch `last_fetched_at`. **No duplicate evidence.**
- If the hash differs, insert `version_number = max + 1`. Earlier versions are
  never updated or deleted, so changed provider content can never silently
  destroy the original evidence.
- `lecture_transcript_artifacts` carries a denormalised pointer to the current
  version, guarded by a CHECK that a content pointer is either wholly absent or
  complete and hash-shaped.

Raw content lives in a dedicated `raw_content text` column, never in metadata
JSON. Metadata JSON holds structured provider/audit fields only; a database
check confirmed **0 rows** contain transcript text in metadata.

## Graph user context

Every call used the Graph object ID Phase 1 persisted as
`lecture_sessions.meeting_lookup_user_id` (`737679b4-…` for all seven lectures,
context source `DISCOVERY_MAILBOX_OBJECT_ID`, Oid hint
`JOIN_URL_OID_MATCHES_CONTEXT`). No `/me`, no rederivation from
`calendar_organizer_address` (a Unified Group cohort mailbox), and no
re-parsing of the JoinWebUrl Oid. The existing app-only client-credentials
client was reused unchanged, with its in-memory refresh-before-expiry token
cache; no second authentication system was built.

## Error taxonomy

A successful but empty Graph collection is `NO_TRANSCRIPTS_AVAILABLE` and is
never reported as a failure. Distinguished separately:
`TRANSCRIPT_NOT_READY`, `TRANSCRIPT_NOT_FOUND`,
`GRAPH_ACCESS_TO_TRANSCRIPTS_DISABLED`, `SPEAKER_ATTRIBUTION_NOT_ALLOWED`,
`GRAPH_PERMISSION_ERROR`, `APPLICATION_ACCESS_POLICY_ERROR`, `PROVIDER_ERROR`,
`CONTENT_FETCH_ERROR`.

## Speaker attribution

Content is requested as `…/content?$format=text/vtt` with `Accept: text/vtt`.
Attribution is then decided **by inspecting the returned bytes** for `<v …>`
voice spans, never by what was requested, so unattributed content can never be
recorded as attributed.

If and only if the request fails specifically with a speaker-attribution
denial, one controlled fallback re-requests the same verified `/content`
endpoint without the `$format` override, and the result is stored with
`speaker_attribution = false` and status
`CONTENT_STORED_NO_SPEAKER_ATTRIBUTION`. No undocumented media type was
invented. In this tenant the fallback never fired: all 79 artifacts returned
speaker-attributed `text/vtt`.

## Real results for 2026-09-04

| Metric | Dry run | First persistence run | Second run |
| --- | ---: | ---: | ---: |
| Canonical lectures | 8 | 8 | 8 |
| Downstream-ready | 7 | 7 | 7 |
| Skipped unresolved | 1 | 1 | 1 |
| Meetings queried | 7 | 7 | 7 |
| Meetings with zero artifacts | 0 | 0 | 0 |
| Meetings with one artifact | 0 | 0 | 0 |
| Meetings with multiple artifacts | 7 | 7 | 7 |
| Artifacts discovered | 79 | 79 | 79 |
| Artifacts created | — | 79 | **0** |
| Artifacts reused | — | 0 | 79 |
| Contents fetched | 79 | 79 | 79 |
| Content versions created | — | 79 | **0** |
| Content unchanged | — | 0 | 79 |
| Speaker-attributed | 79 | 79 | 79 |
| Unattributed fallback | 0 | 0 | 0 |
| Transcript access blocked | 0 | 0 | 0 |
| Errors | 0 | 0 | 0 |
| Legacy QA parity | 6/6 | 6/6 | 6/6 |

Artifacts per lecture: 2, 2, 12, 14, 14, 17, 18. Stored content: 8,913,441
bytes across 79 versions, every one `text/vtt` and speaker-attributed.

The skipped lecture is the unresolved 2026-09-04 MSP occurrence
(`downstream_ready = false`, no `meeting_id`). It was skipped explicitly and
reported, never silently dropped.

### Idempotency proof

Second run: artifact IDs identical (79/79), content hashes identical, 0 changed
hashes, 0 new content versions, 0 artifacts created. Database after both runs:
79 artifacts, 79 distinct artifact IDs, 79 distinct provider transcript IDs,
max `content_version` = 1, all `CONTENT_STORED`.

## Legacy QA session_id parity

The legacy QA flow stored the **selected** transcript artifact ID in
`qa_doctors_sessions.session_id`. That identity design is deliberately not
adopted. Measured read-only:

| Legacy QA lecture | session_id among discovered artifacts | Artifacts discovered |
| --- | :---: | ---: |
| Femi-Commercial Intelligence-Oct 25 | YES | 12 |
| G2-Juliane -Impact and Planning June 2026 | YES | 14 |
| G3 - Femi - Customer Journey Optimisation | YES | 14 |
| Project Planning & Control (PPC) \| Andrew | YES | 17 |
| Ray-Managing Successful Programmes (MSP) Jan 2026 | YES | 2 |
| Ray-Project Management Office (PMO) | YES | 18 |

**6 / 6 legacy QA session IDs reproduced.** `qa_doctors_sessions` was read
only and not modified.

"AI in Project Control 2026" has no legacy QA row, as expected from Phase 1. Its
two artifacts were acquired normally and no QA row was created for it.

## Security

No token, Authorization header, client secret, connection string, or transcript
text is logged or persisted outside the dedicated `raw_content` column. Log
records and run summaries carry only run/lecture/meeting/transcript IDs,
statuses, byte counts, a 12-character hash prefix, durations, and error codes. A
regression test asserts that neither transcript text nor `Bearer` appears in any
log record or in the returned summary.

## Tables

Written: `public.lecture_transcript_artifacts`,
`public.lecture_transcript_artifact_contents`,
`public.lecture_transcript_acquisition_runs` (all created by migration 004).

Read-only: `public.lecture_sessions`, `public.qa_doctors_sessions`.

Verified unchanged against pre-run baselines: `lecture_sessions` (8 rows, and
`max(updated_at)` still `2026-09-16 07:50:26.240401+00:00`),
`qa_doctors_sessions` (650), `qa_doctors_checklist_items` (7523),
`qa_perfect_lectures` (140), `qa_doctors_transcripts` (0),
`qa_media_jobs` (4), `qa_positive_clip_assets` (17),
`qa_lecture_split_plans` (1), `qa_lecture_part_assets` (0),
`kbc_attendance` (17015), `kbc_users_data` (686),
`aptem_auto_extracting` (686).

## Phase 2B entry point

Implement selection in a new `app/transcripts/selection.py` reading **only** the
persisted Phase 2A artifacts — no Graph calls are needed, because the evidence
is stored and hash-verified. First milestone: reproduce all 6/6 legacy
`session_id` values for 2026-09-04 by scheduled-window overlap, as a parity
test. Before porting tie-breakers or multi-part combination, obtain a sanitized
export of the legacy QA One Lecture workflow — it is not in this repository —
and validate those rules on a date that actually exercises them. See
[phase2_transcript_selection_reference.md](phase2_transcript_selection_reference.md).
