# Phase 4A — Coded scheduler, reconciliation and stage-aware recovery orchestration

Written for: engineers and operators who will run, extend or debug the coded
lecture pipeline without having read the five phases behind it.

## What this phase added

Everything before Phase 4A was a **pipeline**: a set of validated services, each
correct, each invoked by hand. Phase 4A adds the thing that decides **which of
them to run, for which lecture, and when** — and nothing else. No service was
modified to fit the orchestrator, no rule was reimplemented inside it, and no
new business logic exists in `app/orchestration/` at all.

The organising idea is one sentence:

> For every lecture, find the earliest stage that is incomplete or stale, and
> do exactly that.

That is the opposite of the legacy pipeline's model, which re-ran everything
from zero on every trigger. The difference is not tidiness; it is cost and
safety. A lecture that has already been evaluated must never be evaluated
again, because a second generation costs money and produces a second answer.

## The state model

`app/orchestration/state.py` resolves fifteen stages for one lecture:

```
DISCOVERY → MEETING → TRANSCRIPT → SELECTION → CANONICAL_CUES → SPEAKERS
→ ATTENDANCE → ENGAGEMENT → QA_EVALUATION → QA_RENDER → LEGACY_QA_SYNC
→ PERFECT_ELIGIBILITY → PERFECT_SYNC → RECORDING_LINK → EXCEL_SYNC
```

Each resolves to `COMPLETE`, `MISSING`, `STALE`, `FAILED`, `REVIEW_REQUIRED`,
`WAITING` or `NOT_APPLICABLE`.

### A stage is never COMPLETE just because a row exists

Existence answers "did this ever happen?". `COMPLETE` has to answer "is what
exists still the right answer for the inputs we hold now?". In a versioned
platform those diverge constantly, and three real cases in this database prove
it:

| Situation | Naive answer | Correct answer |
| --- | --- | --- |
| Engagement row computed from a superseded attendance snapshot | COMPLETE | `STALE` → `CALCULATE_ENGAGEMENT` |
| Rendered session belonging to a superseded evaluation | COMPLETE | `STALE` → `RENDER_QA` |
| Transcript content fetched after the selection was made | COMPLETE | `STALE` → `SELECT_TRANSCRIPT` |

So every stage is judged on **version and lineage**, not on row count.

### Two lineage subtleties found while building this

**Andrew holds two canonical documents.** Phase 3C2.3B rebuilt exactly one
lecture under `webvtt_canonical_v2_seam_dedup`. Engagement identity includes
`document_id`, so that lecture legitimately holds **two engagement rows for the
same attendance snapshot**, with identical timestamps. A resolver that picked
"the newest" would break the tie on a uuid and report a settled lecture as
stale forever. The rule instead: **the evaluation declares which document is
current**, and the current engagement row is the one matching both that
document and the current snapshot.

**2026-09-04's legacy rows are not ours.** Those lectures have legacy
`qa_doctors_sessions` rows written by the n8n pipeline and **zero** coded
ownership records. Reporting `LEGACY_QA_SYNC` as `MISSING` would put a
"sync this" instruction in front of an operator for a row the platform must
never touch. They resolve to `NOT_APPLICABLE` /
`LEGACY_ROW_NOT_CODED_OWNED` instead — the ownership model, expressed as a
state.

### WAITING is what makes unattended operation affordable

A lecture whose attendance source is empty is not failed, not incomplete work
we can do, and not something to retry. It is a lecture whose next move belongs
to someone else. Without that state the scheduler has only "done" and "not
done", and "not done" means "try again", which means buying a generation every
night for a lecture that cannot change until an external system changes.

## The next-action model

`next_action` is the action for the **earliest** unsettled stage — first wins,
not most-severe wins. A lecture with a missing transcript and a missing render
has one real problem and it is not the render.

Two fields, because two questions:

- **`next_action`** may be `WAIT_FOR_RECORDING` or `WAIT_FOR_EXCEL_SYNC`.
- **`next_executable_action`** is what the coded platform itself can do; it is
  `NOTHING_TO_DO` when only the observed-only stages remain.

