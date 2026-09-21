# Phase 3C3E: finalising the attendance policy, and building the way out

Phase 3C3D made the platform say "I don't know" honestly. Saying it is only
half an answer: a lecture parked on `PENDING_ATTENDANCE_DATA` needs a
deterministic way out for when the data lands. This phase settles the policy
and builds that way out.

## 1. The business decision, as code

`public.kbc_attendance` is an external, read-only upstream source. A missing
row there is **not** a coded-platform defect, and this phase does not try to
fix it. What the platform owes is a truthful representation:

| the source says | coverage | authoritative |
| --- | --- | :-: |
| rows with surviving members | `SOURCE_AVAILABLE_WITH_MEMBERS` | yes |
| rows, all recorded absent | `SOURCE_AVAILABLE_CONFIRMED_ZERO` | yes |
| nothing at all | `SOURCE_MISSING` | no |
| rows, but none usable | `SOURCE_PARTIAL_OR_INVALID` | no |
| no snapshot yet | `SOURCE_UNKNOWN` | no |

`attended_count = 0` remains as a compatibility placeholder where the schema
needs a number, and it is never evidence. The predicate downstream code must
use is `confirms_zero_attendance(status, count)`, which is true only when the
source actually answered.

## 2. A blind spot found while checking G2 Keith

Before running the recovery, the source was read directly. It holds **one row**
for G2 Keith's module on 2026-09-17 — marked **absent**.

The roster query is the legacy contract: exact date + exact normalized module
+ `Attendance = 1`. So everything reaching `build_roster` is already a present
row, `source_row_count` and `present_row_count` cannot disagree, and the
`SOURCE_AVAILABLE_CONFIRMED_ZERO` branch was **unreachable in production**. A
lecture the source had described only in absences was indistinguishable from
one it had never heard of.

`source_rows_any_status` — the same (date, module) with no attendance filter —
is now captured into snapshot metadata and:

```
source_row_count = 0, any_status = 0   ->  SOURCE_MISSING            (silence)
source_row_count = 0, any_status > 0   ->  SOURCE_PARTIAL_OR_INVALID (absences)
                                           ALL_SOURCE_ROWS_MARKED_ABSENT
```

The second is deliberately **not** a confirmed zero. "Every row we hold says
absent" becomes "nobody attended" only if those rows cover everyone who could
have attended, and the only way to know that is to cross-reference enrolment —
inference, not evidence, and explicitly out of bounds. Both states stay
non-authoritative, so no behaviour changes; what changes is that Operations
can tell silence from a partial answer.

It is not in the fingerprint, so every existing snapshot id is untouched.

**This is a decision for the business**, not a defect: if "all enrolled
learners explicitly marked absent" should count as a confirmed zero, that needs
an enrolment cross-reference the current rules forbid.

## 3. v2 is the default

`kbc_perfect_v2_attendance_required` is now what new work gets without asking —
in the planner, in both CLI commands, and in the recovery path. A default
nobody has to remember is the only kind that survives unattended operation.

Omitting the coverage reader no longer downgrades the policy: the planner
supplies one. The only way to decide a Perfect Lecture without attendance
evidence is to name `legacy_qa_v8_perfect_v1` explicitly, which stays
selectable forever so every row written under it remains reproducible.

The default processing contract is now, in full:

```
provider contract    strict_json_schema_v1
punctuality          canonical_cue_bounds_v1
attendance coverage  attendance_coverage_v1
Perfect policy       kbc_perfect_v2_attendance_required
```

No old provenance was relabelled to match.

## 4. Coverage backfill

