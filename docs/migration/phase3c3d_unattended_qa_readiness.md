# Phase 3C3D: making QA safe to run unattended

Three gates carried out of the two controlled pilots, closed here, and then
proven on the one lecture the pilots could not finish.

## 1. The schema was never enforced by anyone but us

`STRUCTURED_OUTPUT_SCHEMA` has always declared
`"enum": ["Knowledge", "Skill", "Behaviour"]`. `app/qa/provider.py` sent

```python
"response_format": {"type": "json_object"}
```

which is plain JSON mode. The schema reached the model as prompt text and
nothing else. The model was therefore free to answer `"K"`, and across two
pilots it did so on **four of eight first generations** — and on all three
generations for G2 Keith, which is what stranded it.

There is no OpenAI SDK in this project: the provider is `urllib.request`
against `POST /v1/chat/completions`. The strict mechanism for that path is
`response_format: {"type": "json_schema", "json_schema": {..., "strict": true}}`,
and that is what is now sent.

### The strict schema is derived, not written

Strict mode accepts a narrower subset of JSON Schema than the base document
uses, so `app/qa/structured_output.py` derives it mechanically:

- every object gets `additionalProperties: false` and every property into
  `required`;
- `speaker` and `reasoning`, optional in the base schema, become nullable
  unions — strict mode has no other way to express optional;
- `minItems`, `maxItems`, `minimum` and `maximum` are unsupported and are
  dropped.

The derivation is therefore **strictly weaker** than the base schema, and it
says so out loud: `DROPPED_CONSTRAINTS` names all twelve dropped bounds and
they travel in the evaluation's provenance. Those bounds are still in the
system message and still enforced by `validate_structured_output`, which is
exactly why the local validator is kept. Provider enforcement is the first
line, not the only one.

`"K"` is never translated. If it somehow arrives it is still
`INVALID_KSB_TYPE`.

### One encoding artefact, undone precisely

Because strict mode forces the model to emit `speaker` and `reasoning`, absent
values arrive as explicit `null`. `drop_null_optionals` removes exactly those
— only keys the BASE schema marks optional, only when the value is exactly
`null` — and counts them into `null_optionals_dropped`. No value is ever
rewritten and no key is ever added.

## 2. The contract version is provenance

`strict_json_schema_v1` (and the historical `json_object_v1`) joins the QA
source fingerprint:

```
provider_contract:strict_json_schema_v1
```

Changing who enforces the schema is a materially different generation
contract, so it gets a different fingerprint — and because the generation cap
is keyed on the fingerprint, two things follow at once and for free:

- the three spent G2 Keith generations under `json_object_v1` **stay spent**;
- the new contract opens its own budget, still capped at **3**.

Nothing was relabelled. The old attempts keep their original numbers,
timestamps, outcomes and error counts.

## 3. Missing attendance is not zero attendance

2026-09-17 produced three lectures and two of them had **no rows at all** in
`public.kbc_attendance`. All three arrived downstream as a number, and two of
those numbers were `0`. "The source says nobody came" and "the source says
nothing" are different facts, and only the first is evidence.

`app/attendance/coverage.py` derives the distinction from counts
`lecture_attendance_snapshots` already froze — no new column, no re-query of
the external table, and historical lectures answer without being rewritten:

| counts | status | authoritative |
| --- | --- | :-: |
| `source_row_count = 0` | `SOURCE_MISSING` | no |
| members survive filtering | `SOURCE_AVAILABLE_WITH_MEMBERS` | yes |
| rows exist, all marked absent | `SOURCE_AVAILABLE_CONFIRMED_ZERO` | yes |
| rows exist, all filtered away | `SOURCE_PARTIAL_OR_INVALID` | no |
| no snapshot at all | `SOURCE_UNKNOWN` | no |

A confirmed zero is claimed **only** when the source itself carries rows for
the lecture saying so. Downstream code uses `confirms_zero_attendance(...)`,
never `attended_count == 0`.

Engagement keeps its versioned `calculation_status` and its CHECK constraint;
what it gains is `engagement_status_detail`, which reads
`ATTENDANCE_SOURCE_MISSING` when a `NO_ATTENDED_LEARNERS` is a gap rather than
a finding. Both the summary and the row's metadata carry it.

## 4. A Perfect Lecture needs attendance evidence

Martech on 2026-09-17 was published as an 11/11 Perfect Lecture with no
attendance record at all. Its Item 7 was Met **from the model**, because the
deterministic override cannot apply with no attendance — and legacy does the
same thing. A policy gap, not a code defect.

`kbc_perfect_v2_attendance_required` adds one condition:

```
perfect checklist + authoritative coverage   ->  ELIGIBLE
perfect checklist + no coverage              ->  PENDING_ATTENDANCE_DATA
not perfect       + no coverage              ->  the ordinary NOT_ELIGIBLE reason
```

Missing attendance is not an extra way to fail; it is a reason a pass cannot
yet be acted on. `PENDING_ATTENDANCE_DATA` is explicitly **non-final**: it
writes no `qa_perfect_lectures` row and no ownership, and it clears the moment
coverage arrives, with no model call.

