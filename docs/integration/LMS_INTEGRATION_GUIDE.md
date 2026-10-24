# LMS Backend Integration Guide: lecture recordings from Bunny Stream

**For:** the Kent LMS backend team. **Contract:** v1 ([OpenAPI](../api/publisher_lms_v1.openapi.yaml)). **Status:** deployed in production; seven verified lectures released for LMS integration testing (see *Start here*).

## Start here: integration-testing status (24 Oct 2026)

**Base URL (HTTPS only):** `https://positive-moments.kentbusinesscollege.net/api/publisher/v1`
- Plain `http://` requests to `/api/publisher/` are refused by the web server (403) before they reach the API.
- Never send the service token over HTTP. A token that was sent over HTTP even once must be rotated.

**Released for integration testing:** these seven occurrences are `READY`, `RELEASED` and `playback.available: true`. Each has one published part, in library 786045, with a verified English caption track.

| LMS occurrence | Lecture date |
|---|---|
| `OCC-4493AC65DF5041459BC5A14DA8B357A0` | 23 Oct 2026 |
| `OCC-188B392FF4ED45A9B46C02E40072A1F9` | 23 Oct 2026 |
| `OCC-774CB73AB0094A03A13F87E30B9FF4C0` | 23 Oct 2026 (Teams part 1 excluded; see §4) |
| `OCC-EB064C5DE7B44E119E67F128896C592B` | 23 Oct 2026 |
| `OCC-5FDA481E5B1145398AA108E04D19F9A4` | 23 Oct 2026 |
| `OCC-075C8136DD8546FD834CC6B0E4B7DE18` | 23 Oct 2026 |
| `OCC-B8C49F1C2ED4429786178B5914ED7368` | 23 Oct 2026 |

Every other occurrence stays unavailable until Kent releases it. New lectures are imported and verified automatically, but never released automatically.

**What `playback.available: true` means:** Kent's side is finished. The recording is verified, released and delivered from a library that passes the security check.

**What it does not mean:** that any particular person may watch it. The API is server to server and knows nothing about learners. **Learner login, enrolment and entitlement checks are entirely the LMS's responsibility**, and they come before every signature (see *Learner access control* below). Nothing in the Publisher bypasses or replaces them.

**Still the LMS's to do before learners see a recording:**
1. Enforce login and enrolment, then sign server side (§5, *Secrets*).
2. Run the full-length staff browser acceptance (§6, *Live checks*). Kent has verified the token behaviour at the HTTP level:
   - an unsigned, expired or wrong-video token gets 403;
   - a correctly signed URL from the LMS domain gets 200;
   - a correct signature from another domain, or with no referrer, gets 403.

   **No real browser has played a full lecture yet.**
3. Answer §10.

## 1. What changes for learners

Bunny Stream becomes the primary playback source for recorded live sessions. Teams remains the place lectures are recorded, and the fallback while a Bunny recording isn't available.

**Where it lives:**
- **Library:** every recording is delivered from Bunny library **786045**.
- **Captions:** each part has an English caption track, verified against the transcript.
- **Hidden until released:** a recording reaches learners only after it's *released*, and only while Kent's global delivery switch is on.
  - The switch was turned on on 24 Oct 2026 for the seven lectures above.
  - Kent can withdraw any lecture immediately, or turn the switch off for all lectures.

## 2. Who does what

| Concern | Publisher (Kent) | LMS backend (you) |
|---|---|---|
| Which recording belongs to which occurrence | Owns it, and verifies it | Asks by occurrence ID; never parses titles |
| Video and caption quality, readiness | Owns it, computed live | Trusts `state` and `playback.available` |
| Multi-part order and excluded fragments | Owns it (`sequence`) | Shows the parts in `sequence` order |
| Release and withdrawal | Owns it (audited) | Decides *when* to show a released recording |
| Learner login, enrolment, entitlement | Never | **Owns it**, and checks on every playback |
| Signing the Bunny embed URL | Never (holds no learner context) | **Owns it**, using the library's token authentication key |
| Viewing analytics | — | **Owns it**, keyed on your `artifact_id` (see §7) |
| Bunny management (API) keys | Only holder | Never needs them |

