# QA Core — Safety Fix Validation (F-01, F-02, F-03)

**Date:** 2026-09-22 · **Base:** `qa-core-rc3` (`f85932e45a4ecb413cb198cae57b8e4b81cfcd1e`)
**Follows:** [QA_CORE_F01_F02_F03_ROOT_CAUSE_2026-09-22.md](QA_CORE_F01_F02_F03_ROOT_CAUSE_2026-09-22.md)
**Written for:** the engineering and operations leads who will approve the controlled data-repair phase.

**Code and tests only. Nothing is committed. No production data was modified.** Every production
connection in this phase used `options="-c default_transaction_read_only=on"`, asserted
`transaction_read_only = on`, and proved it with a deliberate write that PostgreSQL refused
(`ReadOnlySqlTransaction`). No Graph, OpenAI, SharePoint or n8n call was made. No duplicate row was
deleted, no transcript artifact was merged, the Martech Perfect row was not retracted, no attendance
was recovered, and the scheduler was not enabled.

---

## 1. Files changed

**New (3)**

| File | Purpose |
|---|---|
| `app/transcripts/identity.py` | Fix C — canonical transcript identity: strict MessagePack + LZ4 decoder |
| `app/writer/legacy_identity.py` | Fixes A + B — the single same-occurrence guard shared by the writer and resolver |
| `tests/unit/test_safety_fixes_f01_f02_f03.py` | 69 regression tests |

**Modified (14)** — 325 insertions, 13 deletions

| File | Fix | Change |
|---|---|---|
| `app/writer/service.py` | A | Guard evaluated before any `WOULD_INSERT`; `_retarget` re-addresses to an owned row |
| `app/writer/modes.py` | A | New decision `BLOCKED_AMBIGUOUS_LEGACY_IDENTITY` |
| `app/orchestration/sync_safety.py` | A | That decision is manual: `LEGACY_IDENTITY_AMBIGUOUS` |
| `app/orchestration/state.py` | B | `_legacy_qa_sync` asks the same guard before reporting `MISSING` |
| `app/db/repositories/pipeline_observations.py` | B | Exposes the shared guard to the resolver |
| `app/db/repositories/transcript_selections.py` | C | Candidates collapsed by canonical identity; first-seen spelling wins |
| `app/qa/perfect.py` | D | `PUBLISHABLE_PERFECT_ELIGIBILITY_VERSIONS` allowlist, `may_publish()` |
| `app/writer/perfect_service.py` | D | Planner refuses a non-publishable policy in any write mode |
| `app/cli/main.py` | D | `write-legacy-qa` refuses v1 with a write mode before connecting |
| `app/qa/service.py` | E | Refuses to finalize on non-authoritative attendance |
| `app/db/repositories/qa_shadow.py` | E | QA input carries the consumed snapshot's frozen counts |
| `tests/unit/test_backfill_legacy_compatibility.py` | — | Harness gains a fake occurrence repository over its own fake tables |
| `tests/unit/test_pipeline_state.py` | — | Resolver stub gains `legacy_qa_occurrence`, defaulting to `CLEAR` |
| `tests/unit/test_shadow_qa.py` | — | Shared fixture now states the snapshot counts its 10 attendees already implied |

**No migration.** Canonical identity is derived deterministically from the retained raw id at read
time, so it works for every historical row without backfilling a column. That removes any deploy-order
dependency from a safety release. A persisted `canonical_transcript_key` column is proposed in §12 for
the repair phase.

---

## 2. Exact code changes

### Fix A — writer (`LegacyQaWriter._plan_one`)

The guard runs **only** when the exact `session_id` lookup finds nothing — the single path that
previously produced `WOULD_INSERT`. The readiness and payload checks keep their precedence.

| Guard verdict | Writer behaviour | Case |
|---|---|---|
| (exact owned row exists) | guard not consulted; existing ownership policy | **A** |
| `FOREIGN_SAME_OCCURRENCE` | the foreign row becomes the target → `PROTECTED_EXISTING_LEGACY_ROW`, in every mode | **B** |
| `OWNED_SAME_OCCURRENCE` | payload re-addressed to the owned row's `session_id` → `WOULD_SKIP_IDENTICAL` / `WOULD_UPDATE` on that row; never a second row | **C** |
| `AMBIGUOUS` | `BLOCKED_AMBIGUOUS_LEGACY_IDENTITY` → manual | **D** |
| `CLEAR` | unchanged — `WOULD_INSERT` for a genuinely new lecture | — |

