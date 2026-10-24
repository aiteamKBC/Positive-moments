# Bunny library 786045: security checklist for learner delivery

**Who:** the Bunny account owner (Kent), together with the LMS backend team. **When:** before anything is released to learners.

**Rules for this work:**
- Nothing here is changed automatically. The publisher only *reads* these settings (`security-check`), and refuses to release a lecture while any required item fails.
- Don't change a setting while recordings are being watched through an existing embed. Plan a short window.

Bunny references (read 24 Oct 2026):
- [Embed view token authentication](https://bunny.net/docs/stream-embed-token-authentication)
- [Stream security](https://bunny.net/docs/stream/security)
- [Security options](https://bunny.net/docs/stream/security-options)
- [CDN advanced token authentication](https://bunny.net/docs/cdn/security/token-authentication/advanced)
- [Get Pull Zone API](https://bunny.net/docs/api-reference/core/pull-zone/get-pull-zone)

## Current state (read-only, 24 Oct 2026)

| Setting | API field | Now | Required |
|---|---|---|---|
| Embed view token authentication | `PlayerTokenAuthenticationEnabled` | **false** | **true** |
| Direct play page | `AllowDirectPlay` | **true** | **false** |
| Block requests without a referrer | `BlockNoneReferrer` | true | true |
| Allowed referrers | `AllowedReferrers` | 1 entry | The LMS learner domains only (agree the list) |
| MP4 fallback | `EnableMP4Fallback` | false | false |
| CDN token authentication (pull zone 6840885) | `ZoneSecurityEnabled` (Get Pull Zone) | not yet read | **true** |
| MediaCage / DRM | `EnableDRM` | false | Optional (business decision) |
| Keep original files | `KeepOriginalFiles` | true | Business decision (storage cost against the ability to re-encode) |
| Encoding tier / AI transcription | `EncodingTier` / `EnableTranscribing` | Free / off | Unchanged (the publisher refuses anything else at start-up) |

## Steps

1. [ ] **Agree the referrer list** with the LMS team: every domain that serves the player iframe, and nothing else.
2. [ ] **Set the Allowed domains** in library 786045 > Security. Keep *Block requests without a referrer* on.
   - Bunny warns that Apple devices may not send referrer headers. Test Safari on iOS in step 9.
3. [ ] **Copy the token authentication key** (Security > *Token authentication key* card) into the LMS backend's secret store through a secure channel.
   - It's labelled "API key", but it's a different value from the library API key.
   - Don't put it in email, chat or a ticket.
4. [ ] **Turn on embed view token authentication.**
   - From this moment, every embed without a valid `token` and `expires` answers 403. Nothing is released yet, so learners are unaffected.
5. [ ] **Turn off direct play.**
6. [ ] **Turn on CDN token authentication** on the library's pull zone (*Stream > library > API > Pull Zone > Manage*).
   - Bunny states it uses the same key as the embed token.
7. [ ] **Give the publisher what it needs to keep verifying captions.** Add these to the publisher's env file in the activation step (see the [runbook](../../deploy/DELIVERY_ROLLOUT.md)):
   - `BUNNY_PRODUCTION_CDN_HOSTNAME` (the pull zone hostname);
   - `BUNNY_CDN_TOKEN_KEY` (the pull zone token key).
8. [ ] **Run the read-only check on the VPS:**
   ```bash
   sudo -u www-data backend/venv/bin/python -m app.cli.publisher --env-file backend/config/publisher.env \
        security-check --caption-readback-video-id <one existing video GUID>
   ```
   **Expected:** `"secure": true`, `"problems": []`, and `"caption_readback": {"status": 200, "signed": true, "is_webvtt": true}`.

   **A `403` here** means the publisher's CDN token signing doesn't match the zone. Don't release anything. New captions would stay `CAPTION_CONTENT_UNVERIFIED` until it's fixed.
9. [ ] **Test playback for staff** through the LMS integration on one released lecture. Check:
   - **Player:** a signed embed plays; an unsigned or expired one gets a 403; a copied URL stops working after `expires`.
   - **Captions:** the caption track appears in the player while CDN token authentication is on. Bunny's documentation doesn't say how the embed player fetches captions under CDN tokens, so this must be checked live.
   - **Browsers:** desktop Chrome/Edge, Safari on macOS, and Safari on iOS (referrer behaviour).
   - **Referrer:** an iframe on a non-allowed domain is refused.

## Ongoing

- **Weekly:** rerun `security-check`. Anyone who turns a protection off makes it fail, and from then on every `release` is refused.
- **Rotation:** rotate the token authentication key on staff changes, and at least yearly. The LMS updates its signing key in the same window; existing embeds expire within their 5–15 minute lifetime.
