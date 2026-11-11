# Bunny library 786045: security checklist for learner delivery

**Who:** the Bunny account owner (Kent), with the LMS backend team. **When:** before anything is released to learners.

**How the publisher uses these settings:** it only *reads* them (`security-check`), and `release` refuses while any required item fails. Nothing is changed automatically.

## Approved policy for this rollout (Kent decision, 24 Oct 2026)

| Protection | API field | Policy | Release blocker |
|---|---|---|---|
| Embed view token authentication | `PlayerTokenAuthenticationEnabled` | **on** | yes |
| Direct play page | `AllowDirectPlay` | **off** | yes |
| Allowed domains | `AllowedReferrers` | **exactly `lms.kentbusinesscollege.org`** | yes (missing *or* extra domains) |
| Block requests without a referrer | `BlockNoneReferrer` | **on**, subject to browser testing (step 7) | yes, unless relaxed by policy (below) |
| MP4 fallback | `EnableMP4Fallback` | off | yes |
| CDN token authentication (pull zone 6840885) | `ZoneSecurityEnabled` | **DEFERRED: off for this rollout** | no; reported as `DEFERRED` with its risk |
| DRM / MediaCage | `EnableDRM` | off | no (a warning if on) |
| Keep original files | `KeepOriginalFiles` | on today | no (a warning; see residual risk) |

**Publisher settings that express this policy** (in `publisher.env`; the defaults already match the policy):
- `PUBLISHER_CDN_TOKEN_AUTH=DEFERRED` (or `REQUIRED` later)
- `PUBLISHER_APPROVED_REFERRERS=lms.kentbusinesscollege.org`
- `PUBLISHER_REQUIRE_BLOCK_NONE_REFERRER=true`. Set it to `false` only if step 7 shows a supported browser fails, and record that decision.

**While CDN token authentication is `DEFERRED`:**
- The publisher never signs its caption read-back, and **`BUNNY_CDN_TOKEN_KEY` is not needed**; a configured key is ignored and reported as such.
- Caption verification keeps working: the publisher reads its own caption file from the CDN without a token, exactly as it did for the 7 published lectures (all `EQUIVALENT`).

## Steps

1. [ ] **Confirm the allowed domain list is exactly `lms.kentbusinesscollege.org`** (library 786045 > Security > Allowed domains). Today's probe shows that domain is already accepted.
2. [ ] **Keep Block requests without a referrer** on.
3. [ ] **Copy the token authentication key** into the LMS backend's secret store through a secure channel.
   - Find it at Security > *Token authentication key*. It's labelled "API key", but it's a different value from the library API key.
   - Never put it in email, chat or a ticket.
4. [ ] **Turn on embed view token authentication**, in an agreed window.
   - From then on, every embed without a valid `token` and `expires` answers 403.
   - Nothing is released yet, but if Teams-to-Bunny playback is already in use anywhere, coordinate the window.
5. [ ] **Turn off direct play.**
6. [ ] **Leave CDN token authentication off** (deferred). Don't set `BUNNY_CDN_TOKEN_KEY`.
7. [ ] **Run the read-only check on the VPS:**
   ```bash
   sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env \
        security-check --caption-readback-video-id <one existing video GUID>
   ```
   **Expected:**
   - `"secure": true` and `"problems": []`;
   - `"cdn_token_authentication": {"policy": "DEFERRED", "status": "DEFERRED", "enabled": false}`;
   - warnings `CDN_TOKEN_AUTHENTICATION_DEFERRED` and `ORIGINAL_FILES_KEPT`;
   - `"caption_readback": {"status": 200, "signed": false, "is_webvtt": true}`.
8. [ ] **Re-probe the direct routes** (status codes only; see *Residual risk*). Expected after steps 4–5:
   - `player.mediadelivery.net/embed` and `/play` without a token: **403**;
   - `iframe.mediadelivery.net/embed` without a token: **403**.
9. [ ] **Test browsers and long playback with staff**: see [the LMS guide, §6](LMS_INTEGRATION_GUIDE.md#6-long-playback-pause-seek-resume-and-token-expiry). Include the Safari on iOS referrer behaviour.

## Residual risk while CDN token authentication is deferred

**What the embed token protects:** the **player page** only. The video files behind the player are protected by the referrer rule alone.

**Probe of 24 Oct 2026:**
- **Target:** one existing hidden video, `069df826…` on pull zone 6840885.
- **Method:** `HEAD` (or a 1-byte ranged `GET`) requests. Only status codes were recorded; no content was downloaded, and nothing was changed. Embed token authentication was still off at the time.

| Route on the pull zone / player | No referrer | `Referer: https://lms.kentbusinesscollege.org/` | Foreign referrer |
|---|---|---|---|
| `/{id}/playlist.m3u8` (HLS master) | 403 | **200** | 403 |
| `/{id}/720p/video.m3u8` (HLS rendition, segments behind it) | 403 | **200** | 403 |
| `/{id}/thumbnail.jpg` | 403 | **200** | 403 |
| `/{id}/original` (**the uploaded source file**, since `KeepOriginalFiles` is on) | 403 | **200** | 403 |
| `/{id}/captions/en.vtt` | **200** | **200** | **200** |
| `/{id}/play_720p.mp4` (MP4 fallback) | 403 | 404 (no MP4s exist) | 403 |
| `player.mediadelivery.net/embed/786045/{id}` (no token) | 200 | 200 | 403 |
| `player.mediadelivery.net/play/786045/{id}` (no token) | 200 | 200 | 403 |
| `iframe.mediadelivery.net/embed/786045/{id}` (legacy host, no token) | 200 | 200 | **200** |

**What this means:**
1. **The referrer check isn't authentication.** Anyone who sends `Referer: https://lms.kentbusinesscollege.org/` (curl, a browser extension) can fetch the HLS stream and **download the full original file** of any video whose GUID they know.
2. **Learners can see the GUIDs.** The GUID and pull-zone host appear in every embed URL and in the player's network requests, so any enrolled learner with developer tools can find them and share a working recipe.
3. **The embed token doesn't stop that.** It expires; the file routes don't check it.
4. **Caption files are served to anyone** with the GUID, whatever the referrer. Captions carry the lecture's spoken words.
5. **The legacy `iframe.mediadelivery.net` host** answered 200 to a foreign referrer at the HTTP level. Re-probe it after embed token authentication is on (step 8): it must answer 403 without a token. The LMS must embed via `player.mediadelivery.net`.
6. **Not exposed:** MP4 fallback files (setting off; none exist). Requests without a referrer and from foreign sites are refused for everything except captions.

**Mitigations available without CDN token authentication** (business decisions; none applied):
- **Turn off *Keep original files*.** This removes the most valuable download (the full-quality source) for **new** uploads. Existing originals stay until removed, and removing them needs separate approval. The publisher doesn't need originals; the only cost is the ability to re-encode from them.
- **Treat video GUIDs as confidential** in the LMS: never log them client-side, and never put them in shareable links.
- **Later,** move to `PUBLISHER_CDN_TOKEN_AUTH=REQUIRED`: turn on CDN token authentication, set `BUNNY_CDN_TOKEN_KEY`, check playback (the player must still load segments and captions), then restart the publisher. The implementation is present and tested.

## Ongoing

- **Weekly:** rerun `security-check`. If someone turns a required protection off, every `release` is refused.
- **Rotation:** rotate the token authentication key on staff changes, and at least yearly. The LMS updates its signing key in the same window.
