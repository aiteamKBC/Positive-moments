# Production video delivery: deployment, activation and rollback

**Scope:**
- Bunny library **786045** becomes Kent's permanent delivery library. It is adopted in place.
- The LMS delivery API (`/api/publisher/v1/`).
- Audited RELEASE / WITHDRAW.

**Pre-conditions:**
- Every phase below needs **explicit approval**. Nothing here has been run.
- **No database migration** is needed:
  - release state uses the existing `bunny_videos.visibility_state` column and its CHECK (migration 024, applied 2026-10-23);
  - the audit trail uses `bunny_publisher_events` (026).
- **No re-import:** the 7 existing videos, their captions and every ID stay as they are.

**CDN token authentication is deferred for this rollout** (Kent decision, 24 Oct 2026). It is reported, never required, and the publisher needs no CDN key. The residual risk is documented in the checklist.

**Gates that keep learners out**, each sufficient on its own:
1. No lecture is RELEASED until an operator runs `release`. `release` refuses while the library fails `security-check`.
2. `PUBLISHER_LMS_PLAYBACK_ENABLED=false` in the Django environment makes `playback.available` false for every answer.
3. Until the LMS backend implements signing and the referrer list includes its domains, nothing embeds.

## Phase A: deploy the code (no behaviour change)

1. **Workstation:** push the reviewed commit: `git push origin release/positive-moments-media`.
2. **VPS:** `cd /var/www/Positive-moments && git pull --ff-only && git rev-parse --short HEAD`.
3. **Leave `bunny-publisher.service` running.**
   - It keeps the code it loaded.
   - Its next (later, approved) restart picks up the new code. In `STAGING_WRITE` that code behaves exactly as before:
     - the read-back host is still `BUNNY_STAGING_CDN_HOSTNAME`;
     - no new setting is required.
