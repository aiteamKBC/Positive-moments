# Phase 1 final hardening

Status: **PHASE_1_FINAL_READY**

Hardening date: 2026-09-16. Target Cairo business date: 2026-09-04.

This was a corrective pass over validated Phase 1 real discovery. No transcript,
QA, AI, Positive Clips, Lecture Parts, media, FFmpeg, SharePoint, scheduler or
n8n behaviour was invoked or changed.

## 1. Corrected registry semantics

`public.lecture_sessions` is the canonical KBC **lecture** registry, not a
mirror of the Teams calendar. A row exists only for a calendar occurrence that
is all of:

- a non-cancelled calendar occurrence,
- carrying a Teams JoinWebUrl, and
- whose normalized subject **exactly** matches an active Aptem group.

Everything else is audit evidence. Non-matching Teams events are counted in
`lecture_discovery_runs` (`unmatched_count`, `non_canonical_calendar_events`)
and described in the run's `metadata.non_canonical_calendar_events`, and they
never become registry rows. Nothing is hardcoded: the canonical count is
whatever the active-Aptem match yields for the day.

The rule is enforced in three places, so no single mistake can reintroduce
calendar rows:

| Layer | Enforcement |
| --- | --- |
| `LectureDiscoveryService` | a non-matching occurrence is skipped before any Graph call or persistence |
| `LectureSessionRepository.upsert` | raises before touching the database if `group_match_status <> 'MATCHED'` |
| `lecture_sessions_canonical_scope` CHECK | the database rejects the row outright |

Because unmatched events no longer persist, `discovery_status` is now only
`READY` or `REVIEW`; the `UNMATCHED` value is gone from the CHECK.

## 2. Rows removed from the shadow registry

`app/cli/cleanup_shadow_registry.py` reports before it writes and requires
`--apply` to commit. It touches only `public.lecture_sessions`, only rows whose
`group_match_status <> 'MATCHED'`, only rows with `meeting_id IS NULL`, and only
after proving nothing references them.

Preflight evidence, all confirmed before deleting anything:

- Foreign keys referencing `public.lecture_sessions`: **none**.
- Columns named `lecture_id` anywhere else in `public`: **none**.
- Every `uuid`/`text`/`varchar` column of every base table in `public` scanned
  for the 11 IDs, QA and media tables included: **zero references**.
- Non-canonical rows carrying a meeting ID: **none**.

Deleted: **11 rows**, all `NO_ACTIVE_GROUP_MATCH` / `NOT_ATTEMPTED` /
`meeting_id IS NULL`.

| Session date | Subject |
| --- | --- |
| 2026-09-03 | Ai team - Kent business college |
| 2026-09-04 | Ai team - Kent business college |
| 2026-09-04 | Aya  Modual |
| 2026-09-04 | EPA – Marketing Executive Cohort \| May 2025 |
| 2026-09-04 | M1 |
| 2026-09-04 | Staregy & planning |
| 2026-09-04 | Test Module |
| 2026-09-04 | hhj |
| 2026-09-04 | EPA Readiness |
| 2026-09-04 | test kalidou & abo ali |
| 2026-09-04 | Weekly Meeting |

Registry rows went 19 -> 8. The 8 surviving canonical rows kept their existing
`lecture_id` values; nothing was recreated with a new identity.

## 3. Business date investigation

One occurrence discovered inside the Cairo 2026-09-04 window carried
`session_date = 2026-09-03`. Full trace from Graph:

| Step | Value |
| --- | --- |
| queried Cairo window (UTC) | `2026-09-03T21:00:00Z` .. `2026-09-04T21:00:00Z` |
| `start.dateTime` | `2026-09-03T00:00:00.0000000` |
| `start.timeZone` | `UTC` |
| `end.dateTime` | `2026-09-04T00:00:00.0000000` |
| `isAllDay` | `true` |
| parsed aware datetime | `2026-09-03T00:00:00+00:00` |
| stored `scheduled_start` | `2026-09-03 00:00:00+00` |
| derived Cairo datetime | `2026-09-03T03:00:00+03:00` |
| derived `session_date` | `2026-09-03` |

