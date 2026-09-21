# Phase 3C2.3C: the multi-part lecture through QA, rendering and a dry run

First real QA evaluation built on a seam-deduplicated, call-relative,
multi-part transcript. **Nothing was written to the legacy QA tables.**

Target: Andrew-Scheduling Professional (SP) Jan 2026, 2026-09-16,
`25e85615-aa7a-5f49-bb40-078d7c7b65d0`, bound to
`webvtt_canonical_v2_seam_dedup`.

## 1. One real defect found before the provider call

The first scoped Phase 3A attempt refused to run:

```
more than one current QA input for lecture 25e85615-…
```

That is the Phase 3A ambiguity guard working. The cause was a genuine blind
spot exposed by version coexistence: the trainer `LEFT JOIN LATERAL` in
`LOAD_QA_INPUTS` matched roles on `attendance_snapshot_id` plus the resolver
and role versions, **but not on the document**. Andrew's snapshot now serves
two canonical documents, each with its own top speaker, so the lateral returned
two rows and fanned the input out.

Fix — one added predicate:

```sql
AND sp.document_id = d.document_id
```

The trainer must come from the same canonical document as the rest of the
input. This does not change trainer *authority* (still the Phase 2C3 VTT top
speaker) and is a no-op for any single-version lecture. Without it, no
multi-part lecture could ever be evaluated.

## 2. Preflight — 26/26 checks passed

Phase 2B `SELECTED`, `legacy_qa_v8_overlap_cluster_v1`, parts
`[(1, 0, primary), (2, 14,336,082)]`; v2 document 592 cues, first 10,026,775,
last 17,195,372, duration 7,168,597, 0 seam overlap, 0 backward steps, 16
speakers; roster v2 7 members, 7 spoke, 100.00 %, score 5, 0 ambiguous;
Phase 3A/3B empty; legacy target absent. n8n verified: `Execute QA One
Lecture` disabled and the only disabled node, recording branch enabled.

## 3. Phase 3A

| | |
| --- | --- |
| Model | `gpt-5.2`, reported `gpt-5.2-2025-12-11` |
| Prompt / schema | `legacy_qa_v8_prompt_v1`, sha `71b5f5874a60cb46…` |
| Generations | **1** (COMPLETED first time; cap 3 never approached) |
| Status | **COMPLETED** |
| Document | the v2 document `e3634b82…` |
| Source fingerprint | `422789535d1fd19e…` |
| Result | **11 Met, 0 Partially Met, 0 Not Met**, teaching quality 4, 7 KSBs |
| Evidence | **43 clips, 0 invalid** |

Deterministic items: Item 1 Met from the **canonical 119 min** (not the
286-minute zero-to-last-cue span); Item 2 Met via `DETERMINISTIC_PUNCTUALITY`;
Item 7 Met via `DETERMINISTIC_ENGAGEMENT_PHASE_2C4` with the override applied,
from the persisted 7/7 = 100.00 %. Trainer `Andrew millington` from
`PHASE_2C3_VTT_TOP_SPEAKER`; the model independently suggested the same name.

### Item 2's −166 minutes is real, not a defect

`start_difference_minutes = -166`, `end_difference_minutes = 0`. The rule is
"Met unless more than 20 minutes late, or ending more than 20 minutes early" —
starting early is always acceptable, so the status is Met either way. The −166
is the genuine gap between the scheduled 11:00 start and the **call** opening
at 08:13:41; it is not the historical ±180-minute timezone defect, and legacy
would compute the same number from the same `actual_start`. The lecture's own
first cue is 11:00:47.9, i.e. 48 seconds late.

## 4. Multi-part evidence validation — the point of the exercise

| check | result |
| --- | --- |
| clips | 43, all `VALID` |
| outside the v2 cue range | **0** |
| `end <= start` | **0** |
| clips resolving to zero cues | **0** |
| minimum clip duration | 14,240 ms |
| maximum clip duration | 211,160 ms |
| earliest clip start | 10,105,057 ms (02:48:25) |
| latest clip end | 17,178,252 ms (04:46:18) |
| chronological | yes |
| cue hits by source part | part 1 → 214, part 2 → 45 |