The dangerous branch was the one that already existed. `not is_perfect` plus a
coded-owned row means `PERFECT_SUPERSEDED_NOT_PERFECT`, which marks the
ownership record — so a pending state reaching that branch would have
retroactively superseded Martech. `PERFECT_PENDING_ATTENDANCE_DATA` is decided
**before** it, and a test pins exactly that.

`legacy_qa_v8_perfect_v1` is untouched and remains the default. v2 is opt-in
via `--perfect-policy`, and becomes the default at scheduler activation.

## 5. `provider_attempts` was counting the wrong thing

G2 Keith's evaluation read `provider_attempts = 1` while
`lecture_qa_generation_attempts` correctly held 3.

Root cause: `provider_attempts` was set from `response["attempts"]` — the
**HTTP retry counter inside one call**, which is 1 whenever the request
succeeds first time. Each generation then upserted the same evaluation row and
overwrote it with 1 again.

Now the evaluation stores the generation number for its fingerprint, known
before the call is made, and the transport retry count lives beside it as
`provider_transport_attempts`. `mark_review_required` raises the aggregate too,
and `sync_provider_attempts` re-derives it from the attempts table for history
— touching no attempt row, and recording what the value used to be.

`qa-recovery-state --lecture-id` answers the whole resume question in one read:
contract version, current fingerprint, attempts used and remaining **per
contract**, coverage, engagement, QA status, Perfect eligibility and Perfect
sync state.

## 6. G2 Keith, recovered

Resumed at **Phase 3A**. No Graph call, no re-discovery, no re-selection, no
re-parse, no re-resolution of speakers; every upstream fingerprint reused.

| | |
| --- | --- |
| old contract | `json_object_v1`, fingerprint `886541a5…`, 3/3 spent, REVIEW_REQUIRED |
| aggregate repair | `provider_attempts` 1 → 3, attempts table byte-identical |
| new contract | `strict_json_schema_v1`, fingerprint `bdcd609e…` |
| Phase 3A | **COMPLETED on generation 1 of 3** — 11/0/0, 49 clips, 0 structured-output errors, 0 invalid clips |
| KSB types returned | `Knowledge`, `Skill` — the exact field that failed three times |
| Item 2 | +1 / −4 under `canonical_cue_bounds_v1`, matching the 3C3C read-only projection |
| Phase 3B | RENDERED, 11 rows, 1 LMS snapshot, 0 provider calls |
| Perfect (v2) | `PENDING_ATTENDANCE_DATA`, coverage `SOURCE_MISSING` |
| dry run | QA `WOULD_INSERT`, Perfect `PERFECT_PENDING_ATTENDANCE_DATA` |
| write | QA WRITTEN — 22/22 session fields, 66/66 checklist cells, digest `8ca5693d…` |
| second run | `WOULD_SKIP_IDENTICAL` / still pending, **all six deltas 0** |

`null_optionals_dropped = 9` on that generation: nine `speaker` / `reasoning`
nulls, which is the strict encoding being undone and nothing else.

One provider call. One lecture. Production deltas **+1 / +11 / 0 / +1 / +1 / 0**
— and the two zeroes are the point: an 11/11 lecture produced **no**
`qa_perfect_lectures` row and **no** Perfect ownership, because the platform
has no attendance evidence for it.

## 7. Nothing else moved

Ray `5092175c…` and Andrew `83d2be26…` byte-unchanged; Andrew's mapped digest
`266deb77…` and `attended_count = 7` unchanged. Stephen and Martech untouched.
Martech's legacy Perfect row, its foreign columns and its `WRITTEN` ownership
under v1 are byte-identical — it now additionally carries a **v2 result row
recording `PENDING_ATTENDANCE_DATA` with `SOURCE_MISSING`**, as an Operations
provenance marker that changes no history. 2026-09-16 Item 2 unchanged.
`qa_perfect_lectures` stayed at 145. Excel Sync selects 0 rows table-wide.

n8n verified read-only at both ends and unmodified: Master active with
`Execute QA One Lecture` the only disabled node, recording branch enabled,
`QA Perfect Lectures — Excel Sync` active on its 15-minute schedule.

## 8. Carried gates

**A** Positive Clips / media coordinate. **B** `planner._zone`. **D** the n8n
ownership guard is still undeployed. **E** Excel Sync's running version is
unreadable through the public API. **J** attendance source coverage is still
genuinely incomplete for real lectures — now *represented* correctly, but the
data is still missing, and that is an upstream question.

Closed this phase: **H** (provider-enforced structured output), **I** (Perfect
reachable with zero attendance), **K** (`provider_attempts` under-counting).

## 9. Before the scheduler

1. **Deploy the ownership guard (Gate D).** Cutover protection must not depend
   on a human remembering a disabled node.
2. **Make `kbc_perfect_v2_attendance_required` the default policy.** It is
   opt-in today so that this attended phase could not disturb v1 history.
3. **Backfill the coverage marker** onto engagement rows written before this
   phase, so Operations reads one model everywhere rather than two.
4. **Fix the attendance source gap itself (Gate J).** Two of three lectures on
   a real day had none; representing that honestly means those lectures now
   correctly *stall* instead of silently publishing.
