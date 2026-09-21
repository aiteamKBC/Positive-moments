# Phase 3C2: the first real legacy QA write

```
lecture_sessions (2026-09-16, discovered in this phase)
        │  2A → 2B → 2C1 → 2C2 → 2C3(v2) → 2C4 → 3A → 3B   [one lecture]
        ▼
lecture_qa_rendered_sessions + _checklist_items
        │  CANARY_NEW_ONLY + explicit lecture id + --confirm-write
        ▼
qa_doctors_sessions (+1) + qa_doctors_checklist_items (+11)   ← REAL PRODUCTION WRITE
        │
        ▼
lecture_qa_legacy_writes (+1)                                 ← coded ownership
```

**One lecture was written. Nothing else was.** Writer version
`legacy_qa_v8_writer_v1`.

## 1. Why a new lecture had to be prepared

On 2026-09-04 no candidate satisfied the canary policy: six lectures are
historical n8n fixtures (`PROTECTED_EXISTING_LEGACY_ROW`), the seventh is
`NON_DELIVERED`, and the eighth never became `downstream_ready`. The canonical
registry held no later date, so Phase 1 discovery was run for the most recent
real lecture day found in the external sources, **2026-09-16** (8 modules in
`kbc_attendance`; the live n8n QA pipeline had last written 2026-09-11, so the
day was entirely unclaimed in the legacy tables).

Seven canonical lectures were discovered and brought through 2A→3B. Phase 3A
and 3B were scoped to the single chosen lecture, so exactly one provider-backed
evaluation was produced.

## 2. The canary

| | |
| --- | --- |
| Subject | Ray \| PMP – June 2026 |
| `lecture_id` | `8c2874d7-3db5-5870-ae47-4f745a847540` |
| Date | 2026-09-16 |
| `legacy_session_id` | the primary provider transcript id (sha256 prefix `45795966b2978805`) |
| Transcript | single part, `PARSED`, 618 cues, 122 minutes |
| Attendance | 13 members on `attendance_roster_v2_exclude_makeup` |
| Engagement | 5 / 13 = 38.46 % → score 2 |
| Outcome | 10 Met, 0 Partially Met, 1 Not Met |

It was chosen over the other six for being single-part with a clean parse, the
largest attendance roster, and a realistic engagement spread. The multi-part
lecture of that day (`Andrew-Scheduling Professional`) was deliberately *not*
used: a first canary should not also be the first multi-part fixture.

## 3. Bounded generations proved themselves on a real call

Generation 1 returned `"type": "Skills"` for one KSB entry →
`INVALID_STRUCTURED_OUTPUT`. This was checked against production before
reacting: all 4,610 KSB entries in the 650 legacy rows use exactly
`Knowledge` (2,325), `Skill` (1,603) and `Behaviour` (682). **Our validator
matches legacy exactly, so it was not loosened.** Generation 2 returned a
conforming payload and completed. Both attempts are preserved in
`lecture_qa_generation_attempts`; the cap of 3 was never reached.

## 4. The human-safe canary guard (added in this phase)

Enforced twice — in the CLI and again in `LegacyQaWriter.__init__`, so a
programmatic caller cannot bypass it:

| Attempt | Result |
| --- | --- |
| `--mode CANARY_NEW_ONLY` with no `--lecture-id` | refused, `writer_guard_refused` |
| scoped but no `--confirm-write` | refused |
| two `--lecture-id` values | refused |
| one lecture + `--confirm-write` | allowed |

`--lecture-id` was also added to `run-qa-shadow` and `render-qa-output`, so a
controlled run cannot spend a provider call on another lecture.

## 5. The write

```
write-legacy-qa --date 2026-09-16 --mode CANARY_NEW_ONLY \
                --lecture-id 8c2874d7-3db5-5870-ae47-4f745a847540 --confirm-write
```

