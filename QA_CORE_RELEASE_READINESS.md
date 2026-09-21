# KBC QA Core — Release Readiness (rc2)

**Written for:** the Backend / Infrastructure team who will deploy this.

This release freezes the nightly coded QA pipeline, the operations console and
**historical backfill**. Phase 6 media processing is **deliberately excluded**
and continues in development; nothing in this release depends on it.

> **rc1 → rc2** adds one feature: Operations Backfill, for recovering the
> September 2026 lectures the stopped legacy n8n QA flow never processed. The
> nightly schedule, the writer protections and the media exclusion are
> unchanged.

---

## What is being deployed now

| Service | Image | Role | Inbound |
|---|---|---|---|
| `scheduler` | `kbc-lecture-scheduler:1.0.0` | The nightly cycle. **The only service that writes.** | none |
| `api` | `kbc-qa-api:1.0.0` | Operations API (Django + DRF) | `127.0.0.1:8000` |
| `backfill-runner` | `kbc-lecture-scheduler:1.0.0` | Historical recovery. Also writes. | none |
| `console` | `kbc-qa-console:1.0.0` | Vue/Vite bundle behind nginx | `127.0.0.1:8080` |

The pipeline these cover, end to end:

calendar discovery → meeting resolution → transcript acquisition → transcript
selection → canonical cues → speakers → attendance → engagement → QA evaluation
→ QA rendering → safe legacy QA sync → Perfect eligibility → Perfect sync →
recording-link stage → reconciliation / recovery.

## What is intentionally NOT being deployed

- **Media worker** (`services/kbc-media-worker`) — separate stack, untouched
- **FFmpeg media production** of any kind
- **Positive Clip producer** and **Lecture Part producer** — no automatic run
- **Unfinished media UI** — see *Honest media state* below

None of these is absent by accident. They are excluded by the `qa-core` Compose
profile, and the QA pipeline has no code path into any of them.

---

## Nightly schedule

| | |
|---|---|
| Timezone | **Africa/Cairo** |
| Runs | **21:00** and **23:00** (`SCHEDULER_CRON=0 21,23 * * *`) |
| Lookback | **3 days** |
| Enabled by | `SCHEDULER_ENABLED=true` — **the single switch that makes it live** |

The schedule is the legacy QA Master's own, kept deliberately rather than
reinvented. Three days covers a late transcript, a late attendance row and a
weekend without reaching into history that is already settled.

The scheduler container sleeps until the next cron instant and runs one cycle.
`restart: unless-stopped` means it returns by itself after a VPS reboot. It
does **not** depend on a laptop, Windows Task Scheduler, or an interactive
terminal. Once deployed, the laptop being off has no effect.

> **Do not run both.** If the Windows Scheduled Task is ever installed on a
> machine that can reach this database, two schedulers will fire. The
> cycle-level advisory lock makes that *safe*, but it makes the logs a puzzle.

---

## Database

**One managed PostgreSQL (Neon, eu-west-2)** via `DATABASE_URL`.

### Migrations QA Core requires

`001` … `016`, applied in order:

| | |
|---|---|
| 001–003 | lecture discovery, organizer context, canonical registry |
| 004–007 | transcript artifacts, candidates, provider identity, selection |
| 008–009 | canonical cues, speaker inventory |
| 010–011 | attendance resolution, engagement |
| 012–013 | shadow QA, QA rendering |
| 014 | writer guardrails |
| 015 | Perfect lecture tables |
| 016 | pipeline orchestration (stage runs, scheduler state) |
| **020** | **backfill runs and per-day summaries (new in rc2)** |

### Migrations already applied that QA Core does not need

`017`, `018`, `019` are **media** migrations (recording-part measurements and
their SharePoint source location). They are **purely additive** — new tables and
new nullable columns, no change to any QA table.

**The QA Core release is compatible with a database that already has them
applied, and that is the current state.** Leave them.

> **Do not roll back migrations to produce a QA-only database.** There is
> nothing to gain: the QA code neither reads nor writes those tables. Rolling
> back would destroy Phase 6 measurements and the three delivered lecture-part
> assets for no benefit.

`qa_media_jobs` and `qa_positive_clip_assets` are legacy tables owned by the
media worker's own `sql/001`, outside this migration sequence. QA Core does not
touch them.

