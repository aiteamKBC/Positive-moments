# KBC Lecture Platform — scheduler deployment handoff

For the backend / infrastructure team. Nothing in this document has been
deployed. The scheduler currently runs only on a developer laptop, on demand,
and there is no unattended production execution anywhere.

---

# QA CORE — DEPLOY NOW

**This is the release to deploy.** It is frozen, tested and self-contained.
Everything in sections 1–12 below remains accurate for the scheduler; this
section is what to actually run, and it adds the API and console alongside it.

## Package location

```
release/qa-core-rc1/
```

Built by `deploy/build_qa_core_bundle.py` from the validated working tree —
**not** by `git clone`, which would produce an empty release because `app/`,
`backend/operations/`, `automation/`, `tests/` and `docs/` are all untracked
(see section 17 of the release report and section 3 below).

321 files, 3.8 MiB. Contains no `.env`, no credentials, no `node_modules`, no
virtualenv, no media files, no build output and no media worker. The build
script re-verifies all of that on every run and prints a bundle `sha256`.

## Services

| Service | Role | Port |
|---|---|---|
| `scheduler` | nightly QA cycle — **the only writer** | none |
| `api` | Operations API | `127.0.0.1:8000` |
| `console` | Vue console behind nginx | `127.0.0.1:8080` |

**Not included:** media worker, FFmpeg, Positive Clip producer, Lecture Part
producer. See *Media platform — deploy later*.

## 1. Configure

```bash
cd release/qa-core-rc1
cp deploy/qa-core.env.template backend/.env
# fill in every value; the template carries names only, never values
```

Leave `SCHEDULER_ENABLED=false` for the first build. It is the single switch
that makes the platform write, and it should be flipped deliberately.

## 2. Build

```bash
docker compose -f deploy/docker-compose.qa-core.yml --profile qa-core build
```

## 3. Migrations

The schema is applied by running the numbered files in order against
`DATABASE_URL`. **QA Core requires `001` … `016`.**

```bash
for f in app/db/migrations/0{0,1}*.sql; do
  echo "== $f"; psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f";
done
```

Each file is idempotent (`CREATE TABLE IF NOT EXISTS`), so re-running is safe.

Migrations `017`–`019` are **media** migrations. They are additive and already
applied on the current database. **Leave them.** Do not roll back migrations to
create a QA-only schema — there is nothing to gain and it destroys Phase 6 data.

## 4. Start

```bash
docker compose -f deploy/docker-compose.qa-core.yml --profile qa-core up -d
docker compose -f deploy/docker-compose.qa-core.yml ps
```

`restart: unless-stopped` brings all three back after a VPS reboot with nobody
logged in. The scheduler exposes **no port**.

## 5. Verify the n8n preflight before enabling writes

```bash
docker compose -f deploy/docker-compose.qa-core.yml exec scheduler \
  python -m app.cli.main n8n-preflight
```

Expect `status: LEGACY_QA_DISABLED`, `writes_permitted: true`,
`read_only: true`, `http_method: GET`, `n8n_modified: false`.

Anything else means the legacy n8n QA branch is active and coded QA must stay
off. **Do not remove the guard to proceed.**

## 6. Enable the nightly cycle

```bash
# in backend/.env
SCHEDULER_ENABLED=true
docker compose -f deploy/docker-compose.qa-core.yml up -d scheduler
```

## 7. Verify the first nightly run

```bash
docker compose -f deploy/docker-compose.qa-core.yml logs -f scheduler
```

At 21:00 Africa/Cairo expect one cycle: the preflight result, the target
business date, the 3-day window, and a per-stage outcome per lecture. A cycle
that finds nothing to do is a successful cycle. Confirm a row landed in
`lecture_pipeline_runs` with `run_type = 'SCHEDULED'`.

## 8. Logs

JSON lines, rotated at 20 MB × 5, per service:

```bash
docker compose -f deploy/docker-compose.qa-core.yml logs --since 24h scheduler
docker compose -f deploy/docker-compose.qa-core.yml logs --since 1h api
```

