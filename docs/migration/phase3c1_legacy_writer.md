# Phase 3C1: controlled legacy QA writer and cutover guardrails

```
lecture_qa_rendered_sessions + _checklist_items     (Phase 3B payload)
        │
        ▼  ownership + mode policy + invariant gate
lecture_qa_legacy_writes                            (coded-platform ownership)
        │
        ▼  only under a write-enabled mode, and not in this phase
qa_doctors_sessions + qa_doctors_checklist_items    (legacy production)
```

**No legacy row was written in Phase 3C1.** The default mode is `DRY_RUN`, and
every write-enabled behaviour was proven inside transactions that were rolled
back. Writer version **`legacy_qa_v8_writer_v1`**.

## 1. The live legacy schema

| | `qa_doctors_sessions` | `qa_doctors_checklist_items` |
| --- | --- | --- |
| Primary key | `session_id` | `session_id_match` |
| Unique indexes | the PK only | the PK only |
| Foreign keys | referenced **by** checklist items (ON DELETE CASCADE), transcripts, split plans | `session_id` → sessions, ON DELETE CASCADE |
| Triggers | none | none |
| NOT NULL | `session_id`, **`trainer`** | `session_id`, `session_id_match`, `checklist_item`, `status` |
| Rows | 650 | 7,523 |

Two details that shaped the mapping:

- **`cancelled_session` is TEXT**, holding the JavaScript booleans n8n wrote
  (`'false'` ×634, `'true'` ×16). A Python bool is rendered the same way.
- The sessions table also carries **`clips_*` and `recording_*` columns owned
  by other workflows** (Positive Clips, the recording lookup). The writer maps
  only the 22 legacy-mapped columns, and its `ON CONFLICT DO UPDATE` list
  excludes everything else, so another workflow's value is never overwritten
  or cleared. A test proves this by setting a recording URL and re-running an
  update.

## 2. Modes

| Mode | Writes? | Behaviour |
| --- | --- | --- |
| `DISABLED` | no | nothing at all |
| **`DRY_RUN`** (default) | no | plan + field-level diff + digests |
| `CANARY_NEW_ONLY` | yes | insert only where no legacy row exists |
| `PRODUCTION_NEW_ONLY` | yes | same protection, wider scope; **not activated** |
| `EXPLICIT_BACKFILL` | yes | additionally requires `--allow-update-existing` **and** an explicit lecture scope; **not activated** |

Both the CLI and the service default to `DRY_RUN`, and an unknown mode is
refused before anything is read.

## 3. Ownership

A legacy session is owned by the coded writer **only** if a row for it exists
in `lecture_qa_legacy_writes` — ownership is never marked on the legacy row
itself. The decision function:

| Situation | Decision |
| --- | --- |
| source not finalized | `BLOCKED_NOT_READY` |
| payload fails the invariant | `BLOCKED_INVALID_PAYLOAD` |
| no legacy row | `WOULD_INSERT` |
| legacy row, not coded-owned | **`PROTECTED_EXISTING_LEGACY_ROW`** in every mode, including backfill with the allow flag |
| coded-owned, same fingerprint | `WOULD_SKIP_IDENTICAL` |
| coded-owned, changed fingerprint | `WOULD_UPDATE` (backfill also needs the allow flag, else `BLOCKED_BACKFILL_NOT_AUTHORISED`) |

## 4. Invariants, transaction and verification

Before any write: exactly 11 rows, orders 1..11 with no gaps, unique
`session_id_match` values equal to `session_id + "_" + order`, and no NOT NULL
violation. A missing trainer is refused rather than written as an empty string.

One lecture writes inside **one savepoint**: session → 11 checklist rows →
ownership row. The written rows are then **re-read and compared field by
field**; any mismatch raises, the savepoint rolls the whole lecture back, and
the result is `WRITE_VERIFICATION_FAILED`. A failure in one lecture never
leaves a partial state and never aborts the others.

