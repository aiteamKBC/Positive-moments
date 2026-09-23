# QA Core — Production Database & Data Quality Audit

**Date:** 2026-09-22 · **Release audited:** `qa-core-rc3` (`f85932e45a4ecb413cb198cae57b8e4b81cfcd1e`)
**Window:** 2026-09-01 → 2026-09-22 · **Backfill audited:** `14e7ee8b-2fe5-4fe8-bccf-074fd61b795b`
**Written for:** the engineering and operations leads deciding whether to enable the nightly scheduler.

**Audit only. No production data was modified.** Every connection was opened with
`options="-c default_transaction_read_only=on"`, the setting was read back and asserted, and a
deliberate write was attempted and **refused** (`ReadOnlySqlTransaction: cannot execute CREATE TABLE
in a read-only transaction`). No Graph, OpenAI, n8n, SharePoint, media-worker or FFmpeg call was made.
No credential is reproduced anywhere in this report.

Reproduce with: `python tools/audit_qa_core_production.py --from 2026-09-01 --to 2026-09-22`

---

## 1. Executive summary

The RC3 release itself is sound, and the fix it shipped demonstrably worked in production. Several
backfill days required **10 and 11 orchestration passes and completed with zero
`MAX_PASSES_REACHED` errors** — arithmetically impossible under RC2's cap of 8. That is direct
evidence, from persisted state, that the compatibility projection now reaches `LEGACY_QA_SYNC` for
lectures discovered from nothing.

The backfill's own behaviour was conservative and correct: **36 coded-owned compatibility rows exist,
all 36 created by INSERT, and `write_status = 'UPDATED'` has never occurred — not once, in the entire
history of the table.** No foreign or historical n8n data has ever been overwritten by the coded
platform. 650 historical rows sit alongside untouched.

Structural integrity is clean. **Zero orphan rows** across thirteen referential checks, zero duplicate
canonical identities, zero duplicate compatibility projections, zero checklist-invariant violations
across 53 evaluations, and **zero field-quality violations** across twelve code-derived domain rules.

The audit nevertheless returns **FAIL**, on three distinct data findings — none of which were caused
by RC3, and one of which is visible to downstream consumers right now:

1. **A Perfect Lecture was published on an empty attendance snapshot** (`Martech - Thur`,
   2026-09-17). It is live in `qa_perfect_lectures` with `engagement = 0.00` and
   `attended_count = 0`, written on 18 September under the superseded
   `legacy_qa_v8_perfect_v1` policy. The current `kbc_perfect_v2_attendance_required` policy
   correctly refuses it — but nothing retracts an already-published row, by design.
2. **Two lectures on 2026-09-02 have no legacy compatibility row** and none is protected, because
   `EVALUATE_PERFECT` ran and persisted nothing, stalling them before `SYNC_LEGACY_QA`.
3. **Two finalized QA evaluations rest on empty attendance snapshots** (both 2026-09-17), each
   scoring 11/11 on a lecture with zero attendees.

Separately, **three telemetry defects** were confirmed. The most serious is that the dashboard's
"suppressed" counter does not count duplicate suppressions at all; it reports a residual bucket, and
in this run it was displaying two *incomplete* lectures as *suppressed* ones. The two numbers
coincided at 2 by chance, which is exactly why it looked plausible.

---

## 2. Overall result

> # FAIL
>
> Three data findings require action before the nightly scheduler is enabled. The RC3 code is not
> implicated in any of them; two predate it and one is an `EVALUATE_PERFECT` persistence gap.
> Nothing found requires a schema change, and nothing requires touching foreign data.

| Category | Result |
|---|---|
| Schema / migration contract | **PASS** (9/9) |
| Backfill arithmetic reconciliation | **PASS** (6/6 cross-table agreements) |
| Referential integrity / orphans | **PASS** (0 orphans, 13 checks) |
| Field quality / domain rules | **PASS** (0 violations, 12 rules) |
| Transcript / cue integrity | **PASS** |
| QA evaluation integrity | **PASS** (0 violations, 15 checks) |
| Legacy compatibility ownership | **PASS** (0 overwrites ever) |
| Legacy compatibility completeness | **FAIL** (2 lectures missing a required projection) |
| Attendance contract | **FAIL** (2 finalized evaluations on empty snapshots) |
| Perfect Lecture contract | **FAIL** (1 published row now ineligible) |
| Telemetry / reporting | **FAIL** (3 defects, no data impact) |

---

## 3. Production scope audited

| | |
|---|---|
| Canonical lectures in window | **57** |
| Business days | 22 (2026-09-01 → 2026-09-22) |
| QA Core tables (from migrations 001–020) | 41, **all present** |
| Foreign tables read or projected into | 7 |
| Public relations in the shared database | 170 |
| Indexes on QA Core tables | 122 |

The database is shared with many other company systems (Django dashboards, attendance, bookings,
YouTube analytics). Only the 48 tables in scope were queried.

---

## 4. Database schema inventory

Derived from `app/db/migrations/*.sql` and the repository layer, **not** from a supplied list.

### QA Core owned tables (41)

