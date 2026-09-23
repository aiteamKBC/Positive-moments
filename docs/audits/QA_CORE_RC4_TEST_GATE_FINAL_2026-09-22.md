# QA Core RC4 — Final Test Gate: Two Suites, One Honest Gate

**Date:** 2026-09-22 (work completed 2026-09-23)
**Decision implemented:** Option B from `QA_CORE_RC4_INTEGRATION_VALIDATION_2026-09-22.md`.
No production learner data was copied. The production-data-dependent tests are now an explicit, marked acceptance suite, and the release gate is deterministic and self-contained.

**Constraints honoured:** no production PostgreSQL connection, no production data copied, no Graph/OpenAI/n8n/SharePoint call, no deploy, no commit, no tag, scheduler not enabled, no production repair.

---

## Summary

| | |
|---|---|
| **RC4 release gate** | **PASS — 1576 selected, 1576 passed, 0 failed, 0 skipped**, 319 acceptance tests deselected, 20.8 s |
| Built from | A database dropped and rebuilt from nothing: 25 SQL files, PostgreSQL 16.15, 52 tables / 145 indexes / 289 constraints |
| Integration tests | **425 collected: 106 release-gate, 319 production-data acceptance** (390 test functions: 95 gate, 295 acceptance) |
| Conversions | **66 test functions** now self-contained that previously depended on production data; **0** obsolete tests found |
| Skips converted to nothing | The gate has **no** skipped tests and **no** runtime "data not present" skipping |
| Django hazard | **Closed.** `manage.py test` refuses production, fails closed, and runs 18 tests against the isolated database |
| RC4-required contract covered only by acceptance tests | **None** |

---

## 1. Classification of every integration test (Part 1)

Every test was classified individually, by **purpose**, not by whether it currently passes. The machine-readable inventory is `docs/audits/qa_core_integration_test_classification_2026-09-22.json` — one entry per test with its class, its suite, the reason, and the production evidence it depends on.

| Class | Test functions | Where they are now |
|---|---:|---|
| **A. SELF_CONTAINED** | 29 | release gate |
| **B. CONVERTIBLE → converted** | 66 | release gate |
| **C. PRODUCTION_DATA_ACCEPTANCE** | 295 | acceptance suite, marked `production_data` |
| **D. OBSOLETE_OR_DUPLICATE** | 0 | — |
| **Total** | **390** | |

### Two findings that shaped the classification

1. **Passing was not evidence.** A number of tests *passed* on the empty database only because they iterate whatever rows exist and assert something about each — with no rows, they assert nothing. Examples: "no duplicate snapshots, members, resolutions or roles", "every ineligible production row is explained by the older rule", "the registry-wide scan finds no case needing a human". Those are Class C: their purpose is the real dataset, and counting them as gate passes would have been exactly the false green this exercise exists to remove.
2. **Failing was not evidence either.** Several failing tests were generic contracts that merely *reached for* a production row (a repository test that borrowed "whichever lecture is in the database"). Those are Class B and are now converted.

### Class D: why it is empty

No test was found that no longer checks a useful contract. The nearest candidates were the contracts that appeared both in an acceptance module and in a new gate module; rather than leave duplicates, those tests were **moved** out of the acceptance modules (see §2), so each contract has exactly one home.

### Class C reasons, by module

Each of these asserts facts about KBC's real history — named lectures, real session dates, real transcripts, real legacy rows, or parity measurements against the legacy system. A synthetic fixture cannot stand in for the fact being asserted, and inventing one would destroy what the test validates.