**Rollback** removes a session *only* when an ownership row proves the coded
writer created it; the checklist rows cascade, and the ownership row is marked
`ROLLED_BACK`. A legacy-owned session returns `PROTECTED_EXISTING_LEGACY_ROW`
and is untouched. Rollback also requires a write-enabled mode.
*Caveat for 3C2:* `qa_doctors_transcripts` and `qa_lecture_split_plans`
reference sessions without a cascade, so a rollback of a session another
workflow has since referenced will fail loudly rather than delete.

**Concurrency** rests on PostgreSQL alone: `UNIQUE (legacy_session_id,
writer_version)` means a second writer's claim conflicts into the same
ownership row instead of creating a duplicate, and the legacy primary keys
prevent duplicate sessions or checklist rows. No Redis.

## 5. Approved policies

- **Item 2.** New rows written by the coded platform carry the corrected,
  timezone-aware status. Historical rows keep their historical values; the
  writer never rewrites them, and the dry run proves the six 2026-09-04 Item 2
  rows are untouched.
- **LMS drift.** A new write uses the frozen Phase 3B snapshot. Historical
  drift is classified, never used to update a historical row.
- **Bounded generations.** At most **3** model generations per QA source
  fingerprint. Each is appended to `lecture_qa_generation_attempts`; the third
  unsuccessful one flips the evaluation to `REVIEW_REQUIRED`
  (`MAX_GENERATIONS_EXHAUSTED`) and scheduled runs then make **zero** provider
  calls. Only an explicit force may try again, and earlier attempts and the
  stored model output are preserved.

## 6. Dry run — 2026-09-04

| Lecture | Decision | Identical mapped fields | Checklist status diffs | Item 2 defect diff |
| --- | --- | ---: | ---: | --- |
| AI in Project Control 2026 | **WOULD_INSERT** | — | — | — |
| Femi-Commercial | PROTECTED | 15 / 22 | 1 (0 excl. Item 2) | yes |
| G2-Juliane | PROTECTED | 15 / 22 | 1 (0 excl. Item 2) | yes |
| PPC \| Andrew | PROTECTED | 15 / 22 | 0 | no |
| Ray-PMO | PROTECTED | 13 / 22 | 1 (0 excl. Item 2) | yes |
| G3 Femi | PROTECTED | 15 / 22 | 1 (0 excl. Item 2) | yes |
| Ray-MSP | PROTECTED | 13 / 22 | 2 (1 excl. Item 2) | yes |

Every differing field classified as `DIFFERENT_EXPECTED_AI` (AI content and the
counts it drives) or `DIFFERENT_LMS_DRIFT` (three lectures). **No
`DIFFERENT_OTHER` remains** — the one that appeared initially was
`Engagement 100.00` vs `100`, a numeric-formatting artefact now normalized in
the comparison. Evidence text differs on 7–11 rows per lecture, expected from a
fresh model run.

Sessions written: **0**. Checklist rows written: **0**.

## 7. Before authorising a real Phase 3C2 canary

1. Pick the canary lecture explicitly — on this date the only eligible one is
   **AI in Project Control 2026**, a `NON_DELIVERED` payload with no historical
   row. Note that writing it inserts a *cancelled* QA session into production.
2. Re-run `write-legacy-qa --mode DRY_RUN` immediately beforehand and confirm
   the decision is still `WOULD_INSERT` and the pre-write digest is unchanged.
3. Confirm the `session_id` is still absent, and take a backup or a verified
   point-in-time recovery window for the two legacy tables.
4. Run `CANARY_NEW_ONLY` scoped to that one lecture, then verify: one session,
   eleven rows, one ownership row, and `post_write_digest` recorded.
5. Confirm downstream consumers tolerate a coded-written row — Positive Clips
   and the recording lookup both read `qa_doctors_sessions`.
6. Keep the rollback path ready, and check first that no
   `qa_doctors_transcripts` or `qa_lecture_split_plans` row references the new
   session.

## 8. Still open

1. **Real multi-part fixture validated: NO.**
2. **Master vs One Lecture timezone divergence** untested on a real ambiguous
   multi-candidate selection occurrence.

Production cutover readiness is not claimed.
