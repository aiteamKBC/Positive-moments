# Phase 3C3A: the controlled full-day QA pilot, 2026-09-16

The first time the coded platform took a whole real day. Seven canonical
lectures: two already written by the canaries, five processed and written here,
one lecture at a time.

## 1. The day, as discovered

Nothing was assumed. Every fact below was read from live state.

| | |
| --- | --- |
| canonical lectures | **7** |
| already coded-complete | 2 (Ray \| PMP, Andrew) |
| pilot candidates | **5** |
| blocked upstream | 0 |
| legacy-protected | 0 |

All five candidates: `downstream_ready`, not cancelled, Phase 2B `SELECTED`
with **1 part**, `webvtt_canonical_v1`, speakers present, attendance snapshot
`attendance_roster_v2_exclude_makeup`, engagement `CALCULATED`. **No candidate
had multiple selected parts**, so the multi-part rule never had to fire and
nothing was moved to Andrew's v2 parser. Engagement was never recalculated, so
Gate C was never touched.

Checked explicitly before any write: for each candidate, the legacy table held
**0** rows under its future `session_id` (the primary provider transcript id),
**0** rows under its date+subject, and its Perfect `lecture_key` was unused. No
unowned legacy row existed to overwrite.

## 2. Phase 3A — 6 provider calls for 5 lectures

| lecture | generations | status | M/P/N | clips |
| --- | ---: | --- | --- | ---: |
| G1- Femi - Customer Journey Optimisation | 1 | COMPLETED | 11/0/0 | 44 |
| Juliane – Impact and Planning June 2026 | 1 | COMPLETED | 10/0/1 | 44 |
| Keith \| Strategy & Planning – June 2026 | 1 | COMPLETED | 11/0/0 | 44 |
| Steve-Earned Value Management(EVM)2026 | **2** | COMPLETED | 11/0/0 | 39 |
| G2 Keith-Commercial Intelligence-Oct 25 | 1 | COMPLETED | 10/1/0 | 54 |

**Total provider calls: 6.** 0 invalid evidence clips anywhere. 0 Graph calls,
0 live attendance queries, 0 engagement recalculations.

### The one retry, and why nothing was loosened

Steve's generation 1 returned `INVALID_STRUCTURED_OUTPUT` with five
`INVALID_KSB_TYPE` errors: the model emitted `K` / `S` / `B` instead of
`Knowledge` / `Skill` / `Behaviour`. The validator rejected it, which is
correct — production uses only the full words. The enums were **not** silently
repaired and the validator was **not** relaxed. Generation 2, inside the
existing cap of 3, completed cleanly. This is the same failure mode the first
canary hit in Phase 3C2, now seen twice: **the model gets the KSB enum wrong
roughly 1 generation in 6.** Worth noting as a cost input, not a defect.

## 3. Phase 3B

All five `RENDERED`: 1 session, 11 checklist rows each, 1 new LMS snapshot
each (none existed), and **0 provider calls, 0 Graph calls, 0 attendance
re-queries, 0 engagement recalculations**.

## 4. Perfect eligibility

Computed from the frozen rendered payload under `legacy_qa_v8_perfect_v1`, not
from model prose:

- **PERFECT_WOULD_INSERT (3):** G1- Femi, Keith \| Strategy & Planning, Steve-EVM
- **PERFECT_NOT_ELIGIBLE (2):** Juliane (10/0/1), G2 Keith (10/1/0)

## 5. Dry runs — all five clean

Every candidate: QA `WOULD_INSERT`, 11 proposed rows, no invariant error, no
key collision, not coded-owned, and **zero** across every blocker counter
(`WOULD_UPDATE`, `PROTECTED_EXISTING_LEGACY_ROW`, `BLOCKED_NOT_READY`,
`BLOCKED_INVALID_PAYLOAD`, `BLOCKED_BACKFILL_NOT_AUTHORISED`,
`REVIEW_REQUIRED`, `PERFECT_BLOCKED_KEY_COLLISION`).

## 6. The writes

Five separate `CANARY_NEW_ONLY --lecture-id … --confirm-write` invocations.
**No date-wide write was ever issued.** Each lecture was written, verified by
independent re-read, and proven idempotent before the next one started.

| lecture | QA | 22/22 | 66/66 | Perfect | attended | idempotent |
| --- | --- | :-: | :-: | --- | ---: | :-: |
| G1- Femi | WRITTEN | ✔ | ✔ | WRITTEN | 3 | ✔ |
| Juliane | WRITTEN | ✔ | ✔ | NOT_ELIGIBLE | — | ✔ |
| Keith \| Strategy | WRITTEN | ✔ | ✔ | WRITTEN | 1 | ✔ |
| Steve-EVM | WRITTEN | ✔ | ✔ | WRITTEN | 5 | ✔ |
| G2 Keith | WRITTEN | ✔ | ✔ | NOT_ELIGIBLE | — | ✔ |