| Group | Tables | Role |
|---|---|---|
| Discovery / registry | `lecture_sessions`, `lecture_discovery_runs` | **Source of truth** — canonical lecture identity (`lecture_id`), PK `lecture_id`, UNIQUE `(source_system, calendar_user_upn, calendar_event_id)` |
| Transcripts | `lecture_transcript_artifacts`, `_artifact_contents`, `_candidates`, `_acquisition_runs`, `_selections`, `_selection_parts`, `_selection_runs`, `_documents`, `_cues`, `_parse_runs`, `lecture_combined_transcripts` | Derived; provider artifacts + deterministic selection |
| Speakers | `lecture_transcript_speakers`, `_speaker_identities`, `_speaker_roles`, `_speaker_runs`, `lecture_speaker_resolution_runs` | Derived |
| Attendance | `lecture_attendance_snapshots`, `_snapshot_members` | Derived from the external `kbc_attendance` source |
| Engagement | `lecture_engagement_metrics`, `_participants`, `_runs` | Derived |
| LMS | `lecture_lms_snapshots`, `_snapshot_members` | Derived from `kbc_users_data` |
| QA | `lecture_qa_evaluations`, `_checklist_items`, `_evidence_clips`, `_runs`, `_generation_attempts` | Derived; model provenance |
| Rendering | `lecture_qa_rendered_sessions`, `_rendered_checklist_items`, `lecture_qa_render_runs` | Derived; the frozen payload the writer persists |
| **Ownership / audit** | `lecture_qa_legacy_writes`, `lecture_perfect_lecture_legacy_writes` | **Ownership ledger.** UNIQUE `(legacy_session_id, writer_version)`. Deliberately *no* FK to the legacy table |
| Perfect | `lecture_perfect_lecture_results` | Derived |
| Orchestration | `lecture_pipeline_runs`, `lecture_pipeline_run_items` | Audit. UNIQUE `(run_id, lecture_id)` |
| Backfill (migration 020) | `backfill_runs`, `backfill_run_days` | Audit |
| Recording | `lecture_recording_parts` | Derived coordinates |

### Foreign tables — QA Core does **not** own these

| Table | Relationship |
|---|---|
| `qa_doctors_sessions` | **One-way compatibility projection target.** 43 columns; QA Core owns exactly **22**. The other 21 (`clips_*`, `positive_clips*`, `recording_*`, `transcript_*`, `cancellation_reason`) belong to the positive-clips and recording workflows |
| `qa_doctors_checklist_items` | Compatibility target, keyed `session_id_match` |
| `qa_perfect_lectures` | Compatibility target; `recording_url`, `recap_url`, `excel_synced_at` foreign-owned |
| `aptem_auto_extracting` | **Read-only** source of active groups |
| `kbc_attendance` | **Read-only** authoritative attendance source |
| `kbc_users_data` | **Read-only** learner/LMS source |
| `qa_doctors_transcripts` | Legacy store, read for comparison only |

### Schema contract results — 9 / 9 PASS

| Contract | Result | Evidence |
|---|---|---|
| All 41 migration tables exist | **PASS** | 0 missing |
| Migration 020 `backfill_runs` | **PASS** | present |
| Migration 020 `backfill_run_days` | **PASS** | present |
| `BACKFILL` allowed in `lecture_pipeline_runs.run_type` | **PASS** | `CHECK (run_type = ANY (ARRAY['MANUAL','SCHEDULED','RECONCILE','DRY_RUN','BACKFILL']))` |
| Ownership UNIQUE `(legacy_session_id, writer_version)` | **PASS** | present — this is what makes two concurrent writers safe |
| Run item UNIQUE `(run_id, lecture_id)` | **PASS** | present |
| `qa_doctors_sessions` PK on `session_id` | **PASS** | the compatibility upsert depends on it |
| All 22 owned legacy columns present | **PASS** | 0 absent |
| `cancelled_session` is legacy `text` | **PASS** | `text`, holding `'true'` / `'false'` as designed |

No schema object required by RC3 is missing. **No schema change is recommended.**

---

## 5. Table-by-table row counts

| Table | Rows | | Table | Rows |
|---|---:|---|---|---:|
| `lecture_sessions` | 57 | | `lecture_qa_legacy_writes` | **36** |
| `lecture_pipeline_runs` | 55 | | `lecture_perfect_lecture_results` | 66 |
| `lecture_pipeline_run_items` | 224 | | `lecture_perfect_lecture_legacy_writes` | 15 |
| `backfill_runs` | 6 | | `lecture_transcript_cues` | 36,214 |
| `backfill_run_days` | 59 | | `lecture_attendance_snapshots` | 61 |
| `lecture_qa_evaluations` | 53 | | `lecture_engagement_metrics` | 60 |
| `lecture_qa_checklist_items` | 583 | | `lecture_recording_parts` | 31 |
| `lecture_qa_evidence_clips` | 2,444 | | `qa_doctors_sessions` | **686** |
| `lecture_qa_rendered_sessions` | 52 | | `qa_doctors_checklist_items` | 7,919 |
| | | | `qa_perfect_lectures` | 155 |

583 checklist rows ÷ 53 evaluations = 11.0 exactly.

---

## 6. Backfill reconciliation

Run `14e7ee8b-2fe5-4fe8-bccf-074fd61b795b`, EXECUTE mode, `COMPLETED`, 22/22 days,
2026-09-22 11:43:29 → 12:12:37 GMT.

