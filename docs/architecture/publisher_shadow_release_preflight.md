# Continuous Bunny Publisher: SHADOW release preflight (P0-E)

Prepared 2026-10-22. **This was a read-only preflight.** Nothing was deployed, migrated or committed. No Bunny calls or Graph writes were made, and no service was touched.

**How it was checked:**
- **Production Lecture Intelligence database:** read-only probes through `tools.audit_qa_core_production.connect_read_only`. That connector proves its session is read-only by having a write refused.
- **LMS:** read through the publisher's own `LmsOccurrenceSource`. It opens a `SET TRANSACTION READ ONLY` transaction, and PostgreSQL enforces it.
- **Daemon wiring:** checked by building the real SHADOW daemon from dummy configuration, without a network connection.
- **VPS:** not accessible from this machine.

**Labels:**
- **VERIFIED:** checked directly, either in code, against production read-only, or by running something.
- **ASSUMPTION:** inferred, not checked directly.
- **NOT ACCESSIBLE:** must be checked on the VPS.

## 1. Overall verdict

**NO-GO for activation as things stand. GO for SHADOW once gates G1–G5 below are closed.** None of the gates needs a code change.

The implementation meets every critical independence requirement (section 3). The blocker is how it would be deployed: production PostgreSQL is reached through Neon's **transaction pooler**, and the publisher relies on **session-level advisory locks**. Those locks are not safe through that pooler (G1).

| Gate | What must be true before SHADOW starts | Code change? |
|---|---|---|
| **G1 (critical)** | The publisher's `DATABASE_URL` uses Neon's **direct** (non-`-pooler`) endpoint | No (config) |
| G2 | P0-A through P0-D are committed, reviewed, pushed and checked out on the VPS | No |
| G3 | Migrations 024–028 are approved and applied after a Neon snapshot | No |
| G4 | Agreed values for `PUBLISHER_START_DATE`, `PUBLISHER_LOOKBACK_DAYS` and `PUBLISHER_LMS_TEST_PROGRAMMES` | No |
| G5 | The VPS checks in section 5 pass | No |

## 2. Critical blockers

### G1: the transaction pooler breaks session advisory locks. VERIFIED, CRITICAL

**Evidence (VERIFIED):**
- The production `DATABASE_URL` host is a Neon `-pooler` endpoint.
- 15 consecutive autocommit statements on one client connection were served by 2 different server backends. That is transaction-mode pooling.

**Where the publisher relies on session locks (VERIFIED in code):**
- **Periodic tasks:** `daemon.run_task` takes `pg_try_advisory_lock` and releases it with `pg_advisory_unlock`. Several autocommit transactions run between the two calls.
- **Per-lecture lock:** the transcript and recording workers take QA's `LectureLockManager` session lock and hold it across separate short transactions.

**What happens through the pooler:**
- A lock is taken on one backend and the unlock runs on another. The lock **leaks**, held by a pooled backend for as long as the pooler keeps that backend open.
- **For the publisher:** DISCOVERY or reconciliation is intermittently, or permanently, reported as `SKIPPED / ANOTHER_DAEMON_RUNS_IT`.
- **For QA:** a leaked per-lecture lock makes QA's lecture-scoped stages see `LOCKED` and skip that lecture. That would make the publisher **change QA behaviour**.

**Not affected:**
- `pg_advisory_xact_lock`, used for the import slots.
- The `QA_CYCLE_ACTIVE` check, which reads `pg_locks`.
- Prepared statements. The publisher connects with `prepare_threshold=None`, and the LMS source does the same.

**Resolution:**
- Give the publisher its own `DATABASE_URL` on the **direct** endpoint: the same host without `-pooler`, the same database and role.
- QA and web keep the pooled URL.
- Session locks taken on a direct connection are visible in `pg_locks` cluster-wide, so per-lecture locking between QA and the publisher keeps working.

