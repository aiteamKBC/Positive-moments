# Production video delivery: acceptance tests and evidence (24 Oct 2026)

**Where the code ran:**
- **Locally only:** the isolated test database (`kbc_qa_integration_test`, Docker), with fake Graph, LMS and Bunny, and with Django's own test database.
- **Production:** one read-only run of the read model (`default_transaction_read_only=on`). Nothing was written; no Bunny setting was read or changed by the tests.

## Focused automated tests

| Suite | Command | Result |
|---|---|---|
| Delivery integration (test DB) | `pytest tests/integration/test_publisher_delivery.py` | **11 passed** |
| Delivery units | `pytest tests/unit/test_publisher_delivery_units.py` | **17 passed** |
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
| Security gate | `test_each_open_library_setting_is_reported`, `test_cdn_token_authentication_off_or_unreadable_is_reported`, `test_library_786045_as_read_on_2026_10_24_is_not_yet_secure`; release refused with `LIBRARY_NOT_SECURE` |

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

## Not verifiable locally (activation checks)

- **CDN token signing against the real pull zone:** `security-check --caption-readback-video-id` must return 200. The algorithm follows Bunny's current documentation; the tests check internal consistency only.
- **Captions in the embed player with CDN token authentication on:** Bunny's documentation doesn't cover this. Staff test, checklist step 9.
- **Safari / iOS referrer behaviour** with *Block requests without a referrer*: staff test.