### Reported vs recomputed

| Metric | Reported | Recomputed from `backfill_run_days` | Recomputed from `lecture_pipeline_runs` | Verdict |
|---|---:|---:|---:|---|
| matched | 57 | 57 | 57 (`lectures_seen`) | ✅ |
| newly discovered | 32 | 32 | — | ✅ |
| processed | 52 | 52 | — | ✅ |
| already complete | 51 | 51 | 51 (`completed_count`) | ⚠️ **mislabelled** |
| waiting | 4 | 4 | 4 | ✅ |
| review required | 0 | 0 | 0 | ✅ |
| failed | 0 | 0 | 0 | ✅ |
| **suppressed** | **2** | **2** | 2 (`skipped_count`) | ❌ **wrong meaning** |
| calendar events considered | — | **0** | — | ❌ **not recorded** |

`lecture_pipeline_run_items` holds exactly **57** rows — 49 `SUCCEEDED`, 4 `SKIPPED`, 4 `WAITING` —
one per canonical lecture, matching `lectures_seen` and the canonical registry. **All six cross-table
agreements pass.** There is no unexplained lecture gap: every one of the 57 has a run item and a
current resolver state.

### Pass counts — direct evidence the RC3 fix worked

| Date | Passes | Lectures | | Date | Passes | Lectures |
|---|---:|---:|---|---|---:|---:|
| 09-01 | 10 | 1 | | 09-10 | **11** | 3 |
| 09-02 | 10 | 8 | | 09-11 | **11** | 6 |
| 09-03 | **11** | 3 | | 09-15 | **11** | 1 |
| 09-04 | 4 | 8 | | 09-16 | 2 | 7 |
| 09-08 | **11** | 1 | | 09-17 | 2 | 3 |
| 09-09 | **11** | 8 | | 09-18 | 2 | 7 |
| | | | | 09-22 | **11** | 1 |

Seven days required **11 passes**; two required 10. **No day recorded a `MAX_PASSES_REACHED` error.**
Under RC2's cap of 8 every one of those days would have halted at pass 8 with that error, and the
lectures would have stopped at `QA_RENDER` with no compatibility row. This is the RC3 fix working in
production, proven from persisted state rather than asserted.

21 `SYNC_LEGACY_QA` and 1 `SYNC_PERFECT` actions executed. `legacy_rows_written = 30` reconciles
exactly: 21 QA ownership rows + 9 Perfect ownership rows created during the run.

---

## 7. Lecture-by-lecture reconciliation

All 57 lectures are in the machine-readable JSON with the full field set (dates, identity, calendar
status, actions executed, Graph/provider calls, every stage state). Classification:

| Classification | Count | Meaning |
|---|---:|---|
| **COMPLETE** | **37** | Every executable stage settled. Observed-only stages (`RECORDING_LINK`, `EXCEL_SYNC`) are excluded — they belong to other workflows |
| **WAITING_EXPECTED** | **4** | `ATTENDANCE` stage is `WAITING`. Correct, deliberate refusal |
| **DUPLICATE_SUPPRESSED** | **3** | Terminal and legitimate |
| **INCOMPLETE_EXPLAINED** | **12** | QA finished; a stage is `STALE` with a named reason (see §7.1) |
| **INCOMPLETE_UNEXPLAINED** | **1** | 2026-09-02 `Keith \| Strategy & Planning – June 2026` |

> **A note on the classification scheme.** The six labels requested did not have a truthful home for
> twelve lectures whose QA is finished and whose only outstanding stage carries an explicit reason
> from the resolver. Filing them as `INCOMPLETE_UNEXPLAINED` would have overstated the problem
> nearly thirteen-fold, so a seventh label, `INCOMPLETE_EXPLAINED`, was added and
> `INCOMPLETE_UNEXPLAINED` reserved for its literal meaning. Exactly one lecture qualifies.

**No lecture was classified complete on the strength of a run status.** Classification comes from
`PipelineStateResolver.for_lecture` — the platform's own state model, run read-only against production.

### 7.1 The 12 `INCOMPLETE_EXPLAINED` lectures

All twelve report `SELECTION = STALE`, reason **`TRANSCRIPT_CONTENT_NEWER_THAN_SELECTION`**, while
`QA_EVALUATION` and `QA_RENDER` are `COMPLETE` and the legacy projection is settled (either written or
protected). Transcript artifact *content* arrived after the selection was made, so the lineage marker
is behind. Dates: 09-01 (1), 09-02 (5), 09-03 (1), 09-04 (4), 09-15 (1).

**The QA answers are not wrong.** But re-running `SELECT_TRANSCRIPT` would re-stale everything
downstream and could turn settled compatibility rows into `WOULD_UPDATE` decisions requiring an
operator. This is a deliberate decision, not a reflex — see finding F-06.

### 7.2 The 1 `INCOMPLETE_UNEXPLAINED` lecture

`Keith | Strategy & Planning – June 2026`, 2026-09-02, `cd9136f2-81f8-5044-8a76-6f36554572ba`.
Outstanding: `PERFECT_ELIGIBILITY = MISSING`, `LEGACY_QA_SYNC = MISSING`, neither with a reason.
Root cause in §9 and finding F-02.

---

