# Continuous Bunny Publisher: VPS production runbook

The publisher runs as **`bunny-publisher.service`** on the Projects VPS (`srv1281908`, git checkout `/var/www/Positive-moments`, venv `backend/venv`). It runs every day without a laptop, a manual trigger or the QA workflow.

**What it does**
- Every 5 minutes: discovers today's Teams calendar (Africa/Cairo day) and matches lectures against the LMS.
- For each lecture, independently: acquires the transcript, resolves the recording, imports to Bunny staging library 786045 (at most 2 in flight), verifies encoding, and publishes and verifies the caption.
- Reconciles every 6 hours, plus a full reconciliation daily at 05:00 Cairo.
- Keeps every video **HIDDEN**.

**What it doesn't touch.** The QA services keep their own units and schedules:
- `positive-moments-scheduler`: the 21:00 / 23:00 Cairo cycle;
- `positive-moments-backfill`;
- `positive-moments-media`.

The publisher never starts, stops or waits for them.

Validated by the 2026-10-23 live pilot: 7 of 7 LMS-verified lectures imported with verified captions (`output/publisher_pilot/FINAL_AUDIT_2026-10-23.md`, workstation only).

## 0. Facts and gates

| | |
|---|---|
| Database | AiTeamKBC on the Neon **DIRECT** endpoint (session advisory locks). A pooled host is refused at startup. |
| Schema | Migrations 024–028 are **already applied** in production (2026-10-23, `bunny_*` names). **Do not re-run them.** Historical Backfill (2026-10-29) adds **029** (`bunny_backfill_runs`, `bunny_backfill_items`; additive): see section 9. |
| Mode | `STAGING_WRITE` on staging library **786045**. Production library writes stay disabled (`PUBLISHER_PRODUCTION_WRITE_ENABLED=false`). 785080 is refused in code. |
| Scope | `PUBLISHER_START_DATE=2026-10-23` (go-live) and an empty `PUBLISHER_END_DATE`, so the day window rolls. Earlier lectures are historical-backfill scope and are never planned. |
| Instances | Exactly **one**. The local pilot daemon was stopped on 2026-10-23 at 16:59 Cairo; never start it again while the VPS service runs. |
| Learner exposure | None. Every video stays HIDDEN. Player, CDN and token settings are not changed. |

## 1. Code (operator)

On the workstation, push the deployment branch:

```bash
git push origin release/positive-moments-media
```

On the VPS:

```bash
cd /var/www/Positive-moments
git fetch && git checkout release/positive-moments-media && git pull --ff-only
git rev-parse --short HEAD            # record: the deployment revision
```

- No new Python packages are needed (`psycopg[binary]==3.2.9` and `python-dotenv` are already pinned).
- No frontend build.
- **No QA restart is required.** This revision also carries a behaviour-preserving refactor of `app/transcripts/selection_service.py`, which QA picks up at its next normal restart, and the read-only `/operations/publisher/*` routes, which the web picks up at its next restart.

## 2. Configuration (operator; it holds secrets)

The verified production file is `backend/config/publisher.production.env` on the workstation. It's git-ignored.

1. Copy it to the VPS over SSH. Don't paste it into a terminal or chat.
2. Install it:

```bash
sudo install -m 640 -o root -g www-data /tmp/publisher.production.env \
     /var/www/Positive-moments/backend/config/publisher.env && shred -u /tmp/publisher.production.env
```

Or start from the template (`deploy/publisher.env.template`) with `sudoedit` and fill it the same way.

## 3. Pre-flight checks (read-only; no Bunny writes, no test imports)

```bash
cd /var/www/Positive-moments
sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env validate-config --check-library
sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env db-smoke
sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env status | head -40
```

| Command | Must show |
|---|---|
| `validate-config` | `"mode": "STAGING_WRITE"`, `"library_id": 786045`, `"end_date": null`, `"database_endpoint": "neon-direct"`, `"problems": []`, `"library_problems": []` |
| `db-smoke` | `"ok": true`, `"publisher_tables_present": "15/15"`, `"migrations_024_028_applied": true`, 1 backend over 20 statements |
| `status` | every worker row has `stopped: true` (no other instance is running) |

## 4. Install and start the service

```bash
sudo cp deploy/systemd/bunny-publisher.service /etc/systemd/system/bunny-publisher.service
sudo systemctl daemon-reload
sudo systemctl enable --now --no-block bunny-publisher.service
```

## 5. Activation checks

```bash
systemctl is-enabled bunny-publisher.service; systemctl is-active bunny-publisher.service
systemctl status bunny-publisher.service --no-pager | head -15
pgrep -fa "app.cli.publisher run"                                         # exactly one line
journalctl -u bunny-publisher.service --since "-10 min" --no-pager | tail -40
sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env status
journalctl -u bunny-publisher.service --since today --no-pager \
  | grep -Eic "postgres(ql)?://|AccessKey|client_secret|tempauth|download\.aspx" # must print 0
systemctl is-active positive-moments.service positive-moments-scheduler.service positive-moments-backfill.service positive-moments-media.service
systemctl list-timers --all | grep -i positive                            # QA schedule unchanged
```

In `status`:
- `workers_healthy: true`;
- three `srv1281908:<pid>:scheduler|w0|w1` heartbeats under 60 s old;
- `schedule` → `DISCOVERY` `SUCCEEDED` within the last 5 minutes;
- no new IMPORT_VIDEO jobs for completed lectures;
- the 7 videos from 23 Oct are unchanged.

**Restart-resume check.** Queued jobs stay in PostgreSQL; leased jobs are reclaimed.

```bash
sudo systemctl restart --no-block bunny-publisher.service
sleep 60; sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env status | head -30
```

