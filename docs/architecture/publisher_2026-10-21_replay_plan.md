# Publisher replay of 2026-10-21: plan

Prepared 2026-10-22. **Nothing here has been executed.**

The replay drives the real publisher runtime (its planner, queue, workers and reconciler, the same code the daemon runs) over every eligible lecture on 21 Oct 2026. It runs from this workstation, not the VPS, so there is no deployment and no systemd change.

**Each step needs its own approval.** Phase A writes nothing to Bunny. Phase B, uploads to the staging library, is approved separately afterwards.

## 0. Where it writes, and why a Neon branch

The runtime's queue and tracking tables come from migrations 024–028, and those exist nowhere in production (checked read-only on 22 Oct). That leaves two targets:

| Target | What it costs | Recommendation |
|---|---|---|
| **A Neon branch of production** (for example `publisher-replay-20261021`) | Migrations go to the branch only. Every publisher write lands in the copy. Production, QA, Django and the media runner are untouched. Discarding the result means deleting the branch. | **Use this** |
| The production Lecture Intelligence database | Needs the production migrations to be approved first. Publisher writes reach shared tables QA reads. | Only after the replay has passed |

On the branch:
- **Reads** are real: Microsoft Graph (calendar, online meetings, transcripts, recordings and drive search), the LMS (read-only transaction), and Aptem.
- **Graph writes:** none. `createLink` is never built.

## 1. Configuration

The publisher-only env file is `backend/config/publisher.env`. It is git-ignored (`*.env`), and on the VPS it is the systemd `EnvironmentFile` at `/var/www/Positive-moments/backend/config/publisher.env`.

**How the file is loaded:**
- Values come from the process environment first, then from `--env-file`.
- **Exception:** `PUBLISHER_*` names in the file always win. A pooled `DATABASE_URL` or `PUBLISHER_*` left in a shell can therefore never redirect the publisher.

**The database the publisher uses:**
- It reads `PUBLISHER_DATABASE_URL`. The generic `DATABASE_URL` is used only as a fallback, and the local file deliberately leaves it out.
- **A Neon `-pooler` host is refused at startup** (`validate-config` and `run` both exit with code 2).

**Already set in the local file:**

| Setting | Value |
|---|---|
| `PUBLISHER_MODE` | `SHADOW` |
| `PUBLISHER_START_DATE`, `PUBLISHER_END_DATE` | `2026-10-21` (inclusive; Africa/Cairo session date) |
| `PUBLISHER_LMS_TEST_PROGRAMMES` | `Final Test;Test Lms;Test` |
| Graph, LMS, Aptem and calendar values | copied from `backend/.env`, which was only read |
| `BUNNY_*` | blank |
| `PUBLISHER_PRODUCTION_WRITE_ENABLED` | `false` |

**Left for the operator:** `PUBLISHER_DATABASE_URL` (line 27), the Neon **direct** connection string.

**Why the scope holds:** `PUBLISHER_END_DATE` caps the planner window, the calendar catch-up days, the full reconciliation and the reconciler's lecture and LMS-only queries. Whatever the real date is, only 21 Oct is discovered, matched or planned.

## 2. Connection smoke test (read-only)

```bash
python -m app.cli.publisher --env-file backend/config/publisher.env validate-config
python -m app.cli.publisher --env-file backend/config/publisher.env db-smoke
```

`db-smoke` opens two sessions with `default_transaction_read_only=on`. **It passes only if all of these hold:**
- a write probe is refused;
- 20 consecutive statements reach one backend, which proves this is not a transaction pooler;
- a session advisory lock taken on session A:
  - is still held three statements later;
  - is refused to session B;
  - is released by A's own unlock;
  - can then be taken and released by B;
- no smoke lock remains afterwards.

