# QA Core — Root Cause Analysis: F-01, F-02, F-03

**Date:** 2026-09-22 · **Release:** `qa-core-rc3` (`f85932e45a4ecb413cb198cae57b8e4b81cfcd1e`)
**Follows:** [QA_CORE_PRODUCTION_DATA_AUDIT_2026-09-22.md](QA_CORE_PRODUCTION_DATA_AUDIT_2026-09-22.md) (not modified)
**Written for:** the engineering and operations leads deciding what to repair and whether to enable the nightly scheduler.

**Root-cause analysis only. Nothing was repaired and no production data was modified.** Every
connection used `options="-c default_transaction_read_only=on"`, asserted `transaction_read_only = on`
before any query, and would have failed closed on a write. The real `LegacyQaWriter` and
`PerfectLecturePlanner` were executed in `DRY_RUN` with result persistence off, against that
read-only connection. No Graph, OpenAI or n8n call was made; the n8n precheck state was read from
persisted `lecture_pipeline_runs.legacy_qa_precheck_status`.

---

## 0. Headline — and a correction to the previous audit

**F-02 is not what the audit said it was, and it hides a more serious defect.**

The audit classified F-02 as "two rendered lectures missing a required legacy projection". That is
wrong. Both lectures **already have** a `qa_doctors_sessions` row — written by n8n on 4 September.
The coded platform cannot see those rows because it is looking for them under a *different
`session_id` for the same transcript*.

On 22 September, during the RC3 backfill, Microsoft Graph returned transcript ids in a **second,
re-serialized form** for a meeting series whose transcripts the platform already held. The platform
treats the raw id string as identity, so it stored **94 duplicate artifacts** — one per real
transcript — and eight lectures were selected and rendered under the new form. Because the legacy
`session_id` *is* that id, the writer's ownership check (exact `session_id` match) could not see
n8n's existing row, decided `WOULD_INSERT`, and for **three lectures wrote a second, duplicate row**
into `qa_doctors_sessions`. Downstream consumers now see those three lectures twice.

F-02's two lectures escaped the same fate only by accident: n8n also held their Perfect Lecture key,
the Perfect planner refused on a key collision, eligibility was never persisted, and the pipeline
stalled one stage before `SYNC_LEGACY_QA`. **The stall is the only thing stopping two more
duplicates.** Forcing those lectures through `SYNC_LEGACY_QA` would create them.

The previous audit reported "0 duplicate compatibility projections". That check only tested *one
`session_id` → two lectures*. It never tested *one lecture → two `session_id`s*. That was a gap in
my audit method, and it changes the scheduler decision.

F-01 and F-03, by contrast, are **historical state**: produced on 18 September by operator-run
shadow QA and an operator canary, before the orchestrator existed and before the
attendance-required Perfect policy existed. The scheduler path refuses both today, proven by
17 consecutive `WAITING` outcomes.

---

## Part 1 — Exact affected rows

### F-01 · `Martech - Thur` · 2026-09-17

| Field | Value |
|---|---|
| `lecture_id` | `9671a3f1-3b46-5b18-a585-74b90964aa45` |
| `meeting_id` | `MSo3Mzc2NzliNC04ZWFjLTRm…` (152 chars) |
| Attendance snapshot | `a5ad81d1-e423-5995-8647-2acdc2ba1a05` · `attendance_roster_v2_exclude_makeup` · **0 source / 0 present / 0 effective** · metadata `attendance_coverage_status = SOURCE_MISSING` · 18 Sep 13:02:25 |
| Engagement | `fcbc41ad-7eb3-58fa-ac13-bb6e68d8c048` · `legacy_qa_v8_engagement_v1` · **`NO_ATTENDED_LEARNERS`** · 0 attended · 0.00% · 18 Sep 13:04:04 |
| Evaluation | `ac267304-5d1a-4131-8b30-8a1ec1e512d2` · `COMPLETED` · 11/11 met · `shadow_qa_v8_engine_v1` · 18 Sep 13:06:34 (updated 13:08:21) |
| Rendered session / `session_id` | canonical form `ktVizInGAAAAi_B6…VjI=` |
| Perfect result (v1) | `6401c5fc-1fa6-5342-95b4-bb936a19fa90` · **`legacy_qa_v8_perfect_v1` · `is_perfect = TRUE` · `ELIGIBLE`** · 18 Sep 13:13:15 |
| Perfect result (v2) | `d53753c5-715f-560f-a46b-6272ad56bd79` · `kbc_perfect_v2_attendance_required` · `is_perfect = FALSE` · **`PENDING_ATTENDANCE_DATA`** · 18 Sep 14:52:01 |
| QA legacy ownership | `lecture_qa_legacy_writes` · `WRITTEN` · **`CANARY_NEW_ONLY`** · 18 Sep 13:13:15 |
| Perfect legacy ownership | `lecture_perfect_lecture_legacy_writes` · `WRITTEN` · `CANARY_NEW_ONLY` · `legacy_qa_v8_perfect_v1` · 18 Sep 13:13:15 |
| Published rows | `qa_perfect_lectures` key `2026-09-17\|Martech - Thur`, engagement 0.00, attended 0, met 11 · `qa_doctors_sessions` Engagement 0.00, met 11 |

