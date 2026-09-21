# Phase 3C3C: the second controlled day, fresh from Microsoft Graph

2026-09-17. The first time the coded platform ran a real day from calendar
discovery all the way to legacy compatibility writing. The 2026-09-16 pilot
started from Phase 3; this one started from nothing.

## 1. The day was genuinely fresh

Every stage checked before touching anything: **0 rows everywhere** — no
`lecture_sessions` identity, no artifacts, no selection, no document, no
attendance, no engagement, no evaluation, no render, no coded ownership. The
legacy tables held **0** QA sessions and **0** Perfect rows for the date, which
is expected: `Execute QA One Lecture` has been disabled since the cutover
began, so legacy QA has not run for this day either.

## 2. Discovery matched the user's expectation exactly

| | |
| --- | --- |
| calendar events found | 13 |
| Teams events | 12 |
| active Aptem groups loaded | 39 |
| **canonical lecture candidates** | **3** |
| excluded as non-canonical | 9 |
| online meetings resolved / unresolved | 3 / 0 |
| downstream_ready | 3 |

**3 discovered, 3 expected.** All 9 exclusions were the single reason
`NOT_AN_ACTIVE_APTEM_GROUP` — internal test meetings ("Test With Ktisha",
"khalido 16", "Ay Haga"), two all-day "Ai team" entries, "End Point Assessment
Preparation", "Final Module", "Shift 2". No cancelled events, no duplicates:
**3 distinct lecture ids, 3 distinct calendar events, 3 distinct meeting ids**.
All three resolved with `ORGANIZER_ID_CONFIRMED`,
`DISCOVERY_MAILBOX_OBJECT_ID` and `JOIN_URL_OID_MATCHES_CONTEXT`.

| lecture_id | subject | scheduled (UTC) |
| --- | --- | --- |
| `de8c6c60-…` | G2 - Keith - Strategy and Planning - June 2026 | 08:00–10:00 |
| `dd5b591e-…` | Stephen-Portfolio Management 2026 | 08:00–10:00 |
| `9671a3f1-…` | Martech - Thur | 11:00–13:00 |

## 3. Acquisition and selection

**25 artifacts** discovered, created and fetched across 3 meetings, all
speaker-attributed, 0 admin-blocked, 0 fallbacks, 0 errors. The recurring
series returned their whole history (back to June), which is exactly what the
date filter exists for: selection narrowed **15 → 1** and **9 → 1**.

All three `SELECTED`, **all single-part**, so the validated v1 canonical path
applied and the seam-aware v2 path was correctly not used. 0 Graph calls during
selection.

Canonical documents, all `PARSED`, **0 backward timeline steps**, **0 duration
parity mismatches**:

| lecture | cues | first ms | last ms | duration | speakers |
| --- | ---: | ---: | ---: | ---: | ---: |
| G2 - Keith | 626 | 192,691 | 7,044,774 | 114.2 min | 10 |
| Stephen | 704 | 166,048 | 7,792,961 | 127.1 min | 6 |
| Martech | 910 | 71,565 | 7,466,205 | 123.2 min | 13 |

Note every document has a **non-zero call-relative origin** (1–3 minutes), so
the Phase 3C2.3A coordinate rule applied on a fresh day without special
handling.

## 4. The attendance finding

**Two of three lectures have no attendance data at all.** The external
`kbc_attendance` source holds 8 rows for 2026-09-17, of which 4 are present
(`Attendance = 1`), and **all four belong to Stephen-Portfolio Management**. G2
Keith and Martech have zero rows.

This was verified against the source table directly rather than inferred from
the resolver: it is real missing source data, not a matching failure. The
resolver behaved correctly and produced empty snapshots.

## 5. Lecture-scoped engagement, proven on a fresh day

Each lecture ran through `calculate_lecture` individually. Every run:
`scope = LECTURE`, `lectures_in_scope = 1`, `snapshots_considered = 1`,
**exactly 1 row touched, 0 foreign rows**, 0 Graph, 0 live attendance, 0 AI.
And after all three, neither of the earlier lectures' fingerprints or
timestamps had moved.