## 8. Waiting lectures

Four, exactly as expected — and all four verified from production, not accepted on trust.

| Date | Subject | `ATTENDANCE` | Why it cannot progress |
|---|---|---|---|
| 2026-09-02 | G2 Keith-Commercial Intelligence-Oct 25 | `WAITING` | No authoritative attendance. `QA_EVALUATION` and `QA_RENDER` are `MISSING`; `LEGACY_QA_SYNC` reports `NOTHING_RENDERED_TO_SYNC`. QA was never run, correctly |
| 2026-09-17 | G2 - Keith - Strategy and Planning - June 2026 | `WAITING` | Attendance snapshot exists but is **empty** (0 source rows, 0 present, 0 effective members) |
| 2026-09-17 | Martech - Thur | `WAITING` | Same — empty snapshot |
| 2026-09-18 | Ray-Managing Successful Programmes (MSP) Jan 2026 | `WAITING` | No authoritative attendance |

**WAITING is not failure.** The platform is refusing to finish QA and Perfect eligibility on a source
that has not answered, which is the designed behaviour and the right one.

One nuance worth stating plainly: the 2026-09-02 lecture's *earliest* incomplete stage is now
`SELECTION` (stale), so its `next_executable_action` reads `SELECT_TRANSCRIPT` rather than
`WAIT_FOR_ATTENDANCE_SOURCE`. Its `ATTENDANCE` stage is still `WAITING` and the underlying cause is
unchanged. A count taken from `next_executable_action` alone would report 3 waiting, not 4.

The two 2026-09-17 lectures are a harder case: they are waiting **and** already carry finalized QA
evaluations built on their empty snapshots. See F-03.

---

## 9. Duplicate suppression audit

**Three** lectures in the window carry a `duplicate_suppression` annotation, all the trailing-space
variant of `Ray-Managing Successful Programmes (MSP) Jan 2026`, all
`DUPLICATE_EVENT_SUPPRESSED` under `duplicate_event_resolution_v1`, all with
`suppressed_calendar_mapping_status = ONLINE_MEETING_NOT_FOUND` — the empty sibling of a duplicate
booking, which is exactly the shape the deterministic rule is for.

| Date | Suppressed lecture | Winner | Suppressed by |
|---|---|---|---|
| 2026-09-04 | `22abbd06…` | `39925a6e…` | **This backfill**, `SUPPRESS_DUPLICATE_EVENT` |
| 2026-09-11 | `10d1db7c…` | `701097dd…` | **This backfill**, `SUPPRESS_DUPLICATE_EVENT` |
| 2026-09-18 | `26e74d25…` | `ea3e1c87…` | Earlier run, 2026-09-19 |

All three resolve to every stage `NOT_APPLICABLE`, produce no legacy row, and are terminal. **Correct.**

### The dashboard's `suppressed_count = 2` is not counting these

`SUPPRESS_DUPLICATE_EVENT` executed on **09-04 and 09-11**. `backfill_run_days` records
`suppressed = 2` on **09-02**, and 0 on both days where suppression actually happened.

The chain: `_counts_from` maps `suppressed` ← `counts["skipped_count"]`, and `_counts()` uses
`skipped_count` as its **residual** bucket — anything not failed, not review, not waiting, and not
settled at `NOTHING_TO_DO`. A genuinely suppressed duplicate settles at `NOTHING_TO_DO` and is
therefore counted as **complete**; meanwhile the two 09-02 lectures that ended the run unsettled at
`EVALUATE_PERFECT` fell into the residual bucket and were surfaced to the operator as "suppressed".

Both numbers are 2. The coincidence is why this looked right.

---

## 10. Transcript / cue integrity — PASS

54 selections, 55 documents, 36,214 cues examined.

| Check | Violations |
|---|---:|
| Orphan selections | 0 |
| `SELECTED` with no primary part | 0 |
| `selected_part_count` vs stored parts | 0 |
| Exactly one primary part per selection | 0 |
| Selection part → missing artifact | 0 |
| Negative or inverted cue ranges | 0 |
| `cue_count` vs stored cues | 0 |
| Orphan cues | 0 |
| Orphan speaker rows | 0 |
| Speaker cue-index range within document | 0 |
| **One primary transcript id across two dates** | **0** |

The last is the one that would matter most: the legacy `session_id` **is** the primary provider
transcript id, so a shared primary across dates would collide two lectures' compatibility rows.
It does not happen.

Two documents show non-monotonic cue starts (2026-09-16 Andrew-Scheduling, 2026-09-18 Risk
Management). Both are already `PARSED_WITH_WARNINGS` with `overlapping_cue_count` of 93 and 179.
That is **overlapping speech, correctly recorded** — not corruption. Reported as INFO.

> One audit-side correction worth recording: an earlier pass flagged 48 speaker cue-range
> violations. `cue_index` is 1-based in this schema (min 1, max = `cue_count`); the check assumed
> 0-based. Corrected in the script; the true count is 0.

---

## 11. Attendance / engagement integrity

61 snapshots, 60 engagement rows.

| Check | Violations |
|---|---:|
| Engagement percentage outside 0–100 | 0 |
| Engagement score outside 0–5 | 0 |
| Negative counts, or `spoke_count > attended_count` | 0 |
| Engagement → missing attendance snapshot | 0 |
| Negative attendance counts | 0 |
| **Finalized QA on an empty attendance snapshot** | **2** ❌ |