### F-02 · two lectures · 2026-09-02

| Field | `d19e74e2-6df2-5ab5-8671-9bccee97e086` | `cd9136f2-81f8-5044-8a76-6f36554572ba` |
|---|---|---|
| Subject | G1- Femi - Customer Journey Optimisation | Keith \| Strategy & Planning – June 2026 |
| Discovered | 22 Sep 11:44:31 (RC3 backfill) | 22 Sep 11:44:31 (RC3 backfill) |
| Selection | `4eb11d4d…` · `SELECTED` · 1 part · primary **variant** form (188 bytes) | `dd192212…` · `SELECTED` · 1 part · primary **variant** form (204 bytes) |
| Attendance snapshot | `2dcc5870…` · 3 / 3 / 3 · authoritative | `5d077567…` · 1 / 1 / 1 · authoritative |
| Engagement | `1e3e43d9…` · `CALCULATED` · 3 attended · 100% | `d990d7e1…` · `CALCULATED` · 1 attended · 100% |
| Evaluation | `cc8a2f3a…` · `COMPLETED` · 11/11 | `911e65a5…` · `COMPLETED` · 10/11 |
| Rendered session | `802dec8a…` · `RENDERED` · `session_id` = variant `ktVizIHGAAAAg_Bylg…VjLA` | `22d3f85d…` · `RENDERED` · `session_id` = variant `ktVizI3GAAAAj_B-lQ…VjLA` |
| Perfect result | **none** | **none** |
| QA legacy ownership | **none** | **none** |
| n8n `qa_doctors_sessions` row for the same lecture | **yes** · canonical `ktVizIDGAAAAgvBxlA…VjI=` | **yes** · canonical `ktVizIzGAAAAjvB9lA…VjI=` |
| n8n `qa_perfect_lectures` row on the same key | **yes** · id 675 · 4 Sep 13:04 · Excel-synced 13:07 | **yes** · id 674 · 4 Sep 13:01 |

### F-03 · two lectures · 2026-09-17

| Field | `Martech - Thur` (`9671a3f1…`) | `G2 - Keith - Strategy and Planning - June 2026` (`de8c6c60-6e73-5b50-b74d-ef5806b9d1bb`) |
|---|---|---|
| Attendance snapshot | `a5ad81d1…` · 0/0/0 · `SOURCE_MISSING` | `6e8dae50…` · 0/0/0 · 18 Sep 13:02:25 |
| Engagement | `fcbc41ad…` · `NO_ATTENDED_LEARNERS` | `bdffdef7…` · `NO_ATTENDED_LEARNERS` · 18 Sep 13:04:04 |
| Evaluations | `ac267304…` `COMPLETED` 11/11 | `300dd594…` `REVIEW_REQUIRED` (`MAX_GENERATIONS_EXHAUSTED`) 13:05:05 → `4b6808c6…` **`COMPLETED` 11/11** 14:48:00 |
| Producing run | `lecture_qa_runs` `0c73a419…`, **mode `SHADOW`** | `lecture_qa_runs` `e1eae207…`, **mode `SHADOW`** |
| Perfect | v1 ELIGIBLE (F-01), v2 PENDING | v2 only · `fabca564…` · `PENDING_ATTENDANCE_DATA` · 14:51:02 · **none published** |
| QA legacy ownership | `CANARY_NEW_ONLY` · 13:13:15 | `CANARY_NEW_ONLY` · 14:51:02 |