## 9. Rollback (application only)

```bash
docker compose -f deploy/docker-compose.qa-core.yml down
# restore the previous image tags, then bring the profile back up
```

The schema is additive, so the previous application version runs against the
current database unchanged. **Never roll back the database as part of an
application rollback.**

To stop writes immediately without a rebuild: set `SCHEDULER_ENABLED=false` and
restart the scheduler, or `docker compose ... stop scheduler` (300 s grace lets
a running cycle finish).

## Still to be decided by whoever deploys

Reverse proxy, TLS and the public hostname are **not** configured here. The API
and console are bound to loopback deliberately; routing `/api` to `api:8000`
and serving `console:80` is the infrastructure team's decision.

---

# MEDIA PLATFORM — DEPLOY LATER

**Not part of this release. Still under active development.**

Positive Clip cutting, Lecture Part cutting, the media worker, FFmpeg
production and the media sections of the console are Phase 6 work in progress.
They are excluded from the `qa-core` profile and QA does not depend on any of
them — proven by a 15-check isolation gate (no `app.media` module is imported
by the scheduler entrypoint; none of the 15 pipeline stages is a media stage;
lectures whose recording was never located still reach a coded QA evaluation).

`services/kbc-media-worker/docker-compose.yml` remains its own separate stack
and is untouched by the QA Core release. Do not start it.

One known limitation is recorded for later:
**`MULTIPART_MEDIA_SOURCE_LOCATOR_BLOCKED`** — recordings from 16 September
onward have no SharePoint drive item located, and the n8n claim workflow
resolves that item unconditionally. It affects media only and has no bearing on
QA Core.

---

## 1. Target architecture

```
        Projects VPS  (deployment target)
        ┌──────────────────────────────────────┐
        │  Docker Compose project              │
        │  name: kbc-lecture-platform          │
        │                                      │
        │    lecture-scheduler  (1 container)  │
        │    · no inbound port                 │
        │    · non-root (uid 10001)            │
        │    · sleeps until the next Cairo     │
        │      cron instant, runs one cycle    │
        └──────────────┬───────────────────────┘
                       │ outbound HTTPS / TLS only
        ┌──────────────┼──────────────┬──────────────┬──────────────┐
        ▼              ▼              ▼              ▼              ▼
  Neon Postgres   Neon Postgres   Microsoft      OpenAI         n8n
  (primary)       (Aptem source)  Graph          API            (SEPARATE VPS)
  eu-west-2       eu-west-2                                     read-only,
  read/write      read-only                                     GET only
```

**n8n is NOT on the deployment target.** It runs on its own separate VPS and
is reached over remote HTTPS through its management API, GET only. The
platform never modifies an n8n workflow; it reads workflow state as a safety
precheck before allowing legacy writes.

**Both databases are managed Neon Postgres in eu-west-2, not on any VPS.**
Nothing about this deployment moves, migrates or reshapes data. The scheduler
is a client of an existing, already-populated production database.

### Target VPS

| | |
| --- | --- |
| **Deploy to** | the company **Projects VPS** |
| **Do NOT deploy to** | the **n8n VPS** |

The two must stay separate. The scheduler needs no co-location with n8n — the
only link between them is an outbound HTTPS call.

---

## 2. What the scheduler is

One long-running process: `python -m app.cli.main scheduler-daemon`.

It computes the next instant matching `SCHEDULER_CRON` in `SCHEDULER_TIMEZONE`,
sleeps until then, runs exactly one cycle through `SchedulerService.run_cycle`,
and repeats. It is not a web service, it has no HTTP surface, and it is not a
host-cron job.

- **There is exactly one scheduling implementation.** The daemon, the manual
  CLI and every validated pilot all call the same `run_cycle`. Please do not
  add a host cron entry or a systemd timer alongside it — a second trigger is
  made *safe* by a PostgreSQL cycle-level advisory lock, but it makes the logs
  unreadable.