`RECORDING_LINK` and `EXCEL_SYNC` belong to live legacy workflows — the QA
Master recording branch and "QA Perfect Lectures — Excel Sync". Phase 4A
**observes** them and triggers neither.

### Automation boundaries

| Category | Actions |
| --- | --- |
| Automated | `ACQUIRE_TRANSCRIPT`, `SELECT_TRANSCRIPT`, `BUILD_CANONICAL_CUES`, `RESOLVE_SPEAKERS`, `RESOLVE_ATTENDANCE`, `CALCULATE_ENGAGEMENT`, `RUN_QA`, `REFRESH_DETERMINISTIC_QA`, `RENDER_QA`, `RECOVER_ATTENDANCE`, `EVALUATE_PERFECT` |
| Operator only | `SYNC_LEGACY_QA`, `SYNC_PERFECT` |
| Human decision | `MANUAL_REVIEW_REQUIRED` |
| Nobody | `WAIT_FOR_ATTENDANCE_SOURCE`, `WAIT_FOR_RECORDING`, `WAIT_FOR_EXCEL_SYNC`, `NOTHING_TO_DO` |

**No legacy production write is ever automated.** The orchestrator names the
work and records it; performing it stays an explicit, confirmed,
ownership-aware, lecture-scoped operation through the existing guarded writers.

## Cost control

Exactly one action costs Microsoft Graph (`ACQUIRE_TRANSCRIPT`) and exactly one
costs a model generation (`RUN_QA`). Both are named in
`app/orchestration/runner.py` and both are behind hard gates: with
`allow_provider=False` the QA service is constructed with `provider=None`, so
the run is *structurally incapable* of buying a generation rather than merely
instructed not to.

`REFRESH_DETERMINISTIC_QA` is a different action from `RUN_QA` on purpose. New
attendance makes an evaluation stale, but the model was asked about a
transcript, not about learners — so its answer is reused and only the
deterministic parts recompute. Same stage, different cost, different name.

### The attempt budget is honoured

A `REVIEW_REQUIRED` evaluation with attempts remaining is retryable work. One
whose generation budget is exhausted for its current contract resolves to
`MANUAL_REVIEW_REQUIRED`. A scheduler that hands an exhausted contract back as
ordinary work buys endless generations for a lecture that keeps failing the
same way.

## The orchestrator

`PipelineOrchestrator.run_window` is the **only** entry point that makes the
pipeline happen, and the scheduler calls exactly it. A scheduler with its own
implementation is tested in one shape and run in another.

### Passes

One lecture can need several stages in sequence. Rather than hard-code a
pipeline order a second time, each pass advances every lecture by at most one
stage and then re-derives the state from the database. The loop stops when a
pass moves nothing — which is also the definition of "this run is idempotent",
so the property is enforced by the control flow rather than asserted about it.

**Progress means the state moved, not that a call returned.** The integration
suite caught the opposite: a waiting lecture handed to `RECOVER_ATTENDANCE`
correctly probes an empty source and writes nothing, and counting that as
progress made the cycle ask eight times. `(lecture, action)` pairs that ran
cleanly and moved nothing are now stalled for the rest of the cycle.

### Operator gates do not block work that does not depend on them

The executable search steps over `SYNC_LEGACY_QA` and `SYNC_PERFECT`, and only
those. Three newly rendered lectures initially stopped at `LEGACY_QA_SYNC` — a
write only an operator may perform — and so never had their Perfect eligibility
computed, breaking an existing Phase 3C3B invariant. Perfect eligibility derives
from the frozen Phase 3B render, not from the legacy row; waiting on a human for
an answer that does not depend on them leaves Operations blind for no reason.
The headline `next_action` still names what the human owes, and
`operator_actions` carries it, so stepping over it never means forgetting it.

`WAITING` and `MANUAL_REVIEW_REQUIRED` are **not** stepped over: those genuinely
make everything below them provisional.

### A repaired legacy row is still ours

