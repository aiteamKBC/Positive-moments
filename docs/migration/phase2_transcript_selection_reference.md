# Phase 2B reference: transcript selection and combination

**Status: VERIFIED against the authoritative legacy export.**

Reference file (sanitized, in-repo):
`automation/legacy_n8n/QA_One_Lecture_Safe_Exact_Recording_v8.json`

Authoritative nodes:

- `Select Transcript Parts` (code node, "Select Transcript Parts V3")
- `Combine Transcripts` (code node)

Every rule below was read from that export and ported in
[selection.py](../../app/transcripts/selection.py) and
[combine.py](../../app/transcripts/combine.py). Where this document and the
earlier Phase 2A notes disagreed, the export won. Nothing here is inferred any
more: the items previously marked TO CONFIRM are now **VERIFIED**.

## The problem selection solves

**VERIFIED by live data.** `GET /users/{userId}/onlineMeetings/{meetingId}/transcripts`
returns the transcripts of **every occurrence of the recurring series**. For
2026-09-04 the 7 downstream-ready lectures exposed **79 artifacts**, of which
exactly 7 belong to that date. Without selection, 91% of the evidence belongs to
other dates.

## 1. Time interpretation — VERIFIED, with one deliberate deviation

The legacy node claims `scheduledStart` / `scheduledEnd` arrive as UTC strings
*without* a trailing `Z`, and parses them accordingly:

```js
// QA Master sends UTC without trailing Z.
const ms = Date.parse(cleaned + 'Z');
```

**That claim is false against the QA Master export**, which requests the
calendar with `Prefer: outlook.timezone="Africa/Cairo"` and so passes naive
*Cairo wall-clock* strings. See the QA Master contract audit appendix below for
the evidence and the measured impact.

**Deviation, deliberate.** The new platform reads canonical `timestamptz` values
from `lecture_sessions`, so that parsing is **not** recreated — recreating it
would reintroduce a naive-time bug the new schema has already fixed. Every comparison runs on timezone-aware instants. Only the business-date
filter uses `Africa/Cairo`. The legacy node also throws when
`scheduledEnd <= scheduledStart`; the port raises `ValueError` identically.

## 2. Cairo-date filtering — VERIFIED

```js
const sameDay = transcripts.filter(t => {
  if (meetingId && t.meetingId && t.meetingId !== meetingId) return false;
  return getCairoDate(t.createdDateTime) === targetDate;
});
```

- The transcript's `createdDateTime`, converted to **Africa/Cairo**, must equal
  the lecture's target business date.
- The meeting guard fires **only when both sides carry a meeting ID**. A blank
  on either side does not reject.

## 3. Occurrence window — VERIFIED

```js
const WINDOW_BEFORE_MS = 2 * 60 * 60 * 1000;
const WINDOW_AFTER_MS  = 3 * 60 * 60 * 1000;
return endMs >= scheduledStartMs - WINDOW_BEFORE_MS
    && startMs <= scheduledEndMs + WINDOW_AFTER_MS;
```

**2 hours before the scheduled start, 3 hours after the scheduled end**, both
boundaries inclusive. Ported as the named constants `WINDOW_BEFORE` and
`WINDOW_AFTER`, with boundary tests on each edge and one millisecond past it.
A missing `endDateTime` falls back to `createdDateTime`.

## 4. Primary ranking — VERIFIED

```js
if (b.overlapMs   !== a.overlapMs)   return b.overlapMs - a.overlapMs;   // 1
if (b.durationMs  !== a.durationMs)  return b.durationMs - a.durationMs; // 2
return a.startDistanceMs - b.startDistanceMs;                            // 3
```

1. greatest overlap with the scheduled interval,
   `max(0, min(tEnd, sEnd) - max(tStart, sStart))`
2. then greatest transcript duration
3. then smallest `|transcript start - scheduled start|`

Never earliest, never longest alone, never provider list order.

**Perfect-tie behaviour — VERIFIED.** Both legacy sorts are stable, so a full
three-way tie falls back to list order. The port keeps that rather than adding a
fourth tiebreaker, which would diverge from legacy on exactly the ambiguous
cases parity exists to pin down. Determinism comes from the input instead: the
repository loads candidates `ORDER BY provider_created_at, provider_transcript_id`.

**Duration clamping — VERIFIED.** When `endDateTime` is missing or earlier than
the start, the end becomes the start, so duration is `0`, never negative.

## 5. Part attachment — VERIFIED

```js
const MAX_PART_GAP_MS = 20 * 60 * 1000;
if (hasSameCallId || isTemporallyConnected) { ... }
```

A candidate joins the cluster when **either**:

- it carries a **non-empty `callId` matching any already-selected part** (this
  holds at any distance), **or**
- its gap from the current cluster is **≤ 20 minutes** (overlapping counts as 0).