- `SCHEDULER_ENABLED=false` is a hard interlock: `run_cycle` refuses and the
  daemon logs the refusal. It is the rollback switch.
- Overlap is prevented in the database, not in the process, so it holds across
  containers, hosts and restarts.
- SIGTERM makes the daemon finish the current cycle and exit; `docker stop`
  therefore needs the grace period configured in the compose file.

---

## 3. Deployment directory and files

Suggested location on the Projects VPS:

```
/opt/kbc-lecture-platform/          <- the repository tree
├── app/                            <- the application package
├── automation/scheduler/
│   ├── docker-compose.yml          <- the stack
│   ├── Dockerfile                  <- the image
│   └── requirements.txt            <- production dependencies only
└── backend/.env                    <- secrets, created on the server
```

The compose file uses `build.context: ../..` and
`env_file: ../../backend/.env`, so **the repository layout must be preserved**
— the compose file cannot be copied out on its own.

> **Note on source delivery:** the application tree (`app/`, `automation/`,
> `tests/`, `docs/`) is currently **untracked in git** — `git ls-files app/`
> returns zero. A `git clone` deployment is therefore not possible today.
> Either the tree needs committing and pushing to a remote the VPS can reach,
> or it needs delivering by `rsync`/`scp`. That decision is yours; flagging it
> because it is the first thing that will block a clone-based deploy.

---

## 4. Environment variables

Create `backend/.env` **on the server**. It is already in `.gitignore`, it is
never `COPY`ed into the image (enforced by a test), and it reaches the
container only at runtime via `env_file`.

```
chmod 600 backend/.env
chown <deploy-user> backend/.env
```

### Names only — values come from the existing project configuration

**Databases**
- `DATABASE_URL`
- `APTEM_DATABASE_URL`

**Microsoft Graph**
- `MICROSOFT_GRAPH_TENANT_ID`
- `MICROSOFT_GRAPH_CLIENT_ID`
- `MICROSOFT_GRAPH_CLIENT_SECRET`
- `MICROSOFT_GRAPH_SCOPE`
- `MICROSOFT_GRAPH_BASE_URL`
- `KBC_LECTURE_CALENDAR_USER_UPN`

**Model provider**
- `QA_MODEL_API_KEY`
- `QA_MODEL_NAME` *(optional; defaults to the legacy model — changing it
  changes the prompt contract, so leave it unset unless that is intended)*

**n8n (remote, read-only)**
- `N8N_BASE_URL`
- `N8N_API_KEY`

**Scheduler** — see the required production values in section 5
- `SCHEDULER_ENABLED`
- `SCHEDULER_TIMEZONE`
- `SCHEDULER_CRON`
- `SCHEDULER_LOOKBACK_DAYS`
- `SCHEDULER_DISCOVER`
- `SCHEDULER_ALLOW_GRAPH`
- `SCHEDULER_ALLOW_PROVIDER`
- `SCHEDULER_ALLOW_LEGACY_WRITES`
- `SCHEDULER_MAX_PASSES` *(optional)*

`backend/.env.example` carries the same names with placeholder values.

---

## 5. Production scheduler configuration

These are the validated values. Please do not change them as part of
deployment; a change here changes when and how the platform writes to
production.

| Variable | Production value |
| --- | --- |
| `SCHEDULER_ENABLED` | `true` *(but see section 8 — start with `false`)* |
| `SCHEDULER_TIMEZONE` | `Africa/Cairo` |
| `SCHEDULER_CRON` | `0 21,23 * * *` |
| `SCHEDULER_LOOKBACK_DAYS` | `3` |
| `SCHEDULER_DISCOVER` | `true` |
| `SCHEDULER_ALLOW_GRAPH` | `true` |
| `SCHEDULER_ALLOW_PROVIDER` | `true` |
| `SCHEDULER_ALLOW_LEGACY_WRITES` | `true` |