Every Perfect row carries `mapping_version = legacy_qa_v8_perfect_mapping_v2`
and a populated `attended_count` — the Phase 3C2.3F fix holding on first
write, rather than needing a repair.

Day deltas, exactly as predicted:

| table | before | after | delta |
| --- | ---: | ---: | ---: |
| `qa_doctors_sessions` | 652 | 657 | **+5** |
| `qa_doctors_checklist_items` | 7,545 | 7,600 | **+55** |
| `qa_perfect_lectures` | 141 | 144 | **+3** |
| `lecture_qa_legacy_writes` | 2 | 7 | **+5** |
| `lecture_perfect_lecture_results` | 1 | 4 | **+3** |
| `lecture_perfect_lecture_legacy_writes` | 1 | 4 | **+3** |

A non-eligible lecture produced **+0** Perfect rows, +0 results and +0
ownership — the derived output stayed absent rather than being written as
"false".

## 7. Item 2: the punctuality source problem, measured

This is what the pilot was for. For every lecture, the first *spoken* cue
translated to wall-clock against the scheduled start:

| lecture | scheduled | first cue | Item 2 start diff | first-cue diff | Item 2 | flag |
| --- | --- | --- | ---: | ---: | :-: | --- |
| G1- Femi | 08:00 | 08:01:47 | 0 | +2 | Met | ok |
| Juliane | 08:00 | 07:58:13 | −7 | −2 | Met | ok |
| Keith \| Strategy | 08:00 | 08:01:26 | 0 | +1 | Met | ok |
| Ray \| PMP | 08:00 | 08:02:03 | −7 | +2 | Met | ok |
| **Steve-EVM** | 08:00 | 08:00:47 | **−21** | +1 | Met | **PUNCTUALITY_SOURCE_REVIEW** (gap 22 min) |
| **Andrew** | 11:00 | 11:00:47 | **−166** | +1 | Met | **PUNCTUALITY_SOURCE_REVIEW** (gap 167 min) |
| G2 Keith | 11:00 | 11:01:37 | −2 | +2 | Met | ok |

**Every lecture actually started within 2 minutes of schedule.** Item 2,
however, measures the *call*, and on **2 of 7 (29 %)** the call opened
materially earlier than the teaching did — 22 minutes for Steve, 167 for
Andrew.

No outcome changed here, because starting early always passes. The exposure is
the mirror case: a call that opens early would **mask a genuinely late lecture
start**, and Item 2 would report Met when it should not. At 29 % incidence
that is not a rarity. Item 2 logic was not changed, and nothing was overridden.

## 8. Foreign-owned state

No recording activity occurred during the pilot window: all seven sessions have
`recording_url` NULL, `recording_link_status` NULL, `clips_status 'pending'`.
All four Perfect rows have `recording_url`, `recap_url` and `excel_synced_at`
NULL.

**Excel Sync currently selects 0 rows**, table-wide. Its condition needs
`recording_url IS NOT NULL`, so the new Perfect rows are not yet eligible.
Nothing was triggered, disabled or altered.

## 9. Canary integrity

Ray digest `5092175cee6d2a25…` unchanged. Andrew digest `83d2be26c4e4c999…`
unchanged, Perfect mapped digest `266deb77351f1cb4…` unchanged,
`attended_count` still 7, exactly one ownership row. Neither canary was
reprocessed.

## 10. Recovery-state observability

All 14 stages resolve to a definite value for all 7 lectures, from persisted
state alone, with no REVIEW entries:

`Discovery, Transcript, Selection, CanonicalCues, Speakers, Attendance,
Engagement, QAEvaluation, QARendering, LegacyQASync, PerfectEligibility`
— **COMPLETE for all 7**.
`RecordingLink` — MISSING for all 7 (the recording branch has not matched them).
`PerfectLegacySync` / `ExcelSync` — COMPLETE/MISSING for the 4 Perfect
lectures, NOT_APPLICABLE for the 3 that are not.

**One real gap.** `PerfectEligibility` is only *persisted* for lectures that
turned out perfect: the shadow result row is written by the writer, so a
NOT_ELIGIBLE answer leaves no row and the state has to be re-derived from the
render. It is deterministic and free to recompute, so nothing is lost — but an
Operations layer that wants to read the answer rather than recompute it needs
the non-eligible result persisted too. That is a small change to when
`persist_shadow_result` is set, and it should be made before the Operations
layer is built.