`_retarget` moves the key **and** all eleven checklist rows' `session_id` and
`session_id_match` (`<session_id>_<order>`). Leaving the old match key would have written eleven
orphan checklist rows under an id no session row carries.

Each plan now reports `proposed_session_id`, `same_occurrence_verdict`,
`same_occurrence_session_id` and the evidence (`relation`, `basis`, id tails only).

### Fix E — QA service (`ShadowQaService._evaluate`)

```python
coverage = package_attendance_coverage(package)   # shared classify() over the consumed snapshot
...
if not is_authoritative(coverage):                 # the orchestrator's own predicate
    return {... "qa_status": WAITING_FOR_ATTENDANCE_SOURCE,
            "review_reason": ATTENDANCE_NOT_AUTHORITATIVE, "persisted": False,
            "ai_called": False, "provider_calls": 0, "existing_evaluation_id": ...}
```

It runs **before** evaluation reuse, so a stale finalized answer (G2 Keith's) is reported as existing
but never re-presented as current. The status is non-final, never persisted, and named after the
orchestrator's `WAIT_FOR_ATTENDANCE_SOURCE`.

---

## 3. Writer guard design (`app/writer/legacy_identity.py`)

**Invariant:** one canonical lecture occurrence ↔ at most one `qa_doctors_sessions` row.

**Candidates:** legacy rows on this lecture's dates — the canonical `session_date` and every legacy
UTC `legacy_date` its renders carry — plus any row the coded writer already owns for this `lecture_id`
on any date.

**This lecture's transcript identities:** every transcript it has been selected under (all selection
parts, all selection primaries) plus every `session_id` its renders targeted, each canonicalized. A
superset is the safe direction: it can only make more rows count as "this occurrence", never fewer.

**Per-candidate classification** (subject is **never** used):

| Condition | Relation |
|---|---|
| canonical transcript identity matches **and** another lecture owns the row | `UNRESOLVED` |
| canonical transcript identity matches | `SAME` |
| coded writer owns it for **this** `lecture_id` | `SAME` |
| coded writer owns it for **another** lecture | `DIFFERENT` |
| same `meeting_id`, same date, no proof either way | `UNRESOLVED` |
| different meeting | `DIFFERENT` |

**Verdict:** no `SAME`/`UNRESOLVED` → `CLEAR`; exactly one `SAME`, nothing unresolved → `FOREIGN_` or
`OWNED_SAME_OCCURRENCE`; anything else → `AMBIGUOUS`. The classifier is a pure function; the
repository issues SELECTs only (asserted structurally by a test).

**Deliberately conservative:** a meeting with two genuine occurrences on one day, where n8n recorded
one, now goes to manual review instead of auto-inserting the second. That is the correct trade for
"never guess".

---

## 4. Transcript canonical identity design (`app/transcripts/identity.py`)

The Graph transcript id is **not opaque**. Base64url-decoded, it is MessagePack in the
MessagePack-CSharp LZ4 envelope:

```
array(2) [ ext(type 98, data = uncompressed length), bin(LZ4 block) ]
  └── LZ4 → array(4) [ 4, "<thread id>", "<meeting timestamp ms>", "<transcript id>" ]
```

The second serialization is `array(5) [ …, nil ]`. Every "+1" byte in the 94 production pairs is a
length field growing to hold that `nil`: the array header `0x94→0x95`, the LZ4 literal length, the
envelope's uncompressed length and the `bin` length. The trailing `0xC0` **is** the `nil`.

**Canonical key:** `teams:v4:<thread>|<meeting ts>|<transcript id>` — the decoded tuple with trailing
nils removed. Not "drop the last byte": that would be correct for one serializer and silently wrong
for the next.

**Proof the model is right, not just that the decoder returns something:** a test-side encoder
regenerates **both real production ids byte-for-byte** from their decoded fields.

**Refusals — the id keeps its RAW identity, which equals only the identical string:**

| Input | Result |
|---|---|
| not base64url, empty, `None` | RAW |
| wrong envelope / missing ext 98 / header-block mismatch | RAW |
| LZ4 length mismatch, out-of-range back-reference, truncation | RAW |
| trailing bytes after the envelope | RAW |
| format version ≠ 4 | RAW |
| extra **non-nil** field | RAW — never collapsed away |
| thread without `@thread`, non-numeric timestamp, blank field | RAW |

The failure mode is *not deduplicated*, never *wrongly deduplicated*.