`0 21,23 * * *` Africa/Cairo matches the legacy QA Master Daily schedule
deliberately. The cron parser accepts only `<minutes> <hours> * * *` and
**refuses** anything using ranges, steps, day-of-month, month or day-of-week
rather than approximating it.

---

## 6. Database and migrations

Migrations are numbered SQL files in `app/db/migrations/`, **001 through 016**.
Phase 4C1 added none.

**Please verify before starting the container, and apply only what is
missing.** Two things to know:

1. There is **no migration runner and no ledger table** in this project. The
   numbered files have been applied by hand. `SELECT` against
   `information_schema.tables` is the current way to check state.
2. The target Neon primary database is **already at 016** — it is the same
   database every validated phase ran against, and it currently holds 38
   `lecture_*` tables. In all likelihood **no migration needs applying at
   all**; please confirm rather than assume.

Do not drop, reset or recreate anything. The database holds the production
evidence for six phases of migration work.

---

## 7. Build and run

```bash
cd /opt/kbc-lecture-platform/automation/scheduler

# validate the compose file before anything runs
docker compose config

# build and start
docker compose up -d --build

# status
docker compose ps
docker stats --no-stream kbc-lecture-scheduler

# logs (JSON lines, rotated at 20 MB x 5)
docker compose logs -f lecture-scheduler
docker compose logs --tail 200 lecture-scheduler

# stop / restart  (the grace period lets a running cycle finish)
docker compose stop
docker compose restart lecture-scheduler

# remove the stack — this project only
docker compose down
```

The explicit `name: kbc-lecture-platform` in the compose file means
`docker compose down` here cannot reach another project's containers,
networks or volumes.

### Image properties (each enforced by `tests/unit/test_deployment_contract.py`)

- **no inbound port** — no `ports:`, no `EXPOSE`
- **non-root** — runs as `scheduler`, uid 10001
- **no secret baked in** — no `.env` in any `COPY`/`ADD`
- **production dependencies only** — `psycopg`, `python-dotenv`, `tzdata`.
  Not Django, not DRF, not pytest.
- **`tzdata` is explicit** — the whole schedule is Africa/Cairo and `ZoneInfo`
  falls back to that package when the base image ships no system zoneinfo
  database. Please keep it pinned.
- **`restart: unless-stopped`** — survives a Docker restart and a host reboot,
  provided the Docker service itself is enabled (`systemctl is-enabled docker`)
- **`stop_grace_period: 300s`** — the default 10s would SIGKILL a cycle
  mid-write. A killed cycle is safe (the transaction rolls back and PostgreSQL
  drops the session advisory lock), but a clean finish is better.
- **log rotation** — json-file, `max-size 20m`, `max-file 5`
- **`TZ: Africa/Cairo`** in the container environment

---

## 8. Recommended activation order

The scheduler writes to live production tables shared with n8n, so please do
not go straight to enabled.

1. Create `backend/.env` with **`SCHEDULER_ENABLED=false`**.
2. `docker compose up -d --build`. Confirm the container stays up and is not
   crash-looping.
3. **Status check** — proves database connectivity, configuration and the
   remote n8n precheck in one call:
   ```bash
   docker compose exec lecture-scheduler \
     python -m app.cli.main scheduler-status --json
   ```
   Expect `cron_fields`, a `next_fire_at` with a `+03:00` offset, and a
   `legacy_qa_precheck` block.
4. **n8n remote preflight** — the precheck inside the status output must
   reach the n8n VPS from the Projects VPS and report `LEGACY_QA_DISABLED`
   (the `Execute QA One Lecture` node disabled in QA Master Daily). It is
   **GET only** and never modifies a workflow.
   **If n8n is unreachable from the Projects VPS, stop and fix the network
   path before enabling anything** — that precheck is what gates legacy writes.
5. **Dry run**, no production writes:
   ```bash
   docker compose exec lecture-scheduler \
     python -m app.cli.main scheduler-cycle --dry-run --force --json
   ```