**Recommended later hardening (not done here, because this preflight makes no code changes):**
- `validate-config` should refuse a `-pooler` host.

### Not blockers, but they change what to expect from SHADOW

These are covered in sections 3 and 9:
- The QA-cycle calendar yield.
- The 7 LMS-only sessions on 21 Oct.
- The lookback window needed to include 21 Oct.

## 3. Verified implementation and gaps

### Repository and consistency

- **All files listed in the P0-E spec exist (VERIFIED):**
  - `deploy/PUBLISHER_ROLLOUT.md`
  - `deploy/systemd/kbc-bunny-publisher.service`
  - `deploy/publisher.env.template`
  - `app/publisher/`, with these modules: config, runtime, daemon, planner, transcripts, recordings, recording_mapping, bunny_jobs, progress, reconcile, operations, pilot_migration, handlers, factory
  - `app/cli/publisher.py`
  - `app/db/migrations_proposed/024–028`, each with its rollback
- **Nothing is committed (VERIFIED)** on branch `release/positive-moments-media`.
  - P0-A through P0-D are untracked new files.
  - 4 tracked files are modified:
    - `app/graph/transport.py`: adds `invalidate_token`, which nothing calls implicitly.
    - `app/transcripts/selection_service.py`: **a refactor of QA code.** `select_day`'s per-lecture body is extracted into `select_lecture`, behaviour unchanged.
    - `backend/operations/urls.py`: publisher routes.
    - A media persistence test.
  - **Consequence:** deploying this branch changes code QA runs. QA's scheduler picks it up at its next restart, or after a reboot. The full suite, including QA's selection tests, passed twice (2,557 / 0).
- **Daemon wiring is correct for SHADOW (VERIFIED by building it).**
  - Job kinds: only `ACQUIRE_TRANSCRIPT` and `RESOLVE_RECORDING`.
  - Calendar: `CalendarDiscovery` → `LectureDiscoveryService`.
  - LMS: `LmsOccurrenceSource`.
  - The reconciler's Bunny client is `None`.
  - Building the daemon loaded no `app.qa`, `app.writer`, `openai`, `app.orchestration.factory` or `app.config.settings` module.
  - The Planner's window on 2026-10-22, with a start date of 2026-10-21, is 21–22 Oct.
- **Runbook defects (VERIFIED, documentation only).** Section 8 replaces these steps:
  1. The migration step sources `backend/config/publisher.env` **before** the step that creates it. The file is also mode 600 and owned by `www-data`, so the SSH user cannot source it.
  2. The pilot manifest (`output/bunny_pilot/manifest.json`) is git-ignored and exists only on this workstation. It must be copied to the VPS before promotion.
  3. There is no backup step.