The first five pass. The sixth is finding **F-03**: two lectures on 2026-09-17 — `Martech - Thur` and
`G2 - Keith - Strategy and Planning - June 2026` — hold `qa_status = COMPLETED` evaluations whose
attendance snapshot has `source_row_count = 0`, `present_row_count = 0`,
`effective_member_count = 0`, and whose engagement is `0.00%` with `attended_count = 0`. Both scored
**11/11 met**.

A genuine 0% engagement with real attendees is legitimate and is deliberately *not* flagged. The
defect shape is an **empty source being treated as an answer**, and that is what these two are.

Both evaluations were created on **2026-09-18**, before the attendance-required policy. The platform
now reports both lectures `WAITING` and refuses to advance them — the guard is working today. But the
evaluations already exist, and both were already projected to `qa_doctors_sessions` with
`Engagement = 0.00` and `met_count = 11`.

---

## 12. QA evaluation integrity — PASS

53 evaluations (50 `COMPLETED`, 2 `NON_DELIVERED`, 1 `REVIEW_REQUIRED`), 52 rendered sessions,
583 checklist rows, 2,444 evidence clips.

| Check | Violations |
|---|---:|
| Finalized evaluation without exactly 11 checklist rows | 0 |
| Checklist orders not exactly 1..11 | 0 |
| `met + partial + not_met ≠ 11` | 0 |
| Orphan checklist rows | 0 |
| Orphan evidence clips | 0 |
| Rendered session → missing evaluation | 0 |
| **Rendered session belonging to a different lecture** | **0** |
| Missing provenance on a finalized evaluation | 0 |
| `ai_called` with no model recorded | 0 |
| `teaching_quality_rating` outside 1–5 | 0 |
| `duration_score` outside 0–5 | 0 |
| Duplicate active evaluation per lecture/version/source | 0 |
| Valid evidence clip outside its document | 0 |

Evidence clip validation: **2,436 VALID**, 4 `SHORTER_THAN_MINIMUM`, 4 `END_NOT_AFTER_START`. The 8
rejections are the evidence policy doing its job — they are recorded as invalid, not silently
accepted, and none reached a rendered payload.

---

## 13. Legacy QA compatibility audit

### Ownership — the central result

| | |
|---|---:|
| Coded-owned rows in `qa_doctors_sessions` (all time) | **36** |
| Foreign / historical n8n rows (all time) | **650** |
| Coded rows created by INSERT (`WRITTEN`) | **36** |
| **Coded rows created by UPDATE (`UPDATED`)** | **0** |
| Rolled back | 0 |
| Ownership records with a missing target | **0** |
| Duplicate ownership rows | **0** |

**`write_status = 'UPDATED'` has never occurred.** Every coded-owned row was an insert onto a
`session_id` that did not previously exist. No foreign or historical data has ever been overwritten
by the coded platform — this is the ownership model proven from the ledger, not from intent.

### Per-lecture classification (57 lectures)

| Bucket | Count | |
|---|---:|---|
| **A — coded-owned compatibility row** | **36** | Projected and owned |
| **B — foreign / historical n8n row** | **14** | `LEGACY_ROW_NOT_CODED_OWNED`, `NOT_APPLICABLE`, **protected** |
| **C — no projection expected** | **5** | 3 suppressed duplicates + 2 with nothing rendered |
| **D — missing a required projection** | **2** ❌ | **Finding F-02** |

Bucket B is the ownership model working as designed. A pre-existing n8n row occupying a `session_id`
the coded platform would use produces `PROTECTED_EXISTING_LEGACY_ROW`, and the stage reports
`NOT_APPLICABLE` rather than putting a "sync this" instruction in front of an operator for a row that
must never be touched. **This is not a defect and requires no action.**

### Field-level check of all 22 owned columns

Every coded-owned row in the window was compared field-by-field against the
`lecture_qa_rendered_sessions` payload it came from: `meeting_id`, `subject`, `trainer`, `duration`,
`duration_score`, `engagement_score`, `Engagement`, `date`, `met_count`, `partial_count`,
`not_met_count`, `lms_module`, `lms_students_count`, `lms_students`, `overall_judgement`,
`teaching_quality_rating`, `teaching_quality_comments`, `cancelled_session`, `ksb_coverage`,
`strengths`, `areas_for_development`, `session_id`.

**Zero field-level mismatches.**

Domain rules on owned rows, all **0 violations**: `trainer` NOT NULL/non-blank; counts summing to 11;
`Engagement` within 0–100; `duration_score` / `engagement_score` within 0–5;
`teaching_quality_rating` within 1–5; `lms_students` a JSON object or array;
`lms_students_count` matching the payload array length; **`cancelled_session` exactly `'true'` or
`'false'`**.

No foreign-owned column was read as a coded responsibility, and none was modified.

---

## 14. Perfect Lecture audit

Version contract discovered from code: **`kbc_perfect_v2_attendance_required`**.