## 3. Calling the API

```
GET {base}/api/publisher/v1/occurrences/{lms_occurrence_id}/recording
Authorization: Bearer <PUBLISHER_SERVICE_TOKEN>
```

`{base}` is `https://positive-moments.kentbusinesscollege.net`.
- **HTTPS only:** certificate verification on, and never `http://`.

**The ID:**
- `lms_occurrence_id` is your `curriculum.live_session_occurrences.id`, e.g. `OCC-4493AC65DF5041459BC5A14DA8B357A0`.
- Look up by occurrence, never by `live_session_id`: a live session is a weekly series, and an occurrence is one lecture.

**The service token:**
- It's issued to the LMS backend only. Keep it server side, never in a browser or mobile app.
- Two tokens can be valid at once, so you can rotate without downtime.

**Responses:**

| Status | Meaning | What the LMS does |
|---|---|---|
| `200` | The publisher knows the occurrence | Read `playback.available` |
| `404 OCCURRENCE_NOT_FOUND` | Never seen, e.g. a session that didn't run in Teams | Fall back to Teams / "not available" |
| `400 INVALID_*` | Malformed ID or parameter | Fix the caller |
| `401` | Bad or missing token | Alert; don't retry in a loop |
| `429` | Rate limit (default 600/min per token) | Back off; honour `Retry-After` |
| `503 NOT_CONFIGURED` / `PLATFORM_UNAVAILABLE` | Temporary | Retry with backoff; treat as not playable |

Treat every response as `Cache-Control: no-store`. The API sends `no-store`, but the web server currently rewrites it to `no-cache` on the way out. If you cache answers, keep them for at most a few minutes, and always re-ask at playback time. A withdrawal must take effect immediately.

## 4. Reading the answer

```json
{
  "contract_version": "1",
  "lms_occurrence_id": "OCC-774CB73AB0094A03A13F87E30B9FF4C0",
  "lms_live_session_id": "LIVE-BC8E097D84364A6F9AB59C64E20B06CD",
  "lms_module_catalogue_id": "MOD-20260921085217095815146F2085A499",
  "lms_session_number": 3,
  "link_role": "PRIMARY",
  "state": "READY",
  "blocking": [],
  "updated_at": "2026-10-23T14:53:53.215792Z",
  "lecture": {
    "lecture_id": "3f09b150-1bdf-5f4f-82b3-eb8d254cfa8c",
    "title": "AI In Marketing",
    "session_date": "2026-10-23",
    "scheduled_start": "2026-10-23T08:00:00Z",
    "scheduled_end": "2026-10-23T10:00:00Z",
    "lms_match": {"status": "VERIFIED", "rule_version": "meeting_key_start_v2"}
  },
  "recording": {
    "teams_part_count": 2,
    "published_part_count": 1,
    "total_duration_seconds": 7653,
    "parts": [{
      "sequence": 1, "source_part_index": 2, "classification": "FULL",
      "teams_recording_id": "<Graph callRecording id>",
      "recorded_start": "2026-10-23T07:55:30.484072Z", "recorded_end": "2026-10-23T10:03:04.164072Z",
      "duration_seconds": 7653,
      "bunny": {"library_id": 786045, "video_id": "456d51cf-…"},
      "encoding": "VERIFIED", "resolutions": ["480p", "720p", "1080p"], "visibility": "HIDDEN",
      "captions": [{"language": "en", "label": "English", "status": "VERIFIED", "version": 1, "content_verified": true}]
    }],
    "excluded_parts": [{"source_part_index": 1, "reason": "OPERATOR_EXCLUDED"}]
  },
  "release": {"status": "NOT_RELEASED", "released_at": null, "withdrawn_at": null},
  "playback": {"available": false, "reason": "NOT_RELEASED", "library_id": 786045,
               "player": "BUNNY_STREAM_EMBED", "token_authentication_required": true}
}
```

**Rules:**

1. **`playback.available` is the only switch.**
   - It's `true` only when all three hold: `state = READY`, `release.status = RELEASED`, and Kent has turned learner delivery on.
   - In every other case, don't embed anything, even if a `video_id` is present.