### The eight independence requirements

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Discover a lecture absent from `lecture_sessions` from the Teams Calendar | **VERIFIED** | DISCOVERY (every 5 min) → `Planner.tick` → `CalendarDiscovery.discover_day(persist=True)`. This is the same `LectureDiscoveryService` QA's `orchestration/factory.py` assembles, with identical components, built without importing QA. Production today: `lecture_sessions` has **0 rows for 22 Oct**, while the LMS has 10 occurrences. QA only discovers at 21:00, so the publisher would add today's lectures within 5 minutes. |
| 2 | Persist lectures independently of nightly QA | **VERIFIED** | `LectureSessionRepository.upsert` and `lecture_discovery_runs` are written on the publisher's own connection. The QA cycle lock is never taken. |
| 3 | Transcripts without OpenAI, QA completion or n8n | **VERIFIED** | The P0-B `TranscriptWorker` reads Graph and writes to the transcript tables only. The import check proves no OpenAI or QA module loads. It **defers while a QA cycle is running** (by design; see below). |
| 4 | Recordings without a legacy QA row or transcript | **VERIFIED** | The P0-C `RecordingWorker` starts from `lecture_sessions` and never reads `qa_doctors_sessions`. Transcripts only corroborate a match when they exist, and are never required. |
| 5 | The Teams → LMS / Aptem rules are reused | **VERIFIED** | Eligibility uses Aptem active groups plus LMS module titles, through the same service. LMS matching is P0-A's v2. **Read-only dry run of production for 21 Oct:** 10 LI lectures; 7 VERIFIED, 1 TEST_EXCLUDED (with the recommended test list), 2 NO_LMS_SESSION. |
| 6 | No hidden QA-completion requirement before import | **VERIFIED** | The import gate is lecture exists, not cancelled or suppressed, no unresolved duplicate, LMS VERIFIED, recording LOCATED, and part policy INCLUDE. No QA table or status is read. `QaValidationRepository` is read only for a diagnostic comparison. |
| 7 | LMS-only occurrences visible in reconciliation | **VERIFIED** | `reconcile_coverage` gives every in-range occurrence one coverage row, and the reconciler raises alerts for LMS-only sessions. **Production evidence:** 7 active, non-test LMS occurrences on 21 Oct (08:00 UTC; 7 distinct Teams meetings across 3 programmes; weekly since 14 Oct, still `scheduled`) match **no** LI lecture. SHADOW will show them as LMS-only. |
| 8 | Recovery after missed discovery cycles | **VERIFIED, with a limit** | After more than 2 h without a successful DISCOVERY, the first tick re-discovers every day since then, bounded by `PUBLISHER_LOOKBACK_DAYS` (default 3, maximum 14) and the start date. A missed 05:00 FULL run starts immediately, and FULL re-discovers the whole lookback. Downtime longer than the lookback needs a temporary lookback increase. |

### QA gating: what exactly is skipped (VERIFIED in code)

- **While the QA cycle advisory lock is held** (from 21:00 and 23:00 Cairo for the length of the cycle):
  - Calendar discovery is skipped, recorded as `SKIPPED_QA_CYCLE_ACTIVE`.
  - Transcript writes defer.
- **What continues:** LMS matching, job planning, recording discovery and reconciliation.
- **When it catches up:** the next tick after QA releases the lock discovers today.
- **Cycle length (NOT ACCESSIBLE):** `lecture_pipeline_runs` records the same value for start and finish (21 Oct at 18:00:00Z and 20:00:00Z, because both are transaction timestamps). Read the length from the scheduler's journal. The 5-minute target does not hold during those windows; outside them it does.

### Smaller gap (VERIFIED in code)

- The DISCOVERY task is recorded as SUCCEEDED even when calendar discovery was skipped, or failed for one day.
- **Effect:** a day whose calendar read failed is recovered only by later "today" ticks, or by the 05:00 FULL run over the lookback. It is not recovered by the downtime catch-up.
- **Impact:** low, because FULL covers it daily. It is a candidate for hardening later.

### Not implemented, by design for this phase

- Alert delivery: alerts are rendered and recorded `DISABLED` or `NOT_SENT`.
- An operator UI. The API and CLI exist.
- The student playback gate.
- An LMS mapping table.
- LMS-only sessions are reported, never processed. The LMS side never creates an LI lecture.

## 4. Migration readiness

**Production state (VERIFIED):**
- **Server:** Neon PostgreSQL 17.11; 208 public tables; `max_connections` 901, with 22 connections in use.
- **Migrations 001–023:** all 51 tables are present.
- **Migrations 024–028:** none of the 46 tables, views and indexes they create exists, so there is no name collision.
- **No migration-tracking table for LI migrations.** Only `django_migrations` exists. LI migration state is the presence of the tables, which is the contract `tools/audit_qa_core_production.py` derives from `app/db/migrations/`.
- **The application role:** owns the LI tables; can create in `public`; is not a superuser; has CREATEROLE. **The publisher would use this same owner role.**
- **Every column the publisher reads is present:**
  - `lecture_sessions`: `lecture_id`, `session_date`, `scheduled_start`, `scheduled_end`, `subject`, `is_cancelled`, `metadata`, `join_url`, `meeting_id`;
  - `lecture_transcript_artifacts`: `artifact_id`, `content_version`;
  - `lecture_transcript_selections`: `selection_id`, `lecture_id`, `selection_version`, `selection_status`, `updated_at`.