**Raw id retained:** `artifact_id` is still `uuid5(provider + raw id)`; content fetch still uses the
raw id. No dependency added — MessagePack and LZ4 are implemented against their published specs,
bounds-checked, with a 16 KiB decode cap.

**Selection:** `collapse_equivalent_transcripts` keeps one candidate per canonical identity, choosing
the spelling **seen first** (earliest `first_seen_at`, then `artifact_id`). A later re-serialization
can therefore never displace the id a lecture is already published under, and one transcript can
never be attached twice as two "parts". Row order is preserved, so the selector's own tie-breaking is
unchanged.

---

## 5. State resolver consistency (Fix B)

`PipelineStateResolver._legacy_qa_sync` now asks `legacy_observations.legacy_qa_occurrence`, which
delegates to the **same** `LegacyOccurrenceGuard` instance type the writer uses. A structural test
asserts neither module holds a private copy of the matching rules.

| Guard verdict | Resolver stage |
|---|---|
| `FOREIGN_SAME_OCCURRENCE` | `NOT_APPLICABLE` · `LEGACY_ROW_NOT_CODED_OWNED` — identical to an exact-id foreign row |
| `OWNED_SAME_OCCURRENCE` | `STALE` · `SYNC_LEGACY_QA` · `LEGACY_ROW_UNDER_EQUIVALENT_SESSION_ID` |
| `AMBIGUOUS` | `REVIEW_REQUIRED` · `MANUAL_REVIEW_REQUIRED` · `LEGACY_IDENTITY_AMBIGUOUS` |
| `CLEAR` | `MISSING` · `SYNC_LEGACY_QA` (unchanged) |

**Invariant test:** across five scenarios the resolver offers an automatic insert
(`MISSING` + `SYNC_LEGACY_QA`) **if and only if** the writer, planning the same starting state in
`DRY_RUN`, returns `WOULD_INSERT`. Verified again on production: **0 disagreements across 52 lectures.**

---

## 6. CLI hardening

**Fix D.** `write-legacy-qa --perfect-policy legacy_qa_v8_perfect_v1` is refused in every
write-enabled mode by `guard_write_command`, before any connection opens, and even with
`--skip-perfect-lecture`. `PerfectLecturePlanner` enforces the same rule independently, so a runner,
console action or future caller cannot bypass it. v1 remains available for `DRY_RUN` historical
reproduction. The default, scheduler and backfill policy remains `kbc_perfect_v2_attendance_required`.
The rule is an **allowlist**: a future policy is refused in write modes until someone adds it
deliberately.

**Fix E.** `run-qa-shadow` returns `WAITING_FOR_ATTENDANCE_SOURCE` for `SOURCE_MISSING`,
`SOURCE_PARTIAL_OR_INVALID` and `SOURCE_UNKNOWN`. It buys nothing and persists nothing. An
**authoritative** confirmed zero (`SOURCE_AVAILABLE_CONFIRMED_ZERO`) is still evaluated: a real,
reported zero is a finding, unlike silence. A package with no counts classifies as `SOURCE_UNKNOWN`
and is refused, so a caller that forgets them fails closed.

---

## 7. Tests added — 69, all passing

| # | Requirement | Test(s) |
|---|---|---|
| 1 | Same lecture + canonical id + n8n row → no insert | `test_1_…` |
| 2 | Same lecture + variant id + same n8n row → no insert | `test_2_…`, `test_2b_…` (every mode) |
| 3 | Coded row under old id → no second insert | `test_3_…`, `test_3b_…` (update lands on the published id with its 11 checklist keys) |
| 4 | Same date, different meeting → no collision | `test_4_…` |
| 5 | Identical subject alone → no collision | `test_5_…` |
| 6 | Ambiguous → manual, never auto-write | `test_6a_…`, `test_6b_…`, `test_6c_…`, ownership-elsewhere test |
| 7 | 94/94 pairs canonicalize | `test_ninety_four_synthetic_pairs_…` + **production: 94/94 (§11)** |
| 8 | No unrelated collapse | 500 distinct transcripts → 500 identities; single-field changes; RAW refusals |
| 9 | F-02 → NOT `WOULD_INSERT` | `test_9_…` + **production: both `PROTECTED` (§10)** |
| 10 | Already-duplicated lectures → no third row | `test_10_…` × both spellings × three modes |
| 11 | v1 write blocked; dry run allowed; v2 unchanged | `test_11_…` through `test_11f_…` |
| 12 | `run-qa-shadow` + `SOURCE_MISSING` → cannot finalize | `test_12_…` for **both** F-03 lectures; `test_12b_…` (stale answer not reused); `test_12c_…` (every non-authoritative status) |
| 13 | Authoritative attendance unchanged | `test_13_…`, `test_13b_…` (confirmed zero still evaluated) |
| 14 | RC3 max-pass regression green | `test_14_…` + existing suite |
| — | Writer ⇔ resolver invariant | 5 scenarios + protected-history and review-state tests |
| — | Format model proven | encoder reproduces both real production ids byte-for-byte |
| — | Guards issue only SELECTs; one shared implementation | structural tests |