**Iterative — VERIFIED.** The legacy loop is `while (addedNewPart)`, rescanning
all candidates after every addition and widening `clusterStart`/`clusterEnd`, so
attaching part B can bring part C into range. A single pass would be wrong. The
port reproduces the loop and is tested with a C that is 55 minutes from the
primary and only reachable through B.

Microsoft can mint a new `callId` when transcription stops and restarts, which
is precisely why `callId` alone is not sufficient.

## 6. Part order and identity — VERIFIED

Selected parts are sorted chronologically by provider created time. The legacy
node emits `transcriptId` (the **primary**, stable session identity, repeated on
every row) separately from `transcriptPartId` (the individual part). The port
keeps both: `primary_provider_transcript_id` on the selection, and one row per
part in `lecture_transcript_selection_parts` with `part_index` and `is_primary`.

Session timing: `actual_start = min(part starts)`, `actual_end = max(part ends)`;
`start_difference_minutes` / `end_difference_minutes` use **JavaScript
`Math.round` semantics** (half rounds toward +∞, including for negatives), which
Python's banker's rounding would get wrong on exact half-minutes.

**`qa_doctors_sessions.session_id` is legacy compatibility only.** It is
compared, never written, and never adopted as an identity.

## 7. VTT combination — VERIFIED

```js
const offsetMs = part.partCreatedMs - sessionStartMs;
const combinedBody = combinedSections.filter(Boolean).join('\n\n');
const transcriptText = `WEBVTT\n\n${combinedBody}`.trim();
```

1. drop parts with empty text or an unparseable created time (counted as failed)
2. sort by real Microsoft `createdDateTime`
3. `session_start` = earliest selected part's created time
4. strip each part's own `WEBVTT` header
   (`/^﻿?\s*WEBVTT[^\r\n]*(?:\r?\n)+/i`, then trim)
5. `offset = part.createdDateTime - session_start`
6. shift every cue start and end by that offset
7. concatenate chronologically, joined by a blank line
8. emit exactly one `WEBVTT` header
9. scan the combined cue range
10. `duration_minutes = round((max - min) / 60000)`, and a non-positive result
    is an error, not a silent zero

**VTT safety — VERIFIED.** The legacy timing regex is
`(\d{2,}):(\d{2}):(\d{2})[.,](\d{3})` on both sides of the arrow, so hours may
exceed 23 and either `.` or `,` is accepted; output is normalized to `.` with
millisecond precision and zero-padded hours that are never wrapped. Speaker text
and cue count are untouched.

## 8. Real 2026-09-04 outcome

Selection ran entirely from persisted Phase 2A evidence, with **zero Graph
calls**, and reproduced the legacy choice for every lecture that has one:

| Lecture | Candidates | Same-day | In window | Parts | Legacy match |
| --- | ---: | ---: | ---: | ---: | :---: |
| AI in Project Control 2026 | 2 | 1 | 1 | 1 | no QA row |
| Femi-Commercial Intelligence-Oct 25 | 12 | 1 | 1 | 1 | YES |
| G2-Juliane -Impact and Planning June 2026 | 14 | 1 | 1 | 1 | YES |
| Project Planning & Control (PPC) \| Andrew | 17 | 1 | 1 | 1 | YES |
| Ray-Project Management Office (PMO) | 18 | 1 | 1 | 1 | YES |
| G3 - Femi - Customer Journey Optimisation | 14 | 1 | 1 | 1 | YES |
| Ray-MSP Jan 2026 | 2 | 1 | 1 | 1 | YES |

**6 / 6 legacy session IDs reproduced.**

The PPC case is the instructive one: its selected transcript ran 05:47–09:17Z
against an 08:00–10:00Z window — only 77 minutes of overlap, starting 133
minutes early — and it still wins because no other same-day candidate overlaps
at all. A rule requiring containment, or requiring the transcript to start after
`scheduled_start`, would have picked nothing.

## 9. Real multi-part fixture — NOT YET VALIDATED

**REAL MULTIPART FIXTURE VALIDATED = NO.**

Every one of the seven 2026-09-04 lectures has exactly one same-day candidate,
so the cluster loop selected a single part each time and the tie-breakers were
never exercised on real data. The multi-part rules are covered only by the
synthetic fixtures in `tests/unit/test_transcript_selection.py` and
`tests/unit/test_transcript_combine.py`.

This does not block selection parity, which is validated. It must be closed
before production cutover: find a historical date whose Teams call dropped and
restarted, re-run Phase 2A acquisition for it, and re-run these parity tests
against real multi-part evidence.

## 10. What Phase 2B does not do

No canonical cue model, speaker normalization, attendance matching, engagement
scoring, QA, AI, checklist generation, Perfect Lecture logic, Positive Clips,
Lecture Split planning, media jobs, FFmpeg, SharePoint upload, or scheduler
activation. The combined transcript is derived data in its own table; Phase 2A
raw artifacts stay immutable and hash-verifiable.

---

# Appendix: QA Master contract audit (2026-09-16)

Second authoritative export, now also in the repo:

`automation/legacy_n8n/QA Master Daily — Safe Exact Recording v8`