- **Foreign-key targets have primary or unique keys:** `lecture_sessions(lecture_id)`, `lecture_transcript_artifacts(artifact_id)`, `lecture_transcript_selections(selection_id)`.
- **No advisory locks are currently held** in the LI (`KBL1`) or publisher (`KBPT`) namespaces.

**Order and dependencies (VERIFIED):**
1. 024: Bunny tables.
2. 025: LMS matching tables. Needs nothing from 024.
3. 026: queue.
4. 027: recording sources.
5. 028: runtime tables and the views. These depend on 024–027.

Each migration file is one `BEGIN … COMMIT` and is purely additive: no `ALTER` or `DROP` on existing objects, no extensions, functions or triggers.

**Locking (VERIFIED in SQL; the impact is an ASSUMPTION):**
- Creating the foreign keys briefly takes SHARE ROW EXCLUSIVE on `lecture_sessions` and other parent tables.
- The new tables are empty, so the lock lasts milliseconds.
- Do not run the migrations during a QA cycle (21:00–00:00 Cairo).

**Uniqueness and concurrency safeguards in the schema (VERIFIED, and tested in P0-A to P0-D):**
- one live job per subject;
- one owner per Graph recording and per SharePoint file;
- among live rows, one video per source, per part and per identity token;
- one current caption per (library, video, language);
- one open alert per dedupe key.

**Privileges:**
- The current owner role can apply and run everything.
- **Recommended, not required for SHADOW:** a dedicated `kbc_publisher` role with DML on the LI and publisher tables and no DDL. Creating it is a production write and needs separate approval. **ASSUMPTION:** the team accepts the owner role for the shadow period.

**Pilot manifest promotion (VERIFIED):**
- The pilot is in library **786045**.
- Its lecture exists in production, with `session_date` 2026-10-16.
- Its caption's source transcript artifact exists in production.
- The video is IDENTIFIED / VERIFIED, with 1 caption.
- 16 Oct is **before** the recommended start date, so the publisher would never plan that lecture. Promotion only matters before any backfill reaches 16 Oct, so it is **optional for SHADOW**. `pilot-manifest` plan, then `--apply`, is idempotent and refuses on conflict.

**Migration checklist.** Every step requires approval.
- [ ] A Neon branch snapshot or PITR point recorded, labelled `pre-publisher-024-028`.
- [ ] Confirm, read-only, that none of the 46 objects exists. This preflight found none.
- [ ] Apply over the **direct** endpoint, outside 21:00–00:00 Cairo, in order 024 → 028, with `psql -v ON_ERROR_STOP=1 --single-transaction` per file.
- [ ] Afterwards, confirm read-only that 15 tables and 5 views exist and every table is empty.
- [ ] Keep the files in `migrations_proposed/` for this release. Moving them into `app/db/migrations/` changes QA's audit contract and the test bootstrap, so it is a separate code change.

## 5. VPS and environment readiness

**NOT ACCESSIBLE.** I cannot SSH to `srv1281908`. Last known state, from an operator paste on 2026-10-15 (ASSUMPTION that it still holds):
- the checkout is `/var/www/Positive-moments`, with a venv at `backend/venv`;
- the units are `positive-moments` and `positive-moments-media` (EnvironmentFile `backend/config/.env`) and `positive-moments-scheduler` and `positive-moments-backfill` (EnvironmentFile `backend/qa-core.runtime.env`);
- they run as `www-data`;
- the scheduler cron is `0 21,23 * * *` Africa/Cairo, with a 3-day lookback.