| Module | Tests | Why it is acceptance, not convertible |
|---|---:|---|
| `test_attendance_recovery` | 17 | The real 2026-09-17 recovery of a named lecture: what the live external source held, what it later held, what the platform did. |
| `test_attendance_resolution_persistence` | 17 | Re-runs Phase 2C3 over the real roster and asserts the real snapshot/member/role counts and the legacy trainer parity measurement. |
| `test_automated_production_sync` | 17 | The record of the real unattended cycle of 2026-09-18: which named lectures synced, what was written and what was refused. |
| `test_canonical_transcript_persistence` | 12 | Parses the real stored transcripts and asserts their real cue counts, durations and Phase 2B parity. |
| `test_duplicate_resolution_persistence` | 17 | The real duplicate booking: two genuine calendar events for one class, and the suppression that retired one. |
| `test_engagement_persistence` | 14 | Engagement parity against the real legacy QA rows. |
| `test_lecture_scoped_engagement` | 6 | The real engagement evidence, snapshots and version pins of named lectures. |
| `test_legacy_writer_persistence` | 18 | Exercises the writer against the real legacy rows n8n wrote, then re-asserts those tables are byte-identical. |
| `test_makeup_roster_drift` | 11 | Measures the v1→v2 roster rule change against the real live makeup rows. |
| `test_media_timeline_real_lectures` | 7 | Real cue timelines inside really measured media; there is no synthetic stand-in for a real recording's duration. |
| `test_orchestration_persistence` | 21 | Describes the real pilot days lecture by lecture: which were waiting, which settled, what a run would not do. |
| `test_perfect_lecture_persistence` | 31 | The real canary and the real Perfect rows, the `attended_count` repair, and the v1/v2 policy history. |
| `test_qa_rendering_persistence` | 22 | Renders the real evaluations and asserts parity with the real legacy payload, including LMS snapshot semantics against the live roster. |
| `test_seam_dedup_persistence` | 18 | The real two-part recording and the seam its rebuild removes. |
| `test_shadow_qa_persistence` | 24 | The real frozen model answers, the real generation history, legacy deterministic parity. |
| `test_speaker_inventory_persistence` | 12 | Aggregates the real speaker labels of the real transcripts. |
| `test_transcript_selection_persistence` | 8 | The real selection outcome, the legacy session-id parity, the real unresolved occurrence. |
| `test_unattended_readiness` | 23 | The three real lectures whose attendance evidence exposed F-03, and the real Perfect rows written under v1. |

## 2. What was converted (Part 2)

