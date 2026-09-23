# QA Core RC4 — Integration Validation on an Isolated Database

**Date:** 2026-09-22
**Scope:** Four things, in order:

1. Make `tests/integration` unable to reach production.
2. Build a non-production PostgreSQL database from the repository's migrations.
3. Run the whole integration suite there.
4. Prove the F-01/F-02/F-03 safety fixes at the repository, database and orchestration level.

**Follows:** `QA_CORE_SAFETY_FIX_VALIDATION_2026-09-22.md`, where the safety fixes were held back from release because the integration suite had not run.

**Constraints honoured:**

- No production connection was opened in this phase.
- No production data was modified.
- No Graph, OpenAI, n8n or SharePoint call was made.
- The scheduler was not enabled.
- No repair was done.
- No commit, tag or deploy was made.

---

## Summary

| Question | Answer |
|---|---|
| Can the integration suite still reach production? | **No.** The gate has six independent checks, and psycopg is guarded for the whole session. A test run with `TEST_DATABASE_URL` set to the real production value aborted before connecting. |
| Test database | A local Docker container on `postgres:16-alpine` (16.15), at `127.0.0.1:55432/kbc_qa_integration_test`. It holds only synthetic data and no secrets, and it is bound to loopback. |
| Migrations | 25 of 25 SQL files applied cleanly on a fresh database: app 001–020, 4 automation migrations and the test-only legacy baseline. The result is 50 tables, 142 indexes and 286 constraints. |
| F-01/F-02/F-03 at integration level | **36 of 36 new integration tests pass:** 29 safety-fix tests and 7 live gate tests. They run the real writer, guard, resolver, SQL, constraints and savepoints. |
| Full integration suite | **395 collected: 93 passed, 149 failed, 153 skipped.** Every one of the 149 failures and 153 skips depends on production evidence that an isolated database cannot contain. None is a code defect (see §5). |
| Offline suite | **1453 of 1453 passed.** |
| RC4 | **Not ready.** The rule for this phase was "if and only if the full suite passes", and it does not. What would change that is in §10. |

---

## 1. Original production-test risk

Every integration module opened its database the same way:

```python
settings = Settings.from_environment()          # load_dotenv(backend/.env, override=False)
if not settings.database_url: pytest.skip(...)
return psycopg.connect(settings.database_url)    # -> production DATABASE_URL
```

There were three process hazards, and all of them were real on this machine:

1. **`backend/.env` holds the production `DATABASE_URL`**, the managed Neon Postgres in eu-west-2. Nothing stopped the integration suite from using it, and it did: the RC3 commit message records "1680 passed (unit + integration)", and `QA_CORE_RELEASE_READINESS.md` described the suite as "each opening its own connection to managed Postgres in eu-west-2".
2. **`pytest.ini` put `tests/integration` in the default `testpaths`.** A bare `pytest`, typed to run unit tests, also ran rolled-back DML against production.
3. **"Rolled back" is not "untouched".** Row locks on live tables, sequence advances, a writer savepoint path with a mistake in it, or one test that forgets its rollback are all real effects on a production database. Several modules also *commit by design* inside savepoints before rolling back the outer transaction.

## 2. Safety gate implemented

**`tools/integration_db_guard.py`** holds the gate. A database is accepted only when **all** of the following checks pass; any single failure refuses it.

| # | Signal | How it is enforced |
|---|---|---|
| 1 | Dedicated `TEST_DATABASE_URL` | It must be set explicitly. There is **no fallback** to `DATABASE_URL`, ever. |
| 2 | `APP_ENV=test` | It must be exactly `test`. `testing`, `dev`, `production` and empty are refused. |
| 3 | Test marker in the database name | `test` must be a whole token: `kbc_qa_integration_test` passes; `latest`, `contest`, `attestation` and `AiTeamKBC` are refused. |
| 4 | Loopback host | A remote host needs an explicit `KBC_TEST_DB_ALLOW_REMOTE_HOST=1`, and still has to pass every other check. |
| 5 | Never production | The value is refused if it is identical to, or on the **same host:port server** as, any `DATABASE_URL` or `APTEM_DATABASE_URL` in the environment, `backend/.env`, `backend/qa-core.runtime.env` or `deploy/qa-core.env`. Production is identified at run time; no production credential or host appears in source. |
| 6 | Live marker | Once connected, `current_database()` must equal the named database, and it must hold a `public.kbc_test_database_marker` row with purpose `kbc-qa-integration-test`. Only `tools/bootstrap_integration_db.py` writes that row, and that tool applies checks 1–5 before it writes anything. |