**Partly VERIFIED from production:** QA cycles ran at 21:00 and 23:00 Cairo every day from 15 to 21 Oct, all COMPLETED.

**Checks to run on the VPS.** All are read-only; paste the output back.

```bash
cd /var/www/Positive-moments && git status -sb && git log -1 --oneline
backend/venv/bin/python -V && backend/venv/bin/python -c "import psycopg, dotenv; print(psycopg.__version__)"
which psql && psql --version
systemctl list-units 'positive-moments*' 'kbc-*' --all --no-pager
systemctl cat positive-moments-scheduler.service positive-moments-backfill.service positive-moments-media.service | grep -E 'User|EnvironmentFile|ExecStart|Restart'
systemctl is-active positive-moments-scheduler positive-moments-backfill positive-moments-media
ls -l backend/config/ backend/qa-core.runtime.env          # permissions/owners only
sudo -u www-data test -r /var/www/Positive-moments/app/publisher/daemon.py && echo readable   # after checkout
journalctl -u positive-moments-scheduler --since "2026-10-21 20:55" --until "2026-10-22 01:00" --no-pager | grep -iE 'cycle (start|finish|complete)|lock' | head   # QA cycle length
df -h /var/log && journalctl --disk-usage
# outbound (status codes only, no secrets):
curl -s -o /dev/null -w '%{http_code}\n' https://graph.microsoft.com/v1.0/$metadata
curl -s -o /dev/null -w '%{http_code}\n' https://video.bunnycdn.com/   # SHADOW makes no Bunny calls; needed later
getent hosts <direct-neon-host>    # G1: the direct endpoint resolves from the VPS
```

**What the publisher unit needs (VERIFIED from the unit file):**
- `User=www-data`; `EnvironmentFile=/var/www/Positive-moments/backend/config/publisher.env`;
- `ExecStartPre` runs `validate-config` and exits 2 when the configuration is invalid;
- `Restart=on-failure`, `RestartSec=30`, `RestartPreventExitStatus=2`;
- `TimeoutStopSec=180`, `MemoryMax=512M`; logs go to journald;
- it is independent of every QA unit (no `After=` or `Requires=` on them);
- **outbound connections:** Neon PostgreSQL (direct endpoint), the LMS and Aptem PostgreSQL databases, and `graph.microsoft.com` / `login.microsoftonline.com`. Bunny is not contacted in SHADOW.

**ASSUMPTION:** a failing `ExecStartPre` restarts every 30 s under `Restart=on-failure`. Check the journal on the first start.

**Load and cost (ASSUMPTION, estimated from code):**
- **Database connections:** each worker and the scheduler open short connections (a claim or heartbeat every 5 s). That is roughly 1 connection per second against the direct endpoint.
- **Discovery runs:** DISCOVERY adds about 288 `lecture_discovery_runs` rows a day.
- **Neon compute:** the polling keeps the compute permanently awake. That only matters if Neon autosuspend is currently saving cost.

## 6. Table-write inventory for SHADOW (VERIFIED in code)

SHADOW writes only to the Lecture Intelligence database. **The LMS and Aptem databases are read-only**: the LMS through `SET TRANSACTION READ ONLY`, and Aptem through SELECTs on a per-day connection. All Graph calls are reads: calendar, online meetings, transcripts, call recordings and drive search. `createLink` is never built.

