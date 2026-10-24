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

Follow [the security checklist](../docs/integration/BUNNY_786045_SECURITY_CHECKLIST.md), steps 1–6. Learners are unaffected, because nothing is released.

**Rollback for Phase B:** revert the setting in the Bunny dashboard. This only re-opens access, and nothing is released.

## Phase C: switch the publisher to production mode on the same library

1. **Edit `backend/config/publisher.env`** (root:www-data 0640, `sudoedit`). Keep every existing line and add or change:
   ```
   PUBLISHER_MODE=PRODUCTION_WRITE
   PUBLISHER_PRODUCTION_WRITE_ENABLED=true
   BUNNY_PRODUCTION_LIBRARY_ID=786045
   BUNNY_PRODUCTION_API_KEY=<the same library API key as BUNNY_STAGING_API_KEY>
   BUNNY_PRODUCTION_ACCOUNT_API_KEY=<the same account key as BUNNY_STAGING_ACCOUNT_API_KEY>
   BUNNY_PRODUCTION_CDN_HOSTNAME=<the same host as BUNNY_STAGING_CDN_HOSTNAME>
   BUNNY_CDN_TOKEN_KEY=<the pull zone token key; only once CDN token authentication is on>
   ```
2. **Check the configuration (read-only):**
   ```bash
   sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env validate-config --check-library
   ```
   **Expected:** `"mode": "PRODUCTION_WRITE"`, `"library_id": 786045`, `"adopts_staging_library": true`, `"problems": []`, `"library_problems": []`.
3. **Check security (read-only):**
   ```bash
   sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env security-check --caption-readback-video-id <GUID>
   ```
   **Expected:** `"secure": true`, `caption_readback.status` 200.
4. **Restart the publisher gracefully** (approved window): `sudo systemctl restart --no-block bunny-publisher.service`.
5. **Verify:**
   - `status` shows new-PID heartbeats.
   - No new `IMPORT_VIDEO` for the 7 lectures.
   - `bunny_videos` still holds the same 7 GUIDs.
   - Exactly 7 `LECTURE_FULLY_READY` events in total.
   - The next new lecture runs end to end, and its caption `readback.content_check` is `IDENTICAL` / `EQUIVALENT`.

   Tested locally: `test_production_mode_adopts_existing_videos_and_still_processes_new_lectures`.

**Rollback for Phase C:** restore the previous `publisher.env` (`PUBLISHER_MODE=STAGING_WRITE`), then restart. Every row is keyed by library, not by mode, so nothing changes.

## Phase D: release, then turn learner playback on

1. **Staff playback test** (checklist step 9) on one lecture:
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

## Never, in any phase

- Re-import or delete a Bunny video, or switch to another library. Library 785080 is refused in code.
- Restore Neon, or run or roll back migrations.
- Touch the QA units (`positive-moments-scheduler`, `-backfill`, `-media`).