**`tests/conftest.py`** is new and applies to every pytest session, unit runs included.

- **Environment scrub:** before any module can connect, every secret or endpoint variable is blanked, including those present only in `backend/.env`. The pattern matches `*DATABASE_URL`, `*_URL`, `*_KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `TENANT`, `CLIENT_ID`, `UPN`, `WEBHOOK` and `DSN`. Because `load_dotenv(override=False)` then finds them already set, it cannot reload production values. `DATABASE_URL` is then set to the approved test URL and nothing else.
- **`ConnectGuard`:** `psycopg.Connection.connect` and `psycopg.connect` are wrapped for the whole session.
  - A connection to anything other than the approved host:port/dbname raises `UnsafeTestDatabase` before libpq is called.
  - Every accepted connection is re-checked live against the marker.
  - With no approved target, every connection is refused.
- **`NetworkGuard`:** every non-loopback `socket.connect`, `connect_ex`, `create_connection` and `getaddrinfo` raises. An accidental Graph, OpenAI, n8n or SharePoint call therefore fails the test instead of leaving the machine.
- **Configuration outcomes:**
  - No `TEST_DATABASE_URL`: integration tests are **skipped** with the reason "TEST_DATABASE_URL is not set. The integration suite never falls back to DATABASE_URL."
  - `TEST_DATABASE_URL` set but failing any check: the whole session **aborts** (`pytest.UsageError`). A misconfigured target is never downgraded to a skip.
- **Evidence:** the header and terminal summary report the approved target (host:port/dbname only), the number of connections opened and refused, and the number of external network attempts blocked.

**Other changes:**

- `pytest.ini` gains `pythonpath = .`, so the gate always imports however pytest is launched.
- URLs are never logged. `Target.describe()` prints host:port/dbname only, and refusal messages are tested to contain no user, password, production host or URL.

**Tests proving the gate fails closed:**

| Where | Tests | What they prove |
|---|---|---|
| `tests/unit/test_integration_db_guard.py` (offline) | 36 | Each of signals 1–6 refuses on its own. Nothing leaks in refusals. The connect guard refuses non-approved targets without calling libpq. The network guard blocks `graph.microsoft.com`, `api.openai.com`, `*.sharepoint.com` and an n8n host. The scrub stops `load_dotenv` from refilling production values. |
| `tests/integration/test_database_safety_gate.py` (live) | 7 | Settings resolve only to the test database, and secrets are blank. The marker is present. A sibling database on the same test server is refused. **The real production URL from `backend/.env` is refused in-process.** `tools/bootstrap_integration_db` refuses the real production URL before connecting, refuses to rebuild the marked database without `--reset`, and refuses an unmarked database that already holds tables. |

**Demonstrations against the real hazard:**

| Run | Result |
|---|---|
| `pytest tests/integration` with no test configuration | 359/359 integration tests **skipped** with the no-fallback reason; 0 connections opened. |
| `pytest tests/integration` with `TEST_DATABASE_URL` set to the **real production value** from `backend/.env` and `APP_ENV=test` | **Exit 4, before any connection:** `integration database REFUSED: database name 'AiTeamKBC' carries no 'test' marker`. The URL was verified absent from all output. Had the name carried a marker, checks 4 and 5 would still have refused it: the host is not loopback and it is the production server. |

## 3. Test database architecture

| | |
|---|---|
| Choice | Option 3 of the brief. No local PostgreSQL or repository Postgres infrastructure existed: `deploy/docker-compose.qa-core.yml` runs only app services against an external database. Docker Desktop was installed but stopped, so it was started **locally**. The VPS was not touched. |
| Container | `kbc-qa-integration-test`, image `postgres:16-alpine` (server 16.15), labelled `purpose=kbc-qa-integration-test` |
| Binding | `127.0.0.1:55432 → 5432`: loopback only, on a non-standard port, so it can never be mistaken for a production server |
| Database / role | `kbc_qa_integration_test` / `kbc_test`. The password is a throwaway random value generated for this container, kept in the session scratch directory and never printed or committed. |
| Contents | Schema from migrations plus the marker row. Test data is synthetic and created per test inside a transaction that is rolled back. **No production data was copied, sampled or anonymised into it.** |
| Connection used | `postgresql://kbc_test:<redacted>@127.0.0.1:55432/kbc_qa_integration_test` |
| Assumption | Production's PostgreSQL major version is not recorded anywhere in the repository, and connecting to production to read it was out of bounds. PostgreSQL 16 was chosen, and matching it to production is listed in §10. |