| Table | Exists today? | Written by | Notes |
|---|---|---|---|
| `lecture_sessions` | yes, shared with QA | calendar discovery (upsert) | The same rows and identity QA's discovery writes. **QA consumes what the publisher discovered.** |
| `lecture_discovery_runs` | yes, shared with QA | one row per discovered day per tick | Labelled `mode = 'SHADOW'`, the label QA's persisting discovery also uses |
| `lecture_transcript_artifacts`, `lecture_transcript_candidates`, `lecture_transcript_artifact_contents` | yes, shared with QA | transcript worker | Content is append-only and versioned; QA reuses stored content |
| `lecture_transcript_selections`, `lecture_transcript_selection_parts`, `lecture_combined_transcripts` | yes, shared with QA | transcript worker, through QA's own `select_lecture` | The same selection logic QA runs |
| `bunny_lms_match_evaluations`, `bunny_lms_occurrence_links`, `bunny_lms_occurrence_coverage` | no (025) | LMS matching and coverage | |
| `bunny_publisher_jobs`, `bunny_publisher_events`, `bunny_publisher_heartbeats` | no (026) | queue, audit, heartbeat | |
| `bunny_recording_discoveries`, `bunny_recording_sources` | no (027) | recording worker | |
| `bunny_publisher_schedule`, `bunny_publisher_reconciliation_runs`, `bunny_publisher_alerts` | no (028) | scheduler and reconciler | |
| `bunny_publisher_part_decisions` | no (028) | **only** on an operator `decide-part` | |

**Not written in SHADOW:**
- `bunny_videos`, `bunny_caption_publications`, `bunny_caption_current` (024). The exception is the separately approved pilot promotion.
- Any `qa_*` table; `lecture_recording_parts` and links (017–022); Positive Moments and social tables; Django tables.

**What else SHADOW does not do (VERIFIED):**
- It makes **no Bunny call of any kind**: no client is built, the reconciler's library scan is skipped, and no import, delete, retitle or caption happens.
- It exposes nothing to students.
- It never takes QA's cycle lock and does not change QA's schedule.

**How to read "shadow":** it means no Bunny writes. It is **not** free of side effects on QA's shared tables: lectures and transcripts the publisher stores are the ones QA then uses. That is the P0-B and P0-C design, and it is why acceptance includes comparing QA's output (section 9).

## 7. Recommended start date and test exclusions

**`PUBLISHER_START_DATE=2026-10-21`:**
- **Format:** `YYYY-MM-DD`. Any other format is refused at startup.
- **Meaning:** an **Africa/Cairo calendar date**, compared with `lecture_sessions.session_date`, which is the Cairo business date.
- **Effect:** lectures with `session_date` before it are never discovered, planned or marked failed by the publisher. They remain backfill scope, and are never treated as legacy by inference.
- **Scope today (VERIFIED):** 10 LI lectures on or after 21 Oct. All have a meeting id; none is cancelled or suppressed.

**`PUBLISHER_LOOKBACK_DAYS` must cover 21 Oct at activation (VERIFIED from the window rule):**
- The rule is `max(cairo_today − lookback, start_date)`.
- With the default of 3, 21 Oct leaves the window from **25 Oct** onwards.
- **Recommendation:** set `PUBLISHER_LOOKBACK_DAYS = (activation date − 21 Oct) + 1`, at most 14. After 21 Oct has been validated, return it to 3.
- Jobs already created keep running whatever the window.

**Historical archive (VERIFIED):** the planner and reconciler both filter on `session_date >= start_date` and on the lookback window. `lecture_sessions` holds 168 rows from 1 Sep, and 158 of them are outside scope.

**`PUBLISHER_LMS_TEST_PROGRAMMES="Final Test;Test Lms;Test"`:**
- **Evidence (VERIFIED):** exact names from the LMS gap investigation; with this list, one of the 21 Oct lectures becomes TEST_EXCLUDED.
- **Series:** `feedback-test`, superseded and cancelled series are already excluded by the series-status rule.
- **Still open (ASSUMPTION):** confirmation from the LMS team. On 22 Oct, 5 of the 10 LMS occurrences belong to test programmes.

**Cancelled and suppressed lectures:** `is_cancelled` and `metadata.duplicate_suppression` exclude lectures in every planner and reconciler query (VERIFIED).