| lecture | attended | spoke | % | score | status | Item 7 override |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| G2 - Keith | 0 | 0 | 0.00 | 1 | `NO_ATTENDED_LEARNERS` | no |
| Stephen | 4 | 4 | 100.00 | 5 | `CALCULATED` | **yes** |
| Martech | 0 | 0 | 0.00 | 1 | `NO_ATTENDED_LEARNERS` | no |

## 6. Item 2 on the new source

All three new evaluations bound to **`canonical_cue_bounds_v1`**. Read-only
comparison against what `legacy_call_bounds_v1` would have produced:

| lecture | old start | new start | old end | new end | old Item 2 | new Item 2 |
| --- | ---: | ---: | ---: | ---: | :-: | :-: |
| G2 - Keith | −2 | **+1** | −4 | −4 | Met | Met |
| Stephen | −4 | **−2** | **+33** | **+6** | Met | Met |
| Martech | −9 | **−8** | −5 | −5 | Met | Met |

**No status changed on this day.** The largest correction is Stephen's end:
the call was left open 27 minutes after teaching stopped, and the new source
reports +6 instead of +33. Small but exactly the behaviour the change was for.

## 7. Phase 3A — and the real headline

| lecture | generations | outcome |
| --- | ---: | --- |
| Stephen | 1 | **COMPLETED** — 10 Met / 1 Partial / 0 Not Met, 39 clips |
| Martech | 2 | **COMPLETED** — 11 / 0 / 0, 45 clips |
| G2 - Keith | **3** | **REVIEW_REQUIRED**, `MAX_GENERATIONS_EXHAUSTED` |

**6 provider calls total.** Nothing was loosened; the bounded policy did its
job and G2 Keith was not written.

Every one of those four failed generations failed the same way:
`INVALID_KSB_TYPE`. The model returned `"K"` / `"S"` / `"B"` instead of
`Knowledge` / `Skill` / `Behaviour`.

### Root cause, found this phase

`STRUCTURED_OUTPUT_SCHEMA` **does** declare
`"enum": ["Knowledge", "Skill", "Behaviour"]`. But
`app/qa/provider.py` sends:

```python
"response_format": {"type": "json_object"}
```

That is plain JSON mode. The schema is never given to the provider as a
constraint — it reaches the model only as prompt text, and our validator is the
only thing enforcing it. The model is therefore free to emit `"K"`, and it does.

Across the two pilots this failure has now hit **4 of 8 first generations**,
and on G2 Keith it persisted through all three. That is roughly one wasted paid
generation in two, plus one lecture permanently blocked. Not changed in this
phase (the phase forbids touching the prompt, schema or validator), but it is
the single highest-value fix before unattended operation.

## 8. Rendering and eligibility

Both completed lectures `RENDERED`: 1 session, 11 checklist rows, 1 new LMS
snapshot each, **0 provider calls, 0 Graph calls, 0 engagement recalculations**.

Perfect eligibility persisted for both, under `legacy_qa_v8_perfect_v1`:

- Stephen — `is_perfect = false`, `NOT_ELIGIBLE_STATUS_NOT_ALL_MET` (10/1/0)
- Martech — `is_perfect = true`, `ELIGIBLE` (11/0/0)

### A data-quality exposure worth a decision

**Martech qualifies as a Perfect Lecture with zero attendance data.** Its Item 7
is Met from the **AI**, not from the deterministic override — the override could
not apply because engagement is `NO_ATTENDED_LEARNERS`. So an 11/11 Perfect
Lecture rests partly on a model judgement about learner engagement for a
lecture where the platform knows of no learners.

This is **not a platform defect**: legacy behaves identically, falling back to
the AI status whenever its own Item 7 override cannot be computed. It was
written because it satisfies every writer invariant and the phase authorises
writing clean lectures. But 2026-09-16 never produced this shape, and it should
be an explicit decision rather than an inherited accident.

## 9. The writes

Two lecture-scoped `CANARY_NEW_ONLY --lecture-id … --confirm-write` runs. No
date-wide command. Each verified and proven idempotent before the next.