**There is no timezone bug.** `calendarView` returns occurrences that *overlap*
the requested window. This is an all-day occurrence running
`2026-09-03T00:00Z` -> `2026-09-04T00:00Z`, which overlaps the window that opens
at `2026-09-03T21:00Z` but begins on the previous Cairo day. `2026-09-03` is the
correct business date and was retained, not patched.

The canonical rule is unchanged and now stated once, in
`app/common/time.cairo_business_date`:

```
session_date = timezone-aware scheduled_start -> Africa/Cairo -> .date()
```

Never from the UTC date directly, never from a naive datetime, never from raw
string slicing. `require_aware` makes a naive input raise rather than silently
produce a wrong date. `business_date_trace()` records the derivation on every
row under `metadata.business_date`, so any future date question is answerable
from the row itself.

(The occurrence above is also non-canonical — "Ai team - Kent business college"
matches no active Aptem group — so it left the registry with the other ten.)

## 4. Meeting user context strategy

`/users/{userId}/onlineMeetings` needs a *user* object ID, and the calendar
event's organizer is a Unified Group cohort mailbox, which is not one.

- **PRIMARY, the production dependency:** the configured discovery mailbox's
  Graph object ID, resolved once from `KBC_LECTURE_CALENDAR_USER_UPN` via
  `/users/{upn}?$select=id` and cached for the run.
- **SECONDARY, diagnostic only:** the `Oid` embedded in the JoinWebUrl
  `context` parameter. Microsoft documents the joinWebUrl format as internal
  and subject to change and tells clients not to rely on information extracted
  from it, so the application must not depend on it exclusively — and now does
  not. It is recorded as a hint status
  (`JOIN_URL_OID_ABSENT` / `MATCHES_CONTEXT` / `DIFFERS_FROM_CONTEXT`), used to
  explain mismatches, and used as a **guarded fallback** that fires only when
  the primary context found no meeting *and* the hint names a different user.
  `allow_join_url_oid_fallback=False` disables it entirely; discovery still
  works when no Oid is present at all.

Validated tenant behaviour for 2026-09-04: all 8 candidates resolved (or failed)
in `DISCOVERY_MAILBOX_OBJECT_ID` context, and all 8 hints reported
`JOIN_URL_OID_MATCHES_CONTEXT` — the JoinWebUrl Oid currently equals the
discovery mailbox object ID `737679b4-…`. The fallback did not fire.

A further correction: Graph answers an unmatched JoinWebUrl `$filter` with
**HTTP 404 `3004: Meeting properties are not found`**, not an empty collection.
The previous code read any 404 as a missing *user* and reported
`ORGANIZER_NOT_RESOLVABLE`. Since the user context is resolved from the
directory before the query, a 404 there means no such meeting, so it is now
classified `ONLINE_MEETING_NOT_FOUND`. `ORGANIZER_NOT_RESOLVABLE` is retained
for a genuinely unresolvable user context.

## 5. Organizer validation strategy

The old comparison — calendar organizer address vs. onlineMeeting organizer —
was structurally meaningless in this tenant and has been removed. The calendar
organizer is a Microsoft 365 Unified Group / Team cohort mailbox, never the
Teams meeting organizer user.

Validation now runs after onlineMeeting resolution and compares
`onlineMeeting.participants.organizer.identity.user.id` to the Graph user
context the lookup ran in (and, as a secondary check when safely available, to
the parsed JoinWebUrl Oid):

| Status | Meaning |
| --- | --- |
| `ORGANIZER_ID_CONFIRMED` | the meeting's organizer user ID is the context user |
| `ORGANIZER_ID_DIFFERENT` | it is some other user — surfaced, never silently accepted |
| `ORGANIZER_ID_UNAVAILABLE` | Graph returned no organizer identity |
| `NOT_ATTEMPTED` | no meeting resolved, so there was nothing to validate |