| | |
|---|---:|
| Eligibility results in window | 66 |
| `is_perfect = true` | 21 |
| Coded Perfect ownership rows | 15 |
| Results under a superseded version | 16 |
| **11/0/0 checklist rule violations** | **0** |
| Perfect with no attendance snapshot at all | 0 |
| Perfect ownership with missing target | 0 |
| Duplicate `qa_perfect_lectures` rows on one key | 0 |
| Foreign Perfect rows in window | 4 |
| Foreign enrichment present (`recording_url`/`recap_url`/`excel_synced_at`) | 3 — **untouched** |
| **Published Perfect rows now ineligible** | **1** ❌ |

The 11/0/0 rule holds without exception, and no coded process has written a foreign enrichment field.

**Finding F-01.** `2026-09-17|Martech - Thur` is live in `qa_perfect_lectures` with
`engagement = 0.00`, `attended_count = 0`, `met_count = 11`. It was written on 2026-09-18 13:13 GMT
in `CANARY_NEW_ONLY` mode under the superseded `legacy_qa_v8_perfect_v1` policy, which had no
attendance requirement. At 14:52 the same day, re-evaluation under
`kbc_perfect_v2_attendance_required` produced `is_perfect = false`,
`reason = PENDING_ATTENDANCE_DATA` — the v2 guard working correctly.

Nothing retracted the published row, and that is deliberate: the writer's
`PERFECT_SUPERSEDED_NOT_PERFECT` decision is operator-only precisely because the Excel Sync workflow
may already have exported it. The design is right; the outcome needs a human.

The sibling case is the contrast that proves the guard: `G2 - Keith - Strategy and Planning`, same
day, same empty-snapshot condition, has **only** a v2 result (`PENDING_ATTENDANCE_DATA`) and **no**
`qa_perfect_lectures` row. Waiting attendance did not produce a Perfect row for it.

---

## 15. Recording-link audit

Persisted state only. No FFmpeg, no media worker, no clipping.

| State | Lectures |
|---|---:|
| `COMPLETE` | 15 |
| `MISSING` (legitimately pending) | 35 |
| `NOT_APPLICABLE` | 7 |

| Check | Violations |
|---|---:|
| Recording part `meeting_id` ≠ lecture `meeting_id` | **0** |
| Recording part `legacy_session_id` not among that lecture's rendered ids | **0** |

25 recording parts in window; 20 legacy rows in window carry a non-empty `recording_url`.
**No recording link is associated with the wrong lecture, meeting or session.** The 35 `MISSING` are
awaiting the recording branch of QA Master Daily v8, which QA Core observes and does not run.

---

## 16. Referential integrity / orphan audit — PASS

Thirteen parent-child checks, **all zero**:

`lecture_pipeline_run_items`→run · →lecture · `lecture_qa_evaluations`→lecture ·
`lecture_qa_checklist_items`→evaluation · `lecture_qa_evidence_clips`→evaluation ·
`lecture_qa_rendered_checklist_items`→rendered_session · `lecture_transcript_selections`→lecture ·
`lecture_transcript_cues`→document · `lecture_attendance_snapshots`→lecture ·
`lecture_engagement_metrics`→lecture · `lecture_qa_legacy_writes`→lecture ·
`lecture_perfect_lecture_results`→lecture · `backfill_run_days`→backfill_run

| Check | Result |
|---|---:|
| Duplicate canonical lecture identities | 0 |
| Live lectures sharing a `meeting_id` on one date | 0 |
| **Duplicate compatibility projections** (one `session_id`, two lectures) | **0** |
| Coded ownership records with no target row | 0 |
| Duplicate active ownership rows | 0 |
| Impossible date / scheduled-range combinations | 0 |

---

## 17. Data-quality / null / domain audit — PASS

Twelve rules, each derived from the **code contract** rather than from column nullability — a nullable
column is not a defect. **Total violations: 0.**

`downstream_ready` implies `meeting_id` · `subject` non-blank · owned-row `trainer` non-blank ·
owned-row counts sum to 11 · owned-row `Engagement` in 0–100 · owned-row scores in range ·
`lms_students` JSON shape · `lms_students_count` matches payload · finalized evaluation has a
judgement · parsed document has cues · attendance counts non-negative · run-item status in domain.

---

## 18. Historical / foreign legacy row audit

**650 foreign rows** in `qa_doctors_sessions` (20 of them inside the audit window) have no
`lecture_qa_legacy_writes` ownership record. **This is expected and is not corruption.** They were
written by the n8n QA pipeline before the coded platform existed.

| Question | Answer |
|---|---|
| Did any coded process overwrite foreign data? | **No.** `write_status = 'UPDATED'` count is **0**, all time |
| Did any coded process clear a foreign-owned column? | **No.** The upsert's SET list is the 22 owned columns only; `clips_*`, `recording_*`, `transcript_*`, `cancellation_reason` are unreachable by construction |
| Did any foreign row block a required coded insertion? | **Yes — 14, and correctly.** Reported as `LEGACY_ROW_NOT_CODED_OWNED` / `NOT_APPLICABLE` |
| Overlap conflicts by date + meeting identity | None beyond the 14 protected rows |

The 14 protected rows are recorded as **INFO**, not as a defect. If the coded answer should ever
supersede n8n's for a given lecture, that is an explicit operator decision with its own audit trail —
not something this audit recommends.

---

## 19. Dedicated section — 16 September 2026