---

## Safety guarantees that must not be weakened

### The n8n ownership preflight

Before the coded scheduler performs **any** write, it verifies read-only that
the legacy n8n QA execution path is disabled.

- Workflow `8yeJigbj8BCNBMkU` (QA Master), node `Execute QA One Lecture`
- **`GET` only.** The platform never POSTs, PATCHes, activates or deactivates.
- Pass → `LEGACY_QA_DISABLED`, `writes_permitted: true`
- Fail → the cycle **fails closed**; no write is attempted

If the legacy branch is ever re-enabled, coded QA stops writing. That is the
intended behaviour and must not be removed for convenience.

### How we know QA owns a write

The writer guardrails (migration 014) record ownership per row. A QA row the
coded platform did not create is **protected**: it is never overwritten. A
lecture's QA checklist is exactly **11 items** — not 10, not 12 — and a render
that would produce a different count is refused rather than truncated.

### What happens if attendance is missing

Attendance that cannot be established authoritatively stays **`WAITING`**. It
is never reported as a confirmed zero. A lecture waiting on attendance is
visible as waiting in the console and is retried on the next cycle; it does not
fail and it does not silently pass.

### Perfect eligibility

Perfect sync runs **only** when a lecture is eligible. Eligibility is computed
from the coded QA result and the attendance state; an ineligible lecture is
skipped with a recorded reason, not forced through.

### Suppressed duplicate lectures

Where the same lecture appears more than once, duplicate suppression picks one
and **suppressed duplicates are not processed downstream** — no QA write, no
Perfect sync, no legacy sync. They remain visible in the console as suppressed.

---

## Honest media state in the console

The console shows Positive Moments history (read-only) and lecture media state.
It reports what is actually true:

- "Ready" comes **only** from an asset row with a durable SharePoint URL
- A plan or a queued job is **never** reported as ready
- Job status and asset status are shown separately, not merged into one word

An incomplete asset displays its real state. No media control in this release
triggers production processing.

---

## Historical backfill (new in RC2)

September 2026 contains lectures the coded platform never processed, because
the legacy n8n QA flow was stopped deliberately. Backfill recovers them.

### What it is

A loop and a checkpoint. **There is no second pipeline.** Recovering a past day
is the question the platform already answers every night, so a backfill day is
one call to `PipelineOrchestrator.run_window` with `lookback_days=0` and
`discover=True` — the same entry point the nightly scheduler uses. It therefore
inherits, rather than re-implements:

- the read-only n8n ownership preflight, which fails the run closed
- the cycle advisory lock, which is how it coexists with the nightly scheduler
- active Aptem groups from `public.aptem_auto_extracting`
- Microsoft calendar discovery and the production subject matcher
- canonical `lecture_id` identity and registry reconciliation
- the StageResolver, the StageRunner and every writer protection
- a normal `lecture_pipeline_runs` audit row per day, `run_type = BACKFILL`

### Preview is a run too

A preview costs the same Graph reads as an execution — **measured: ~7 s of
Graph plus ~2.6 s of stage resolution per day, so 1–21 September is roughly
three minutes.** That does not fit in an HTTP request, and a preview that times
out halfway is worse than none.

So preview is the same durable run with `mode = PREVIEW`. It calls the
production `discover_day` with `persist=False`: production discovery,
production matcher, production canonical identity, **writing nothing** — no
registry row, no discovery run row, no QA row, no Perfect row. The runner gives
preview work a PostgreSQL read-only connection, so that is enforced rather than
promised.

Verified on real data: a three-day September preview changed **nothing** in
`lecture_sessions`, `lecture_pipeline_runs`, `lecture_discovery_runs`,
`lecture_qa_evaluations`, `lecture_perfect_lecture_results` or
`qa_doctors_sessions`.

### Services

`backfill-runner` is a **second container from the scheduler's image**, running
`python -m app.cli.main backfill-runner`. It is separate from the scheduler
daemon on purpose: that daemon's design is "sleep until the next cron instant",
and a backfill must start within seconds of an operator pressing Start.
Rewriting its sleep into a poll loop would put the nightly 21:00/23:00 timing
at risk to save one container.

`restart: unless-stopped`, **no port**, 300 s stop grace.

### Resume, cancel and idempotency

