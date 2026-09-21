# Phase 3C3B: fresh-day readiness hardening

Three prerequisites the controlled-day pilot exposed, closed together. No new
production day, no scheduler, no n8n change.

## A. Lecture-scoped engagement

### What was wrong

`calculate_day` was the only entry point. `LOAD_SNAPSHOTS` selects on
`l.session_date = %s`, so every lecture on the date was recalculated and
re-upserted. The values were identical, but `ON CONFLICT DO UPDATE` sets
`updated_at = now()`, so every sibling was observably written. That is fine for
a one-off backfill and wrong for everything the platform is about to do:
a scheduler, recovery, a Retry stage, or a single-lecture repair all need
"what did this run touch?" to have an answer.

Inputs, unchanged: `lecture_attendance_snapshots` (pinned to
`attendance_roster_v2_exclude_makeup`), `lecture_attendance_snapshot_members`,
`lecture_transcript_speaker_roles` / `_identities` / `lecture_transcript_speakers`
(pinned to `speaker_resolver_exact_then_legacy_fuzzy_v1` and `vtt_top_speaker_v1`).
Writes, unchanged: `lecture_engagement_metrics`, `_participants`, `_runs`.
`engagement_id` is a uuid5 over document, snapshot, all three versions and the
input fingerprint, so a rerun on unchanged evidence lands on the same row.

### What changed

`EngagementInputRepository.load_snapshots_for_lecture` is a **separate SQL
statement with no `session_date` predicate at all** — the lecture id *is* the
scope, so there is no argument to leave unset that could widen it. A test
asserts that property on the SQL text itself.

`EngagementService.calculate_lecture(connection, lecture_id)` refuses an
unknown lecture, refuses a lecture with no usable attendance evidence (a
recovery signal, not a silent no-op), and refuses if the loaded snapshots ever
name another lecture. Both entry points share one `_run`; the summary now
carries `scope`, `scope_value` and `lectures_in_scope`. Legacy parity is
date-mode only, since it compares a whole day.

`calculate-engagement` now takes a **required mutually exclusive** `--date` or
`--lecture-id`. `--date` is no longer "required and therefore the default", so
lecture mode cannot fall back into date mode by omission; the CLI refuses both
neither and both.

Files: `app/db/repositories/engagement.py`, `app/engagement/service.py`,
`app/cli/main.py`.

### Proof, against production

Target: Steve-EVM (`eaccf843-…`), 8 engagement rows on 2026-09-16.

| | |
| --- | --- |
| scope / lectures_in_scope | `LECTURE` / **1** |
| engagements created / updated | 0 / **1** (same `engagement_id` reused) |
| values | attended 5, spoke 4, 80.00 %, score 5 — **unchanged** |
| rows with a new `updated_at` | **1**, the target |
| **siblings touched** | **0** |
| rows whose values changed | **none** |
| graph / attendance / AI calls | 0 / 0 / 0 |

## B. Perfect eligibility persisted for every finalized render

### What was wrong

The shadow result was written only on the write path, so a NOT_ELIGIBLE
lecture left no row. Operations could not distinguish "not eligible" from
"never computed" without re-deriving the answer.

### What changed

`_may_persist_result` records the coded answer for **every finalized rendered
session** — on request in a dry run, and always when the run is already
authorised to write. It refuses for a payload that is not finalized or does not
map, because an answer derived from an unusable render is provenance for
nothing. A non-perfect lecture still gets **no** `qa_perfect_lectures` row and
**no** Perfect legacy ownership.

### A second defect found while proving B3

The first idempotency run showed 0 duplicates and 0 changed answers — but
`computed_at` advanced on every rerun, because the upsert set it
unconditionally. That is the same "what did this run change?" problem as Part A,
in a different table. Fixed: `computed_at` and `updated_at` now move only when
the answer actually changed, decided in Python against the already-loaded
previous row (a `RETURNING` expression would compare against the values the
statement had just written). Two further full reruns now produce a **byte-identical
table**.

### Backfill

14 finalized renders across two dates, coded shadow state only:

| | before | after |
| --- | ---: | ---: |
| `lecture_perfect_lecture_results` | 4 | **14** (5 perfect, 9 not) |
| 2026-09-16 results | 4 | **7** (4 perfect, 3 not) |
| `qa_perfect_lectures` | 144 | **144** |
| `lecture_perfect_lecture_legacy_writes` | 4 | **4** |
| `qa_doctors_sessions` / `_checklist_items` | 657 / 7,600 | **657 / 7,600** |

No legacy table changed. Note 2026-09-04's `G2-Juliane` is eligible and has a
persisted result but **no legacy row** — correct: it was never coded-written,
so the platform does not own a Perfect row for it.