`write_status` has five values. `WRITTEN` and `UPDATED` both mean the coded
platform owns the row — `UPDATED` is Phase 3C2.3F's `attended_count` repair, so
it marks a row that is correct *because* it was touched again. Accepting only
`WRITTEN` reported Andrew as `ELIGIBLE` with `PERFECT_SYNC = NOT_APPLICABLE`, a
pairing that cannot be true, and would have invited an operator to create a row
that already exists. `NOOP`, `ROLLED_BACK` and `WRITE_VERIFICATION_FAILED` stay
real absences.

### Isolation

Each lecture is processed under its own advisory lock; its failure is caught,
recorded and stepped over. Two deliberate exceptions stop the whole run:

- **writer-integrity refusal** — the guard bounding production writes has
  objected, and continuing means making more of them;
- **shared-infrastructure failure** (Graph auth, the database) — the next
  lecture will fail identically, and a hundred identical audit rows is noise.

### Day-scoped vs lecture-scoped

Four validated services are day-scoped by construction (transcript acquisition,
selection, canonical parsing, speaker inventory). That is a property of those
services; rewriting them to fit the orchestrator would be exactly the mistake
this phase avoids. They run **once per day per cycle** and the result is
shared. Every stage that can fail for lecture-specific reasons — attendance,
engagement, QA, rendering, Perfect — is lecture-scoped, which is what makes
per-lecture isolation real rather than nominal.

## Concurrency

PostgreSQL **session-level advisory locks**, one per lecture, namespace
`0x4B424C31`, key derived from the lecture id by SHA-256.

A lock row in a table outlives the process that wrote it: a scheduler killed
mid-cycle leaves the row, and every later cycle skips that lecture until
somebody deletes it by hand. An advisory lock is released by the server when
the connection ends, for any reason, including a process that never ran a line
of cleanup. Recovery after a crash is not a procedure; it is the absence of
one.

`pg_try_advisory_lock` is non-blocking on purpose. A cycle that finds a lecture
busy records `LOCKED` and moves on — a scheduler that waits on a lock can be
made to wait forever by one slow lecture.

## Retry vs force reprocess

| | RETRY | FORCE REPROCESS |
| --- | --- | --- |
| Means | resume from the earliest missing/stale stage using valid state | deliberately create new provenance where versioning allows |
| Touches a COMPLETE stage | never | yes, that is the point |
| Available to the scheduler | **always, and only this** | **never** |
| How invoked | automatic | explicit operator action |

The scheduler has no parameter that reaches force reprocess, and a test asserts
the constant does not appear in the orchestrator, scheduler or runner.

`OperationsService.force_reprocess_eligibility` refuses a settled lecture, a
contract whose generation budget is exhausted, and a legacy row the platform
does not own.

## The n8n read-only preflight

Legacy QA is manually paused: QA Master Daily v8 is active, the recording branch
is enabled, Excel Sync is enabled, and exactly one node — `Execute QA One
Lecture` — is disabled. **That single disabled node is the entire reason the
coded platform may write legacy QA rows without racing the legacy pipeline.**

The ownership guard that would make this robust is not deployed, so the
coexistence rests on a manual configuration that anyone can change. Before any
production orchestration run the platform therefore **asks**:

```
GET /api/v1/workflows/8yeJigbj8BCNBMkU   →  is "Execute QA One Lecture" disabled?
```

One HTTP GET. There is no PUT, POST, PATCH or DELETE in
`app/orchestration/n8n_preflight.py`, and a test asserts it structurally. If the
answer is wrong the correct response is to stop the coded run and tell a human,
not to "fix" a live production workflow unattended.

**Fail closed.** Unreachable, unauthorised, malformed, timed out, or
guarded-node-not-found all mean "we do not know", and not knowing is treated
exactly like "the node is enabled". The run is marked
`BLOCKED_LEGACY_QA_ACTIVE` and performs nothing.

The API key is read from the environment, sent as a header, and never logged or
returned — including in error results, whose bodies are dropped rather than
echoed, because an n8n error body can quote the request that produced it.

## Run and audit persistence

Migration `016`, two tables:

