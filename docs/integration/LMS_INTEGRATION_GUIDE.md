# LMS Backend Integration Guide: lecture recordings from Bunny Stream

**For:** the Kent LMS backend team. **Contract:** v1 ([OpenAPI](../api/publisher_lms_v1.openapi.yaml)). **Status:** implemented and tested on a local copy; not yet deployed (see [the rollout runbook](../../deploy/DELIVERY_ROLLOUT.md)).

## 1. What changes for learners

Bunny Stream becomes the primary playback source for recorded live sessions. Teams remains the place lectures are recorded, and the fallback while a Bunny recording isn't available.

**Where it lives:**
- **Library:** every recording is delivered from Bunny library **786045**.
- **Captions:** each part has an English caption track, verified against the transcript.
- **Hidden until released:** a recording reaches learners only after it's *released*. Learner playback is also switched off globally until Kent turns it on.

## 2. Who does what

| Concern | Publisher (Kent) | LMS backend (you) |
|---|---|---|
| Which recording belongs to which occurrence | Owns it, and verifies it | Asks by occurrence ID; never parses titles |
| Video and caption quality, readiness | Owns it, computed live | Trusts `state` and `playback.available` |
| Multi-part order and excluded fragments | Owns it (`sequence`) | Shows the parts in `sequence` order |
| Release and withdrawal | Owns it (audited) | Decides *when* to show a released recording |
| Learner login, enrolment, entitlement | Never | **Owns it**, and checks on every playback |
| Signing the Bunny embed URL | Never (holds no learner context) | **Owns it**, using the library's token authentication key |
| Viewing analytics | — | **Owns it**, keyed on your `artifact_id` (see §6) |
| Bunny management (API) keys | Only holder | Never needs them |

## 3. Calling the API

```
GET {base}/api/publisher/v1/occurrences/{lms_occurrence_id}/recording
Authorization: Bearer <PUBLISHER_SERVICE_TOKEN>
```

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

Every response is `Cache-Control: no-store`. If you cache answers, keep them for at most a few minutes, and always re-ask at playback time. A withdrawal must take effect immediately.

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
- **Referrer:** the iframe must be served from a domain on the library's allowed list (agree the list with Kent).

**Working examples** (placeholders only): [Python](examples/lms_playback_example.py) · [Node.js](examples/lms_playback_example.mjs).

Both examples do the same four steps:
1. Check enrolment.
2. Call the publisher API.
3. Refuse unless `playback.available` is true.
4. Sign each part.

## 6. Keeping your viewing analytics

`parts[].teams_recording_id` is the Microsoft Graph recording ID. It equals `curriculum.live_session_artifacts.graph_artifact_id`.

- **Verified:** on 24 Oct 2026, all 7 published lectures matched exactly, and the call ID and content correlation ID matched too.
- **Effect:** you can keep `live_session_recording_views` keyed on your own `artifact_id` and just change the player.
- **Not every Teams recording becomes a Bunny part:** excluded fragments don't.

## 7. Incremental sync (optional)

```
GET {base}/api/publisher/v1/occurrences?changed_since=2026-10-24T08:00:00Z&limit=50
```

**Ordering:** items come oldest change first. Follow `next_cursor` while `has_more` is true.

**Delivery is at least once:**
- An occurrence appears again whenever anything it is computed from changes. Upsert by `lms_occurrence_id`.
- For the next run, pass the newest `updated_at` you stored as `changed_since`, minus a 2-minute overlap.

**The feed is for listings, not for playback:** use it to keep "recording available" badges current. Playback must still re-ask the single-occurrence route.

## 8. Health and support

- **Health check:** `GET {base}/api/publisher/v1/health` returns the contract version, library, whether playback is switched on, and database status. It needs the same token.
- **Logging:** every request writes one access-log line (route, status, duration, token fingerprint, occurrence ID). Quote the occurrence ID when you report a problem.
- **Contract changes:** v1 only gains optional fields. A breaking change gets `/v2/`.

## 9. Before go-live, please confirm

1. Will Bunny replace Teams playback, with Teams as the fallback while a recording isn't `available`? (Decided by Kent; confirm the LMS rollout plan.)
2. Which LMS domains serve the player? They go on the library's allowed referrer list.
3. Where does the LMS backend run? This is for an optional IP allowlist at Kent's proxy.
4. Who receives the service token and the token authentication key, and how are they rotated?
5. How does enrolment resolve to an occurrence, including shared-module cohorts?
6. Are multi-part lectures shown as a playlist, or one after another in a single player?