---

## 8. Full test-suite result

| Suite | Result |
|---|---|
| Offline suite (`tests/unit` + `automation/lecture_parts/test_graph_client.py`) | **1417 passed, 0 failed** (1348 before + 69 new) |
| New safety suite alone | **69 passed** |
| Integration suite (`tests/integration`, 362 tests) | **NOT RUN in this phase — deliberately** |

**Why the integration suite was withheld.** It connects through `Settings.from_environment()`, which
loads `backend/.env`, i.e. the **production** `DATABASE_URL`, over a read-write connection. It then
executes real DML inside transactions it rolls back. A rolled-back write is still a write executed on
production: it takes locks, can fire triggers and consumes sequence values. This phase forbids
production writes, so it was not run.

To cover the gap, the SQL this change adds or modifies was exercised against real PostgreSQL,
read-only:

- the occurrence guard's repository (52 lectures);
- the extended QA input query (17 packages, 3 dates);
- the extended candidate loader (54 lectures).

**The integration suite must pass against a non-production database before release** (see §12).

---

## 9. Production read-only dry-run results

The real `LegacyQaWriter` in `DRY_RUN` and the real `PipelineStateResolver`, against production:

| Group | Lecture | Date | New writer decision | Guard verdict | Resolver stage |
|---|---|---|---|---|---|
| **F-02** | `d19e74e2…` G1- Femi - Customer Journey Optimisation | 09-02 | **`PROTECTED_EXISTING_LEGACY_ROW`** | `FOREIGN_SAME_OCCURRENCE` | `NOT_APPLICABLE` |
| **F-02** | `cd9136f2…` Keith \| Strategy & Planning – June 2026 | 09-02 | **`PROTECTED_EXISTING_LEGACY_ROW`** | `FOREIGN_SAME_OCCURRENCE` | `NOT_APPLICABLE` |
| Duplicated | `24720852…` G2 - Femi - Customer Journey Optimisation | 09-03 | `WOULD_SKIP_IDENTICAL` | (exact owned) | `COMPLETE` |
| Duplicated | `4a4c01dc…` Stephen-Portfolio Management 2026 | 09-03 | `WOULD_SKIP_IDENTICAL` | (exact owned) | `COMPLETE` |
| Duplicated | `5a7f7932…` G1 Keith-Commercial Intelligence-Oct 25 | 09-08 | `WOULD_SKIP_IDENTICAL` | (exact owned) | `COMPLETE` |
| Variant-only | `d38dc566…` Samar - Marketing Technology (MarTech) oct 25 | 09-09 | `WOULD_SKIP_IDENTICAL` | (exact owned) | `COMPLETE` |
| Variant-only | `f7c4df2e…` G1- Femi - Customer Journey Optimisation | 09-09 | `WOULD_SKIP_IDENTICAL` | (exact owned) | `COMPLETE` |
| Variant-only | `b1949e45…` G3 - Femi - Customer Journey Optimisation | 09-11 | `WOULD_SKIP_IDENTICAL` | (exact owned) | `COMPLETE` |

For the F-02 pair the target resolves to n8n's canonical row (`…VjI=`) while the proposal is the
variant (`…VjLA`). That is exactly the case the exact-id lookup could not see.

**Sweep of every rendered lecture 2026-09-01 → 2026-09-22 (52):** 36 `WOULD_SKIP_IDENTICAL`,
16 `PROTECTED_EXISTING_LEGACY_ROW`, **0 `WOULD_INSERT`**, **0 writer/resolver disagreements**.

**F-03 coverage the new service computes from production snapshots:** Martech - Thur →
`SOURCE_MISSING`, not authoritative; G2 - Keith - Strategy and Planning → `SOURCE_MISSING`, not
authoritative. Both would now return `WAITING_FOR_ATTENDANCE_SOURCE`. All 15 other lectures loaded on
09-02, 09-16 and 09-17 are authoritative, so their QA behaviour is unchanged.