Decision `WOULD_INSERT` → `write_status = WRITTEN` in one transaction:
session, 11 checklist rows, ownership row, re-read verification, commit.
The post-write digest equals the digest planned before the write:
`5092175cee6d2a25be1236eb8d2ae56f536bded0950f8f8ff2968122878f3f43`.

| Table | Before | After |
| --- | ---: | ---: |
| `qa_doctors_sessions` | 650 | **651** |
| `qa_doctors_checklist_items` | 7,523 | **7,534** |
| `qa_perfect_lectures` | 140 | 140 |
| `qa_doctors_transcripts` | 0 | 0 |
| `lecture_qa_legacy_writes` | 0 | **1** |

Independent re-read: **22/22** session columns and **66/66** checklist cells
exact against the Phase 3B payload. Writer-time provider, Graph, LMS,
attendance and transcript calls: **0 each**.

## 6. Foreign-owned columns

All 21 non-mapped columns hold their natural state: 16 NULL, and five that are
schema-derived rather than written — `positive_clips` `'[]'`,
`positive_clips_count` `0`, `clips_review_count` `0`, `clips_status`
`'pending'` (all column DEFAULTs) and `has_positive_clips` `false`, which is a
`GENERATED ALWAYS` column computed from `positive_clips`. Re-checked after the
whole validation sequence: **0 changed**.

## 7. Idempotency

Re-running the identical command: decision `WOULD_SKIP_IDENTICAL`,
`coded_owned = true`, same source fingerprint, 0 sessions, 0 checklist rows,
0 ownership rows written. The second run's pre-write digest equals the first
run's post-write digest.

## 8. Downstream interaction

Exactly one scheduled trigger exists across the workflow exports:
`kbc-positive-clips-reconciliation` (hourly). **Both of its queries filter on
`clips_analysis_completeness = 'positive_clips_v5_final'` and
`jsonb_array_length(positive_clips) > 0`, and the canary row matches neither**,
so it cannot be picked up. Every other consumer — `QA_One_Lecture ... v8`, both
Positive Clips producers — is an `executeWorkflowTrigger` that only runs when a
parent calls it. The lecture-parts export is a manual snippet holder.

**Open risk for 3C3:** the QA *Master* workflow is not in this repository, so
its schedule could not be inspected here. `QA_One_Lecture ... v8` performs
`UPDATE public.qa_doctors_sessions`, so if the Master is ever run for
2026-09-16 it would overwrite the canary row — n8n has no knowledge of coded
ownership. This must be settled before a wider cutover.

## 9. Test-suite impact

The canary changed the database the integration suite reads, and the new guard
changed the writer's construction contract, so 19 tests needed updating. None
was a product regression:

- **9 writer tests** built a write-enabled `LegacyQaWriter` with no scope and no
  confirmation. They now supply both, exactly as an operator must. One
  assertion (`protected_existing_legacy_row == 6`) was retired: a scoped canary
  no longer *considers* the other six, and their protection is asserted by the
  `EXPLICIT_BACKFILL` test and by the whole-table digest.
- **10 tests** used table-wide queries (`SELECT DISTINCT outcome FROM
  lecture_qa_generation_attempts`, engagement rows for a roster version,
  rendered fingerprints, ownership-row counts) that silently assumed one lecture
  date existed. Each is now scoped to its own target date.

The real canary is never deleted or mutated by any test; every integration test
still rolls its transaction back, and the canary's 22/22 and 66/66 parity was
re-verified after a full suite run.

## 10. Carried cutover gates

1. **Real multi-part fixture validated: NO.** One now exists in the pipeline
   (`Andrew-Scheduling Professional`, 2 parts, 2026-09-16) but was not written.
2. **Master vs One Lecture timezone divergence** still not exercised on a real
   ambiguous multi-candidate selection.
3. **n8n Master overwrite risk** (section 8) — new in this phase.

Neither of the first two blocked one controlled canary. All three block a
broad production cutover.