The cohort mailbox is preserved separately as calendar metadata in
`calendar_organizer_address` (renamed from the misleading `organizer_upn`), and
`organizer_object_id` became `meeting_organizer_user_id`. Legacy
`ORGANIZER_MATCH` / `ORGANIZER_MISMATCH` / `ORGANIZER_UNVERIFIED` values were
cleared to NULL by migration 003 rather than translated, because they describe a
check that no longer exists; the next run recomputed them.

## 6. Duplicate MSP occurrence

The two 11:00 UTC MSP occurrences are **not** deduplicated. There is no
subject+start merge rule anywhere in the pipeline; occurrence identity stays
UUIDv5 over the calendar owner and `iCalUId`.

| | Resolved occurrence | Unresolved occurrence |
| --- | --- | --- |
| subject | `Ray-Managing Successful Programmes (MSP) Jan 2026` | same + trailing spaces |
| calendar `type` | `occurrence` | `exception` |
| `iCalUId` | `…FE9765E0532A2F4EB0A5AFFCA261897C` | `…68917E6BDC6F6648A08B55B1CB6BF53D` |
| cohort organizer | `Ray-ManagingSuccessfulProgrammesMSPJan2026@…` | `G2-PCPFeb26Friday@…` |
| JoinWebUrl | distinct | distinct |
| outcome | `RESOLVED`, `downstream_ready = true` | `ONLINE_MEETING_NOT_FOUND`, `downstream_ready = false` |

Both remain distinct canonical lectures. The unresolved one is retained, not
deleted or merged, and is excluded from downstream processing by the explicit
`downstream_ready` column rather than by guesswork. A
`lecture_sessions_downstream_ready` CHECK makes `downstream_ready = true`
impossible without a `meeting_id`.

Its Graph 404 is `3004: Meeting properties are not found` against a user
context that resolves fine, so the honest classification is a missing meeting,
not a missing organizer. No attempt was made to guess which meeting it "should"
have pointed at.

## 7. AI in Project Control 2026

Preserved as a legitimate canonical lecture: it is a non-cancelled Teams
occurrence whose normalized subject exactly matches an active Aptem group, and
its onlineMeeting resolved with `ORGANIZER_ID_CONFIRMED`. Legacy QA has no row
for it, and **no QA row was created**. It is reported by the run as
`qa_comparison.normalized_subjects.discovered_but_absent_from_qa`, which is the
intended evidence that the new discovery layer finds lectures the legacy
pipeline missed.

## 8. Meeting context provenance

Persisted per row, with no tokens, no Authorization headers, and no
unnecessary personal data:

- `meeting_lookup_context_source` — `DISCOVERY_MAILBOX_OBJECT_ID` or `JOIN_URL_OID_FALLBACK`
- `meeting_lookup_user_id` — the Graph user object ID the lookup ran as
- `meeting_organizer_user_id` — the meeting's own organizer user ID
- `organizer_validation_status` — the comparison outcome
- `join_url_oid_hint_status` — the secondary hint's verdict (the raw Oid is not stored)
- `calendar_organizer_address` — cohort mailbox, calendar metadata only
- `metadata.business_date` — the auditable business-date derivation

## 9. Real validation: 2026-09-04

| Metric | First corrected run | Second corrected run |
| --- | ---: | ---: |
| Calendar events | 20 | 20 |
| Eligible Teams events | 19 | 19 |
| Active Aptem groups | 43 | 43 |
| Aptem-matched canonical candidates | 8 | 8 |
| Non-canonical calendar events (audit only) | 11 | 11 |
| Canonical registry rows for the date | 8 | 8 |
| Resolved meetings | 7 | 7 |
| Unresolved meetings | 1 | 1 |
| Downstream-ready lectures | 7 | 7 |
| Registry rows created | 0 | 0 |
| Registry rows updated | 8 | 8 |
| QA sessions | 6 | 6 |
| QA meeting-ID matches | 6/6 | 6/6 |
| QA subject matches | 6/6 | 6/6 |
| QA rows missing from new discovery | 0 | 0 |