### Chronological timelines

**Martech - Thur (F-01, F-03)**

| When (GMT) | Event |
|---|---|
| 18 Sep 12:58:00 | Lecture registered by discovery |
| 18 Sep 13:02:25 | Attendance snapshot: 0 rows, `SOURCE_MISSING` — the source had not answered |
| 18 Sep 13:04:04 | Engagement: `NO_ATTENDED_LEARNERS`, 0.00% |
| 18 Sep 13:06:34 | Operator `run-qa-shadow` produces `COMPLETED` 11/11 |
| 18 Sep 13:13:15 | Perfect **v1** says `ELIGIBLE`; operator **CANARY** writes both legacy rows |
| 18 Sep 14:51:02 | First `kbc_perfect_v2_attendance_required` result **anywhere in the database** |
| 18 Sep 14:52:01 | v2 re-evaluation: `PENDING_ATTENDANCE_DATA` — correct, but the row is already published |
| 19 Sep 10:31:43 | First orchestrator run **ever** in production |
| 19 Sep → 22 Sep | 9 orchestrator runs; every one: `RECOVER_ATTENDANCE` → `WAITING` |

**G2 - Keith - Strategy and Planning (F-03)** — same pattern: snapshot 13:02:25 (0 rows); first
generation exhausted to `REVIEW_REQUIRED`; operator re-run at 14:48:00 → `COMPLETED` 11/11;
canary write 14:51:02; v2 `PENDING` 14:51:02, so **no** Perfect row was published; every
orchestrator run since: `WAITING`.

**The two F-02 lectures**

| When (GMT) | Event |
|---|---|
| 4 Sep 13:01 / 13:04 | n8n writes canonical rows to `qa_doctors_sessions` and `qa_perfect_lectures` |
| 17 Sep 09:29 | Canonical-form artifacts for this meeting series first acquired |
| 22 Sep 11:44:31 | RC3 backfill: lectures registered; **variant-form** artifacts first acquired; selection picks the variant primary; attendance, engagement, QA (11/11, 10/11) and render all succeed |
| 22 Sep, same run | `EVALUATE_PERFECT` → `PERFECT_BLOCKED_KEY_COLLISION` → no result persisted |
| 22 Sep, same run | Pass loop sees `EVALUATE_PERFECT` change nothing, marks it stalled; day settles at **pass 10, no `MAX_PASSES_REACHED`** |

---

## Part 2 — Reconstructed orchestration

Stage order, from `app/orchestration/stages.py`:

```
DISCOVERY → MEETING → TRANSCRIPT → SELECTION → CANONICAL_CUES → SPEAKERS → ATTENDANCE
→ ENGAGEMENT → QA_EVALUATION → QA_RENDER → PERFECT_ELIGIBILITY → LEGACY_QA_SYNC
→ PERFECT_SYNC → RECORDING_LINK → EXCEL_SYNC
```

`PERFECT_ELIGIBILITY` precedes `LEGACY_QA_SYNC` (Phase 4B ordering). The orchestrator advances a
lecture by one action per pass and, in `_passes`, adds `(lecture, action)` to a run-scoped `stalled`
set whenever an action leaves the lecture's next action unchanged; a stalled pair is never retried in
that run.

### F-02 — both lectures, backfill run `078b109b-3e2e-4cce-b05f-ffa766b8184d`

| Pass | Action | Persisted result |
|---|---|---|
| 1 | `ACQUIRE_TRANSCRIPT` (day) | Variant-form artifacts stored alongside existing canonical ones |
| 2 | `SELECT_TRANSCRIPT` (day) | Selection primary = variant id |
| 3 | `BUILD_CANONICAL_CUES` (day) | Documents and cues |
| 4 | `RESOLVE_SPEAKERS` (day) | Speaker inventory |
| 5 | `RESOLVE_ATTENDANCE` | Authoritative snapshots (3 and 1 members) |
| 6 | `CALCULATE_ENGAGEMENT` | 100% / 100% |
| 7 | `RUN_QA` | `COMPLETED` |
| 8 | `RENDER_QA` | `RENDERED`, `session_id` = variant |
| 9 | `EVALUATE_PERFECT` | **Nothing persisted** — `PERFECT_BLOCKED_KEY_COLLISION` |
| 10 | — | Next action still `EVALUATE_PERFECT` → pair stalled → run settles |