Both exports were read. Responsibilities are kept separate: the **Master** owns
orchestration and the input contract; **One Lecture** owns transcript selection
and combination. Neither was treated as authoritative for the other's area.

## The legacy Master contract

`Matched Lecture?` passes the whole item through an `executeWorkflowTrigger`
whose `inputSource` is **passthrough**, and `Lecture Input` consumes exactly six
fields. Their origin in the Master:

| Legacy field | Built by | From |
| --- | --- | --- |
| `meetingId` | `Pick Meeting Id` | `/me/onlineMeetings?$filter=JoinWebUrl eq '…'` |
| `module` | `Match Calendar to Groups` | exact normalized active Aptem group |
| `subject` | `Extract Calendar Fields` | `event.subject`, raw |
| `targetDate` | `Match Calendar to Groups` | `String(meetingStart).slice(0, 10)` |
| `scheduledStart` | `Match Calendar to Groups` | `event.start.dateTime`, raw |
| `scheduledEnd` | `Match Calendar to Groups` | `event.end.dateTime`, raw |

## Mapping to the new platform

| Legacy | New canonical field | Representation |
| --- | --- | --- |
| `meetingId` | `lecture_sessions.meeting_id` | stored directly |
| `module` | `lecture_sessions.module` | stored directly (exact active-group match) |
| `subject` | `lecture_sessions.subject` (+ `normalized_subject`) | stored directly, normalized form added |
| `targetDate` | `lecture_sessions.session_date` | derived: aware start → Africa/Cairo → `.date()` |
| `scheduledStart` | `lecture_sessions.scheduled_start` | normalized to `timestamptz` |
| `scheduledEnd` | `lecture_sessions.scheduled_end` | normalized to `timestamptz` |

## The one real discrepancy: a stale comment in QA One Lecture

`Get Today's Calendar Events` sends:

```
Prefer: outlook.timezone="Africa/Cairo"
```

so `event.start.dateTime` reaching `scheduledStart` is a **naive Cairo
wall-clock** string. But `Select Transcript Parts V3` states the opposite:

```js
// scheduledStart / scheduledEnd coming from QA Master
// are already UTC timestamps even when they do NOT end with Z.
// Example: 2026-08-07T08:00:00.0000000 means 08:00 UTC, NOT 08:00 Cairo.
```

and parses them with `Date.parse(cleaned + 'Z')`. Against this Master export
that comment is **wrong**: it reinterprets Cairo wall-clock as UTC, shifting the
occurrence window three hours (Cairo is UTC+3 in September). The comment's own
example, `08:00`, is the UTC rendering — evidence it predates the Master's
`Prefer` header.

Phase 2B had already declined to recreate that parsing, using canonical
`timestamptz` instants instead. That decision is now justified by the Master
export rather than by preference.

**Impact on 2026-09-04: none, and measured rather than assumed.**
`app/cli/audit_qa_master_contract.py` runs the ported selector under both
readings. Every one of the seven lectures has exactly one same-day candidate, so
the Cairo-date filter decides and the window shift cannot change the winner:
`timezone_readings_agree_on_every_lecture = true`, parity 6/6 either way.

The divergence is latent, not resolved. On a date with several same-day
candidates the two readings could disagree, and the platform's reading is the
correct one.

## Semantic checks

- **targetDate** — legacy sliced a Cairo-rendered datetime; the platform
  converts an aware instant to Africa/Cairo. Same semantics, verified equal for
  all seven lectures. The platform's is robust to the render timezone; the
  legacy slice silently depended on the `Prefer` header.
- **scheduledStart / scheduledEnd** — same absolute instants, stored as
  `timestamptz`. Old naive serialization deliberately not recreated.
- **meetingId** — the platform value equals `qa_doctors_sessions.meeting_id` for
  **6/6** downstream-ready lectures. Master used delegated `/me/onlineMeetings`;
  Phase 1 uses `/users/{discovery mailbox object id}/onlineMeetings`, which in
  this tenant resolves to the same `onlineMeeting` identity. The unresolved MSP
  occurrence has no `meeting_id` by design and is excluded from the comparison.
- **subject** — raw calendar subject preserved; `normalized_subject` is additive.
- **module** — from the exact active-Aptem-group match. The platform normalizer
  (`html.unescape` + `casefold`) is a strict superset of the legacy one
  (`&amp;`→`&` + `toLowerCase`); on the live subjects and modules the two agree
  exactly (`module_normalizer_differences = []`).
- **Recurring-occurrence safety** — the Master passes the *series* meeting ID, so
  the risk is real in both systems. Legacy relied solely on the Cairo-date filter
  inside One Lecture. The platform keeps that filter and adds per-lecture
  candidate links plus the occurrence window, and the 79→7 reduction on
  2026-09-04 is the measured proof.

## Not reimplemented

No calendar discovery and no Aptem query were added to Phase 2B; Phase 1 already
owns that layer. The audit is read-only and makes zero Graph calls.