- **`lecture_pipeline_runs`** — one row per cycle: type, window, timings,
  status, the precheck answer, a count partition, and the Graph/provider/legacy
  costs.
- **`lecture_pipeline_run_items`** — one row per lecture per run: the decision
  before, the action, the decision after, a status, a reason code, and a
  **scrubbed** message.

Deliberately not there: no transcript text, no model output, no prompt, no
credential, no learner name. `error_message_safe` is named for the rule it
carries. No per-stage row either — the stage matrix is derived state,
recomputable at any time, and persisting a copy would create a second source of
truth that drifts the moment it is written. And no lock table, for the reason
above.

The counts are a **partition**: every lecture lands in exactly one bucket and
they sum to `lectures_seen`. Overlapping counters are worse than none — "5 seen,
4 complete, 3 waiting" is a puzzle, not a summary — and the reconciliation
report is checked against them.

## Commands

```bash
# read-only
kbc pipeline-state    --lecture-id <uuid> [--probe-attendance]
kbc reconcile-day     --date YYYY-MM-DD [--lookback-days N] [--probe-attendance]
kbc scheduler-status
kbc operations        --view day|lecture|recent-runs|run-detail|pending-attendance|review-required|errors|lecture-history

# acts
kbc run-pipeline      --date YYYY-MM-DD [--lookback-days N] [--dry-run]
                      [--discover] [--no-provider] [--no-graph]
kbc reconcile-day     --date YYYY-MM-DD --execute
kbc scheduler-cycle   [--dry-run] [--force] [--date YYYY-MM-DD]
```

A **dry run** writes nothing at all — not a coded row, not a legacy row, not
even an audit row — and gets four independent reasons why: no run repository,
no lock manager, a non-required precheck, and a runner with `persist=False`. It
reports `planned_actions` rather than `actions`, plus `would_call_graph`,
`would_call_provider`, `would_write_coded_state`, `would_write_legacy_qa` and
`would_write_perfect`.

## Scheduler configuration

All environment-driven, nothing buried in business logic, **disabled by
default**.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCHEDULER_ENABLED` | `false` | automatic execution |
| `SCHEDULER_TIMEZONE` | `Africa/Cairo` | the college's business day |
| `SCHEDULER_CRON` | `0 21,23 * * *` | matches QA Master Daily v8 |
| `SCHEDULER_LOOKBACK_DAYS` | `3` | late transcripts, late attendance, weekends |
| `SCHEDULER_DISCOVER` | `true` | run Graph calendar discovery |
| `SCHEDULER_ALLOW_GRAPH` | `true` | permit transcript acquisition |
| `SCHEDULER_ALLOW_PROVIDER` | `true` | permit paid generations |
| `SCHEDULER_MAX_PASSES` | `16` | defect detector, not a budget. Derived as `len(EXECUTABLE_STAGES) + 3`: a lecture advances by at most one stage per pass, so a cap below the chain length is a silent budget. It was `8`, and a lecture discovered from nothing stopped at `QA_RENDER` — never reaching `LEGACY_QA_SYNC`, so no `qa_doctors_sessions` compatibility row was created for it. Fixed in rc3. |

The cron copies the legacy QA Master's own schedule. That is a compatibility
decision, not a preference: inventing a new time would mean inventing new
assumptions about when transcripts are ready.

`run_cycle(force=True)` runs one cycle through the scheduled entrypoint while
`SCHEDULER_ENABLED` is still false — the controlled pilot. It enables nothing.

## Operations interface

`app/orchestration/operations.py` is the service layer a future UI calls. It
exists now so the UI is built against a stable contract rather than against
whatever SQL is convenient later.

Every read returns plain JSON-serialisable data. Every mutation is separately
named and guarded — there is no generic `execute(action)` entry point, because
a UI that can post an arbitrary action name is a UI that can force-reprocess a
settled lecture by typo. `request_retry` returns a **plan**, not a result.

## What this phase did not do

No media work of any kind: Positive Clips, Lecture Parts, `qa_media_jobs`,
FFmpeg and SharePoint are untouched. No n8n workflow was modified. The
ownership guard was not deployed. No legacy production table was written.