Final item: `status = SUCCEEDED`, `final_state = EVALUATE_PERFECT`,
`metadata.blocking_stage = PERFECT_ELIGIBILITY`.

**Why it stopped at `EVALUATE_PERFECT` instead of reaching `SYNC_LEGACY_QA`** — traced through the code:

1. `runner._evaluate_perfect` calls `plan_perfect_for_day` with a `DRY_RUN` planner and
   `persist_shadow_result=True` (`app/orchestration/runner.py:368-374`).
2. `PerfectLecturePlanner.plan_one` builds `lecture_key = "2026-09-02|<subject>"` and calls
   `legacy_repository.collisions(...)` with the **variant** `session_id`
   (`app/writer/perfect_service.py:162-169`).
3. `qa_perfect_lectures` already holds that key with n8n's **canonical** `session_id`, so
   `foreign_session = True` and `plan_perfect_decision` returns `PERFECT_BLOCKED_KEY_COLLISION`
   before eligibility is even considered (`app/writer/modes.py`, rule 3).
4. `_may_persist_result` explicitly excludes `PERFECT_BLOCKED_KEY_COLLISION`
   (`app/writer/perfect_service.py:315-330`), so no `lecture_perfect_lecture_results` row is written.
5. `PipelineStateResolver._perfect_eligibility` finds no result → `PERFECT_ELIGIBILITY = MISSING`,
   action `EVALUATE_PERFECT`. Re-running it cannot change the answer; the orchestrator correctly
   stalls it.
6. `LEGACY_QA_SYNC` comes later in `STAGE_ORDER` and is never reached.

**This is not the pass cap.** The run completed at pass 10 of 16 with no error.

### F-01 and F-03 — no orchestration involvement

None of the F-01/F-03 rows was produced by the orchestrator. The first `lecture_pipeline_runs` row in
production is 19 Sep 10:31:43. The evaluations were produced by `run-qa-shadow` (`lecture_qa_runs`
`mode = SHADOW`) and the legacy rows by `write-legacy-qa` in `CANARY_NEW_ONLY` — both operator CLI
commands — on 18 September. Every orchestrator visit since (9 runs, 17 items) went
`WAIT_FOR_ATTENDANCE_SOURCE → RECOVER_ATTENDANCE → WAITING`.

---

## Part 3 — Legacy QA sync root cause (F-02)

The real writer, executed read-only in `DRY_RUN` for both lectures today:

| Check | `d19e74e2…` | `cd9136f2…` |
|---|---|---|
| Rendered payload exists | ✅ | ✅ |
| Primary transcript / `session_id` exists | ✅ (variant form) | ✅ (variant form) |
| All 22 mapped fields valid (`payload_invariant_error`) | ✅ none | ✅ none |
| Checklist rows | 11 | 11 |
| Legacy row under **this** `session_id` | none | none |
| Coded ownership | none | none |
| **QA writer decision** | **`WOULD_INSERT`** | **`WOULD_INSERT`** |
| Perfect decision | `PERFECT_BLOCKED_KEY_COLLISION` | `PERFECT_BLOCKED_KEY_COLLISION` |
| Sync gate (`classify_plan`) | `may_write_qa = True` | `may_write_qa = True` |
| n8n precheck (persisted, every run) | `LEGACY_QA_DISABLED` | `LEGACY_QA_DISABLED` |
| **n8n row for the same lecture** | **exists, canonical id** | **exists, canonical id** |

**If `SYNC_LEGACY_QA` were reached, the scheduler would write a duplicate.** `classify_plan`
deliberately lets a safe QA insert proceed despite an unsafe Perfect decision, so the Perfect
collision would not stop it.

### The underlying defect: provider transcript id used as a raw-string identity

**Proof that both forms are the same transcript.** Every one of the **94** variant artifacts has a
canonical twin (same meeting, same `provider_created_at`, same lookup user `737679b4…`). Decoded:

| Pairs | Embedded transcript GUID identical | Difference |
|---:|---:|---|
| 94 | **94 / 94** | Always the same: variant is **1 byte longer**, ends in **`0xC0`**, and exactly **4 embedded length-prefix bytes are each `+1`** |

Example, Keith 09-02, both decode to thread `19:Pn7JwWg1gGbN…@thread.tacv2`, meeting timestamp
`1778773673728`, transcript `9fec4226-6443-40b4-b58e-2a5be69e6797-1788335805-TranscriptV2`.