The container was left running so the suite can be re-run. To remove it: `docker rm -f kbc-qa-integration-test`.

## 4. Migrations applied

`tools/bootstrap_integration_db.py` is new. It builds the database in a fixed order and refuses any target the gate would refuse.

| Step | Files | Result |
|---|---|---|
| Test-only legacy baseline | `tests/integration/fixtures/legacy_baseline.sql` | APPLIED |
| Automation migrations that alter legacy tables | `050_add_queue_priority`, `051_add_clips_media_origin`, `100_create_lecture_split_tables`, `101_add_recording_duration_seconds` | 4/4 APPLIED |
| Application migrations | `001` … `020_create_backfill_tables` | **20/20 APPLIED** |
| Marker | `kbc_test_database_marker` | written |

- **Migration count:** 20 application migrations, 001 through 020. The safety-fix branch added **no** new migration, so there was none further to verify. Every F-01/F-02/F-03 change is code-only.
- **Schema objects (public):** 50 tables, 0 views, 142 indexes, 286 constraints, 0 triggers, 0 functions.
- **Fresh-build repeatability:** a second scratch database (`kbc_migration_check_test`) was built from nothing and then dropped. It produced identical object counts.
- **Re-application:** every file was then applied a second time to that same scratch database.
  - 23 of 25 re-apply cleanly.
  - **`003` and `004` are one-shot** (they rename or move columns), so a second run fails inside its own transaction and changes nothing. Object counts after the attempt were identical.
  - This is not a safety-release issue, because nothing re-runs applied migrations, but it is recorded in §10.

**Why a legacy baseline was needed.** Migrations 001–020 never create the n8n-owned legacy tables, because production already has them. The platform reads and writes six of them:

- `qa_doctors_sessions`
- `qa_doctors_checklist_items`
- `qa_perfect_lectures`
- `qa_doctors_transcripts`
- `qa_media_jobs`
- `qa_positive_clip_assets`

A seventh, `qa_lecture_split_plans`, is created by automation migration 100. The baseline is derived from the platform's own column contracts (`SESSION_COLUMNS`, `CHECKLIST_COLUMNS`, `PERFECT_COLUMNS`, repository SQL) and from the automation migrations that alter these tables. It is **not** the authoritative production DDL. Its fidelity was corrected once during this phase: `qa_perfect_lectures` now reproduces production's exact column order, which `test_perfect_lecture_persistence` asserts.

## 5. Integration suite results

**Command:**

```bash
APP_ENV=test TEST_DATABASE_URL=… python -m pytest tests/integration
```

| | Count |
|---|---|
| Collected | **395** (the 359 pre-existing tests plus 29 in `test_safety_fixes_integration.py` and 7 in `test_database_safety_gate.py`) |
| Passed | **93** |
| Failed | **149** |
| Errors | 0 |
| Skipped | **153** |
| Duration | 25.9 s |

**Why 359 and not 362.** "362" was quoted in `QA_CORE_RELEASE_READINESS.md` from an earlier state. The 20 pre-existing integration files are **byte-identical to `f85932e`**, and their parametrisation is static, so their collection is deterministically 359.

**Every failure and skip is missing production evidence, not a defect.** The pre-existing modules are production acceptance tests: they assert on real lectures from 2026-09-04 and 2026-09-16/17, such as named lecture UUIDs, "Andrew-Scheduling", "GN-Juliane" and "Risk Management", and on real row counts.

Each of the 149 failures was triaged:

| Cause | Tests |
|---|---|
| Reads a hard-coded production lecture UUID or a real 2026 date (checked by parsing each test and the helpers it calls) | 105 |
| Parametrised on a production lecture id or name | 29 |
| Reads the external Aptem table `public.kbc_attendance`, which is not part of this schema | 9 |
| Asserts that a production table is non-empty, or reads the Andrew canary row; each was read by hand | 6 |
| **Unexplained** | **0** |

The 153 skips are the suite's own "evidence not present" guards:

| Skip reason | Tests |
|---|---|
| Phase 2C4 v2 engagement evidence is not present | 24 |
| no renderable Phase 3A evaluations | 22 |
| no Phase 3B rendered payload present | 17 |
| the multi-part fixture is not present in this database | 15 |
| speaker inventory is not present | 15 |
| Phase 2C3 evidence is not present | 14 |
| no Phase 2B combined transcripts persisted | 13 |
| no canonical cues persisted | 12 |
| no Phase 2A artifacts persisted | 7 |
| 11 more reasons of the same kind (for example "no rendered session for 9671a3f1…") | 14 |

**Per module:**

| Module | Pass | Fail | Skip |
|---|---:|---:|---:|
| test_attendance_recovery | 2 | 17 | 0 |
| test_attendance_resolution_persistence | 10 | 7 | 0 |
| test_automated_production_sync | 6 | 30 | 0 |
| test_canonical_transcript_persistence | 1 | 0 | 13 |
| **test_database_safety_gate** (new) | **7** | 0 | 0 |
| test_duplicate_resolution_persistence | 4 | 13 | 0 |
| test_engagement_persistence | 0 | 0 | 14 |
| test_lecture_scoped_engagement | 2 | 5 | 1 |
| test_legacy_writer_persistence | 2 | 0 | 18 |
| test_makeup_roster_drift | 0 | 0 | 15 |
| test_media_timeline_real_lectures | 0 | 11 | 0 |
| test_orchestration_persistence | 10 | 17 | 0 |
| test_perfect_lecture_persistence | 5 | 28 | 0 |
| test_qa_rendering_persistence | 1 | 0 | 22 |
| test_registry_repository | 4 | 0 | 0 |
| **test_safety_fixes_integration** (new) | **29** | 0 | 0 |
| test_seam_dedup_persistence | 3 | 0 | 17 |
| test_shadow_qa_persistence | 1 | 0 | 24 |
| test_speaker_inventory_persistence | 0 | 0 | 12 |
| test_transcript_artifact_repository | 3 | 0 | 4 |
| test_transcript_selection_persistence | 1 | 0 | 8 |
| test_unattended_readiness | 2 | 21 | 5 |

**What this means:**

- The 57 pre-existing tests that pass are the ones that exercise schema, constraints and refusals without needing real lectures.
- The 302 that fail or skip **cannot pass on any isolated database** without either production data or a large synthetic re-creation of specific real lectures.
- **None of them has been run against the post-fix code on production-shaped data.** They could only be run that way against production, which is forbidden, so this is a gap in coverage, not a pass. See §10.

## 6. Full suite results

| # | Suite | Command scope | Collected | Passed | Failed | Skipped | Duration |
|---|---|---|---:|---:|---:|---:|---:|
| 1 | Offline / unit (no database configured) | `tests/unit` + `automation/lecture_parts/test_graph_client.py` | 1453 | **1453** | 0 | 0 | 4.2 s |
| 2 | Full integration | `tests/integration` | 395 | 93 | **149** | **153** | 25.9 s |
| 3 | Scheduler / orchestration | unit: `test_scheduler_runtime`, `test_orchestration`, `test_pipeline_state`, `test_automated_sync`, `test_backfill`, `test_backfill_legacy_compatibility`; integration: `test_orchestration_persistence`, `test_automated_production_sync`, `test_unattended_readiness` | 352 | 279 | 68 | 5 | 6.7 s |
| 4 | Migrations | `test_migration_transaction_safety` (10) + bootstrap build (25/25 files) + fresh-build repeat + re-apply check (23/25 re-runnable; 003 and 004 one-shot) | 10 | **10** | 0 | 0 | 0.1 s |
| 5 | Safety-specific | `test_safety_fixes_f01_f02_f03`, `test_integration_db_guard`, `test_production_auditor_is_read_only`, `test_backfill_legacy_compatibility`, `test_safety_fixes_integration`, `test_database_safety_gate` | 194 | **194** | 0 | 0 | 8.2 s |
| 6 | Everything, default `testpaths`, one session | `pytest` | 1848 | 1546 | 149 | 153 | 30.3 s |

- **Row 1:** 1453 = the 1417 from the safety-fix phase plus the 36 new gate tests. Opened 0 database connections.
- **Row 3:** all 261 scheduler and orchestration **unit** tests pass. The 68 failures are all in the three production-evidence integration modules named in that row: 30 + 17 + 21.
- **Row 6:** the 149 failures are the same 149 as row 2.

