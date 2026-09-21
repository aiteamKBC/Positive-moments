# Phase 4B — Automated production sync, evidence-failure hardening and scheduler activation

Written for: engineers and operators who will run the unattended lecture
pipeline, and whoever next has to decide whether a writer decision is safe.

## What changed

Phase 4A could take a lecture from a calendar event to a rendered QA result
without a human. It then stopped, because every legacy production write was
operator-only. A lecture whose QA was finished, rendered and verified sat at
`OPERATOR_ACTION_REQUIRED` for ever unless somebody clicked something.

Phase 4B closes that last gap and hardens the one failure mode that was
burning paid generations.

## 1. The evidence failure — what it actually was

Two real 2026-09-18 lectures were rejected on generation 1:

| Lecture | Valid clips | Invalid | Why |
| --- | --- | --- | --- |
| G2-Juliane | 54 | 2 | `start == end` — zero-length clips |
| Risk Management | 47 | 1 | 1,560 ms — below the 2,000 ms minimum |

Both produced a **complete, correct eleven-row checklist** and **zero**
structured-output errors. Both were discarded entirely, and each spent a paid
generation.

**Classification: `MODEL_OUTPUT_INVALID`.** Not a validator bug, not a
coordinate bug, not a canonical-data problem. The legacy system message states
both rules verbatim:

> - Each evidence clip MUST have a duration of at least 2 seconds.
> - Never return identical start and end timestamps.

The validator enforces exactly the prompt's own contract, and it was right.

### Why not simply regenerate

Because the schema cannot prevent it. JSON Schema cannot express a cross-field
constraint (`end > start`) or a duration minimum, and Phase 3C3D already
dropped the bounds keywords OpenAI strict mode rejects. The prompt already says
both rules. So there is no deterministic pre-generation fix available — only
re-rolling the dice, with roughly fifty clips per generation and three attempts
before the lecture is stuck in manual review with a checklist that was right
the first time.

## 2. The hardening — a versioned evidence policy

`app/qa/evidence_policy.py` distinguishes two kinds of invalid clip:

**FABRICATED** — `OUT_OF_TRANSCRIPT_RANGE`, `NO_CUE_OVERLAP`,
`UNPARSEABLE_TIMESTAMP`. The model named a place that does not exist in the
transcript. That is hallucination; it says the answer was not grounded in the
evidence it cites, and it still fails the whole evaluation.

**DEGENERATE** — `END_NOT_AFTER_START`, `SHORTER_THAN_MINIMUM`. The model named
a **real** place with an unusable window. A zero-length clip is *empty*
evidence, not *false* evidence: it points nowhere wrong, it simply points at
nothing usable.

Excluding an empty pointer costs one supporting citation. Discarding the
evaluation costs its entire verdict and a paid generation. The old rule always
took the larger loss.

### What was NOT weakened

`app/qa/validation.py` is **untouched**. A 1,560 ms clip is still
`SHORTER_THAN_MINIMUM`; a zero-length clip is still `END_NOT_AFTER_START`;
every clip is persisted with its real status; no timestamp is rewritten. Only
VALID clips ever render, under every policy — tolerating a degenerate clip in
the verdict never lets it into the published evidence. A test asserts the
policy module cannot even see `MIN_CLIP_MS` or `parse_timestamp_ms`.

### Why it is not in the fingerprint

`evidence_policy_version` joins the evaluation metadata, **not**
`qa_source_fingerprint` and **not** `qa_model_input_fingerprint`.

That is a deliberate and load-bearing decision. Putting it in the source
fingerprint would have orphaned all 23 existing evaluations — `find_by_fingerprint`
would miss every one of them, the attempt count would reset to zero, and the
next scheduled cycle would have bought a fresh generation for roughly twenty
lectures. The policy changes how an answer is **judged**, never what the
provider was **asked**, so the stored answer stays reusable and history keeps
its meaning.

### The recovery: `revalidate_evidence`

A new, narrow operation re-judges the **stored** raw output under the current
policy. It buys nothing: no provider call, no generation attempt recorded, the
bounded budget neither consumed nor reset. The QA service it runs on is built
with `provider=None`, so it is structurally incapable of buying a generation.