All 94 variant artifacts were first seen **22 Sep 11:44–12:05**, inside the RC3 backfill; before that
no variant existed anywhere. Both forms came from identical request coordinates. Why Graph changed its
serialization is not observable from the database; what the platform did with it is.

**Where the invariant breaks — exact lines:**

| # | Location | What happens |
|---|---|---|
| 1 | `app/transcripts/graph.py:126` `provider_transcript_id=str(row["id"])` | The raw Graph id string becomes artifact identity. No canonicalization |
| 2 | Artifact identity, migration 006 | Keyed on `provider_transcript_id`, so one transcript becomes two artifacts |
| 3 | `app/rendering/service.py:264` `"session_id": evaluation["primary_provider_transcript_id"]` | The legacy key is that raw string |
| 4 | `app/db/repositories/qa_writer.py` `load_session` — `WHERE session_id = %s` | Target lookup is exact-string only |
| 5 | `app/writer/modes.py` `plan_decision` — `if not target_exists: return WOULD_INSERT` | No check for another row of the **same lecture** |
| 6 | `app/orchestration/state.py` `_legacy_qa_sync` → `legacy_qa_session(connection, target)` | Protection is also exact-string only |

**Invariant violated:** *one canonical lecture occurrence ↔ at most one `qa_doctors_sessions` row.*
Nothing in the code enforces it. The ownership model protects rows it can **find**, and it can only
find them by an identifier the provider does not keep stable.

**Why the backfill did not create F-02's rows:** the Perfect key collision stalled them first.
**Why it did create three duplicates:** those three had an n8n `qa_doctors_sessions` row but no n8n
`qa_perfect_lectures` row, so there was no collision to stall them, and they walked straight to
`WOULD_INSERT`.

---

## Part 4 — Attendance / Perfect root cause (F-01, F-03)

### What attendance evidence existed at decision time

| Question | Answer, from persisted state |
|---|---|
| Authoritative attendance, genuinely zero attendees? | **No** |
| Empty / missing / non-authoritative? | **Yes.** Both snapshots record `source_row_count = 0` and Martech's metadata records `attendance_coverage_status = SOURCE_MISSING` at creation, 18 Sep 13:02:25. The source had not answered; nobody was counted and found absent |
| Historical, before attendance-required semantics? | **Yes, for Perfect.** `kbc_perfect_v2_attendance_required` first appears at **14:51:02 on 18 Sep**. Martech's Perfect decision (13:13:15) was made under `legacy_qa_v8_perfect_v1`, which has no attendance condition |
| Stale cached result reused after a contract change? | **No.** Fingerprints differ per evaluation and nothing was reused. The problem is the opposite: nothing *retracted* the v1 result once v2 disagreed |

For **QA** (F-03): `app/qa/service.py` contains **no attendance-coverage check at all**. The
attendance gate lives only in the orchestrator's resolver
(`PipelineStateResolver._attendance`: not `is_authoritative(status)` → `WAITING`,
`WAIT_FOR_ATTENDANCE_SOURCE`). An operator invoking `run-qa-shadow` directly bypasses the resolver,
and on 18 September, before the orchestrator existed, that was the only path.

### Would current code produce the same result today?

| Path | F-01 (Perfect on empty attendance) | F-03 (finalized QA on empty attendance) |
|---|---|---|
| **Scheduler / backfill / console RETRY** | **No.** Pinned to `kbc_perfect_v2_attendance_required` (`DEFAULT_PERFECT_ELIGIBILITY_VERSION`, threaded through `build_orchestrator` → resolver → runner); non-authoritative coverage gives `PERFECT_PENDING_ATTENDANCE_DATA`, which writes and supersedes nothing | **No.** `ATTENDANCE = WAITING` precedes `QA_EVALUATION`, so `RUN_QA` is never offered. Proven: 17 consecutive `WAITING` items across 9 runs for exactly these lectures |
| **Operator CLI** | **Yes.** `write-legacy-qa --perfect-policy legacy_qa_v8_perfect_v1` is still accepted (`app/cli/main.py:248`) and in a write mode would publish on empty attendance | **Yes.** `run-qa-shadow` has no coverage gate |

---

## Part 5 — Blast radius (entire database, all time)