| | |
|---|---|
| Checkpoint | `backfill_runs.current_business_date` — the next day still to do |
| Ordering | the day summary is written, **then** the checkpoint advances, in one transaction |
| Crash | the interrupted day re-runs; existing stage resolution makes it `NOTHING_TO_DO` |
| Cancel | cooperative — the day in flight finishes, no new day starts, status `CANCELLED` |
| Re-running a range | no duplicate lecture, QA, Perfect or pipeline identity; the second pass is almost entirely `NOTHING_TO_DO` |

There is **no force-reprocess**, and no parameter that could add one.

### Scheduler coexistence

Both take the same cycle advisory lock. If the scheduler holds it, the backfill
day is recorded `DEFERRED_CYCLE_BUSY` and re-attempted — deferring, not
fighting, because the scheduler is reconciling the same registry with the same
rules. **The scheduler is not disabled by the existence of a backfill run.**

### Routes

| Method | Path | |
|---|---|---|
| POST | `/api/operations/backfills/preview/` | queue a read-only inspection (202) |
| POST | `/api/operations/backfills/start/` | queue a real recovery (202) |
| GET | `/api/operations/backfills/` | history plus the active run |
| GET | `/api/operations/backfills/{id}/` | one run with its per-day results |
| POST | `/api/operations/backfills/{id}/cancel/` | request a safe stop |

All authenticated. Console route: **`/operations/backfill`**.

### Migration

**020** — `backfill_runs`, `backfill_run_days`, and a widened `run_type` CHECK
to admit `BACKFILL`. Additive and re-runnable; widening a CHECK cannot
invalidate an existing row. The tables hold intent and progress only — every
lecture outcome stays in `lecture_pipeline_runs`, so there is no second history
to drift.

---

## Two things that will surprise you when running the tests

Neither affects the deployed release; both will waste an afternoon if nobody
says so in advance.

**1. The Django suite cannot tear down its test database on Neon.** Neon
fronts connections with `pgbouncer`, which holds an idle session on
`test_AiTeamKBC`, so Django's `destroy_test_db` never gets the exclusive access
it needs and ends with:

```
OperationalError: database "test_AiTeamKBC" is being accessed by other users
```

**The tests have already run and reported by that point** — the error is in
teardown. Read the `Ran N tests` / `OK` lines above it. To clear a stuck test
database, terminate the pooler's session with `pg_terminate_backend` and re-run.

Do **not** "solve" this by blanking `DATABASE_URL` to fall back to sqlite:
39 of the 76 tests skip and one errors, which looks like a pass and proves
almost nothing.

**2. The integration suite takes hours, not minutes.** 362 tests, each opening
its own connection to managed Postgres in eu-west-2, at roughly 20 seconds per
test. It is not hung. The 1262-test unit suite runs in about 4 seconds and is
what to use during development; the integration suite is a pre-release gate.
`pytest-xdist` is not installed, so there is no parallel mode today.

## Operating the release

### Verifying the first nightly run

```bash
# the cycle is recorded in the orchestration tables
docker compose -f deploy/docker-compose.qa-core.yml logs -f scheduler
```

Look for one cycle at 21:00 Africa/Cairo with a recorded run row, the preflight
result `LEGACY_QA_DISABLED`, and per-stage outcomes. A cycle that finds nothing
to do is a successful cycle.

### Stopping the scheduler safely

```bash
docker compose -f deploy/docker-compose.qa-core.yml stop scheduler
```

`stop_grace_period: 300s` lets a cycle in progress finish. Killing it is also
safe — the transaction rolls back and PostgreSQL drops the session advisory
lock — but a clean finish is better than a safe abort.

To pause writes without stopping the container, set `SCHEDULER_ENABLED=false`
and restart it.

### Restart safety

A restart mid-cycle loses that cycle, not data. The next cycle re-resolves the
same lookback window and picks up where the pipeline actually is, because every
stage's state lives in the database rather than in the process.

### Rolling back the application without touching the database

```bash
docker compose -f deploy/docker-compose.qa-core.yml down
# redeploy the previous image tags, then:
docker compose -f deploy/docker-compose.qa-core.yml --profile qa-core up -d
```

The schema is additive across this release, so a previous application version
runs against the current database unchanged. **Do not roll back migrations as
part of an application rollback.**