**Leave unchanged:**
- `RECORDING_LINK_SEARCH_REGION`: blank, the current platform default. **ASSUMPTION:** QA's `backend/config/.env` value should be copied.
- `PUBLISHER_DISCOVER_CALENDAR=true`.
- `PUBLISHER_WORKERS=2`.

## 8. Deployment steps that need approval

Not executed. The operator runs them on the VPS. Use `--no-block` for every `systemctl` restart.

**A. Backup and validation (approval A).**
1. Create a Neon branch or record a PITR point for the LI database. Name it `pre-publisher-024-028`.
2. Rerun the read-only object check from section 4.

**B. Code (approval B; QA services are not restarted).**
3. Locally: review the diff, then commit P0-A through P0-D on a release branch. The user pushes.
4. On the VPS:

   ```bash
   cd /var/www/Positive-moments && git fetch && git checkout <approved-branch> && git pull
   ```

   This changes code QA loads at its next restart (see section 3); no QA unit is restarted now.

**C. Migrations (approval C; production DDL; outside 21:00–00:00 Cairo).**

```bash
cd /var/www/Positive-moments
read -rs PUB_DB_URL   # paste the DIRECT (non -pooler) URL; not echoed, not stored in history
for f in 024_create_bunny_stream_tables 025_create_lms_matching_tables 026_create_publisher_queue_tables \
         027_create_recording_source_tables 028_create_publisher_runtime_tables; do
  echo "== $f"; psql "$PUB_DB_URL" -v ON_ERROR_STOP=1 --single-transaction -f "app/db/migrations_proposed/$f.sql" || break
done
unset PUB_DB_URL
```

**D. Configuration (approval D).**

```bash
sudo install -m 600 -o www-data -g www-data deploy/publisher.env.template backend/config/publisher.env
sudoedit backend/config/publisher.env
#   PUBLISHER_MODE=SHADOW
#   PUBLISHER_START_DATE=2026-10-21
#   PUBLISHER_LOOKBACK_DAYS=<covers 21 Oct>
#   PUBLISHER_LMS_TEST_PROGRAMMES=Final Test;Test Lms;Test
#   DATABASE_URL=<DIRECT endpoint>   (G1)
#   APTEM_DATABASE_URL, LMS_DATABASE_URL, MICROSOFT_GRAPH_*, KBC_LECTURE_CALENDAR_USER_UPN:
#     copy the values QA uses
#   leave every BUNNY_* blank; PUBLISHER_PRODUCTION_WRITE_ENABLED=false
```

**E. Startup checks.**

```bash
sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env validate-config
```

It must print `mode: SHADOW`, `writes_bunny: false`, `library_id: null`, and no problems.

**F. SHADOW activation (approval F).**

```bash
sudo cp deploy/systemd/kbc-bunny-publisher.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now --no-block kbc-bunny-publisher.service
systemctl is-active kbc-bunny-publisher.service; journalctl -u kbc-bunny-publisher -n 100 --no-pager
P="sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env"
$P status
```

**G. Verify against 21 Oct (read-only).**
- `$P status`, `$P exceptions` and `$P lecture <id>` for each of the 10 lectures.
- The checks in section 9.

**H. QA coexistence.**
- After the next 21:00 and 23:00 cycles, compare QA's run counts and statuses with the previous nights.
- `journalctl -u positive-moments-scheduler` must show no LOCKED or skipped lectures that are new.

**I. Monitoring.**
- `$P status` (worker heartbeat, schedule) every few hours on day 1, then daily.
- `$P exceptions` daily after 05:00 Cairo.
- `journalctl -u kbc-bunny-publisher -p warning`.

**Optional (approval):** pilot promotion. Copy `output/bunny_pilot/manifest.json` to the VPS (`scp`), then run `$P pilot-manifest --manifest <path> --library-id 786045` to plan, and add `--apply --actor <name>` to apply. Library `785080` is refused in code.

**Deferred:** restarting the web service (`positive-moments.service`) to expose the publisher API. The CLI covers SHADOW without it.