Expect new-PID heartbeats, the same job counts, and no duplicate jobs.

## 6. Monitoring (`bunny_*` tables and views)

All commands run from the checkout:

```bash
cd /var/www/Positive-moments
PUB="sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env"
```

| Need | Command |
|---|---|
| Service status | `systemctl status bunny-publisher.service --no-pager` |
| Recent logs | `journalctl -u bunny-publisher.service -n 200 --no-pager` (follow: `-f`; problems only: `-p warning`) |
| Heartbeats, jobs, schedule, alerts, videos | `$PUB status` |
| Failed / retrying / review jobs | `$PUB exceptions` (view `bunny_publisher_exceptions_v`) |
| One lecture: recording parts, transcript, Bunny, captions | `$PUB lecture --lecture-id <uuid>` |
| Recording and transcript coverage | `status` → `lectures_by_track` (views `bunny_recording_coverage_v`, `bunny_publisher_lecture_coverage_v`) |
| Bunny encoding and caption state | `status` → `videos` (tables `bunny_videos`, `bunny_caption_publications`, `bunny_caption_current`) |
| Reconcile now | `$PUB reconcile --kind light` |
| Audited operator retry | `$PUB retry --job-id <uuid> --actor "<name>" --reason "<why>"` |
| Fragment decision | `$PUB decide-part --lecture-id <uuid> --part <n> --decision INCLUDE\|EXCLUDE --actor "<name>" --reason "<why>"` |

The read-only API for the future dashboard tab lives at `/operations/publisher/status/`, `/pending/`, `/exceptions/`, `/lectures/<id>/` and `/jobs/<id>/history/`.

## 7. Graceful restart and rollback

| Situation | Action | Effect |
|---|---|---|
| Graceful restart | `sudo systemctl restart --no-block bunny-publisher.service` | SIGTERM; the step in hand finishes; leases are reclaimed on start |
| Stop Bunny writes, keep acquisition | Set `PUBLISHER_MODE=SHADOW` in `publisher.env`, then restart | Discovery and acquisition continue; in-flight imports are reconciled when writes resume |
| Stop the service | `sudo systemctl disable --now --no-block bunny-publisher.service` | Nothing is deleted; queued jobs wait in PostgreSQL |
| Roll back code | `git checkout <previous revision>` on the VPS, then restart | The `bunny_*` schema is compatible; do not roll back migrations while they hold rows (their rollbacks refuse) |
| Remove the unit | `sudo rm /etc/systemd/system/bunny-publisher.service && sudo systemctl daemon-reload` | |

**Never as part of a rollback:**
- delete Bunny videos, `bunny_*` rows or `lecture_sessions` rows;
- restore Neon;
- touch the QA units.

## 8. Known exceptions (don't block operation)

- **AI in Project Control 2026** (2026-10-23): `NO_LMS_SESSION`. Its recording and transcript are stored, and it isn't imported. The LMS team is investigating. No LMS occurrence is faked or bypassed.
- **LMS session `OCC-C3535F99…`** (Marketing Executive L4, 2026-10-23 11:00): `LMS_ONLY_PENDING`; no Teams lecture was discovered for it.
- **Orphan staging video** `be785a53-…` (2026-10-21, the single-video pilot): reported as `ORPHAN_BUNNY_VIDEO` and deliberately untouched.

## 9. Historical Backfill (Publishing Monitor → Historical Backfill tab)

Publishes the lectures of a past, **inclusive** range of Cairo dates through the same pipeline: LMS matching, the two tracks, the longest-recording selection, Bunny import, encoding, captions and AUTO_RELEASE. Code: `app/publisher/backfill.py`.

- **Nothing starts by itself.** The daemon's `HISTORICAL_BACKFILL` task (every minute, after the daily tasks) is idle until an operator confirms a run in the monitor.
- **Daily publishing comes first.** Historical jobs run below every daily priority, and at most `PUBLISHER_BACKFILL_MAX_IMPORTS` (default 1) historical imports are live at once. None is added while a daily import is waiting.
- **Unchanged:** the daily schedule, the lookback and the start date.
- **Optional settings** in `publisher.env`, with defaults:

  | Setting | Default |
  |---|---|
  | `PUBLISHER_BACKFILL_MAX_DAYS` | 31 |
  | `PUBLISHER_BACKFILL_BATCH_SIZE` | 10 |
  | `PUBLISHER_BACKFILL_MAX_ACTIVE` | 20 |
  | `PUBLISHER_BACKFILL_MAX_IMPORTS` | 1 |
  | `PUBLISHER_BACKFILL_INTERVAL_SECONDS` | 60 |

  The web reads `PUBLISHER_BACKFILL_MAX_DAYS` (default 31).
- **Preview** is read-only: a READ ONLY transaction from the stored publisher state, with no Graph, LMS or Bunny call. Any signed-in user can preview.
- **Start, cancel and retry** need the Django group `kbc_publisher_backfill_operators`. Grant it with `backend/venv/bin/python backend/manage.py publisher_backfill_operator --grant <username>` (`--list`, `--revoke`).
- **Schema 029:**
  - check (read-only): `python -m app.cli.publisher --env-file backend/config/publisher.env backfill-schema`;
  - install once: add `--apply`.

  Additive. Its rollback refuses while runs exist.
- **Cancel** stops pending lectures and the run's imports that have not reached Bunny (audited `OPERATOR_CANCEL`). Anything already submitted, encoding or captioning finishes normally.
- **Audit events** in `bunny_publisher_events`:
  - `HISTORICAL_BACKFILL_REQUESTED`
  - `_STARTED`
  - `_COMPLETED`
  - `_CANCEL_REQUESTED`
  - `_CANCELLED`
  - `_RETRY`