## 7. F-01 / F-02 / F-03 integration evidence

All cases live in `tests/integration/test_safety_fixes_integration.py` and share the same setup:

- **Seeding:** each test seeds a complete synthetic chain with real constraint-checked INSERTs: lecture, transcript artifact(s) and candidates, selection and parts, evaluation, rendered session and 11 rendered checklist items. The QA tests also get the combined transcript, document, cues, speaker, trainer role, attendance snapshot and engagement.
- **Isolation:** every test runs in one transaction that is rolled back.
- **Nothing stubbed between code and PostgreSQL:** the real `LegacyQaWriter`, `LegacyOccurrenceGuard`, `PipelineStateResolver` (`build_resolver`), repositories, savepoints and SQL run against real tables.
- **The one stub:** the QA model provider, because not calling it is what F-03 requires.
- **Synthetic ids:** transcript ids are produced in both Graph serialisations by the same MessagePack/LZ4 envelope the unit suite reproduces byte for byte from real production ids.

| Case | Tests | Evidence | Result |
|---|---|---|---|
| **A.** Foreign canonical row + variant id → PROTECTED, no second row | `test_A_a_foreign_row_under_the_other_spelling_is_protected` ×4 (both spellings × DRY_RUN and PRODUCTION_NEW_ONLY); `test_A_explicit_backfill_with_update_allowed_still_protects_the_foreign_row` | Decision is `PROTECTED_EXISTING_LEGACY_ROW` with verdict `FOREIGN_SAME_OCCURRENCE`, and the target is the n8n row. The whole-table md5 of `qa_doctors_sessions` is unchanged. Exactly one row exists for the meeting, and no ownership row was written, **even in write-enabled modes and with `allow_update_existing`**. | **PASS** (5) |
| **B.** Owned row under an equivalent id → retarget, never insert | `test_B_an_owned_row_under_the_old_spelling_is_the_target`; `test_B_an_approved_update_lands_in_place_on_the_published_id` | The coded writer really publishes under the canonical id. Graph then re-spells it and the lecture is re-rendered under the variant. The verdict is `OWNED_SAME_OCCURRENCE`, the target is the published canonical id, and the decision is never `WOULD_INSERT`. An approved `EXPLICIT_BACKFILL` updates **in place**: 1 session row, 11 checklist rows keyed `<canonical>_1..11`, 0 rows under the variant, and 1 ownership row on the canonical id. | **PASS** (2) |
| **C.** Ambiguous identity → review, no write | `test_C_two_rows_for_one_occurrence_are_ambiguous_and_never_written`; `test_C_same_meeting_same_day_without_proof_is_ambiguous` | Decision is `BLOCKED_AMBIGUOUS_LEGACY_IDENTITY` in DRY_RUN and PRODUCTION_NEW_ONLY. Legacy table md5 is unchanged. The resolver reports `REVIEW_REQUIRED` / `LEGACY_IDENTITY_AMBIGUOUS`. | **PASS** (2) |
| **D.** Writer and resolver agree | `test_D_writer_and_resolver_agree_on_the_same_occurrence` ×5 (new; other meeting same day; foreign variant; foreign exact; ambiguous); `test_D_the_resolver_sees_an_owned_equivalent_row_as_ours_not_missing` | In every scenario, writer `WOULD_INSERT` ⇔ resolver `MISSING`. Protected ⇒ `NOT_APPLICABLE` / `LEGACY_ROW_NOT_CODED_OWNED`; ambiguous ⇒ `REVIEW_REQUIRED`. An owned equivalent row is never reported `MISSING`. | **PASS** (6) |
| **E.** Variant and canonical ids → one identity | `test_E_the_two_spellings_decode_to_one_identity`; `test_E_twin_artifacts_collapse_to_one_candidate_through_real_sql` | Both spellings decode to one `teams:v4:` key, and a different transcript does not. Through the real `TranscriptSelectionRepository.load_candidates` SQL, 3 stored candidates (twin pair + 1 other) become 2, and the unrelated transcript is kept. | **PASS** (2) |
| **F.** Legacy Perfect v1 cannot publish in write mode | `test_F_the_scheduler_writer_refuses_perfect_v1_in_every_write_mode` ×3 (CANARY_NEW_ONLY, PRODUCTION_NEW_ONLY, EXPLICIT_BACKFILL); `test_F_a_v1_dry_run_still_reproduces_history_and_writes_nothing`; `test_F_the_cli_refuses_v1_with_a_write_mode_before_opening_a_connection` | `StageRunner._writer` raises `WriterModeError` for v1 in every write mode. `qa_doctors_sessions` and `qa_perfect_lectures` are unchanged. A v1 DRY_RUN plans and writes nothing. The real CLI `main()` exits non-zero, and the connect guard shows **no connection was opened**. | **PASS** (5) |
| **G.** SOURCE_MISSING shadow QA → no model call, nothing persisted | `test_G_non_authoritative_attendance_buys_nothing_and_persists_nothing` ×3 (SOURCE_MISSING, SOURCE_PARTIAL_OR_INVALID from `source_rows_any_status`, SOURCE_PARTIAL_OR_INVALID with 0 members) | The counts reach the service through the **real `LOAD_QA_INPUTS` SQL**, whose columns 38–41 were added by Fix E. The status is `WAITING_FOR_ATTENDANCE_SOURCE` and `persisted=False`. The provider was called 0 times. There are 0 `lecture_qa_evaluations` rows and 0 `lecture_qa_generation_attempts` rows. | **PASS** (3) |
| **H.** Authoritative attendance → normal QA | `test_H_authoritative_attendance_still_evaluates_and_persists_normally` | Coverage is authoritative under the real `is_authoritative` predicate. The provider was called exactly once, a `COMPLETED` evaluation was persisted, and it has 11 checklist rows. | **PASS** (1) |
| **I.** RC3 max-pass / projection regression | `test_I_the_pass_cap_still_covers_the_whole_stage_chain`; `test_I_a_new_lecture_is_projected_once_and_a_rerun_is_identical`; `test_I_the_projection_leaves_the_clips_and_recording_columns_alone` | `DEFAULT_MAX_PASSES == len(EXECUTABLE_STAGES) + 3`. A genuinely new lecture is `WOULD_INSERT` and is projected once (1 row, 11 checklist rows, 1 `WRITTEN` ownership), after which the resolver reports `COMPLETE` and a re-run is `WOULD_SKIP_IDENTICAL` with rows unchanged. An approved update keeps `recording_url` and `clips_status`. The 32 unit tests in `test_backfill_legacy_compatibility` also pass in suite 5. | **PASS** (3 + 32 unit) |

