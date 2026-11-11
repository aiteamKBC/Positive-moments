# Production video delivery: acceptance tests and evidence (24 Oct 2026)

**Where the code ran:**
- **Locally only:** the isolated test database (`kbc_qa_integration_test`, Docker), with fake Graph, LMS and Bunny, and with Django's own test database.
- **Production:** one read-only run of the read model (`default_transaction_read_only=on`). Nothing was written; no Bunny setting was read or changed by the tests.

## Focused automated tests

| Suite | Command | Result |
|---|---|---|
| Delivery integration (test DB) | `pytest tests/integration/test_publisher_delivery.py` | **13 passed** |
| Delivery units | `pytest tests/unit/test_publisher_delivery_units.py` | **22 passed** |
| Affected units (config, read-back, Bunny client, recording fakes) | `pytest tests/unit/test_publisher_runtime_units.py tests/unit/test_bunny_caption_readback.py tests/unit/test_bunny_client.py tests/unit/test_publisher_recording_mapping.py tests/unit/test_publisher_recording_worker.py` | all passed (98 and 75 in two runs) |
| Django LMS API + staff withdraw + existing publisher API | `cd backend && python manage.py test operations.tests.test_lms_api operations.tests.test_publisher_api` | **18 OK** |
| Publisher runtime regression (test DB) | `pytest tests/integration/test_publisher_runtime.py` | **43 passed** (7 min 45 s) |

## Requirement → test

