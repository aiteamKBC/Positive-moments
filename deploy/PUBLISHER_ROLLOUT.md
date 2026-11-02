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
- **`RECORDING_SET_CHANGED`**: Teams listed another recording of the same occurrence after the lecture was located (for example a ~1 min false start located alone, then the 2 h lecture recording appears later). Since this fix, the re-check runs the full discovery again and selects the longest verified recording by itself, provided no Bunny video, Bunny job or operator part decision refers to the lecture's parts yet. Otherwise review stays. A lecture already left in this review by an earlier version stays there until someone takes one audited action:
  1. `recording-preview --lecture-id <id>` (read-only) should show `RESELECT_AND_IMPORT` (or `IMPORT_PENDING`) with the longest part selected.
  2. Then run `reselect-recording --lecture-id <id> --actor <name> --reason "<why>"`. It re-runs discovery, audits `OPERATOR_RECORDING_RESELECTED` and queues the selected part's import, even outside the lookback window.

  **A false start re-grouped out of the lecture** (2026-10-30 MSP: a 59 s recording, then the 2 h 22 min lecture on another Teams call 21 min 36 s later). The unchanged 20-minute grouping rule keeps only the lecture, so the verified false start no longer belongs to it. The worker *releases* it, and the preview lists it under `released_parts`. All of these must hold:
  - Graph still lists it;
  - it was verified as a FRAGMENT;
  - the new session is unambiguous with at most one full-length recording;
  - the lecture recording is fully located;
  - nothing in Bunny refers to the lecture's parts.

  The released part's evidence (Graph ids, times, file ids, duration) is kept in the `RECORDING_LOCATED` / `OPERATOR_RECORDING_RESELECTED` event and the discovery metadata.

  **A lecture MP4 named at the call start** (the same lecture: the file is named 11:57:26, 1,304 s before the recording's Graph start 12:19:09). The 0–120 s file-name rule can't see it. Teams writes a `source` facet on every recording driveItem: `externalId` = the Graph recording's `callId`, `threadId` = the meeting thread. A recording the file-name rule leaves with no file is proven by that facet only when all of these hold:
  - the call ID and the lecture's thread match exactly;
  - the call has exactly one Graph recording;
  - exactly one file names that call, and no file names it with another thread;
  - the file's subject and date match;
  - the measured duration passes the unchanged duration check.

  Otherwise nothing is matched. The discovery metadata records `files_proven_by_teams_source` or `teams_source_refused`.

  Still review:
  - a recording that vanished from Graph;
  - a non-fragment that left;
  - competing full recordings;
  - a file that can't be proven, or several candidate files;
  - existing Bunny state.

## 9. Historical Backfill (Publishing Monitor → Historical Backfill tab)

Publishes the lectures of a past, **inclusive** range of Cairo dates, including dates the publisher has **no records** for. Code: `app/publisher/backfill.py`.

**Preview.** Any signed-in user can ask for one. The publisher service discovers the range live, day by day:
- Teams `calendarView`, every page, through the daily `LectureDiscoveryService` with `persist=False` on a READ ONLY connection;
- a dry LMS match;
- the stored state of the lectures that already exist.

It writes **only its own** `bunny_backfill_runs` / `_items` rows: no lecture, job, discovery run or lock. A day that cannot be discovered (3 attempts) makes the preview **INCOMPLETE**, and an incomplete preview can't be run.

**Two sources every day (2026-11-02).** Each day also reads the QA database (`qa_doctors_sessions`, READ ONLY, never written), independently of Teams. Code: `app/publisher/historical_sources.py`.
- A QA record and a lecture are **one lecture** when they share the Teams meeting id on the same Cairo date. Several QA records for one lecture are duplicates. Several live lectures sharing a meeting that day are a **conflict**, never merged. A suppressed duplicate stays suppressed. Titles are never evidence.
- A day is **COMPLETE** only when both sources answered. One failing makes it **PARTIAL**, which is retried and never shown as empty. **Empty day** means both answered with nothing.
- A **QA-only** lecture (Teams no longer lists it) is shown with its blocker and is **never registered**: the LMS match needs the calendar occurrence's exact start, which QA lacks.
  - `QA_ONLY_NO_LMS_SESSION`: the LMS has no session of that meeting that day.
  - `QA_ONLY_SCHEDULE_UNKNOWN`: the LMS has one, and an operator decision is needed.
  - `QA_ONLY_LMS_UNCHECKED`, `QA_ONLY_NO_MEETING`, and `QA_CANCELLED` (skipped).
- Production, read-only on 2026-11-02:
  - 803 QA records (2026-02-02 to 2026-10-30). The 630 from February–August have no registry lecture.
  - Of the 615 not cancelled, 599 have **no LMS session** for their meeting on their date.
  - Sep 1–5: 29 unique lectures, with all 19 QA records matched.
  - Feb 2–8: 23 lectures, 22 of them QA-only.

**Transcripts never hold a video back.** A missing transcript is not a blocker: import, encoding and AUTO_RELEASE need only a verified recording, a VERIFIED LMS match and verified encoding. A transcript that arrives later is captioned onto the same video, with no re-upload and no re-release.

Past the 72 h deadline, which covers every historical lecture:
- Teams having nothing is the confirmed answer (`DEADLINE_PASSED:…`).
- A **transient** Graph or database failure is still retried with its backoff, for 4 attempts, before review. This applies to both the transcript and the recording worker, for daily and historical lectures alike.

**Run.** Operators only, for a COMPLETE preview less than 30 minutes old. Stale stored state is refused. The run then:
1. registers every day's Teams lectures exactly as the daily publisher does (`CalendarDiscovery.discover_day`), and adds lectures found now;
2. matches the range against the LMS;
3. takes each VERIFIED lecture through the publisher's own jobs: tracks, `longest_recording_v1`, import, encoding, captions and AUTO_RELEASE.

Each lecture resumes from its persisted stage, and nothing already in Bunny is uploaded again.

**Nothing starts by itself.** The daemon's `HISTORICAL_BACKFILL` task runs every minute, after the daily tasks, and is idle until a preview is requested or a run is confirmed.

**Daily publishing comes first.**
- Historical jobs run below every daily priority.
- At most `PUBLISHER_BACKFILL_MAX_IMPORTS` (default 1) historical imports are live at once, and none is added while a daily import is waiting.
- `PUBLISHER_BACKFILL_DISCOVERY_DAYS` (default 2) Teams days are discovered per tick.
- The daily schedule, lookback and start date are unchanged.

**Optional settings** in `publisher.env`, with defaults:

| Setting | Default |
|---|---|
| `PUBLISHER_BACKFILL_MAX_DAYS` | 31 |
| `PUBLISHER_BACKFILL_BATCH_SIZE` | 10 |
| `PUBLISHER_BACKFILL_MAX_ACTIVE` | 20 |
| `PUBLISHER_BACKFILL_MAX_IMPORTS` | 1 |
| `PUBLISHER_BACKFILL_DISCOVERY_DAYS` | 2 |
| `PUBLISHER_BACKFILL_INTERVAL_SECONDS` | 60 |

The web reads `PUBLISHER_BACKFILL_MAX_DAYS` (default 31).

**Access.** Every signed-in user of the Publishing page may preview, start, cancel and retry a backfill (the same IsAuthenticated policy as the rest of the page); there is no separate operator role. Unauthenticated requests get 401. Each start, cancel and retry is audited with the signed-in username.

**Schema 029:**
- check (read-only): `python -m app.cli.publisher --env-file backend/config/publisher.env backfill-schema`;
- install once: add `--apply`.

Additive. Its rollback refuses while runs exist.

**Cancel** stops pending lectures and the run's imports that have not reached Bunny (audited `OPERATOR_CANCEL`). Anything already submitted, encoding or captioning finishes normally.

**Audit events** in `bunny_publisher_events`:
- `HISTORICAL_BACKFILL_REQUESTED`
- `_STARTED`
- `_COMPLETED`
- `_CANCEL_REQUESTED`
- `_CANCELLED`
- `_RETRY`