## 9. Rollback

None of these steps deletes live state.

| Situation | Action | Effect |
|---|---|---|
| Any concern | `sudo systemctl disable --now --no-block kbc-bunny-publisher.service` | Stops discovery and workers. Leased jobs are reclaimed on the next start. The data stays as evidence. |
| Remove the unit | `sudo rm /etc/systemd/system/kbc-bunny-publisher.service && sudo systemctl daemon-reload` | |
| Code | `git checkout <previous commit>` on the VPS. Restart QA only if the shared-code change is suspected. | QA reverts to the old `selection_service` at its next restart. |
| Schema | **Leave the tables in place.** | The 028 → 024 rollbacks refuse while rows exist, by design. Dropping tables holding live state is not recommended. If it is ever needed, use the Neon branch from step A. |

**Lectures and transcripts the publisher wrote to shared tables are valid.** QA would have written the same rows at 21:00. They are not rolled back.

## 10. Final acceptance checklist (SHADOW)

All checks are read-only. Start them at least 24 h after activation, covering one 05:00 FULL run and one pair of QA cycles.

- [ ] **Coverage:**
  - every in-scope LI lecture on 21 Oct and later has exactly one LMS status;
  - every in-range LMS occurrence has exactly one coverage row;
  - expected for 21 Oct: 7 VERIFIED, 1 TEST_EXCLUDED, 2 NO_LMS_SESSION;
  - the **7 LMS-only 08:00 UTC sessions** are visible as LMS-only, and their cause has been investigated with the LMS team (e.g. organiser is not the calendar user).
- [ ] **Correct occurrence ids:** each VERIFIED lecture's `lms_occurrence_id` matches the LMS occurrence, checked by hand for all 21 Oct lectures.
- [ ] **Exclusions:** no cancelled, duplicate-suppressed or test-programme lecture has a `bunny_publisher_jobs` row.
- [ ] **Independence:**
  - recording status reaches LOCATED for lectures with no transcript, if any;
  - transcript selections exist for lectures QA has not processed yet (e.g. a lecture from 22 Oct before 21:00).
- [ ] **No Bunny calls:**
  - no `IMPORT_VIDEO`, `POLL_ENCODING` or `PUBLISH_CAPTION` jobs;
  - `bunny_videos` is empty, or holds only the promoted pilot;
  - the journal mentions no Bunny host.
- [ ] **No duplicate jobs:** at most one live job per `(job_kind, subject_key)`. The database enforces it, and `$P status` shows the counts.
- [ ] **Nothing dropped, stuck or untracked:**
  - `bunny_publisher_exceptions_v` has been reviewed;
  - no RUNNING job is older than its lease;
  - each ended in-scope lecture has both track jobs.
- [ ] **Heartbeat:** `workers_healthy: true` (`$P status`), and every worker's age is under 5 minutes.
- [ ] **Reconciliation:**
  - LIGHT runs every 6 h and FULL ran at 05:00 Cairo, all SUCCEEDED in `bunny_publisher_reconciliation_runs`;
  - alerts are deduplicated, with delivery `NOT_SENT` or `DISABLED`.
- [ ] **QA unchanged:**
  - cycles ran at 21:00 and 23:00 Cairo and COMPLETED;
  - lectures seen and statuses are consistent with the nights before activation;
  - the scheduler journal shows no new LOCKED skips.
- [ ] **Locks are healthy (G1):** no `KBPT` or `KBL1` advisory lock is held for longer than one job, and no task has a `SKIPPED / ANOTHER_DAEMON_RUNS_IT` streak.
- [ ] **Restart resilience:**
  - after `systemctl restart --no-block kbc-bunny-publisher`, pending jobs resume;
  - no job is lost or duplicated;
  - the schedule continues from `bunny_publisher_schedule`.

**Waiting for explicit approval** before any production migration, configuration change or deployment.