Both lectures recovered to `COMPLETED` — 11/0/0 and 10/1/0 — for **zero
provider calls**. `lecture_qa_generation_attempts` still shows exactly one
attempt each, outcome `INVALID_EVIDENCE`, and always will: the generation
history is immutable, and the evaluation metadata records the re-judgement
alongside it.

The state resolver exposes this as its own action, `REVALIDATE_EVIDENCE`, so
the scheduler tries the free recovery **before** anything that costs money.
Three actions now touch `QA_EVALUATION` and only one of them spends: `RUN_QA`
buys a generation, `REFRESH_DETERMINISTIC_QA` recomputes from a reusable
answer, `REVALIDATE_EVIDENCE` re-applies the evidence rule.

## 3. Automated legacy sync

`app/orchestration/sync_safety.py` holds the whole safe list, by name, so
widening it is a visible edit to a file whose only job is to say what is safe.

| Target | Auto-approved | Everything else |
| --- | --- | --- |
| QA | `WOULD_INSERT` (write) · `WOULD_SKIP_IDENTICAL` (no-op) | `MANUAL_REVIEW_REQUIRED` with the writer's own reason code |
| Perfect | `PERFECT_WOULD_INSERT` (write) · `PERFECT_WOULD_SKIP_IDENTICAL` (no-op) · `PERFECT_NOT_ELIGIBLE` (nothing to sync) · `PERFECT_PENDING_ATTENDANCE_DATA` (waiting) | `MANUAL_REVIEW_REQUIRED` |

Refused outright and for ever: `WOULD_UPDATE`, `PROTECTED_EXISTING_LEGACY_ROW`,
`PERFECT_WOULD_UPDATE`, `PERFECT_BLOCKED_KEY_COLLISION`,
`PERFECT_SUPERSEDED_NOT_PERFECT`, `PERFECT_NOT_ELIGIBLE_LEGACY_ROW_EXISTS`, and
every `BLOCKED_*`. An **unrecognised** decision is never auto-approved either —
a new writer outcome must be classified deliberately rather than inherit
permission by default.

### Two plans, and the writer always has the last word

1. Plan under **`DRY_RUN`**, through the real writer, so the decision the gate
   reads is the writer's own rather than a prediction of it.
2. Classify. Anything outside the safe list becomes `ManualReviewRequired`,
   which is recorded as review — not as a failure, because nothing went wrong
   and nothing was half done.
3. Only then run the writer again in **`PRODUCTION_NEW_ONLY`**, where the
   planner runs a **second** time and can still refuse. If a legacy row
   appeared in between, the second plan sees it and declines.

`allow_update_existing` is `False` on every writer the scheduler builds, and
`EXPLICIT_BACKFILL` is unreachable from it.

One subtlety worth knowing: `PERFECT_WOULD_UPDATE` is *actionable* to the
writer and **would** be performed under `PRODUCTION_NEW_ONLY`. What prevents
it is that the Perfect planner is left out of the write pass entirely whenever
its own decision is unsafe — not trusting the writer to decline something it is
entitled to do. An unsafe Perfect therefore never blocks a safe QA insert: the
two are separate legacy targets with separate lifecycles.

### Stage order corrected

`PERFECT_ELIGIBILITY` now comes **before** both writes. Phase 4A had stepped
the executable search *over* an operator gate so a pending human action could
not block eligibility; that was a workaround for an ordering mistake.
Eligibility is derived from the frozen Phase 3B render and never needed the
legacy row. Only `PERFECT_SYNC` depends on the QA row, so:

```
QA_EVALUATION → QA_RENDER → PERFECT_ELIGIBILITY → LEGACY_QA_SYNC → PERFECT_SYNC
```

`OPERATOR_ONLY_ACTIONS` is now empty, and kept, so re-gating an action later is
one line rather than a rewrite.

## 4. Concurrency — two scopes

Phase 4A's per-lecture advisory locks stop two cycles working the same lecture.
They do nothing about two cycles both running day-wide discovery, both sweeping
Graph, and both independently deciding a lecture needs a generation.

`SchedulerCycleLock` adds a second, coarser scope: one cycle at a time, taken
before any Graph sweep or provider call, non-blocking, on a fixed key that
cannot collide with any uuid-derived lecture key. Session-level again, so a
crashed scheduler releases it when its connection dies.

## 5. Deployment — the honest answer