Seven canonical lectures. The day completed in **8.3 seconds** with **0 provider calls** and
**0 legacy rows written**. Speed here is not evidence of success, so it is proven from persisted state.

| Lecture | Evals before | Renders before | Legacy writes before | Backfill action | Provider | Legacy state | Class |
|---|---:|---:|---:|---|---:|---|---|
| G1- Femi - Customer Journey Optimisation | 1 | 1 | 1 | `NOTHING_TO_DO` | 0 | `COMPLETE` | COMPLETE |
| Juliane – Impact and Planning June 2026 | 1 | 1 | 1 | `SELECT_TRANSCRIPT(shared)` | 0 | `COMPLETE` | COMPLETE |
| Keith \| Strategy & Planning – June 2026 | 1 | 1 | 1 | `SELECT_TRANSCRIPT` | 0 | `COMPLETE` | COMPLETE |
| Ray \| PMP – June 2026 | 1 | 1 | 1 | `SELECT_TRANSCRIPT(shared)` | 0 | `COMPLETE` | COMPLETE |
| Steve-Earned Value Management (EVM) 2026 | 1 | 1 | 1 | `SELECT_TRANSCRIPT(shared)` | 0 | `COMPLETE` | COMPLETE |
| Andrew-Scheduling Professional (SP) Jan 2026 | 1 | 1 | 1 | `SELECT_TRANSCRIPT(shared)` | 0 | `COMPLETE` | COMPLETE |
| G2 Keith-Commercial Intelligence-Oct 25 | 1 | 1 | 1 | `SELECT_TRANSCRIPT(shared)` | 0 | `COMPLETE` | COMPLETE |

Pipeline run `8a1b1be7-480e-457a-ba29-2f6df48e55e6`: `COMPLETED`, 7 seen, 7 complete, 0 waiting,
0 skipped, 8 Graph calls, **0 provider calls**, **0 legacy rows written**, 2 passes,
0 newly discovered.

**Why it completed quickly:** every one of the seven already had a QA evaluation, a rendered session
and a coded-owned legacy write **before the backfill started**. There was genuinely nothing to buy and
nothing to write. The 8 Graph calls are read-only discovery and transcript probing; the single
`SELECT_TRANSCRIPT` was day-scoped and shared across six lectures, which is exactly the sharing
behaviour the orchestrator is designed for. The 2 passes are the correct settling cost for a day with
no work: one to attempt, one to confirm nothing moved.

**Was a QA/provider call necessary? No** — and none was made. All seven legacy rows are structurally
valid: counts sum to 11, `trainer` non-blank, `Engagement` in range, `cancelled_session` exactly
`'true'`/`'false'`, zero field mismatches against their rendered payloads.

**16 September is correct.**

---

## 20. Telemetry / reporting defects

All three are reporting-layer only. **No lecture data is affected by any of them.**

| ID | Defect | Where | Impact |
|---|---|---|---|
| **T-01** | `suppressed` counts the residual `skipped_count` bucket, not suppressions | `backfill.py:_counts_from` → `_counts()` | Operators see unsettled lectures reported as suppressed duplicates, and real suppressions counted as complete |
| **T-02** | `calendar_events_considered` hardcoded to 0 on the EXECUTE path | `backfill.py:_record` default `day_counts` | 239 calendar events were examined across 22 discovery runs; the dashboard shows 0. PREVIEW populates it correctly |
| **T-03** | `already_complete` means "complete at end of run", not "was already complete" | `backfill.py:_counts_from` | 2026-09-02 reports 8 newly discovered **and** 5 already complete — impossible as labelled |

---

## 21. Findings requiring action

### F-01 · Perfect Lecture published on an empty attendance snapshot

| | |
|---|---|
| **Severity** | **HIGH** |
| **Tables** | `qa_perfect_lectures`, `lecture_perfect_lecture_results`, `lecture_perfect_lecture_legacy_writes` |
| **Lecture** | `9671a3f1-3b46-5b18-a585-74b90964aa45` — `Martech - Thur` |
| **Date** | 2026-09-17 (published 2026-09-18 13:13 GMT) |
| **Evidence** | `qa_perfect_lectures` JOIN `lecture_perfect_lecture_legacy_writes` JOIN `lecture_perfect_lecture_results` (version `kbc_perfect_v2_attendance_required`) WHERE `is_perfect IS FALSE` |
| **Data actually wrong?** | **Yes**, and visible downstream |
| **Recommended fix** | **Operator decision, not automated.** Recover attendance for 2026-09-17 first; if the lecture is genuinely not perfect, retract the published row through the writer's rollback path. Retracting has outward consequences — Excel Sync may already have exported it. **Not implemented.** |

Published with `engagement = 0.00` and `attended_count = 0` under the superseded
`legacy_qa_v8_perfect_v1` policy. The current policy correctly refuses it.

### F-02 · Two rendered lectures have no legacy compatibility row