It also reports `deadlock_timeout` (the transcript worker's 250 ms lock wait assumes the default, at least 1 s) and whether migrations 024–028 are present. It prints no host, user or URL.

**Run it twice:**
1. against the production direct endpoint, read-only (expect `migrations_024_028_applied: false`);
2. against the branch's direct endpoint, after the branch migrations.

## 3. Phase A: the full day, SHADOW (no Bunny call of any kind)

| # | Step | Approval |
|---|---|---|
| A1 | Create the Neon branch from production's current state (Neon console). Copy its **direct** connection string into `PUBLISHER_DATABASE_URL`. | yes |
| A2 | Apply 024 → 028 **to the branch** through its direct endpoint, one file at a time, with `psql -v ON_ERROR_STOP=1 --single-transaction -f app/db/migrations_proposed/<file>.sql`. | yes |
| A3 | Run `db-smoke` against the branch. Every check must be true, and `migrations_024_028_applied` must be `true`. | |
| A4 | Run `python -m app.cli.publisher --env-file backend/config/publisher.env run --once`. It runs one scheduler tick (calendar discovery for 21 Oct, LMS matching, planning) and then drains the queue (transcript and recording jobs, at most 100). | yes |
| A5 | Repeat A4 every 5–10 minutes until no job is waiting or retrying, about 30–60 minutes. Retries are scheduled by the database clock. | |
| A6 | Run `... reconcile --kind light` and `... reconcile --kind full`. | |
| A7 | Collect evidence: `status`, `exceptions`, and `lecture --lecture-id <id>` for every 21 Oct lecture. These are read-only transactions. | |

**Time limit:** transcript and recording waits go to review 72 hours after a lecture's scheduled end. **Phase A should run before about 24 Oct 12:00 UTC.** After that, anything not yet available goes straight to review: still safe and visible, but less informative.

**Expected outcome**, from the read-only LMS dry run on 22 Oct (test list applied):

| Item | Expected |
|---|---|
| LI lectures on 21 Oct | 10. Calendar discovery may add any that QA never saw. |
| LMS status | 7 `VERIFIED`, 1 `TEST_EXCLUDED`, 2 `NO_LMS_SESSION` |
| LMS-only occurrences | 7 active, non-test sessions at 08:00 UTC, shown as `LMS_SESSION_NOT_DISCOVERED` |
| Track jobs | one `ACQUIRE_TRANSCRIPT` and one `RESOLVE_RECORDING` per eligible, ended lecture; none for test-excluded lectures |
| Bunny jobs | **none**. `would_import_parts` counts the included parts of VERIFIED and LOCATED lectures. |
| Duplicates | any group reported as winner, `DUPLICATE_LOSER` or `DUPLICATE_AMBIGUOUS` |

**Phase A acceptance:**
- every in-scope lecture has exactly one LMS status, a recording status and a transcript status;
- every LMS occurrence on 21 Oct has a coverage row;
- the occurrence ids match the LMS, checked by hand;
- recordings reach LOCATED independently of transcripts;
- there are no `IMPORT_VIDEO`, `POLL_ENCODING` or `PUBLISH_CAPTION` jobs, and `bunny_videos` is empty;
- there is at most one live job per subject;
- nothing is stuck, and every exception is explained;
- the `RECONCILE_LIGHT` and `RECONCILE_FULL` runs succeeded;
- transcripts match QA's results for 21 Oct on production, compared read-only.

## 4. Phase B: controlled staging uploads (separate approval)

**Preconditions:**
- Phase A accepted;
- library **786045** (never 785080);
- `BUNNY_STAGING_LIBRARY_ID`, `BUNNY_STAGING_API_KEY` and `BUNNY_STAGING_ACCOUNT_API_KEY` added to the local file;
- `PUBLISHER_MAX_IMPORTS_IN_FLIGHT=1` for the first pass.

| # | Step |
|---|---|
| B1 | Set `PUBLISHER_MODE=STAGING_WRITE`, then run `validate-config --check-library`. It must report no problems: Free tier, no JIT, AI transcribing off, x264 output. |
| B2 | Run `run --once` repeatedly. Only lectures that are LMS-`VERIFIED`, recording-`LOCATED`, have an `INCLUDE` part and are not blocked as duplicates get `IMPORT_VIDEO`. Each part goes intent → fresh Graph URL → URL Fetch. Then encoding is polled and verified, and the caption is built, uploaded and read back. |
| B3 | Check every video: hidden, deterministic title, one video per part, captions verified. There should be no `SUBMISSION_UNCERTAIN` or cleanup alerts. |

**Limits:**
- There is no per-lecture allowlist, so Phase B covers every eligible 21 Oct part (at most the 7 VERIFIED lectures). `MAX_IMPORTS_IN_FLIGHT=1` paces them one at a time.
- Restricting it to a single lecture first would need a small allowlist option, which is a code change needing separate approval.

## 5. Rollback

| What | How |
|---|---|
| Stop | Stop running `run --once`. No service exists to stop. |
| Branch data | Delete the Neon branch. Production never received a publisher write. |
| Phase B videos | They stay hidden in staging library 786045, and their ids are recorded in the branch. Delete them only on an explicit operator decision. |
| Configuration | Clear `PUBLISHER_DATABASE_URL` and the `BUNNY_*` values in the local file. |