## C. Item 2 punctuality source

### The old semantics, traced

`app/transcripts/selection.py` sets `actual_start = min(part.start)` and
`actual_end = max(part.end)`, where each part's start/end are the provider
artifact's `provider_created_at` / `provider_end_at` — **the Teams call open
and close instants**. Those are persisted on `lecture_transcript_selections`
along with the rounded differences, read by `LOAD_QA_INPUTS`, and passed to
`item2_status`, which returns Met unless the start is more than 20 minutes late
or the end more than 20 minutes early.

So Item 2 has always measured *the call*, never *the lecture*.

### The new semantics

`app/qa/punctuality.py` makes the source an explicit version:

- `legacy_call_bounds_v1` — the call instants, preserved by name so an old
  evaluation stays reproducible rather than being retroactively reinterpreted.
- `canonical_cue_bounds_v1` — the first and last surviving canonical cue,
  converted through the call-start instant:
  `lecture_start = call_start + first_cue.start_ms`,
  `lecture_end = call_start + last_cue.end_ms`.

No threshold moved. Missing cue data is a refusal, never a silent fallback to
the other source. For a multi-part v2 document the offsets are already
expressed against part one's call, which is the same instant `min(part.start)`
produced, so the conversion holds with no special case.

**QA evidence timestamps are untouched and still call-relative.** This
conversion exists only for punctuality wall-clock semantics.

### Replay, read-only, across all 14 selected lectures

| | |
| --- | --- |
| lectures examined | **14** |
| candidate uncomputable | **0** |
| multi-part | 1 |
| START divergence min / median / max | 0 / **2** / **167** min |
| START divergence > 5 min | **3 of 14** |
| START divergence > 20 min | **2 of 14** |
| END divergence min / median / max | 0 / **0** / **157** min |
| END divergence > 5 min | **4 of 14** |
| END divergence > 20 min | **3 of 14** |
| **Item 2 status changes** | **1** |

The one change: **AI in Project Control 2026 (2026-09-04), Met → Not Met.** Not
a start problem — the *end* difference goes from **+50 to −107 minutes**. The
call stayed open 50 minutes past the scheduled end while the last spoken cue
was 107 minutes before it. This is the `NON_DELIVERED` lecture, so the new
source is describing it correctly and the old one was hiding it behind an open
call.

That single case is the strongest argument for the change: the end criterion
("ended more than 20 minutes early") was effectively **dead** under call
bounds, because a call left open can never end early. Under cue bounds it
starts doing its job.

### Decision

Adopt `canonical_cue_bounds_v1` as the default for **new** evaluations. The
evidence supports it: computable on 14/14, it corrects the two early-opening
calls the pilot flagged, and its one status change is a correction on a
non-delivered lecture rather than a regression.

**No production row was rewritten.** All seven 2026-09-16 evaluations keep
their stored `start_difference_minutes` / `end_difference_minutes` and all
seven rendered Item 2 statuses remain Met. 2026-09-16 stands as historical
production evidence under `legacy_call_bounds_v1`.

### Provenance

`ShadowQaService` takes `punctuality_source_version`, defaulting to
`canonical_cue_bounds_v1` and validated against the known set. Per evaluation
it records, in the existing metadata jsonb — no new schema:

`punctuality_source_version`, `scheduled_start`, `scheduled_end`,
`derived_actual_start`, `derived_actual_end`, `start_difference_minutes`,
`end_difference_minutes`, plus `call_actual_start` / `call_actual_end` so the
two sources stay comparable after the fact.

The source version also participates in the **QA source fingerprint**. Two
sources can agree on one lecture and differ wildly on the next, so the source
belongs in the fingerprint, not just the minutes it happened to produce. A
consequence worth stating plainly: any future re-evaluation of an existing
lecture will produce a new fingerprint and therefore a new evaluation, rather
than silently reusing one computed under the old source.

## Operations state model

All 14 stages now resolve for all 7 lectures of 2026-09-16 **by reading
persisted state, with no inference and no recomputation**. `PerfectEligibility`
is COMPLETE for all seven — the pilot's gap is closed, and an absence there is
now a genuine gap rather than something to derive. `RecordingLink` is MISSING
for all seven (the recording branch has not matched them) and `ExcelSync` is
MISSING or NOT_APPLICABLE accordingly. No REVIEW entries.

## Carried gates

**A** Positive Clips / media coordinate. **B** `planner._zone`. **D** n8n
ownership guard undeployed; QA paused by a manual node disable. **E** Excel
Sync's running version unreadable via the public API. **Gate C (date-scoped
engagement) and Gate G (unpersisted eligibility) are closed.** Gate D2 is now
a versioned, tested policy rather than an unexamined assumption.