---

## 10. F-02 decisions — before vs after

Each of the 52 rendered September lectures was planned twice on production: once with a guard that
always answers `CLEAR`, which is exactly the old exact-id-only behaviour, and once with the fix.

| | Lectures |
|---|---:|
| Planned | 52 |
| **Decisions that changed** | **2** |

| Lecture | Before | After |
|---|---|---|
| `d19e74e2-6df2-5ab5-8671-9bccee97e086` (09-02) | `WOULD_INSERT` | `PROTECTED_EXISTING_LEGACY_ROW` |
| `cd9136f2-81f8-5044-8a76-6f36554572ba` (09-02) | `WOULD_INSERT` | `PROTECTED_EXISTING_LEGACY_ROW` |

The fix is surgical: the only behaviour it changes in production is the one it was written to change.

---

## 11. 94 transcript-pair canonicalization proof

Every artifact in `lecture_transcript_artifacts`, read-only:

| | |
|---|---:|
| Artifacts | 338 |
| Decoded by the full envelope → LZ4 → payload path | **338 / 338** (0 RAW) |
| Identity groups | 244 = **150 singletons + 94 pairs** |
| Canonical/variant pairs known by SQL | 94 |
| **Pairs collapsing to one identity** | **94 / 94** |
| Groups larger than 2 | 0 |
| Groups whose members disagree on meeting, start or end (a wrong collapse) | **0** |

The candidate loader collapses **0** candidates for any of 54 September lectures today, because no
lecture currently sees both spellings of one transcript. No existing selection changes.

---

## 12. Remaining risks

1. **Integration suite not run.** It targets production by default. It must be run against a
   non-production database before this change is released. Separately, the default itself is a
   process hazard: the test configuration should refuse a production `DATABASE_URL`.
2. **Existing data unchanged, by instruction.** The 3 duplicate `qa_doctors_sessions` rows, the
   published Martech Perfect row (F-01), the two F-03 evaluations and the 94 twin artifacts all remain.
   The code will not add to them. It does not remove them either; that is the repair phase.
3. **F-02 lectures remain unsettled.** Their `LEGACY_QA_SYNC` is now correctly `NOT_APPLICABLE`, but
   `PERFECT_ELIGIBILITY` stays `MISSING` (`PERFECT_BLOCKED_KEY_COLLISION` is never persisted), so their
   next action is still `EVALUATE_PERFECT` / `SELECT_TRANSCRIPT`. That is harmless — the collision can
   only refuse — and both are outside the 3-day window, but they will not reach `NOTHING_TO_DO` without
   a repair decision.
4. **Acquisition still stores twins.** A future Graph re-serialization will still create a second
   artifact row. It is now harmless to selection and to the legacy writer, but it is waste. Proposed
   for the repair phase: an additive `canonical_transcript_key` column, backfilled, then a unique index
   once the 94 existing pairs are merged.
5. **Deliberately conservative ambiguity.** Two genuine occurrences of one meeting on one day, where a
   foreign row exists for one, now go to manual review. No such case exists in production today (sweep:
   0 `AMBIGUOUS`).
6. **Other operator paths.** `refresh_deterministic` and `revalidate_evidence` were not given the
   coverage guard. Both run only after QA has already been reached, which the orchestrator gates on
   authoritative attendance, and neither is exposed as a free-standing finalizing CLI. Worth closing
   in a follow-up for defence in depth.
7. **Two undecodable legacy `session_id`s** (the "other" form found earlier) are matched by exact
   string only. They are n8n history and no coded lecture targets them.

---

**CODE SAFETY STATUS:** **PASS** — offline suite 1417/1417 and production read-only validation;
conditional on the integration suite passing against a non-production database before release.

**F-02 duplicate insert path closed:** **YES**

**94/94 transcript identity pairs canonicalized:** **YES**

**Operator F-01 path closed:** **YES**

**Operator F-03 path closed:** **YES**

**Existing production data modified:** **NO**

**Safe to begin controlled data repair phase:** **YES** — the code can no longer add to the defects
the repair will remove. Enabling the scheduler remains a separate decision. The three duplicate rows,
the published F-01 Perfect row and the two F-03 evaluations still need their own repair decisions,
and the integration suite still needs a clean run on a non-production database.