2. **`state`** tells you why:

   | State | Meaning |
   |---|---|
   | `READY` | Verified |
   | `PROCESSING` | Still being recorded, imported or captioned (normally within a few hours of the lecture's end) |
   | `REVIEW_REQUIRED` | A Kent operator must decide something |
   | `BLOCKED` | A duplicate calendar entry, or nothing to publish |
   | `WITHDRAWN` | Pulled by Kent |
   | `NOT_DISCOVERED` | No recording found for this session (yet) |
   | `NOT_APPLICABLE` | Cancelled or test |

   `blocking` lists the machine-readable reasons; show them only to staff.
3. **Play parts in `sequence` order**, from 1 to `published_part_count`.
   - `source_part_index` is the Teams recording number. It can have gaps: above, Teams part 1 was a 71-second false start that a Kent operator excluded.
   - Show "Part n of N" from `sequence` and `published_part_count`.
4. **`link_role = SHARED_MODULE`** means the LMS shares this Teams meeting between modules. The same recording can answer several occurrences; check enrolment for the occurrence the learner asked about.
5. **Never store a `video_id` without its `library_id`.**

## 5. Signing playback (the LMS's job)

Bunny embed token authentication, from [Bunny's documentation](https://bunny.net/docs/stream-embed-token-authentication):

```
token   = SHA256_HEX(token_security_key + video_id + expires)
expires = UNIX timestamp in seconds (milliseconds are not supported)
URL     = https://player.mediadelivery.net/embed/{library_id}/{video_id}?token={token}&expires={expires}
```

- **The key:** `token_security_key` is on library 786045's **Security** page, in the **Token authentication key** card. Bunny labels the field "API key", but it is **not** the library API key. Kent hands it over through a secure channel. It's the LMS's to hold, and it never leaves your servers.
- **Lifetime:** sign one URL per part, per request, valid for 5–15 minutes, and only after the enrolment check passes.
- **Referrer:** the iframe must be served from `lms.kentbusinesscollege.org`, the only allowed domain. Requests without a referrer are blocked, subject to browser testing.
- **Host:** always embed via `player.mediadelivery.net`, never the legacy `iframe.mediadelivery.net`.
- **Keep GUIDs confidential:** don't log `video_id` in the browser and don't put it in shareable links.
- **Known risk while CDN token authentication is deferred:** the embed token protects only the *player page*. The files behind it are protected by the referrer rule alone.
- **What a leaked GUID gives:** anyone with the GUID who forges a `Referer: https://lms.kentbusinesscollege.org/` header can fetch the HLS stream and the **original uploaded file**.
- **Captions** are readable with the GUID alone.
- **Where GUIDs leak:** an enrolled learner can see a GUID in their own browser's network tab.
- **Mitigations:** treat GUIDs as confidential, and see the [security checklist](BUNNY_786045_SECURITY_CHECKLIST.md#residual-risk-while-cdn-token-authentication-is-deferred) for options; turning off *Keep original files* and moving to CDN tokens later are Kent decisions.

**Working examples** (placeholders only): [Python](examples/lms_playback_example.py) · [Node.js](examples/lms_playback_example.mjs).

Both examples do the same four steps:
1. Check enrolment.
2. Call the publisher API.
3. Refuse unless `playback.available` is true.
4. Sign each part.

## Secrets: what you receive and how to hold them

You receive two secrets. They are unrelated, and neither ever reaches a browser, a mobile app, a log line or Git.

| | **Publisher API service token** | **Bunny embed signing key** |
|---|---|---|
| What it does | Lets your backend call `/api/publisher/v1/*` (`Authorization: Bearer …`) | Signs player URLs: `token = SHA256_HEX(key + video_id + expires)` |
| Who issues it | Kent (Positive Moments platform) | The Bunny account owner (Kent): library 786045 > Security > *Token authentication key* |
| What it can do if leaked | Read recording metadata for every occurrence (no video access, no write access) | Mint playable URLs for **any** video in library 786045, from the LMS domain |
| Where it lives at Kent | `PUBLISHER_LMS_SERVICE_TOKENS` in the API's server environment (two may be valid at once) | Bunny. The Publisher never signs embeds. |
| Rotation | Kent adds a second token, you switch, Kent removes the old one: no downtime | Kent regenerates it in Bunny and you update it in the **same** window: every URL signed with the old key stops working at once |

**Transfer:**
- **Channel:** one-time, expiring secret-sharing links (or the college password manager's shared vault), one secret per message, to a named recipient on the LMS backend team.
- **Never** use email, Teams/Slack chat, tickets, documents or Git.
- **Confirm receipt** on a separate channel, then delete the share.
- **Exposure:** if either secret is ever pasted somewhere it shouldn't be, tell Kent and rotate it.

**Storage on your side:**
- **Where:** a secret manager or the server environment of the LMS backend only.
- **Never:** in front-end bundles, mobile apps, client-side config, analytics or error-reporting payloads.
- **Logging:** redact the `Authorization` header and the `token=` query parameter in your HTTP client and access logs.

**Generating a signed URL (server side, after the enrolment check):**
```python
import hashlib, time
expires = int(time.time()) + 600                       # 10 minutes; seconds, not milliseconds
token = hashlib.sha256(f"{SIGNING_KEY}{video_id}{expires}".encode()).hexdigest()
url = (f"https://player.mediadelivery.net/embed/786045/{video_id}"
       f"?token={token}&expires={expires}&captions=en")  # + &t=<seconds> to resume
```
Return `url` only inside the page you render for that learner on `lms.kentbusinesscollege.org`. Never cache it or reuse it for another learner. The full flow is in the [Python](examples/lms_playback_example.py) and [Node.js](examples/lms_playback_example.mjs) examples.

**Token expiry, multi-hour lectures, reloads and captions:** see §6. In short:
- sign per page view;
- the loaded player is expected to keep playing past `expires` (to be confirmed in the staff checks);
- every reload or next part needs a fresh URL, using `t=` to resume;
- add `captions=en` to show the English track by default.

## Learner access control (the LMS's responsibility)

Before signing, on **every** playback request, the LMS backend must:
1. **Authenticate** the learner through the LMS's normal login. Never serve the player page to an anonymous session.
2. **Check enrolment:** the learner must be enrolled on the module and cohort that owns *this* `lms_occurrence_id`. For `link_role = SHARED_MODULE`, check the occurrence the learner asked about, not the recording.
3. **Ask the Publisher** for that occurrence and refuse unless `playback.available` is `true`.
4. **Sign** a short-lived URL per part and render it in the learner's page.

**Kent's controls are not a substitute:**
- **What they cover:** release and the global switch decide whether a recording *can* be shown; the referrer rule and the embed token decide *where* and *how long* a URL works.
- **What they don't cover:** none of them knows who the learner is.

## 6. Long playback: pause, seek, resume and token expiry

Lectures run up to about 2 h 15 min, and a token lives for 5–15 minutes.

**What Bunny documents:** the iframe URL must carry a valid `token` / `expires` pair. Its documentation doesn't say whether anything *after* the iframe has loaded (playlists, segments, captions) checks the token.

**Expected in this rollout:**
- CDN token authentication is off, so the loaded player's file requests are checked only against the referrer. Pause, seek and resume inside an already-loaded player should therefore keep working after `expires`.
- Anything that **reloads the iframe** needs a **new** signed URL: a page refresh, opening the next part, coming back later, or a browser discarding the tab.

**Status:** the seven lectures are released for integration testing, but this behaviour **has not yet been confirmed in a real browser**. The live checks below are the LMS team's staff acceptance, and must pass before learners are given access.

**What the LMS should do:**
- **Sign when the page is rendered**, never ahead of time, and never cache a signed URL.
- **Resume from a saved position** with a fresh URL plus Bunny's documented start-time parameter `t`, e.g. `&t=3725` for 1:02:05. Optionally use `rememberPosition=true`, which uses the browser's own memory.
- **Default captions** with `captions=en`.
- **For multi-part lectures**, sign each part's URL when that part is shown.
- **If live testing shows playback stops after `expires`:**
  - **either** re-render the iframe with a new URL and `t` = the current position (from your viewing heartbeat);
  - **or**, as a fallback, raise the token lifetime to cover the lecture plus a margin (e.g. 3 h). This makes leaked URLs usable for longer; decide with Kent.

### Live checks (staff, before learners get access)

Use a 10-minute token on one released lecture over 2 h long, e.g. `OCC-774CB73AB0094A03A13F87E30B9FF4C0` (2 h 8 min). Embed it on a staff-only page of `lms.kentbusinesscollege.org`. Record the browser, device, token TTL and result for each.

| # | Scenario | Expected |
|---|---|---|
| 1 | Play continuously for 20 min (past `expires`) | Keeps playing |
| 2 | Pause 30 min (past `expires`), then resume | Resumes |
| 3 | After expiry, seek to the last 5 minutes | Plays from there |
| 4 | After expiry, seek back to 0:00 | Plays from there |
| 5 | Captions on (`captions=en`), seek several times | Captions stay in sync |
| 6 | Reload the page with the **old** URL after expiry | 403; the LMS's freshly rendered URL plays |
| 7 | Resume next day from the saved position (`t=`) | New URL, starts at the position |
| 8 | Part 1 to the end, then open part 2 | Part 2 signed on open; plays |
| 9 | Laptop asleep / tab in background for 1 h, then return | Continues, or recovers on reload with a new URL |
| 10 | Wi-Fi → mobile data mid-lecture | Continues after a short buffer |
| 11 | Copy the signed URL into a page on another domain | 403 (referrer) |
| 12 | Open the signed URL with no referrer (address bar) | 403 (no-referrer block) |
| 13 | Browsers | Chrome, Edge, Firefox, Safari (macOS), **Safari (iOS)**, Chrome (Android): all play when embedded on `lms.kentbusinesscollege.org` |

If #13 fails on Safari for iOS because of the no-referrer block, Kent can relax that one protection (`PUBLISHER_REQUIRE_BLOCK_NONE_REFERRER=false`, plus the library setting). That is a recorded policy decision, not a code change.

## 7. Keeping your viewing analytics

`parts[].teams_recording_id` is the Microsoft Graph recording ID. It equals `curriculum.live_session_artifacts.graph_artifact_id`.

- **Verified:** on 24 Oct 2026, all 7 published lectures matched exactly, and the call ID and content correlation ID matched too.
- **Effect:** you can keep `live_session_recording_views` keyed on your own `artifact_id` and just change the player.
- **Not every Teams recording becomes a Bunny part:** excluded fragments don't.

## 8. Incremental sync (optional)

```
GET {base}/api/publisher/v1/occurrences?changed_since=2026-10-24T08:00:00Z&limit=50
```

**Ordering:** items come oldest change first. Follow `next_cursor` while `has_more` is true.

**Delivery is at least once:**
- An occurrence appears again whenever anything it is computed from changes. Upsert by `lms_occurrence_id`.
- For the next run, pass the newest `updated_at` you stored as `changed_since`, minus a 2-minute overlap.

**The feed is for listings, not for playback:** use it to keep "recording available" badges current. Playback must still re-ask the single-occurrence route.

## 9. Health and support

- **Health check:** `GET {base}/api/publisher/v1/health` returns the contract version, library, whether playback is switched on, and database status. It needs the same token.
- **Logging:** every request writes one access-log line (route, status, duration, token fingerprint, occurrence ID). Quote the occurrence ID when you report a problem.
- **Contract changes:** v1 only gains optional fields. A breaking change gets `/v2/`.

## 10. Before go-live, please confirm

1. Will Bunny replace Teams playback, with Teams as the fallback while a recording isn't `available`? (Decided by Kent; confirm the LMS rollout plan.)
2. Is `lms.kentbusinesscollege.org` the only domain that serves the player (web *and* any mobile app web views)?
3. Where does the LMS backend run? This is for an optional IP allowlist at Kent's proxy.
4. Who on your team receives the service token and the signing key (one named person each; see *Secrets*)?
5. How does enrolment resolve to an occurrence, including shared-module cohorts?
6. Are multi-part lectures shown as a playlist, or one after another in a single player?