**`SCHEDULER_ENABLED=true` does not schedule anything.** It removes the
platform's own refusal to run a cycle. No process appears, no timer starts, and
nothing fires at 21:00. Something outside this code must call a cycle.

Two supported runtimes, both using the same `SchedulerService.run_cycle` the
CLI and the validated pilots used. There is no second scheduling
implementation.

### Option 1 — Windows Task Scheduler (the current local reality)

```powershell
powershell -ExecutionPolicy Bypass -File automation\scheduler\install-windows-task.ps1
```

Fires `scheduler-cycle` at 21:00 and 23:00. **No process stays resident between
firings.** `MultipleInstances IgnoreNew`, `StartWhenAvailable`, `WakeToRun`, and
three retries fifteen minutes apart. Logs to `logs/scheduler-cycle.log`.

This machine is on **Egypt Standard Time**, which is the platform's business
timezone, so a 21:00 local trigger *is* 21:00 Cairo. On any other machine the
trigger times must be converted — the installer prints both timezones so a
mismatch is visible.

### Option 2 — container

```bash
docker compose -f automation/scheduler/docker-compose.yml up -d
```

`restart: unless-stopped`, log rotation, no exposed port — the same conventions
as `services/kbc-media-worker`. **The container must stay running**: it sleeps
until the next cron instant and then runs one cycle.

Use one or the other, never both. The cycle lock makes overlap *safe*, but two
schedulers make the logs a puzzle.

### The cron parser is deliberately tiny

It accepts `<minutes> <hours> * * *` with `*` or comma-separated numbers, and
refuses everything else loudly — ranges, steps, day-of-week. A full cron
implementation would be more code than the thing it schedules, and a silently
misparsed expression is how an unattended system runs at the wrong time for a
month before anybody notices.

## 6. What the controlled cycles did

| | Cycle 1 | Cycle 2 | Cycle 3 (enabled path) |
| --- | --- | --- | --- |
| Passes | 5 | 1 | 1 |
| Provider calls | **0** | 0 | 0 |
| Graph calls | 21 | 21 | 21 |
| Legacy rows written | **6** | 0 | 0 |
| Idempotent | no (real work) | **yes** | **yes** |
| Row changes | +5 sessions, +55 items, +1 Perfect | **none** | **none** |

Cycle 1, unattended and with no per-lecture writer command:

- G2-Juliane and Risk Management: `REVALIDATE_EVIDENCE` → `RENDER_QA` →
  `EVALUATE_PERFECT` → `SYNC_LEGACY_QA` → done, for zero provider calls;
- Martech-Fri, Femi and AI in Project Control: `SYNC_LEGACY_QA` → done;
- G2-Juliane additionally reached 11/0/0 and became the **first Perfect row
  ever written under `kbc_perfect_v2_attendance_required`**.

Every write verified by the writer's own re-read: 22 session columns, 11
checklist rows with 6 mapped columns each, ownership row, post-write digest.
`recording_url`, `recap_url` and `excel_synced_at` were left NULL and untouched,
so Excel Sync's `WHERE` still selects nothing until the recording branch fills
its own column.

### A transient failure, handled

The first run of cycle 3 hit a Microsoft Graph timeout during discovery. It
aborted with zero row changes, no orphaned run row and no held lock — safe, but
wasteful: sixteen known lectures went unreconciled because a calendar query was
slow. Discovery failures are now recorded and the cycle continues with the
registry it already has. The undiscovered day is picked up by the next cycle,
which is what the lookback window is for.

## 7. 2026-09-18, finished

| | |
| --- | --- |
| Canonical lectures | 7 |
| Complete | **5** |
| Waiting | 1 — attendance source genuinely empty |
| Review | 1 — duplicate calendar booking |
| Failed | **0** |
| Legacy QA synced | 5 |
| Perfect eligible / synced | 1 / 1 |

The review lecture is a **duplicate booking**: two calendar events at the same
instant with the same normalized subject and different iCalUIDs, only one of
which is a real Teams meeting. Re-running discovery will never resolve the
other, and inventing a meeting id is forbidden — so the platform now names it
(`DUPLICATE_CALENDAR_EVENT_SIBLING_RESOLVED`) and points at the sibling that
did resolve, turning an opaque manual review into a one-look decision.