| Requirement | Test |
|---|---|
| Authentication and unauthorised access | `ServiceAuthenticationTests`: no header, staff `Token`, staff token as Bearer, empty Bearer, `Basic`, too-short and wrong tokens are all 401 with `WWW-Authenticate`; no configured token means nobody; both rotation tokens work |
| Rate limiting | `test_rate_limit_per_service_token`: 2/min gives 200, 200, 429, and the other token is unaffected |
| Access log | `test_recording_is_looked_up_by_lms_occurrence_with_the_configured_library`: JSON line with route, status and client fingerprint; never the token |
| Error handling | `test_unknown_malformed_unconfigured_and_unavailable`: 404 / 400 / 503 NOT_CONFIGURED / 503 PLATFORM_UNAVAILABLE without the exception text |
| Pagination | `test_feed_validates_and_forwards_paging` and `test_feed_pages_by_cursor_and_returns_changes_after_changed_since`; `test_cursor_round_trips_and_refuses_tampering` |
| LMS occurrence mapping | `test_occurrence_maps_to_its_lecture_parts_and_verified_video`: occurrence → lecture → part → Bunny video; `teams_recording_id` = Graph recording ID; no URL / meeting / SharePoint text in the answer |
| Multi-part ordering, excluded fragment | `test_multi_part_order_and_an_excluded_fragment`: an unpaired fragment needs review → EXCLUDE → `sequence` 1, 2 over source parts 2, 3 |
| Live readiness | `test_readiness_is_computed_live_not_from_the_historical_event`: the caption becomes unverified → not READY while `LECTURE_FULLY_READY` remains; cancelled → NOT_APPLICABLE |
| Only verified recordings released | `test_an_unverified_lecture_cannot_be_released`, `test_a_caption_never_content_checked_cannot_be_released` |
| Release / withdraw, audited, idempotent, immediate | `test_release_and_withdraw_are_audited_idempotent_and_immediate` |
| Concurrent operations | `test_concurrent_releases_and_withdrawals_serialise`: 8 parallel releases → 1 change, 1 event; mixed release/withdraw → strictly alternating events, consistent final state |
| Existing video preservation + production-mode adoption | `test_production_mode_adopts_existing_videos_and_still_processes_new_lectures`: same record IDs, GUIDs, captions and visibility; no fetch or delete; one `LECTURE_FULLY_READY` |
| New lecture after production-mode configuration | Same test: a new lecture reaches READY with its caption read back through a signed CDN URL, and stays NOT_RELEASED |
| Token security | `test_embed_token_is_the_documented_sha256_hex_of_key_video_and_expiry`, `test_signed_embed_url_is_short_lived_and_never_carries_the_key`, `test_a_token_for_one_video_or_expiry_does_not_fit_another`, `test_cdn_token_is_the_documented_hmac_sha256_base64url`, `test_the_pull_zone_read_drops_its_secret_key`, `test_a_wrong_cdn_key_never_counts_as_a_content_check` |
| Blocked playback | `PLAYBACK_DISABLED`, `NOT_RELEASED`, `WITHDRAWN` and `STATE_*` reasons in the tests above; `test_unknown_and_lms_only_occurrences` |
| Security gate: approved policy (24 Oct 2026) | `test_each_approved_protection_is_a_release_blocker` (embed token, direct play, no-referrer block, MP4 fallback, allowed domain missing *or* extra); `test_library_786045_as_read_on_2026_10_24_is_not_yet_releasable`; release refused with `LIBRARY_NOT_SECURE` |
| CDN token authentication DEFERRED | `test_the_approved_rollout_library_passes_with_cdn_protection_reported_deferred`, `test_deferred_cdn_protection_is_never_reported_as_enabled_or_silently_secure` (status `DEFERRED` / `DEFERRED_BUT_ENABLED`, never "secure" by omission); `test_block_none_referrer_can_be_relaxed_by_policy_after_browser_testing`; `test_drm_on_is_reported_but_not_a_blocker` |
| No CDN key needed while deferred | `test_production_write_is_disabled_by_default` (config: no key needed; a configured key is ignored; `REQUIRED` needs key and host) |
| Caption verification without CDN signing | `test_deferred_cdn_protection_reads_back_unsigned_even_with_a_key_configured`; `test_production_mode_adopts_existing_videos_and_still_processes_new_lectures[cdn-deferred-approved]` (a new lecture's caption is read back unsigned from the production CDN host: content verified, READY) |
| CDN protection kept as a future mode | `…[cdn-required-future]` (signed read-back), `test_a_wrong_cdn_key_never_counts_as_a_content_check`, `test_required_cdn_protection_is_a_release_blocker` |
| CDN protection enabled while the policy is deferred | `test_cdn_protection_turned_on_while_policy_is_deferred_blocks_release_not_playback`: read-back 403, caption not content-checked, release refused |

## Read-only run against production data (24 Oct 2026)

The connection had `transaction_read_only = on`, library 786045, and `playback_enabled=false`.

| LMS occurrence | State | Parts (sequence, source index, video) | Caption content verified | Release | Playback reason |
|---|---|---|---|---|---|
| OCC-075C8136DD85… | READY | (1, 1, 069df826…) | yes | NOT_RELEASED | NOT_RELEASED |
| OCC-188B392FF4ED… | READY | (1, 1, 550c9c30…) | yes | NOT_RELEASED | NOT_RELEASED |
| OCC-4493AC65DF50… | READY | (1, 1, a1a8fc1d…) | yes | NOT_RELEASED | NOT_RELEASED |
| OCC-5FDA481E5B11… | READY | (1, 1, 51920222…) | yes | NOT_RELEASED | NOT_RELEASED |
| OCC-774CB73AB009… | READY | (1, **2**, 456d51cf…); part 1 excluded | yes | NOT_RELEASED | NOT_RELEASED |
| OCC-B8C49F1C2ED4… | READY | (1, 1, 04fe964d…) | yes | NOT_RELEASED | NOT_RELEASED |
| OCC-EB064C5DE7B4… | READY | (1, 1, 010900eb…) | yes | NOT_RELEASED | NOT_RELEASED |
| 3 inactive LMS occurrences | NOT_APPLICABLE | — | — | NOT_RELEASED | STATE_NOT_APPLICABLE |
| OCC-DOES-NOT-EXIST | 404 | | | | |

**Leak check:** 0 answers contained a URL, a Teams meeting or join identifier, a SharePoint identifier or a file name.

**Database afterwards:** `bunny_videos` visibility was still `HIDDEN` × 7, with 0 publication events.

**Latency from the workstation:** 1.6–4.3 s per READY occurrence (about 25 sequential queries, each crossing the workstation-to-Neon round trip); 0.5 s for coverage-only or unknown occurrences. The feed page is capped at 50 items because of this. Measure again from the VPS in Phase A.

## Direct-access probe (24 Oct 2026, read-only)

Status codes only, for one existing video. Full table and interpretation: [security checklist, *Residual risk*](BUNNY_786045_SECURITY_CHECKLIST.md#residual-risk-while-cdn-token-authentication-is-deferred).

**Findings:**
- **With an LMS `Referer`:** the HLS playlists, thumbnails and the original file answer 200. With no referrer or a foreign one, they answer 403.
- **Captions** answer 200 to everyone.
- **MP4 fallback:** 404 (none exist).
- **Player pages without a token:** 200 for now. Embed token authentication was still off; re-probe after Phase B.

## Not verifiable locally (activation checks)

- **Unsigned caption read-back from the production CDN host:** `security-check --caption-readback-video-id` must return 200 with `signed: false`. CDN token signing (the future `REQUIRED` mode) follows Bunny's documentation; the tests check internal consistency only.
- **Long playback past token expiry** (pause, seek, resume, reload): the live checks in [LMS guide §6](LMS_INTEGRATION_GUIDE.md#6-long-playback-pause-seek-resume-and-token-expiry).
- **Safari / iOS referrer behaviour** with *Block requests without a referrer*: staff test.