All conversions use `tests/integration/seeding.py`, a shared fixture module with **no learner PII** (names are obviously invented), **no copied production transcript text** (WebVTT is generated), **no production identifiers** (fresh uuid4s; the Graph transcript-id *format* is reproduced only where that format is itself the subject under test), and **no production dates** (everything happens on 2031-03-04, far outside the platform's history). `insert()` fills any NOT NULL column a test did not name, so fixtures stay minimal. Every test runs in one transaction that is rolled back, so tests are order-independent and leave nothing behind.

| Converted into | Tests | What was replaced |
|---|---:|---|
| `test_pipeline_contracts.py` (new) | 37 | Advisory lock behaviour, schema CHECK/UNIQUE/FK enforcement, attendance resolution from the external source, engagement scoping, shadow-QA economics (preview, reuse, provider error, generation cap), the writer's write/protect/rollback contracts, and the Perfect dry-run/protection/foreign-column contracts — each now seeds its own lecture chain. |
| `test_platform_invariants.py` (new) | 22 | Contracts that never needed history and were moved out of acceptance modules: mapping arity, schema shape, the scheduler cycle lock, identity derivations, and structural refusals ("no provider at all", "unknown lecture refused"). |
| `test_transcript_artifact_repository.py` (in place) | 7 | Used to borrow whichever lecture was in the database; now seeds one. Its ready/unresolved split test previously passed vacuously on an empty table and now seeds both sides. |
| `test_registry_repository.py` (in place) | 4 | Already built its own rows; only its 2026 date literals echoed production and are now synthetic. |

Two fixture fidelity gaps were found **by** these conversions and fixed in `tests/integration/fixtures/legacy_baseline.sql`:

- `qa_doctors_checklist_items.session_id` now carries the `ON DELETE CASCADE` foreign key production relies on — `delete_owned_session` deletes only the session row and expects the eleven checklist rows to cascade.
- The two externally owned read-only upstreams, `kbc_attendance` and `kbc_users_data`, are now created in the test baseline, which is what made the attendance and engagement contracts convertible at all.

**Deliberately not converted:** anything whose subject is a historical case (the real seam rebuild, the real duplicate booking, the September F-02/F-03 incidents, every legacy parity measurement). Rewriting those against invented data would have destroyed exactly what they validate.

## 3. The acceptance suite is marked, not skipped (Part 3)

- Marker: `pytest.mark.production_data`, applied as a **module-level `pytestmark`** in all 18 acceptance modules, under a header comment that says what the module is and how to run it. The categorisation is static and visible in the source — not derived at runtime from whether data happens to be present.
- Registered in `pytest.ini` under `markers`, with its meaning.
- The gate deselects it: `pytest -m "not production_data"`.
- With no acceptance dataset configured, those tests are **deselected (319)**, never counted as passes.

| | Collected tests |
|---|---:|
| Total integration | **425** |
| Release gate (`-m "not production_data"`) | **106** |
| Production-data acceptance (`-m production_data`) | **319** |
| Obsolete | **0** |

(425 collected from 390 test functions; the difference is parametrisation.)

## 4. The RC4 release gate (Part 4)

One documented command, `tools/release_gate.py`:

```bash
export APP_ENV=test
export TEST_DATABASE_URL=postgresql://kbc_test:<throwaway>@127.0.0.1:55432/kbc_qa_integration_test
python -m tools.release_gate --reset
```

`--reset` rebuilds the isolated database from the repository's migrations first, then it runs one pytest session over `pytest.ini`'s `testpaths` with `-m "not production_data"`, covering:

| Required by the brief | Where it runs in the gate |
|---|---|
| unit / offline tests | `tests/unit` + `automation/lecture_parts/test_graph_client.py` — 1470 tests |
| self-contained integration tests | `tests/integration -m "not production_data"` — 106 tests |
| migration tests | `test_migration_transaction_safety` (10) **plus** the `--reset` rebuild applying all 25 files, **plus** `test_platform_invariants` asserting schema shape |
| scheduler / orchestration tests | `test_scheduler_runtime`, `test_orchestration`, `test_pipeline_state`, `test_automated_sync`, `test_backfill*` (unit) and the lock/orchestration contracts in `test_pipeline_contracts` / `test_platform_invariants` |
| F-01 / F-02 / F-03 safety tests | `test_safety_fixes_f01_f02_f03` (unit, 69) and `test_safety_fixes_integration` (29 against real PostgreSQL) |

**The gate requires 0 failed. It currently has 0 failed and 0 skipped**, so there are no platform-dependent skips to enumerate.

## 5. The production-data acceptance suite (Part 5)

**It is not run in this phase, and it is never run against production.** The same gate that protects the integration suite protects it: it can only reach a database that passes all six signals of `tools/integration_db_guard`, so pointing it at `DATABASE_URL` is refused before a connection is opened.

Two supported ways to run it in future — both require an explicit decision that is not part of RC4:

- **Option 1 — an approved acceptance fixture.** A curated, anonymised or wholly synthetic dataset that reproduces the *shapes* the suite asserts (a two-part recording with a seam, a duplicate booking pair, a lecture whose attendance source is silent), loaded into an isolated database and pinned as a fixture. Durable and safe, and the only option that keeps the suite meaningful indefinitely; it costs real work to build, and each test's expected values must be re-derived from the fixture rather than from history.
- **Option 2 — an approved sanitised snapshot.** A point-in-time copy of production with learner names, emails and transcript text removed, restored into an isolated database. Cheaper and higher fidelity; it carries data-protection obligations and the tests that assert on real names would need adjusting.

Either way: a dedicated `TEST_DATABASE_URL` pointing at that isolated database, never a fallback to `DATABASE_URL`.

### Contract gaps: what the acceptance suite alone covers

These are covered by acceptance tests and not by the gate:

| Contract only asserted on real data | Also covered offline? | RC4-required? |
|---|---|---|
| Legacy parity measurements (engagement, Item 2 timing, trainer, deterministic QA) | Algorithms unit-tested (`test_engagement_calculator`, `test_punctuality_source`, `test_shadow_qa`); the *measurement against the real legacy system* is not | No — parity of already-shipped phases, untouched by this release |
| Real media timeline coverage (cues inside measured media) | `test_media_coordinates`, `test_lecture_part_media`, `test_mp4_movie_header` | No — Phase 6 media is excluded from this release |
| The real seam rebuild, the real duplicate booking, the September incidents | `test_seam_dedup`, `test_duplicate_resolution`, `test_safety_fixes_f01_f02_f03` cover the logic | No — these are historical verifications; the logic they exercise is in the gate |
| LMS snapshot semantics against the live roster | `test_qa_rendering` unit module | No — the renderer is unchanged in this release |

**Every contract this release changes — the F-01 Perfect write policy, the F-02 occurrence guard, the F-03 attendance guard, the RC3 projection cap, writer ownership and rollback — is asserted in the gate, against real PostgreSQL.** So no acceptance dataset is required for RC4.

## 6. Django `manage.py test` safety (Part 6)

**The hazard:** `config/settings.py` built `DATABASES["default"]` from `DATABASE_URL`. Django's test runner then created — and dropped — `test_<NAME>` on whatever server that pointed at, which on an operator machine is the production Neon server. The previous report recorded exactly that symptom (`database "test_AiTeamKBC" is being accessed by other users`).

**Closed in two layers, both fail-closed:**

1. `backend/config/test_database.py` — on a test command (`manage.py test`, or `DJANGO_TEST_MODE=1`), the database is built from `TEST_DATABASE_URL` and **never** from `DATABASE_URL`, after passing the same six-signal gate the integration suite uses. It pins `TEST.NAME` explicitly and raises `UnsafeDjangoTestDatabase` otherwise.
2. `backend/config/test_runner.py` — `TEST_RUNNER = "config.test_runner.SafeDatabaseTestRunner"` re-checks the **final** connection settings immediately before Django creates any database, so an IDE, a `--settings` override or pytest-django cannot reach production either.

**Django's `test_` prefix is explicitly not treated as a safety property:** the check is on the *server*. A database called `test_AiTeamKBC` on the production host is refused.

Nothing logs a URL, a user or a password — asserted by the tests.

**Automated tests:** `tests/unit/test_django_test_database_guard.py`, 17 offline tests covering which commands are guarded, each refusal, the accepted case, the runner's final check, and that `settings.py` still wires the safe runner.

**Proven end to end** with the production URL present in the environment, exactly as on an operator machine:

| Run | Result |
|---|---|
| `manage.py test` with no `TEST_DATABASE_URL` | exit 1, refused before connecting: "TEST_DATABASE_URL is not set. The integration suite never falls back to DATABASE_URL." |
| `manage.py test` with `TEST_DATABASE_URL` = the real production URL | exit 1, refused: "database name 'AiTeamKBC' carries no 'test' marker" |
| `manage.py test` against the isolated container | exit 0, **Ran 18 tests, OK** |

No URL appeared in any output.

## 7. Test-database guard regression (Part 7)

| # | Case | Outcome | Evidence |
|---|---|---|---|
| 1 | `TEST_DATABASE_URL` missing | **Refused**, exit 2, before any connection | `python -m tools.release_gate` → "TEST_DATABASE_URL is not set. The integration suite never falls back to DATABASE_URL." For a bare `pytest`, integration tests are skipped with the same reason and 0 connections opened |
| 2 | `TEST_DATABASE_URL` = production `DATABASE_URL` | **Refused** before connection | exit 2, "database name 'AiTeamKBC' carries no 'test' marker"; the URL never appeared in output |
| 3 | Production **server**, database called `test_something` | **Refused** | exit 2, "points at the same server (host:port) as a production DATABASE_URL; a test database must live on its own server" — and unit test `test_a_test_database_on_the_production_server_is_refused` |
| 4 | Local PostgreSQL, non-test database name | **Refused** | exit 2, "database name 'kbcqa' carries no 'test' marker" |
| 5 | Local explicit test DB with marker | **Accepted** | the gate run: 96 connections, all to `127.0.0.1:55432/kbc_qa_integration_test` |
| 6 | External network calls | **Blocked** | `NetworkGuard` blocks every non-loopback socket and DNS lookup; 0 attempts recorded in the gate run; unit tests prove Graph, OpenAI, SharePoint and n8n hosts raise |
| 7 | `backend/.env` secrets available to tests | **No** | scrubbed session-wide; `test_settings_resolve_to_the_approved_test_database_only` asserts `QA_MODEL_API_KEY`, `N8N_API_KEY`, `MICROSOFT_GRAPH_CLIENT_SECRET` and `APTEM_DATABASE_URL` are all empty |
| 8 | `manage.py test` with production config | **Blocked** | §6 |

The 2 "refused" connections reported by the gate run are the deliberate probes in `test_database_safety_gate.py` (a sibling database, and the real production URL refused in-process).

## 8. Migration findings (Part 8)

**Why 003 and 004 cannot run twice** — confirmed by re-applying each to a built database:

| Migration | Error on second run | Cause |
|---|---|---|
| `003_canonical_registry_and_meeting_context.sql` | `UndefinedColumn: column "organizer_upn" does not exist` | It **renames** `lecture_sessions.organizer_upn` → `calendar_organizer_address`. After the first run the old name is gone. |
| `004_create_transcript_artifact_tables.sql` | `UndefinedColumn: column "lecture_id" does not exist` | `CREATE TABLE IF NOT EXISTS` skips the existing table, then the index and comment reference `lecture_transcript_artifacts.lecture_id` — a column **migration 006 deliberately drops**. |

**Are they normal one-time migrations, or does some process rerun them?**

They are ordinary forward-only migrations superseded by later ones, and there is **no migration runner and no ledger table** in this project — the documented procedure is "apply only what is missing", verified against `information_schema.tables`. The only bootstrap path that re-executes migration SQL is `app/cli/normalize_transcript_artifacts.py`, and it executes **005 and 006** only, both of which are re-runnable.

**However, one documented path did rerun them, and claimed it was safe.** `DEPLOYMENT_HANDOFF.md` gave a `for f in app/db/migrations/0{0,1}*.sql` loop and stated "Each file is idempotent (`CREATE TABLE IF NOT EXISTS`), so re-running is safe." On an already-migrated database that loop stops at 003 with `ON_ERROR_STOP=1`. Nothing is damaged — each failure is inside its own transaction and changes nothing — but an operator would be left mid-procedure with a false explanation. **The documentation is corrected** to say which files are not re-runnable and why.

**Classification: non-blocking.** No supported path reruns them, the production database is already past them, and no migration was changed for this release.

**Production PostgreSQL major version: not determined.** Part 8 allows a read-only check, but this phase's instructions also say plainly *do not connect to production PostgreSQL*; I treated the prohibition as binding, and the version is recorded nowhere in the repository. The test container runs **PostgreSQL 16.15**. To settle it without exposing secrets, one read-only statement suffices:

```bash
psql "$DATABASE_URL" -At -c "SHOW server_version"     # prints only the version
```

The risk of the assumption is low — no migration or query in this release uses version-specific syntax — but it remains unverified, as in the previous report.

## 9. Release gate run (Part 9)

The database was **dropped and rebuilt from nothing** first:

| | |
|---|---|
| Rebuild | `DROP DATABASE` → `CREATE DATABASE` → `tools/bootstrap_integration_db` |
| Files applied | **25/25**: the test-only legacy baseline, 4 automation migrations, app migrations **001–020** |
| Server | PostgreSQL **16.15** |
| Schema | 52 tables, 0 views, 145 indexes, 289 constraints, 0 triggers, 0 functions |

Then `python -m tools.release_gate`:

| Metric | Value |
|---|---:|
| Collected | 1895 |
| **Selected** | **1576** |
| **Deselected** (`production_data`) | **319** |
| **Passed** | **1576** |
| **Failed** | **0** |
| **Skipped** | **0** |
| Duration | **20.8 s** |

Breakdown of the 1576: 1470 offline (unit + `automation/lecture_parts`) and 106 self-contained integration.

Isolation evidence from the same run: **96 psycopg connections, all to `127.0.0.1:55432/kbc_qa_integration_test`**; 2 deliberate refusals; **0 external network attempts**.

## 10. RC4 decision (Part 10)

| # | Question | Answer |
|---|---|---|
| 1 | Any failing tests left in the release gate? | **No.** 1576 selected, 1576 passed, 0 failed. |
| 2 | Any skipped release-gate tests hiding required contracts? | **No.** The gate has 0 skipped tests. Runtime "data not present" skipping was removed from the gate entirely: those modules are either converted to seed their own data or moved to the acceptance suite. |
| 3 | Are F-01/F-02/F-03 covered at real PostgreSQL integration level? | **Yes.** 29 tests in `test_safety_fixes_integration` (A–I) plus the writer, Perfect and attendance contracts in `test_pipeline_contracts`, all against real tables, constraints and savepoints. |
| 4 | Is the production-test safety gate fail-closed? | **Yes.** Six independent signals; missing configuration skips, bad configuration aborts; psycopg and the network are guarded process-wide. Proven against the real production URL (§7). |
| 5 | Is `manage.py test` safe? | **Yes.** Two fail-closed layers, 17 automated tests, and proven end to end with the production URL present (§6). |
| 6 | Are all remaining `production_data` tests genuinely acceptance tests? | **Yes.** Each was classified by purpose with a stated reason (§1 and the JSON inventory), and everything generic was converted or moved out. |
| 7 | Any RC4-required contract only covered by production-data tests? | **No.** The acceptance-only contracts are historical parity measurements and verifications of already-shipped phases (§5); every contract this release changes is in the gate. |

**RC4 is ready to be committed and tagged.** Nothing has been committed, tagged or deployed.

## 11. Files changed in this phase

**Added**

| File | Purpose |
|---|---|
| `tests/integration/seeding.py` | Shared synthetic fixtures: no PII, no production text, ids, or dates. |
| `tests/integration/test_pipeline_contracts.py` | 37 converted, self-contained pipeline contracts. |
| `tests/integration/test_platform_invariants.py` | 22 invariants moved out of the acceptance modules. |
| `backend/config/test_database.py` | Fail-closed database selection for Django test commands. |
| `backend/config/test_runner.py` | `SafeDatabaseTestRunner`: the final check before Django creates a database. |
| `tests/unit/test_django_test_database_guard.py` | 17 offline tests for the Django guard. |
| `tools/release_gate.py` | The one documented RC release-gate command. |
| `docs/audits/qa_core_integration_test_classification_2026-09-22.json` | The machine-readable classification of all 390 tests. |
| `docs/audits/QA_CORE_RC4_TEST_GATE_FINAL_2026-09-22.md` | This report. |

**Modified**

| File | Change |
|---|---|
| 18 modules in `tests/integration/` | Module-level `pytestmark = pytest.mark.production_data` with an explanatory header; 26 self-contained tests moved out to the gate modules. |
| `tests/integration/test_transcript_artifact_repository.py`, `test_registry_repository.py` | Converted in place to seed their own rows and use synthetic dates. |
| `tests/integration/test_safety_fixes_integration.py` | Now uses the shared `seeding.py` instead of its own copies. |
| `tests/integration/fixtures/legacy_baseline.sql` | Added the checklist `ON DELETE CASCADE`, and the two external upstream tables `kbc_attendance` / `kbc_users_data`. |
| `backend/config/settings.py` | Test commands build `DATABASES` from `TEST_DATABASE_URL` only; `TEST_RUNNER` wired. |
| `pytest.ini` | Registered the `production_data` marker. |
| `QA_CORE_RELEASE_READINESS.md` | Documents the two-suite model and the gate command. |
| `DEPLOYMENT_HANDOFF.md` | Corrected the false claim that every migration is re-runnable. |

**Unchanged:** all `app/` code. The `app/` modifications in `git status` remain the F-01/F-02/F-03 fixes from the earlier phase. No previous audit report was modified.

## 12. Remaining risks

1. **The acceptance suite has not run against the post-fix code.** 295 test functions assert facts about real history and cannot run until an acceptance dataset exists (§5). Some of their expectations *should* now differ — anything that expected `WOULD_INSERT` for the F-02 lectures, for example. This is a known, stated gap, not a silent one, and no RC4-required contract depends on it.
2. **The legacy baseline is reconstructed, not authoritative.** It follows the platform's own column contracts and has now been corrected twice by evidence (column order, and the checklist cascade), but production's real constraints, defaults and triggers on the n8n tables have never been read — that would mean contacting production.
3. **The production PostgreSQL major version is still assumed (16).** §8 gives the one read-only command that settles it.
4. **Migrations 003 and 004 are not re-runnable.** Documented and non-blocking; no supported path reruns them.
5. **Synthetic fixtures can drift from production shapes.** A contract can pass against a fixture that no longer resembles reality. The acceptance suite is the intended counterweight, which is another reason to fund Option 1 or 2 in §5 rather than leave it indefinitely deselected.
6. **Operational note, outside the code:** during this phase a repository-wide search matched `backend/.env` and printed live database credentials into the session log. Nothing was transmitted anywhere and no file changed, but those credentials are now in a transcript; rotating the Neon credentials is the prudent response.
7. **Carried over, unchanged:** the F-02 lectures are still stalled at EVALUATE_PERFECT; acquisition can still create twin transcript artifacts; the 3 duplicate `qa_doctors_sessions` rows, the Martech Perfect row and the F-03 evaluations still need repair decisions. **This report does not declare the scheduler safe to enable.**

---

RC4 RELEASE TEST GATE:
PASS

Selected release-gate tests:
1576

Passed:
1576

Failed:
0

Skipped:
0

Production-data acceptance tests deselected:
319

Production DB contacted:
NO

Django production-test hazard closed:
YES

F-01 integration coverage:
PASS

F-02 integration coverage:
PASS

F-03 integration coverage:
PASS

Any RC4-required contract only covered by production-data tests:
NO

READY TO COMMIT AND TAG QA-CORE-RC4:
YES