| | |
|---|---|
| **Severity** | **HIGH** |
| **Tables** | `qa_doctors_sessions`, `lecture_qa_legacy_writes`, `lecture_perfect_lecture_results` |
| **Lectures** | `d19e74e2-6df2-5ab5-8671-9bccee97e086` (G1- Femi - Customer Journey Optimisation); `cd9136f2-81f8-5044-8a76-6f36554572ba` (Keith \| Strategy & Planning – June 2026) |
| **Date** | 2026-09-02 |
| **Evidence** | `LEGACY_QA_SYNC` in (`MISSING`,`STALE`) while `QA_RENDER = COMPLETE`; both have `COMPLETED` evaluations and `RENDERED` renders but **no `lecture_perfect_lecture_results` row at all** |
| **Data actually wrong?** | **Yes.** Downstream consumers cannot see these two lectures |
| **Recommended fix** | Investigate why `EVALUATE_PERFECT` executed without persisting a result, then `RETRY` through the orchestrator. **Do not write the rows by hand.** Both target `session_id`s are free, so a clean `WOULD_INSERT` is available. **Not implemented.** |

**Root cause.** `EVALUATE_PERFECT` appears in both lectures' executed-action lists and returned
without error, but persisted no eligibility result. `PERFECT_ELIGIBILITY` therefore stayed `MISSING`,
the orchestrator's stall detection saw the action change nothing and stopped advancing them, and
`SYNC_LEGACY_QA` was never reached. This is **not** the pass cap: the 09-02 run settled at 10 passes
with no `MAX_PASSES_REACHED` error. These are the same two lectures the dashboard mislabelled as
"suppressed" (T-01).

### F-03 · Finalized QA evaluations resting on empty attendance snapshots

| | |
|---|---|
| **Severity** | **HIGH** |
| **Tables** | `lecture_attendance_snapshots`, `lecture_engagement_metrics`, `lecture_qa_evaluations`, `qa_doctors_sessions` |
| **Lectures** | `9671a3f1…` (Martech - Thur); `de8c6c60-6e73-5b50-b74d-ef5806b9d1bb` (G2 - Keith - Strategy and Planning - June 2026) |
| **Date** | 2026-09-17 |
| **Evidence** | snapshot `source_row_count = 0 AND effective_member_count = 0` AND `qa_status IN ('COMPLETED','NON_DELIVERED')` |
| **Data actually wrong?** | **Yes.** Both scored 11/11 with 0 attendees; both projected to `qa_doctors_sessions` with `Engagement = 0.00` |
| **Recommended fix** | Recover attendance for 2026-09-17, then let the normal pipeline supersede the evaluations. The platform already reports both `WAITING` and refuses to advance them. **Not implemented.** |

### F-04 · `suppressed` counter does not count suppressions

**HIGH** · telemetry only · `backfill_runs`, `backfill_run_days`, `lecture_pipeline_runs` ·
2026-09-02 vs 09-04 / 09-11 · See §9 and T-01.
**Fix:** count `SUPPRESS_DUPLICATE_EVENT` actions (or `duplicate_suppression` annotations) for the
suppressed counter, and surface `skipped_count` under a name that says what it is, e.g. `unsettled`.
**Not implemented.**

### F-05 · `calendar_events_considered` always 0 on EXECUTE

**MEDIUM** · telemetry only · `backfill_run_days`, `lecture_discovery_runs` ·
**Fix:** have `_counts_from` carry `calendar_events_found` out of `summary["discovery"]` the way
`_preview_day` already does, and pass it to `_record` as `day_counts`. **Not implemented.**

### F-06 · Twelve lectures carry a stale selection lineage

**MEDIUM** · not wrong data · `lecture_transcript_selections` · 09-01, 09-02, 09-03, 09-04, 09-15 ·
`SELECTION = STALE / TRANSCRIPT_CONTENT_NEWER_THAN_SELECTION` while QA is complete.
**Fix:** decide deliberately. Re-running `SELECT_TRANSCRIPT` would re-stale everything downstream and
could convert settled compatibility rows into `WOULD_UPDATE` decisions requiring an operator. The QA
answers are not wrong; only the lineage marker is behind. **Not implemented.**

### F-07 · `already_complete` mislabelled

**LOW** · telemetry only · `backfill_run_days` · See T-03. **Not implemented.**

### F-08 · Sixteen Perfect results under a superseded eligibility version

**MEDIUM** · not wrong data · `lecture_perfect_lecture_results` · The resolver already treats these as
not-current. **Fix:** re-run `EVALUATE_PERFECT` when convenient. **Not implemented.**

### F-09 · Fourteen foreign rows protected from coded insertion

**INFO** · not a defect · `qa_doctors_sessions` · The ownership model working as designed.
**Fix: none. Do not overwrite.**

### F-10 · Two documents with non-monotonic cue starts

**INFO** · not a defect · `lecture_transcript_cues` · Overlapping speech, already recorded as
`overlapping_cue_count` with `PARSED_WITH_WARNINGS`. **Fix: none.**

---

## Appendix — audit method

- `tools/audit_qa_core_production.py`, read-only, deterministic, safe to re-run.
- Connection: `options="-c default_transaction_read_only=on"`, setting asserted, write probe refused.
- Lecture state comes from `PipelineStateResolver.for_lecture` — the platform's own state model —
  rather than a second implementation of it, so the audit checks production and not its own arithmetic.
- Machine-readable output: `docs/audits/QA_CORE_PRODUCTION_DATA_AUDIT_2026-09-22.json`.
- No transcript text, learner name, credential or connection string was selected or recorded.