6. **One controlled cycle**, using the same entrypoint automation uses:
   ```bash
   docker compose exec lecture-scheduler \
     python -m app.cli.main scheduler-cycle --force --json
   ```
   Then **immediately run it again** — the second cycle should report
   `passes: 1`, zero provider calls, zero legacy rows written and zero row
   changes. That is the idempotency proof.
7. Only then set `SCHEDULER_ENABLED=true` and
   `docker compose up -d --force-recreate`.

`--force` runs one cycle while `SCHEDULER_ENABLED` is still false. It enables
nothing and schedules nothing.

Please do **not** run individual writer commands by hand. Every production
write goes through the scheduler's guarded path, which plans each write twice
and refuses anything that is not a clean insert or a true no-op.

---

## 9. Health and status

| Question | Command |
| --- | --- |
| Is the container up? | `docker compose ps` |
| Is the configuration right, and when does it next fire? | `docker compose exec lecture-scheduler python -m app.cli.main scheduler-status --json` |
| Is n8n reachable and in the expected state? | the `legacy_qa_precheck` block of the same output |
| What happened last night? | `docker compose logs --since 24h lecture-scheduler` |
| What does the platform think of a given day? | `docker compose exec lecture-scheduler python -m app.cli.main reconcile-day --date YYYY-MM-DD --json` |

There is **no container healthcheck**, deliberately. A check that only proves
the process exists would report a wedged daemon as healthy, and a real
liveness signal needs a heartbeat the scheduler does not yet write. Treat
`scheduler-status` plus the nightly log lines as the health signal for now;
a heartbeat is a sensible small follow-up.

Logs are JSON lines carrying run id, cycle status, lecture counts, reason
codes, Graph and provider call counts, and error codes. They do not carry API
keys, tokens, transcript text, learner PII or signed URLs.

---

## 10. Rollback

**The switch:**

```bash
# in backend/.env
SCHEDULER_ENABLED=false

docker compose up -d --force-recreate lecture-scheduler
```

The daemon keeps running and refuses every cycle. Nothing is written.

**Full stop, this project only:**

```bash
cd /opt/kbc-lecture-platform/automation/scheduler
docker compose down
```

Rollback touches **nothing else**: not n8n (a separate VPS), not the Neon
databases, not legacy QA data, not the external attendance tables. There is no
destructive database rollback and none should be added — the platform's safety
model is that a stopped scheduler simply stops, leaving every completed stage
intact and resumable.

Restarting the container is always safe. Every stage is idempotent and
version-aware: a cycle interrupted anywhere resumes from the earliest
genuinely incomplete stage and re-runs nothing that is already complete.

---

## 11. Confirmations

- **The scheduler exposes no inbound port.** No `ports:` in the compose file,
  no `EXPOSE` in the Dockerfile, no HTTP server in the process. It is a client
  of five systems and a server to none. **No firewall rule is needed for it,
  and no domain or reverse proxy is required.**
- **No public domain is needed at this stage.** A company subdomain behind an
  HTTPS reverse proxy will be needed later, for the Operations API and UI —
  neither of which exists yet. Please do not provision one for the scheduler.
- **n8n is not modified.** GET only, against a remote VPS.
- **No Windows Task Scheduler task is used or installed.** The Windows
  artefacts under `automation/scheduler/` are for the development laptop and
  must not be activated on any server.
- **Exactly one production scheduler.** One container, one entrypoint, one
  cron source. Please do not add a host cron entry or a second compose project.
- **Secrets are never baked into the image** — runtime `env_file` only.
- **The deployment does not reset or reshape any database.**

---

## 12. Open items for whoever deploys

1. **Source delivery** — the application tree is untracked in git (section 3).
2. **No migration runner** — verification is manual (section 6).
3. **No heartbeat / healthcheck** — a wedged daemon would look running
   (section 9).
4. **First-run verification must be done on the server**, not inferred from
   the laptop. The local test suite (1489 passing) proves the logic; it cannot
   prove the container's network path to Neon, Graph, OpenAI or the n8n VPS.