**Two defects found in the test setup, not in the code:**

1. The first draft simulated a re-render by deleting the old rendered row. `lecture_qa_legacy_writes.rendered_session_id` is `ON DELETE CASCADE`, so that also deleted the ownership record, and the guard then (correctly) saw a foreign row. The real renderer never deletes a render (`ON CONFLICT (source_fingerprint)` inserts a new one), so the test now keeps the first render under a superseded renderer version, as a renderer upgrade leaves it.
2. Seeded engagement counts violated `spoke_count + silent_count = attended_count`, and one assertion used a guessed coverage label. The assertion now uses the real `is_authoritative` predicate instead of a literal.

**No application code changed in this phase.**

## 8. External-service isolation proof

| Claim | Evidence |
|---|---|
| Production PostgreSQL was never opened by the integration suite | Every psycopg connection in the process passes through `ConnectGuard`, which records the target of each connection it allows. Across the full integration run, **372 connections opened, all to one target: `127.0.0.1:55432/kbc_qa_integration_test`**. No other database driver exists in `app/`, `tools/` or `tests/` (no psycopg2, SQLAlchemy, asyncpg or pg8000), so there is no path around the guard. |
| The two refused connections were deliberate | They are the live gate tests: a sibling database on the test server, and the real production URL refused **in-process** before libpq was called. The bootstrap refusal of the production URL happens in a child process whose static check fails before any connect. |
| Production was not written | It was never connected to, so it could not be written. |
| No Graph, OpenAI, n8n or SharePoint call | `NetworkGuard` blocks every non-loopback socket and DNS lookup in the process, and it recorded **0 blocked attempts** across all six matrix runs. The QA provider in the tests is an in-process stub, and the environment scrub leaves no Graph, OpenAI or n8n credential to call with. |
| No production contact in this phase at all | No read-only production validation was run in this phase, unlike the previous one. The production URL was read from `backend/.env` into memory only, to prove that it is refused. It was never passed to libpq and never printed. |

## 9. Files changed

**Added in this phase:**