4. **Django environment** (the web service's env file, *not* `publisher.env`). Add:
   ```
   PUBLISHER_LMS_SERVICE_TOKENS=<one random token, >= 32 characters, e.g. `python -c "import secrets;print(secrets.token_urlsafe(48))"`>
   PUBLISHER_LMS_LIBRARY_ID=786045
   PUBLISHER_LMS_PLAYBACK_ENABLED=false
   # optional: PUBLISHER_LMS_RATE=600/min
   ```
   Confirm that the web service's `DATABASE_URL` is the AiTeamKBC database the publisher writes to. The existing `/api/operations/publisher/*` routes already read it.
5. **Restart the Django web service only** (the unit that serves `/api/`), in an approved window.
6. **Verify** (from the VPS; the token is read from a file, never pasted into a shell history):
   ```bash
   T=$(cat /root/lms_token.txt)
   curl -s -H "Authorization: Bearer $T" https://<host>/api/publisher/v1/health
   curl -s -H "Authorization: Bearer $T" https://<host>/api/publisher/v1/occurrences/OCC-4493AC65DF5041459BC5A14DA8B357A0/recording
   curl -s -o /dev/null -w "%{http_code}\n" https://<host>/api/publisher/v1/health        # 401
   ```
   **Expected:**
   - `health`: `"status": "ok"`, `"library_id": 786045`, `"playback_enabled": false`.
   - The occurrence: `"state": "READY"`, `"release": {"status": "NOT_RELEASED"}`, `"playback": {"available": false, "reason": "NOT_RELEASED"}`.
7. **Hand over to the LMS team:** the token (through a secure channel), the base URL, [the guide](../docs/integration/LMS_INTEGRATION_GUIDE.md) and [the OpenAPI spec](../docs/api/publisher_lms_v1.openapi.yaml).

**Rollback for Phase A:**
- Remove `PUBLISHER_LMS_SERVICE_TOKENS`, so every call answers 401.
- Or `git checkout <previous revision>`, then restart the web service.
- The publisher is unaffected either way.

## Phase B: secure library 786045 (Bunny account owner)

Follow [the security checklist](../docs/integration/BUNNY_786045_SECURITY_CHECKLIST.md), steps 1–6:
- embed token authentication **on**;
- direct play **off**;
- allowed domain exactly `lms.kentbusinesscollege.org`;
- block requests without a referrer **on**;
- CDN token authentication **left off** (deferred);
- DRM off.

Learners are unaffected, because nothing is released. Read the checklist's *Residual risk* section before Phase D.

**Rollback for Phase B:** revert the setting in the Bunny dashboard. This only re-opens access, and nothing is released.

## Phase C: switch the publisher to production mode on the same library

**Scripted path (preferred):** [`deploy/activate_phase_c.sh`](activate_phase_c.sh) performs steps 1–5 below with gates. Run it as root on the VPS:
```bash
cd /var/www/Positive-moments && git pull --ff-only
sudo bash deploy/activate_phase_c.sh check    # pre-flight only: changes nothing, restarts nothing
sudo bash deploy/activate_phase_c.sh apply    # backup, atomic swap, restart bunny-publisher.service only, verify
sudo bash deploy/activate_phase_c.sh verify   # read-only post-restart checks, re-runnable
```
- **Before any change:** it builds a candidate `publisher.env`, keeping every existing line byte for byte, and gates on all of these:
  - `validate-config --check-library`;
  - `security-check` (secure, CDN `DEFERRED`, caption read-back 200 unsigned);
  - an independent HTTP 200 check of one existing caption URL;
  - a baseline of the 7 videos (VERIFIED, HIDDEN, captions verified, no release events).
- **Apply:** it writes a root-only backup to `/root/kbc-phase-c/`, swaps the file with one atomic rename and restarts **only** `bunny-publisher.service`.
- **Verify:** it checks:
  - the daemon started in `PRODUCTION_WRITE` on 786045;
  - fresh scheduler/w0/w1 heartbeats;
  - discovery ran;
  - the same video records and caption publications;
  - no new `IMPORT_VIDEO` / `PUBLISH_CAPTION` job for those lectures;
  - no failed jobs;
  - the QA units' start times are unchanged.
- **Re-runs are idempotent:** an already-applied file and a daemon already in production mode means no backup and no restart.
- **If the restarted unit fails to become active,** the script restores the backup and restarts the unit on the previous file.

The manual steps below are what the script does.

1. **Edit `backend/config/publisher.env`** (root:www-data 0640, `sudoedit`). Keep every existing line and add or change:
   ```
   PUBLISHER_MODE=PRODUCTION_WRITE
   PUBLISHER_PRODUCTION_WRITE_ENABLED=true
   BUNNY_PRODUCTION_LIBRARY_ID=786045
   BUNNY_PRODUCTION_API_KEY=<the same library API key as BUNNY_STAGING_API_KEY>
   BUNNY_PRODUCTION_ACCOUNT_API_KEY=<the same account key as BUNNY_STAGING_ACCOUNT_API_KEY>
   BUNNY_PRODUCTION_CDN_HOSTNAME=<the same host as BUNNY_STAGING_CDN_HOSTNAME>
   PUBLISHER_CDN_TOKEN_AUTH=DEFERRED
   PUBLISHER_APPROVED_REFERRERS=lms.kentbusinesscollege.org
   PUBLISHER_REQUIRE_BLOCK_NONE_REFERRER=true
   ```
   - The last three lines are the defaults; writing them down makes the policy visible.
   - **Do not set `BUNNY_CDN_TOKEN_KEY`:** CDN token authentication is deferred, and the caption read-back stays unsigned.
2. **Check the configuration (read-only):**
   ```bash
   sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env validate-config --check-library
   ```
   **Expected:** `"mode": "PRODUCTION_WRITE"`, `"library_id": 786045`, `"adopts_staging_library": true`, `"problems": []`, `"library_problems": []`.
3. **Check security (read-only):**
   ```bash
   sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env security-check --caption-readback-video-id <GUID>
   ```
   **Expected:**
   - `"secure": true` and `"problems": []`;
   - `"cdn_token_authentication": {"policy": "DEFERRED", "status": "DEFERRED"}`;
   - warnings `CDN_TOKEN_AUTHENTICATION_DEFERRED` and `ORIGINAL_FILES_KEPT`;
   - `"caption_readback": {"status": 200, "signed": false}`.
4. **Restart the publisher gracefully** (approved window): `sudo systemctl restart --no-block bunny-publisher.service`.
5. **Verify:**
   - `status` shows new-PID heartbeats.
   - No new `IMPORT_VIDEO` for the 7 lectures.
   - `bunny_videos` still holds the same 7 GUIDs.
   - Exactly 7 `LECTURE_FULLY_READY` events in total.
   - The next new lecture runs end to end, and its caption `readback.content_check` is `IDENTICAL` / `EQUIVALENT`.

   Tested locally: `test_production_mode_adopts_existing_videos_and_still_processes_new_lectures`.

**Rollback for Phase C:** two steps, both required:
```bash
install -m 0640 -o root -g www-data /root/kbc-phase-c/publisher.env.before-phase-c.<timestamp> /var/www/Positive-moments/backend/config/publisher.env
systemctl restart --no-block bunny-publisher.service
```
- **Restoring the file alone changes nothing.** systemd reads the `EnvironmentFile` only when the unit starts, so the running daemon stays in `PRODUCTION_WRITE` until it is restarted.
- **No data rollback is needed.** Every row is keyed by library, not by mode.

## Phase D: release, then turn learner playback on

**Integration-testing variant (Kent decision, 24 Oct 2026):** release the seven verified lectures and turn the playback switch on, so the LMS team can integrate and test signed playback.
- Learners still reach nothing until the LMS enforces login and enrolment and signs embeds.
- The full-length staff browser acceptance (step 1 below) moves to the LMS team, before learner access.

Run as root on the VPS, in this order:
```bash
cd /var/www/Positive-moments && git pull --ff-only
sudo bash deploy/harden_api_https.sh inspect     # read-only: vhost, config file, listeners, current answers
sudo bash deploy/harden_api_https.sh apply       # HTTP /api/publisher/ -> 403 at the web server; HTTP pages -> 301 HTTPS
sudo bash deploy/activate_phase_d.sh check       # read-only: the seven READY, verified, same video IDs, library secure
sudo bash deploy/activate_phase_d.sh apply "<your name>"   # release the seven, playback flag on, restart Django only, verify
```
- `activate_phase_d.sh apply` refuses while plain HTTP still reaches the Publisher API.
- It uses only the audited `release` command, which re-checks verification and library security per lecture.
- It restarts only `positive-moments.service`, never the publisher or the QA units.
- Each script prints its own rollback.
- `activate_phase_d.sh verify` re-runs the public HTTPS checks at any time.

1. **Staff playback test** on one lecture: checklist steps 8–9, and every long-playback live check in [the LMS guide, §6](../docs/integration/LMS_INTEGRATION_GUIDE.md#6-long-playback-pause-seek-resume-and-token-expiry):
   ```bash
   sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env \
        release --lecture-id <lecture uuid> --actor "<name>" --reason "<why>"
   ```
   - `release` refuses with `LIBRARY_NOT_SECURE`, `NOT_VERIFIED` or `NOT_PRODUCTION_MODE` when anything is missing.
   - Repeating it changes nothing.
2. **Release the remaining verified lectures**, one command each. Use `delivery --occurrence-id` to inspect any lecture first.
3. **Set `PUBLISHER_LMS_PLAYBACK_ENABLED=true`** in the Django environment, then restart the web service. From then on, a released, READY lecture answers `"playback": {"available": true}`.

**Withdraw at any time** (immediate; no restart). Either:
```bash
sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env \
     withdraw --lecture-id <lecture uuid> --actor "<name>" --reason "<why>"
```
or `POST /api/operations/publisher/lectures/<lecture uuid>/withdraw/` with `{"reason": "..."}` (staff token).

**Rollback for Phase D:** either of these works:
- `PUBLISHER_LMS_PLAYBACK_ENABLED=false` and restart the web service: every answer becomes unavailable.
- Withdraw the affected lectures: immediate, per lecture.

Nothing is deleted from Bunny or the database in any rollback.

## Automatic release of new lectures (optional, off by default)

**What it does:** the publisher's scheduler gains an `AUTO_RELEASE` task. It runs every discovery interval (5 min), right after discovery, inside `bunny-publisher.service`; there is no new service.

**A lecture is released automatically only when all of these hold:**
- it is dated on or after `PUBLISHER_AUTO_RELEASE_FROM`;
- it has a current link to an LMS occurrence, and its LMS match is VERIFIED;
- its live state is READY: verified video, plus a verified and content-checked English caption, for every included part;
- it is not a duplicate, not cancelled and not under review;
- every part is still HIDDEN, and it has never been released or withdrawn (re-checked inside `release()`'s lock);
- library 786045 passes the live security check.

The release goes through the same audited `release()` as the operator command, recorded with actor `publisher:auto-release`.

**Never released automatically:**
- unmatched, duplicate, incomplete or unverified lectures;
- lectures before the start date;
- withdrawn lectures: an operator's withdrawal is never undone;
- a lecture that gained a new part after an earlier release: an operator decides, as before;
- the orphan Bunny video, which has no publisher record.

**Failures:** a temporary failure (database, the security read, a busy lecture) releases nothing and is retried on the next run. If the library is not secure, the task records `FAILED` / `LIBRARY_NOT_SECURE` and releases nothing.

**To turn it on** (needs approval; deploy the code first):
1. Add these to `backend/config/publisher.env`:
   ```
   PUBLISHER_AUTO_RELEASE=true
   PUBLISHER_AUTO_RELEASE_FROM=<first lecture day to release automatically, YYYY-MM-DD>
   ```
2. Check the configuration: `validate-config --check-library` must report `"auto_release": true` and `"problems": []`.
3. Restart the publisher: `systemctl restart --no-block bunny-publisher.service`.
4. Watch the result: `status` shows the `AUTO_RELEASE` schedule row, and each automatic release writes one `PUBLICATION_RELEASED` event by `publisher:auto-release`.

**To turn it off:** set `PUBLISHER_AUTO_RELEASE=false`, then restart the publisher. Lectures already released stay released; withdraw any of them per lecture if needed.

## Never, in any phase

- Re-import or delete a Bunny video, or switch to another library. Library 785080 is refused in code.
- Restore Neon, or run or roll back migrations.
- Touch the QA units (`positive-moments-scheduler`, `-backfill`, `-media`).