**Seam proof.** Exactly one clip touches the removed window
[14,342,932, 14,403,937]: `14,276,857 → 14,344,777`, resolving to v2 cues 343,
344 and 345 — **all three from part 1**. No dropped cue can be cited at all,
because 0 part-2 cues survive inside part-1 coverage; the nine removed cues do
not exist in the document.

## 5. Phase 3B

`RENDERED` — 1 session, **11** checklist rows, 43 clips consumed, **134
blocks**, 1 LMS snapshot created (`legacy_qa_v8_active_lms_roster_v1`, 11
students). **0 provider calls, 0 Graph calls, 0 Aptem queries.**

Re-rendering reuses: `sessions_reused 1`, `lms_snapshots_reused 1`,
`sessions_rendered 0` — evidence is byte-stable, fingerprint
`2813bef30aa597e9…` unchanged.

**Seam render audit:** 88 rendered timestamps, min 10,105,057, max 17,149,532,
**0 inside the removed window**, and **0 that do not land on a surviving v2 cue
boundary**. Call-relative coordinates preserved throughout.

## 6. Structural compatibility

11 rows, orders 1..11, unique well-formed `session_id_match`, consistent
`session_id` equal to the primary provider transcript id, counts summing to 11,
`meeting_id` correct, `duration` `"1 hours 59 minutes"` with score 4,
`Engagement` 100.00 with score 5, `cancelled_session` rendered as the legacy
TEXT `'false'`, all 22 session and 6 checklist columns mapped with the same
types as the first canary. Invariant check: `(True, None)`.

Andrew has no historical legacy row, so no AI-wording parity was manufactured
against an unrelated lecture.

## 7. Writer dry run

`write-legacy-qa --date 2026-09-16 --mode DRY_RUN --lecture-id 25e85615-…`

`lectures_considered 1`, **`would_insert 1`**, and **0** for `would_update`,
`protected_existing_legacy_row`, `blocked_not_ready`, `blocked_invalid_payload`,
`blocked_backfill_not_authorised`, `review_required`. Sessions written **0**,
checklist rows **0**, ownership rows **0**. Proposed digest
`83d2be26c4e4c999…`.

## 8. A decision to take before the real write: Perfect Lecture

Andrew is **11/11 Met**, and the legacy child computes

```js
isPerfect = metCount === 11 && uniqueOrders.size === 11 && allMet
            && first.cancelled_session !== true;
```

Andrew satisfies every clause, so legacy QA would also have inserted a
`qa_perfect_lectures` row. **The coded writer never does** — Perfect Lecture is
a separate migration phase and this one forbids it. Writing Andrew therefore
produces a QA session that legacy would have accompanied with a Perfect Lecture
row.

This is tolerated rather than broken: the Master's recording branch `LEFT JOIN`s
`qa_perfect_lectures`, so an absent row does not break it. But it is the first
time the gap has been reachable, and it should be an explicit decision, not a
surprise.

## 9. Test-suite consequences

Two failures, both obsolete assumptions rather than product defects:

- `test_an_unusable_evaluation_is_reported_not_evaluated` picked *any*
  `COMPLETED` evaluation with an unscoped `LIMIT 1`, which now selects
  Andrew's 2026-09-16 row while the test renders 2026-09-04. Scoped to the
  test's own date.
- `test_this_phase_wrote_no_qa_evaluation_or_render_for_the_multi_part_lecture`
  asserted a *phase boundary* that this phase deliberately crossed. Replaced by
  the invariant that still matters — Andrew has no legacy session, checklist,
  perfect-lecture or ownership row — plus a new test that any evaluation for
  Andrew must be bound to the v2 document.

## 10. Carried gates, untouched

- **Gate A — Positive Clips/media coordinate:** still unverified for a non-zero
  origin.
- **Gate B — Lecture Parts:** `planner._zone` still assumes `0 <= t <= duration`.
- **Gate C — engagement scope:** `calculate-engagement` is still date-scoped.
- **n8n ownership guard:** implemented, not deployed; QA is paused manually.