| File | Purpose |
|---|---|
| `tools/integration_db_guard.py` | The gate: static checks 1–5, live check 6, `ConnectGuard`, `NetworkGuard` and the environment scrub. |
| `tools/bootstrap_integration_db.py` | Builds the test database from the baseline, the automation migrations and app migrations 001–020, then writes the marker. It refuses production URLs, refuses unmarked non-empty databases, and requires `--reset` to rebuild. |
| `tests/conftest.py` | Installs the gate for every pytest session. It skips integration tests when unconfigured, aborts when misconfigured, and reports isolation evidence. |
| `tests/integration/fixtures/legacy_baseline.sql` | Test-only shape of the 6 legacy tables the migrations assume already exist. |
| `tests/unit/test_integration_db_guard.py` | 36 offline fail-closed tests. |
| `tests/integration/test_database_safety_gate.py` | 7 live gate tests. |
| `tests/integration/test_safety_fixes_integration.py` | 29 self-seeding F-01/F-02/F-03 and RC3 integration tests (A–I). |
| `docs/audits/QA_CORE_RC4_INTEGRATION_VALIDATION_2026-09-22.md` | This report. |

**Modified in this phase:**

| File | Change |
|---|---|
| `pytest.ini` | `pythonpath = .` |
| `QA_CORE_RELEASE_READINESS.md` | The integration-suite paragraph no longer describes running against production; it gives the isolated-database procedure. |

**Unchanged in this phase:**

- All `app/` code. The `app/` modifications in `git status` are the F-01/F-02/F-03 fixes from the previous phase.
- All 20 pre-existing `tests/integration/test_*.py` files.

**Not committed. Not tagged. Not deployed.**

## 10. Remaining risks

1. **The production-evidence integration tests (302 of the 359 pre-existing tests) have not been run against the post-fix code.** On an isolated database they can only fail or skip, and against production they may not run. The fixes are proven by 1453 unit tests, 36 self-seeding integration tests and the previous phase's read-only production validation. But nothing in this phase shows that those 302 assertions still hold on real data after the fixes. Some *should* now differ; for example, anything that expected `WOULD_INSERT` for the F-02 lectures. There are two ways out, and both are the user's decision:
   - **(a)** Restore an anonymised production snapshot into an isolated test database. That needs explicit approval, because it copies learner data.
   - **(b)** Reclassify those modules as a production-evidence acceptance suite (a marker, excluded from the release gate), and keep extending the self-seeding suite as the gate.

   Converting those failures to skips without that decision would hide them, so it was not done.
2. **The legacy baseline is reconstructed, not authoritative.** It follows the platform's column contracts, but production's real constraints, defaults and triggers on the n8n tables were not read, since that would have meant contacting production. A difference there would not be caught.
3. **The PostgreSQL major version is assumed (16).** Production's version was not verified.
4. **Migrations 003 and 004 are not re-runnable.** They rename or move columns, so a second application fails harmlessly inside its own transaction. Nothing re-runs applied migrations today, but a future migration runner must track what has been applied.
5. **The Django backend's tests are outside this gate.** `QA_CORE_RELEASE_READINESS.md` records `manage.py test` creating `test_AiTeamKBC` on the managed production server. That is a separate production-contact path which this pytest gate does not cover.
6. **Carried over, unchanged:**
   - The F-02 lectures are still stalled at EVALUATE_PERFECT.
   - Acquisition can still create twin transcript artifacts.
   - The 3 duplicate `qa_doctors_sessions` rows, the Martech Perfect row and the F-03 evaluations still need repair decisions.
   - **The scheduler is not declared safe by this report.**

---

INTEGRATION VALIDATION:
FAIL

Production DB contacted by integration tests:
NO

Production DB written:
NO

Full offline suite:
1453 collected — 1453 passed, 0 failed, 0 skipped (4.2 s)

Full integration suite:
395 collected — 93 passed, 149 failed, 153 skipped (25.9 s). All 149 failures and 153 skips need production evidence that an isolated database cannot hold; 0 are code defects. The 36 new F-01/F-02/F-03 and gate integration tests: 36/36 passed.

F-02 same-lecture duplicate guard:
PASS

Transcript identity integration:
PASS

F-01 write-policy guard:
PASS

F-03 attendance guard:
PASS

RC3 max-pass regression:
PASS

READY TO CREATE QA-CORE-RC4:
NO