| Class | Count | Affected |
|---|---:|---|
| **A.** Finalized coded QA on empty / non-authoritative attendance | **2** | `ac267304…` (Martech - Thur), `4b6808c6…` (G2 - Keith - Strategy and Planning) — both 2026-09-17, both `SOURCE_MISSING`, both 18 Sep. **No others, ever** |
| **B.** Perfect `is_perfect` on empty / non-authoritative attendance | **1** | `6401c5fc…` (Martech - Thur, v1) — **published** |
| **C.** Rendered coded sessions with no projection | 2 apparent · **0 true** | The two F-02 lectures — **both already covered by an n8n row for the same lecture** |
| **D.** Items ended `SUCCEEDED` at an intermediate automatable stage | 10 | **8 resolved** (19 Sep scheduled runs, 7 and 3 passes, no errors, provider gated off during the pilot; all five lectures now complete) · **2 open** (F-02 stall) |
| **E.** Coded ownership → missing target | **0** QA · **0** Perfect | — |
| **F. (new)** Duplicate legacy lectures — one lecture, two `session_id`s | **4 groups** | **3 caused by the coded platform** on 22 Sep: 2026-09-03 *G2 - Femi - Customer Journey Optimisation*, 2026-09-03 *Stephen-Portfolio Management 2026*, 2026-09-08 *G1 Keith-Commercial Intelligence-Oct 25*. **1 historical n8n-only** (2026-02-20, both rows canonical, 0 coded — pre-existing, not ours) |
| **G. (new)** Duplicate transcript artifacts | **94 pairs** | Every variant artifact has a canonical twin |

**Coded rows under a variant id with no n8n twin: 3** — 2026-09-09 *Samar - Marketing Technology
(MarTech) oct 25*, 2026-09-09 *G1- Femi - Customer Journey Optimisation*, 2026-09-11 *G3 - Femi -
Customer Journey Optimisation*. Each is the only row for its lecture and is not a duplicate. Any
downstream workflow that computes the **canonical** id and joins on it — plausibly the n8n recording
branch — will not find these rows. **Not proven**; listed as the next thing to verify.

In the 09-08 duplicate, the two rows **disagree**: coded `met_count = 11`, n8n `met_count = 10`.
Consumers reading both see contradictory answers for one lecture.

---

## Part 6 — Is a code fix required?

| | Classification | Code fix? |
|---|---|---|
| **F-01** | **STALE_VERSIONED_STATE** | Scheduler: **no**. Operator CLI: yes, small (below) |
| **F-02** | **CURRENT_CODE_BUG** | **Yes** |
| **F-03** | **HISTORICAL_DATA_ONLY** | Scheduler: **no**. Operator CLI: yes, small (below) |

### F-02 — smallest generic fix (not implemented)

**Fix 1, the writer guard — smallest, and sufficient to stop new duplicates.** In
`LegacyQaWriter._plan_one`, before a `WOULD_INSERT` is accepted, look for any existing
`qa_doctors_sessions` row for the **same lecture occurrence** — same `meeting_id` and `date`, or
same decoded transcript identity. If one exists and is foreign, return
`PROTECTED_EXISTING_LEGACY_ROW`; if it is coded-owned by the same `lecture_id`, treat it as the owned
target. Add the new outcome to `QA_MANUAL` in `sync_safety.py` so the scheduler can never auto-insert
past it. Mirror the same lookup in `PipelineStateResolver._legacy_qa_sync` so the stage reports
`NOT_APPLICABLE` rather than `MISSING`.

**Fix 2, the identity fix — removes the cause.** In `TranscriptGateway._artifact`
(`app/transcripts/graph.py:126`), derive artifact identity from the decoded
(thread, meeting timestamp, transcript GUID) triple rather than the raw string, or normalize the
proven variant shape (strip trailing `0xC0`, decrement the four length prefixes) to canonical. The
transform is deterministic in 94/94 cases. Keep the raw id in metadata for the content fetch.

Fix 1 is the scheduler gate. Fix 2 is the correct long-term repair, needs its own tests and a
dedupe plan for the 94 twins, and should not be rushed.

### F-01 and F-03 — operator CLI hardening (not implemented)

- `write-legacy-qa`: refuse `--perfect-policy legacy_qa_v8_perfect_v1` in any write-enabled mode;
  keep it for dry-run historical reproduction only.