| lecture | QA | 22/22 | 66/66 | Perfect | attended | 2nd run |
| --- | --- | :-: | :-: | --- | ---: | --- |
| Stephen | WRITTEN | ✔ | ✔ | NOT_ELIGIBLE | — | SKIP_IDENTICAL / NOT_ELIGIBLE |
| Martech | WRITTEN | ✔ | ✔ | WRITTEN | 0 | SKIP_IDENTICAL / SKIP_IDENTICAL |

Both second runs: all six deltas **0**. Martech's Perfect row carries
`mapping_version = legacy_qa_v8_perfect_mapping_v2`, and `recording_url`,
`recap_url` and `excel_synced_at` are all NULL.

| table | before | after | delta |
| --- | ---: | ---: | ---: |
| `qa_doctors_sessions` | 657 | 659 | **+2** |
| `qa_doctors_checklist_items` | 7,600 | 7,622 | **+22** |
| `qa_perfect_lectures` | 144 | 145 | **+1** |
| `lecture_qa_legacy_writes` | 7 | 9 | **+2** |
| `lecture_perfect_lecture_results` | 14 | 16 | **+2** |
| `lecture_perfect_lecture_legacy_writes` | 4 | 5 | **+1** |

**Excel Sync selects 0 rows** table-wide — the new Perfect row has no recording
link. Nothing was triggered.

## 10. Reconciliation

| lecture | Disc | Meet | Trans | Sel | Cues | Spk | Att | Eng | 3A | 3B | Legacy | PerfElig | PerfSync | Rec | Excel |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| G2 - Keith | OK | OK | OK | OK | OK | OK | OK | REVW | REVW | BLKD | BLKD | BLKD | N/A | N/A | N/A |
| Stephen | OK | OK | OK | OK | OK | OK | OK | OK | OK | OK | OK | OK | N/A | MISS | N/A |
| Martech | OK | OK | OK | OK | OK | OK | OK | REVW | OK | OK | OK | OK | OK | MISS | MISS |

Engagement reads REVIEW for the two zero-attendance lectures because
`NO_ATTENDED_LEARNERS` is not `CALCULATED` — the state model surfaces the data
gap rather than hiding it behind a 0 %.

**Unresolved: 1 lecture, G2 - Keith, `REVIEW_REQUIRED` /
`MAX_GENERATIONS_EXHAUSTED`.**

## 11. Recovery readiness

For the unresolved lecture, persisted state answers every recovery question:
resume at Phase 3A; upstream selection, document, speakers and attendance are
all reusable; 3 of 3 generations spent on fingerprint `886541a5…`; all three
attempts recorded with their error counts; engine, prompt, model, parser and
punctuality source all pinned; no render, no legacy write, no Perfect result.

**One small observability gap:** the evaluation row's `provider_attempts` reads
**1** while `lecture_qa_generation_attempts` correctly holds **3**. The
attempts table is the authority and the cap was enforced from it, but the
evaluation counter reflects only the last run rather than the cumulative total,
and a recovery layer reading the evaluation alone would under-count.

## 12. Item 2 historical backfill

**Recommendation: `KEEP_VERSIONED_HISTORY`.**

Across 17 lectures now examined (14 in the replay, 3 live here), exactly **one**
Item 2 outcome differs between the two sources, and it is a `NON_DELIVERED`
lecture on 2026-09-04 whose QA was never written to production. Every lecture
that has actually been written to legacy scores Met under both sources.

So a backfill would rewrite production history to change nothing, while
destroying the property that each row is reproducible from the version recorded
against it. The versions are named, the provenance is persisted, and
Operations can display which rule produced which row. Revisit only if a future
day produces a genuine divergence on a written lecture.

## 13. Carried gates

**A** Positive Clips / media coordinate. **B** `planner._zone`. **D** the n8n
ownership guard is still undeployed and the cutover still depends on a human
remembering a disabled node. **E** Excel Sync's running version is unreadable
through the public API. **New from this phase: H** structured output is not
provider-enforced; **I** Perfect eligibility can be reached with zero
attendance via an AI-sourced Item 7; **J** attendance source coverage is
incomplete for some real lectures.