Created is 0 on the first corrected run because the 8 canonical rows predate the
cleanup and kept their identities; only the 11 non-canonical rows were removed.

Idempotency, verified in the database after the second run: 8 rows total, 8
distinct `lecture_id`, 8 distinct `calendar_event_id`, 8 distinct `i_cal_uid`, 8
distinct `join_url`, 0 non-`MATCHED` rows, stable total.

Meeting status counts: `RESOLVED` 7, `ONLINE_MEETING_NOT_FOUND` 1.
Organizer validation: `ORGANIZER_ID_CONFIRMED` 7, `NOT_ATTEMPTED` 1.
Context source: `DISCOVERY_MAILBOX_OBJECT_ID` 8.
Oid hint: `JOIN_URL_OID_MATCHES_CONTEXT` 8.

## 10. Tests

`python -m pytest -q`: **76 passed**. New or updated coverage:

- unmatched Teams events are not canonical lectures, and never trigger a Graph lookup
- an Aptem-matched event is persisted with its match provenance
- the repository and the database each reject a non-canonical row
- business date UTC -> Africa/Cairo, including EET/EEST offsets
- midnight boundaries at 21:00/22:00 UTC, exact Cairo midnight, UTC midnight, a +05:30 offset
- an all-day occurrence overlapping the window keeps its own earlier Cairo date
- naive datetimes are rejected rather than silently dated
- the discovery mailbox object ID is the primary meeting context, resolved once and cached
- the cohort group address is never used as a user context
- a JoinWebUrl Oid is not required; a matching Oid is a confirming hint; a differing Oid does not redirect the lookup
- the guarded Oid fallback runs only after the primary finds nothing, and can be switched off
- Graph 404 on the filter means no meeting, not a missing user
- onlineMeeting organizer identity confirmed / different / unavailable
- duplicate subject+start occurrences stay distinct
- an unresolved occurrence is retained but not downstream-ready
- idempotency after canonical filtering

## 11. Files and tables

Modified: `app/graph/meetings.py`, `app/graph/calendar.py`, `app/lectures/models.py`,
`app/lectures/service.py`, `app/common/time.py`,
`app/db/repositories/lecture_sessions.py`, `tests/unit/test_service.py`,
`tests/unit/test_graph.py`, `tests/unit/test_time.py`,
`tests/integration/test_registry_repository.py`.
Added: `app/db/migrations/003_canonical_registry_and_meeting_context.sql`,
`app/cli/cleanup_shadow_registry.py`, `tests/unit/test_canonical_registry.py`,
`tests/unit/test_meeting_context.py`, this document.
Removed: `tests/unit/test_organizer_context.py` (superseded).

Tables written: `public.lecture_sessions` (11 non-canonical rows deleted, 8
canonical rows updated, schema altered by migration 003) and
`public.lecture_discovery_runs` (audit rows appended). Read-only:
`public.aptem_auto_extracting`, `public.qa_doctors_sessions`,
`information_schema`, plus the one-time reference scan.

Unchanged: every legacy production table, `qa_doctors_sessions` and all QA rows,
`qa_media_jobs`, `qa_positive_clip_assets`, `qa_lecture_part_assets`,
`qa_doctors_transcripts`, and the unrelated `qa_lecture_split_plans` row created
by another session.

## 12. Phase 2 entry point

Unchanged by this pass. Begin Phase 2 with an additive
`public.lecture_transcript_artifacts` table whose artifact identity is its own
and whose `lecture_id` foreign key references `public.lecture_sessions`.
Acquisition is shadow-only storage first; do not write `qa_doctors_sessions` or
reinterpret legacy `session_id`.

Phase 2 must additionally respect two Phase 1 outputs: select source lectures
with `downstream_ready = true` only, and treat `calendar_organizer_address` as
cohort metadata rather than a user identity.