- `run-qa-shadow`: refuse to finalize (`COMPLETED`) when the lecture's attendance coverage is not
  authoritative — the same `is_authoritative` test the resolver already uses.

### Repair paths — targeted, idempotent, **none run**

| Item | Path |
|---|---|
| F-01 published Perfect row | **Operator decision.** Recover 2026-09-17 attendance first. If the lecture is still not perfect, retract through the writer's existing `rollback_owned_session` equivalent for Perfect, which requires proven coded ownership (present). Excel Sync may already have exported it |
| F-03 two evaluations | Recover attendance; the normal pipeline then supersedes the evaluations. No hand edits |
| F-02 two lectures | **Do not force `SYNC_LEGACY_QA`.** n8n's rows already serve these lectures. After Fix 1 the stage will resolve to `NOT_APPLICABLE` on its own |
| 3 duplicate rows | **Operator decision** on which row downstream should see. The n8n rows carry recording links. Removing a coded-owned row is possible only through the ownership-checked rollback path, and only after Fix 1 is deployed so it cannot recur |
| 94 twin artifacts | Part of Fix 2; no repair until the identity rule is decided |

---

## Part 7 — Scheduler safety (3-day lookback, if enabled now)

`SchedulerConfig.window` = target − 3 … target, i.e. **2026-09-19 → 2026-09-22** tonight.

| # | Question | Answer | Proof |
|---|---|---|---|
| 1 | Could it create another **F-01**? | **NO** | Scheduler Perfect policy is `kbc_perfect_v2_attendance_required`; non-authoritative coverage → `PERFECT_PENDING_ATTENDANCE_DATA`, which writes nothing. `ATTENDANCE = WAITING` also blocks `RUN_QA` upstream |
| 2 | Could it create another **F-02** (stall on a foreign Perfect key)? | **NO** | Requires an n8n `qa_perfect_lectures` row for a window lecture. n8n's QA path has been `LEGACY_QA_DISABLED` on every run since 19 Sep, and no n8n Perfect row exists for 09-19 → 09-22 |
| 3 | Could it create another **F-03**? | **NO** | `RUN_QA` is unreachable while `ATTENDANCE` is `WAITING`. Proven by 17 `WAITING` items for exactly the affected lectures |
| 4 | Could it overwrite a historical n8n-owned row? | **NO** | A non-owned row is `PROTECTED_EXISTING_LEGACY_ROW` in every mode; `write_status = 'UPDATED'` has occurred **0 times, ever**. **But** it can insert a *duplicate beside* one — proven three times — whenever the transcript id form differs |
| 5 | Would it repair the affected September lectures? | **NO** | None is in the 09-19 → 09-22 window. None of the repairs is in the auto-safe set anyway: superseding a published Perfect row and every `WOULD_UPDATE` are operator-only by design |

The honest residual risk is Question 4's caveat. Inside tonight's window, a *new* n8n duplicate
needs an n8n row, and none exist. A *coded-vs-coded* duplicate needs an already-projected lecture to
be re-rendered under a different id form. A read-only re-run of the real selection over all 54
September lectures picked the **identical primary in 54/54** and never attached one transcript twice,
so that path did not trigger today. It is not **closed**, though: the platform has no rule that would
stop it, and the provider changed its id form once already without warning.

---

## ROOT CAUSE SUMMARY

**F-01:**
- **classification:** STALE_VERSIONED_STATE
- **affected rows:** `lecture_perfect_lecture_results` `6401c5fc-1fa6-5342-95b4-bb936a19fa90` (v1, ELIGIBLE); `qa_perfect_lectures` key `2026-09-17|Martech - Thur` (published, 0 attendees); lecture `9671a3f1-3b46-5b18-a585-74b90964aa45`
- **root cause:** On 18 Sep 13:13, an operator canary published a Perfect verdict under `legacy_qa_v8_perfect_v1`, a policy with no attendance requirement, on a snapshot already recorded as `SOURCE_MISSING`. The attendance-required v2 policy first ran at 14:51 and correctly refused it at 14:52, but superseding a published row is operator-only by design, so nothing retracted it.
- **current code vulnerable?:** Scheduler **no**. Operator CLI **yes**: `write-legacy-qa --perfect-policy legacy_qa_v8_perfect_v1` is still accepted in write modes.
- **repair required?:** **Yes, an operator decision** — recover attendance, then retract through the ownership-checked path if still not perfect.