**49 rows stamped** — 24 snapshots and 25 engagement rows — derived entirely
from counts the rows had already frozen. No Graph call, no provider call, no
re-query of `kbc_attendance` to reconstruct old history (reconstructing it from
today's source would be a different fact wearing an old date).

| | |
| --- | --- |
| `SOURCE_AVAILABLE_WITH_MEMBERS` | 45 |
| `SOURCE_MISSING` | 4 |
| `engagement_status_detail = CALCULATED` | 23 |
| `engagement_status_detail = ATTENDANCE_SOURCE_MISSING` | 2 |
| rows claiming a confirmed zero | **0** |

Plus **16 evaluations** stamped with `model_input_fingerprint`, each only after
checking field by field — document, selection, transcript, meeting, duration,
Item 2 timing, prompt hash, model — that the evaluation really was built on the
package being fingerprinted. The first pass refused one: Andrew, whose
evaluation uses the v2 seam-dedup document while the recomputation defaulted to
v1. The guard was right; keying the rebuild on the evaluation's own parser
version made all 16 derivable.

No production table was touched and **no timestamp advanced** — stamping a
derived conclusion onto frozen evidence is not a change to the evidence. The
second run stamped **0 rows**; the NOOP is enforced by the `IS DISTINCT FROM`
in the statement, not trusted in Python.

## 5. `recover-attendance --lecture-id`

A primitive, not a scheduler. Exactly one lecture, only when asked, and the
future reconciliation layer will call it once per lecture rather than the other
way round.

It resumes at the earliest **affected** stage and no earlier:

```
attendance -> engagement -> deterministic Item 7 -> (QA refresh)
           -> (Phase 3B) -> Perfect eligibility -> (Perfect sync)
```

Discovery, meeting resolution, transcript acquisition, selection, canonical
parsing and the Phase 3A model generation are not repeated.

### Why it is free

`build_user_message` sends the transcript, its ids, the subject, the schedule,
the timing and the duration. It sends **nothing about learners**. So when
attendance arrives, the model's answer to the question it was actually asked
has not changed, and re-buying it would be paying twice.

That is not an assumption. `qa_model_input_fingerprint` hashes exactly the
inputs that shape the provider request, and reuse is licensed only when it
matches. If no stored evaluation was asked exactly this question, the refresh
returns `NO_REUSABLE_MODEL_OUTPUT` and **fails closed** — it never quietly
calls the model instead. The recovery's QA service is constructed with
`provider=None`, so it is structurally incapable of buying a generation rather
than merely instructed not to.

### What a refresh recomputes

Item 7's deterministic override, and therefore the Met / Partial / Not Met
counts. Items 1 and 2 are recomputed from their own unchanged inputs and come
out identical; items 3–6 and 8–11 come straight out of the frozen output. The
new evaluation carries a full `deterministic_refresh` block: origin evaluation
id, origin fingerprints, previous and new Item 7 and its source, previous and
new counts, the new snapshot and engagement ids, `openai_reused: true`,
`provider_calls: 0`.

Nothing is rewritten. Snapshots are content-addressed, so a changed source
makes a **new** one and the old `SOURCE_MISSING` snapshot stays exactly where
it is; the engagement row, evaluation, render and Perfect result all key on
provenance the new evidence changes. The pending history survives the recovery
that ends it.

### What it will not do

It reports that a Perfect legacy sync has become available; it never performs
it. That write is ownership-aware, mode-gated and separately confirmed, and
recovery is not a licence to bypass the guarded writer.

## 6. G2 Keith, checked for real

```
recover-attendance --lecture-id de8c6c60-…
```

| | |
| --- | --- |
| status | **`WAIT_FOR_ATTENDANCE_SOURCE`** |
| next action | `WAIT_FOR_ATTENDANCE_SOURCE` |
| persisted / observed coverage | `SOURCE_MISSING` / `SOURCE_PARTIAL_OR_INVALID` |
| authoritative | no |
| OpenAI calls | **0** |
| Graph calls | **0** |
| rows written, all 12 tables | **0** |
| timestamps moved | none |
| second run | identical, still 0 |

The snapshot id did not move, so the recovery stops before touching anything —
a stronger no-op than re-deriving the same answers. The persisted coverage
still reads `SOURCE_MISSING` and correctly so: it is frozen evidence of what
was observable when it was written, and re-stamping it from today's source
would be a different fact wearing an old date.

## 7. Proving the path that has not happened yet

G2 Keith's attendance is still missing, so the arrival path is proven against
its **real** upstream evidence with only the external source *gateway*
substituted — the table itself is never written, and `kbc_attendance`'s row
count is asserted unchanged. Every test rolls back.

**Case 1 — attendance arrives, Item 7 stays Met.** `SOURCE_MISSING` →
`SOURCE_AVAILABLE_WITH_MEMBERS`; new snapshot created, old one byte-identical;
engagement recalculated for one lecture with siblings untouched; 4 attended, 4
spoke, override applied; the refreshed evaluation's `ai_raw_output` is
byte-identical to the origin's; **0 provider calls**; Perfect
`PENDING_ATTENDANCE_DATA` → `ELIGIBLE`; `next_action = SYNC_PERFECT`; no legacy
Perfect row created by the recovery itself; second run `NO_CHANGE`, 0 writes.

**Case 2 — attendance arrives, Item 7 turns Not Met.** 1 speaker of 13
attendees → 7.69% → deterministic Item 7 `Met` → `Not Met`, counts 10/0/1, **0
provider calls**; a new render produced for the refreshed evaluation (10/1),
never reusing the stale one; the superseded evaluation byte-identical; Perfect
→ `NOT_ELIGIBLE_STATUS_NOT_ALL_MET` with no legacy row; second run `NO_CHANGE`.

**Case 3 — still missing.** `WAIT_FOR_ATTENDANCE_SOURCE`, 0 writes, 0 OpenAI,
0 Graph — run against the real source, not a fake.

## 8. Operations state

`qa-recovery-state --lecture-id` now also returns coverage version, coverage
status, authority, snapshot id and provenance, engagement freshness
(`is_current_snapshot`), whether the frozen model answer is reusable, the
Perfect policy version, the eligibility state per version, the sync state, and
a **next action** chosen from the earliest missing or stale stage:

```
WAIT_FOR_ATTENDANCE_SOURCE · RECOVER_ATTENDANCE · RECALCULATE_ENGAGEMENT
RUN_PHASE_3A · REFRESH_DETERMINISTIC_QA · REFRESH_RENDER · EVALUATE_PERFECT
SYNC_PERFECT · REVIEW_REQUIRED_MANUAL · NOTHING_TO_DO
```

The three pilot lectures today:

| lecture | coverage | detail | v1 | v2 | next action |
| --- | --- | --- | --- | --- | --- |
| G2 Keith | `SOURCE_MISSING` | `ATTENDANCE_SOURCE_MISSING` | — | `PENDING_ATTENDANCE_DATA` | `WAIT_FOR_ATTENDANCE_SOURCE` |
| Stephen | `…WITH_MEMBERS` | `CALCULATED` | `NOT_ELIGIBLE_…` | `NOT_ELIGIBLE_…` | `NOTHING_TO_DO` |
| Martech | `SOURCE_MISSING` | `ATTENDANCE_SOURCE_MISSING` | **`ELIGIBLE`, synced** | `PENDING_ATTENDANCE_DATA` | `WAIT_FOR_ATTENDANCE_SOURCE` |

Martech is the point of versioning: v1 eligible and synced is history and stays
exactly as written; v2 pending is today's policy on the same frozen result.
Both are true, neither is corruption.

## 9. A loader precision fix this made necessary

Once a lecture can hold two attendance snapshots, `LOAD_QA_INPUTS` returns two
rows for it and the ambiguity guard stops a lecture that is versioned rather
than ambiguous. The loader now restricts to the **current** snapshot by the
same rule Phase 2C4 and the coverage reader already use — newest, ties on id.
The guard itself is unchanged: two surviving rows still stop the run.

## 10. Carried gates

**A** Positive Clips / media coordinate. **B** `planner._zone`. **D** the n8n
ownership guard is still undeployed. **E** Excel Sync's running version is
unreadable through the public API. **J** the attendance source is still
genuinely incomplete — now represented precisely, but the data is still
missing, and that is upstream.

New, and a decision rather than a defect: **L** whether "all enrolled learners
explicitly marked absent" should count as a confirmed zero.

## 11. Before the scheduler

1. **Deploy the ownership guard (Gate D).** Still the next cutover task.
2. **Decide Gate L.** It determines whether lectures like G2 Keith ever clear
   on their own or wait forever.
3. **Fix the upstream attendance gap (Gate J).** Representing it honestly means
   affected lectures now correctly *stall* instead of publishing — so the stall
   rate is the operational risk to watch.
4. **Wire `recover-attendance` into reconciliation.** The primitive is ready
   and idempotent; the layer that calls it once per pending lecture is not
   built, and deliberately so.