**F-02:**
- **classification:** CURRENT_CODE_BUG
- **affected rows:** lectures `d19e74e2-6df2-5ab5-8671-9bccee97e086` and `cd9136f2-81f8-5044-8a76-6f36554572ba` (2026-09-02). The same bug has already produced **3 duplicate `qa_doctors_sessions` rows** (2026-09-03 ×2, 2026-09-08) and **94 duplicate transcript artifacts**.
- **root cause:** Graph re-serialized transcript ids on 22 Sep (same transcript, 1 byte longer, trailing `0xC0`, four length prefixes `+1`; 94/94). The platform uses the raw id string as artifact identity and as the legacy `session_id`, and the writer and resolver protect existing legacy rows only by exact `session_id`. For these two lectures, n8n's Perfect key collided, `_may_persist_result` refused to persist eligibility, and the pipeline stalled before `SYNC_LEGACY_QA` — which **prevented** two more duplicates. These lectures are **not** missing a projection: n8n's rows exist for both.
- **current code vulnerable?:** **Yes.** Invariant *one lecture occurrence ↔ at most one legacy row* is unenforced (`graph.py:126`, `modes.py:plan_decision`, `qa_writer.py:load_session`, `state.py:_legacy_qa_sync`).
- **repair required?:** **Yes.** Code first: writer same-lecture guard (Fix 1), then transcript identity canonicalization (Fix 2). Then an operator decision on the 3 existing duplicates. **Do not force `SYNC_LEGACY_QA` for the two lectures.**

**F-03:**
- **classification:** HISTORICAL_DATA_ONLY
- **affected rows:** evaluations `ac267304-5d1a-4131-8b30-8a1ec1e512d2` (Martech - Thur) and `4b6808c6-7dfc-4491-8903-d5acb1bd4a87` (G2 - Keith - Strategy and Planning), both 2026-09-17, both projected to `qa_doctors_sessions` with Engagement 0.00 and met 11
- **root cause:** Produced on 18 Sep by operator `run-qa-shadow` (SHADOW runs `0c73a419…`, `e1eae207…`) before the orchestrator existed (first run 19 Sep 10:31). The QA service has no attendance-coverage gate; that gate exists only in the orchestrator's resolver. Attendance was empty and non-authoritative (`SOURCE_MISSING`), not a genuine zero.
- **current code vulnerable?:** Scheduler **no** (17 consecutive `WAITING` outcomes). Operator CLI **yes**: `run-qa-shadow` can still finalize on non-authoritative attendance.
- **repair required?:** **Yes, no hand edits** — recover 2026-09-17 attendance, then let the normal pipeline supersede both evaluations.

**BLAST RADIUS:**
- A — finalized QA on empty attendance: **2**, both F-03, no others ever.
- B — Perfect on empty attendance: **1**, F-01, published.
- C — rendered without projection: **0 true** (2 apparent, both covered by n8n rows).
- D — stopped at an intermediate stage: 10 items, **8 resolved**, **2 open** (F-02).
- E — ownership → missing target: **0**.
- **F (new) — duplicate legacy lectures: 3 caused by the coded platform on 22 Sep**, plus 1 historical n8n-only group.
- **G (new) — duplicate transcript artifacts: 94 pairs.**
- Plus 3 coded rows under a non-canonical id, downstream join impact unverified.

**SCHEDULER SAFE TO ENABLE:** **NO**

**Exact blockers before enabling:**
1. **Close the duplicate-insert path (F-02 Fix 1).** The writer must refuse `WOULD_INSERT` when a `qa_doctors_sessions` row already exists for the same lecture occurrence, and `sync_safety.py` must classify that refusal as manual. This defect has already written three duplicate rows into a table other company systems read, and nothing in the current code prevents a fourth. The risk inside tonight's 3-day window is narrow, but it is not closed.
2. **Decide the three existing duplicate rows** (2026-09-03 ×2, 2026-09-08) so downstream consumers stop seeing contradictory copies — including 09-08's `met_count` 11 vs 10.
3. **Decide the published F-01 Perfect row**, live with 0 attendees.

Not blocking the 3-day scheduler, but due before any backfill or widened lookback: F-02 Fix 2
(identity), the two operator-CLI hardening changes, attendance recovery for 2026-09-17, and
verification of the 3 variant-only rows against the recording workflow.
