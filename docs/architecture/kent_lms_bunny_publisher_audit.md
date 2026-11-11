# Kent LMS Continuous Bunny Publisher — Architecture Audit & Plan

Oct 21, 2026 · @Ktisha

A 24/7 publisher for new lectures is feasible by reusing the pilot and existing services. It is not production-ready today: three couplings to QA, an unpopulated multipart mapping and open Bunny playback security must be fixed first.

Evidence labels: **\[CODE\]** verified in code · **\[PROD\]** verified in production data (read-only) · **\[LMS\]** verified in LMS data (read-only) · **\[CONFIG\]** verified in deployed config · **\[ASSUMPTION\]** needs validation.

## 1. Executive summary

**Verdict: GO to build, NO-GO to switch on in production today.** The pilot proved the media path end to end (1 video, 737 cues, 0 ms offset, about 18 minutes). Five findings decide the design.

1. **The per-lecture LMS identity is the occurrence, not the live session.** `curriculum.live_sessions` holds one weekly Teams series per module (107 rows, 92 weekly). Each lecture is a row in `curriculum.live_session_occurrences` (1,146 rows). The LMS's own playback tables key on `occurrence_id` + `artifact_id`. **\[LMS\]**
2. **LMS matching is strong where the LMS created the meeting.** For 1–20 Oct, 59 of 81 discovered lectures matched exactly one occurrence by canonical join URL + UTC start, and the online meeting id agreed in all 59. 4 were ambiguous and 18, from 6 series including the pilot lecture, have no LMS session at all. **\[LMS\] \[PROD\]**
3. **Transcript storage is independent of OpenAI in code, but not at runtime.** The scheduler commits transcripts in the same transaction as QA, and blocks the whole cycle when the n8n precheck fails. Recording linking only runs after a legacy QA row exists. It also derives the Teams call id from the transcript id. **\[CODE\]**
4. **Everything is late today.** Lectures are discovered a median 8.0 h after they end (p90 10.0 h), because discovery only runs at 21:00 and 23:00 Cairo. Recording links take a median 8.1 h (p90 50.7 h). **\[PROD\]**
5. **Two gaps must close before students see videos.** Multipart lectures (10 of 151 selections) have no per-part SharePoint mapping: 0 rows hold a part location. Bunny player token authentication is off, and the CDN served the pilot caption file with no token. **\[PROD\] \[CONFIG\]**

**Recommendation:** add one supervised systemd service, `positive-moments-publisher`. It is a PostgreSQL-backed job queue that reuses the media runner's SKIP LOCKED lease pattern, runs on its own cycle independent of QA, and calls the existing discovery, transcript and recording services directly with its own transactions. The canonical cross-system mapping lives in the Lecture Intelligence database. The LMS reads it through an authenticated API and enforces enrolment itself. Import only after the LMS occurrence is verified (policy A). The LMS owns visibility.

One decision is needed before any build: **the LMS already archives Teams recordings to its own blob storage** (19 of 213 recording artifacts uploaded) and tracks viewing. Which system owns video hosting must be agreed with the LMS team (section 22).

## 2. Verified current architecture

Today one orchestrator runs all stages in a fixed order, at most one stage per lecture per pass. It commits once per cycle, twice a day. The publisher must not live inside it.

| # | Stage | Code | Writes | Coupling to QA / OpenAI |
| --- | --- | --- | --- | --- |
| 1 | Discovery | `app/lectures/service.py` `LectureDiscoveryService.discover_day`; `app/graph/calendar.py` | `lecture_sessions`, `lecture_discovery_runs` | Blocked if the n8n precheck fails (`orchestrator._run_window`, `RUN_BLOCKED_LEGACY_QA_ACTIVE`) **\[CODE\]** |
| 2 | Meeting | `app/graph/meetings.py` `OnlineMeetingGateway.resolve` | `lecture_sessions.meeting_id` | Same precheck; only retried while the date is inside the 4-day window **\[CODE\]** |
| 3 | Transcript | `app/transcripts/service.py` `TranscriptAcquisitionService.acquire_day` | `lecture_transcript_artifacts`, `_contents`, `_candidates` | Committed in the same cycle transaction as QA **\[CODE\]** |
| 4 | Selection | `app/transcripts/selection_service.py` | `lecture_transcript_selections`, `_parts` | Same transaction **\[CODE\]** |
| 5–8 | Cues, speakers, attendance, engagement | `app/transcripts/document_service.py`, `app/attendance/*`, `app/engagement/*` | derived tables | Same transaction |
| 9–10 | QA evaluation + render | `app/qa/service.py` (the only OpenAI call) | `lecture_qa_*` | Is QA |
| 12 | Legacy QA sync | `app/writer/*` | `qa_doctors_sessions` | Needs a rendered QA result |
| 14 | Recording link | `app/recordings/service.py` `RecordingLinkService` | `lecture_recording_links`, `qa_doctors_sessions.recording_*` | Needs the legacy QA row (`state.py` returns `NO_LEGACY_QA_ROW`); call id comes from `teams_call_id(legacy_session_id)` **\[CODE\]** |

- **Discovery source:** one mailbox (`KBC_LECTURE_CALENDAR_USER_UPN`), `calendarView` per Cairo day. A Teams event enters the registry only if its subject exactly matches an active Aptem group or an LMS `curriculum.modules` title. Unmatched events are kept only in run metadata. **\[CODE\]**
- **Recurring meetings:** expanded occurrences, one `lecture_sessions` row each, keyed by `uuid5(upn, iCalUId)`. A series shares one `meeting_id`. **\[CODE\]** That every occurrence has its own iCalUId is **\[ASSUMPTION\]**.
- **Cancelled events:** skipped at discovery, and `is_cancelled` is never set true afterwards. A lecture cancelled after registration ends up "not delivered" through the 12 h no-transcript grace. **\[CODE\]**
- **Learners and cohorts:** joined by module text and date (`kbc_users_data`, `kbc_attendance`). No cohort or module id is used anywhere. **\[CODE\]**
- **Recording link in production runs in write mode:** 96 `WRITTEN` rows carry the coded stage's v9 method and Graph metadata. **\[PROD\]**

## 3. Exact new-lecture lifecycle

A lecture moves through four independent tracks: LMS match, transcript, recording and video/caption. It is fully ready when every track has reached its end state.

1. **T−14 d … T (scheduled).** The LMS creates the Teams series and its occurrences; 103 of 107 series are set to `record-transcribe` **\[LMS\]**. The publisher's discovery tick (every 5 min) registers the lecture in `lecture_sessions` via the existing `discover_day`, and matches it to its LMS occurrence straight away (section 5).
2. **T\_end (lecture ends).** "Finished" = `scheduled_end` has passed **and** Graph reports the transcript or recording `endDateTime`, or `scheduled_end + 30 min` has passed. Today nothing waits for the end (**\[CODE\]**: no gate on `scheduled_end`), which wastes Graph calls on lectures still running.
3. **Transcript track (from T\_end).** `TranscriptAcquisitionService` lists and fetches the raw VTT. It commits **immediately, in its own transaction**. Then selection runs for that lecture. It polls every 15 min until 24 h after T\_end, then hourly until 72 h, then gives up as `NO_TRANSCRIPT`.
4. **Recording track (from T\_end).** It reads the Graph callRecordings for the meeting, filtered to this occurrence's window, and locates each part's SharePoint file using the existing discovery sources and match rule (section 8). It persists one row per part, **with no URL**. It polls every 15 min until 12 h, then hourly until 48 h, then every 6 h until 7 days, then `REVIEW_REQUIRED`.
5. **Import (recording located + LMS verified + capacity free).** It requests a fresh Graph download URL, sends it to Bunny URL Fetch, and identifies the video by its token. Bunny encodes; the publisher polls without blocking other work.
6. **Video verified.** Bunny length within 2 s of the source; x264 present; every configured resolution up to the source height present; no transcoding errors; no foreign captions.
7. **Caption (video verified + transcript selected + alignment proven).** It builds the publication copy from `raw_content` for that part, validates it against the source, uploads it, reads it back and makes it current.
8. **Ready for LMS.** The mapping becomes `READY` in the publisher's API. The LMS decides when learners see it, and checks enrolment on every playback (section 16).

Each arrow is a job row with its own retries, never a function call chain. A crash at any point resumes at the same step.

## 4. LMS `curriculum.live_sessions` schema findings

The environment variable is `LMS_DATABASE_URL`: a separate Neon database behind a pooler, PostgreSQL 18.6. It was read with a per-transaction `SET TRANSACTION READ ONLY` (a write probe was refused) and no named prepared statements, the same as `app/db/repositories/lms.py`. **\[LMS\]**

**`curriculum.live_sessions` is a Teams calendar series, one per module.** That makes it the wrong key for one recording.

| Fact | Evidence |
| --- | --- |
| PK `id varchar`; the only FK is `module_catalogue_id → curriculum.modules`, ON DELETE CASCADE | catalog **\[LMS\]** |
| Partial unique indexes allow **one active series per module and per draft** (`curriculum_one_active_teams_calendar_per_module`) | catalog **\[LMS\]** |
| 107 rows: 87 active, 9 week-meeting, 7 superseded, 3 cancelled, 1 test; `repeat_pattern` weekly 92 / none 15 | sample **\[LMS\]** |
| Teams identity columns: `graph_event_id` (103 set), `online_meeting_id` (97), `join_url` (106) | sample **\[LMS\]** |
| `start_datetime` is `timestamp without time zone` next to a `timezone` name (98 "GMT Standard Time"); the values are **UTC**: 67 of 67 join-URL pairs had 0 h offset from LI's `timestamptz` | cross-match **\[LMS\] \[PROD\]** |
| Referenced by occurrences, recording events, recording views, join launches, `teams_calendar_sync_state`, `session_result_jobs` | catalog **\[LMS\]** |

**`curriculum.live_session_occurrences` is one lecture.**

| Fact | Evidence |
| --- | --- |
| PK `id varchar` (shape `AAA-` + 32 alphanumerics); `UNIQUE (live_session_id, session_number)` | catalog **\[LMS\]** |
| Columns: `scheduled_start/end`, `actual_start/end`, `graph_event_id`, `join_url`, `online_meeting_id`, `status`, `artifacts_synced_at`, `attendance_report_id` | catalog **\[LMS\]** |
| 1,146 rows: 987 scheduled, 111 completed, 48 cancelled; `join_url` set on 1,144, `online_meeting_id` on only 65 (use the parent's), `graph_event_id` on 1,097 | sample **\[LMS\]** |
| An occurrence's `join_url` equals its series' in 1,140 of 1,146 | sample **\[LMS\]** |

**The LMS already has its own Teams recording pipeline.** It needs an owner decision.

- `curriculum.live_session_artifacts` (344 rows: 213 recordings, 131 transcripts, from 2026-07-16 to today). Keyed `UNIQUE (occurrence_id, artifact_type, graph_artifact_id)`, with `call_id`, `content_correlation_id` and Graph `content_url`. **\[LMS\]**
- 19 recordings are uploaded to blob container `recordings-live-session`. 10 transcripts hold `transcript_text`. Metadata keys include `lmsHiddenFromLearners`, `lmsArchiveProgress` and `lmsTranscriptTimeline`. **\[LMS\]**
- `live_session_recording_views` (181 rows, 63 artifacts, since 2026-09-27) keys a learner's viewing on `artifact_id`. **Learners already watch recordings through the LMS today.** **\[LMS\]**
- 213 recording artifacts against 111 completed occurrences (about 1.9 each) suggests multipart recordings or re-recordings. This is unexplained and is a question for the LMS team. **\[ASSUMPTION\]**

**Conclusion: the authoritative "LMS Curriculum Live Session ID" for one recorded lecture is `live_session_occurrences.id`.** `live_session_id` and `session_number` are kept beside it. The application-level relationship is confirmed by the LMS's own playback tables, which carry `live_session_id`, `occurrence_id` and `artifact_id`. **\[LMS\]**

## 5. LMS ↔ Teams ↔ Lecture Intelligence mapping rules

Match on **canonical join URL + exact UTC start**, confirmed by the online meeting id. Titles and module text are evidence only, never the key. Nothing in the repo references `curriculum.live_sessions` today (0 hits), so this is new code. It reuses the existing `canonicalize_join_url` (`app/graph/meetings.py`) and LI's discovered `lecture_sessions`. **\[CODE\]**

**Measured coverage, 1–20 Oct 2026 (81 LI lectures, 158 LMS occurrences).** **\[PROD\] \[LMS\]**

| Outcome | Lectures | What it was |
| --- | --- | --- |
| Exactly one occurrence: join URL + start; meeting id also equal | 59 | Normal LMS-created sessions |
| Two occurrences at the same start in one series | 4 | "Aya Modual" and "Test Teams Emails"; on 16 Oct one of the pair was `cancelled` |
| No occurrence with that join URL | 18 | 6 series: AI in Project Control 2026 (the pilot), Advanced Project and Logistics Management, Ray PMP June 2026, Ray MSP Jan 2026 (a second calendar with trailing spaces), Stephen Portfolio Management, Sufian Strategic Leadership |
| LMS `completed` occurrences with no LI lecture | 11 of 56 | Mostly tests ("Test-Replac", "M3", "M4") plus 2 `week-additional` events that LI discovery never sees |

**Deterministic rules** (evaluated in order; the result and its evidence are stored, never just the winner):

1. Candidates = occurrences whose canonical `join_url` (or the parent series' URL) equals the lecture's, and whose `status <> 'cancelled'`.
2. Keep candidates whose `scheduled_start` (naive UTC) equals `lecture_sessions.scheduled_start` exactly. If the series `online_meeting_id` is set, it must equal `lecture_sessions.meeting_id`; a mismatch is `CONFLICT`.
3. Exactly one → `VERIFIED` (rule `join_url_start_v1`). More than one → `AMBIGUOUS` (review; never take the first). None, but the URL matches another date within ±7 days → `RESCHEDULED_CANDIDATE` (review). No URL match at all → `NO_LMS_SESSION`, a reported state rather than an error.
4. Re-check every non-final match each discovery tick until T\_end + 24 h, and once daily for 14 days. If a verified occurrence later changes start, URL or status, the match becomes `STALE` and is re-evaluated. A video already shown to learners is never re-pointed automatically.
5. One occurrence may map to several recording parts (section 9). One recording part maps to exactly one occurrence. Several cohorts sharing one Teams meeting appear as several occurrences with one URL + start, which rule 3 sends to `AMBIGUOUS`. **\[ASSUMPTION\]**: not yet observed outside test modules.

**Do not use** `graph_event_id` to join. The LMS stores the organizer mailbox's event id, LI stores the discovery mailbox's ImmutableId, and Outlook event ids are mailbox-scoped. **\[ASSUMPTION\]**, consistent with zero overlap in usage.

### Title and import policy

Title: `{lecture title} | {YYYY-MM-DD} | LMS {occurrence_id} | {rec-xxxxxxxx}`. Multipart adds `  | part {n}/{N} ` before the token. It is sanitized and capped at 200 characters by the existing `app/bunny/identity.build_title`, and the subject is truncated first. The full occurrence id, `live_session_id` and `session_number` live in PostgreSQL. `lecture_id`, the occurrence id and the record id also go in Bunny `metaTags`. Identity never comes from the title: the `rec-` token is only used to find a fetched video, and the DB is the source of truth.

**Recommended: Policy A, hold the import until the LMS occurrence is `VERIFIED`.** The evidence:

- LMS-created meetings already have their occurrence before the lecture starts, so A adds **no delay** in the normal case.
- 18 of 81 lectures (22%) have no LMS session. Importing them under policy B would store about 4.4 GB each with no viewer and no LMS id to put in the title.
- Holding loses nothing: the file stays in SharePoint, and the recording track persists its identity without a URL.

`LMS_MATCH_PENDING` past T\_end + 24 h raises an alert. `NO_LMS_SESSION` lectures need a business rule: skip them, or import with `LMS unassigned` in the title. Retitling stays supported via Update Video, because identity is the token plus the DB.

## 6. Transcript acquisition vs QA/OpenAI independence

The transcript services are independent and reusable. The scheduler that runs them is not, so the publisher must call the services directly and commit each step itself. **\[CODE\]**

| # | Question | Answer | Evidence |
| --- | --- | --- | --- |
| 1 | Raw VTT stored before OpenAI? | **Written** before, **committed** after. Pass 1 inserts `raw_content`; QA runs about 8 passes later; the commit comes at the end of the cycle | `service.py` `_acquire_artifact` → `store_content`; `orchestrator._passes` |
| 2 | Separate transaction? | **No** in every orchestrated path: one commit per cycle (`daemon.run_once`), per backfill day (`_process_day`), or per `run-pipeline`. Stages run inside savepoints | `orchestrator._stage_savepoint`; `daemon.py`; `backfill.py` |
| 3 | OpenAI 401/429/500/timeout/bad output? | Classified and recorded as an evaluation row, with the circuit opened on configuration errors and retries for transient ones; no rollback of earlier stages | `app/qa/provider.py` `classify_provider_failure`; `qa/service.py` |
| 4 | Does the transcript survive an AI failure? | Yes for ordinary failures: no QA path deletes transcript rows. **No** if the connection dies during the long QA call: the whole cycle rolls back. This happened on 2026-10-15 (comment in `runner.allow_idle_through_provider_call`) | `runner.py`; the only DELETE is the operator tool `normalize_transcript_artifacts.py` |
| 5 | No OpenAI key configured? | Works: `require_qa_model()` is only called in `_run_qa`; `run-pipeline --no-provider` / `SCHEDULER_ALLOW_PROVIDER=false` skip QA | `settings.py`; `runner.py` |
| 6 | Independent of nightly QA? | Only via the standalone CLI `acquire-transcripts` (no orchestrator, no n8n precheck, no lock). All orchestrated paths are blocked by the n8n precheck, fail-closed | `cli/main.py`; `orchestrator._run_window` |
| 7 | Does recording discovery depend on QA? | **Yes.** `RECORDING_LINK` is stage 14 and needs a legacy QA row; the call id is derived from the transcript id | `state.py` `_recording_link`; `recordings/service.py` |
| 8 | Is transcript availability gated behind QA? | The transcript and selection states are not. The cycle-wide precheck and the shared transaction are the couplings | `state.py` `_transcript`, `_selection` |
| 9 | Can the publisher reuse the persistence functions? | **Yes.** Plain classes, connection passed in, no commits, no globals; uuid5 ids + `ON CONFLICT`; `UNIQUE (artifact_id, version_number)` and `(artifact_id, content_sha256)` in production stop duplicate versions | **\[CODE\] \[PROD\]** |
| 10 | Graph hasn't generated it yet? | `NO_TRANSCRIPTS_AVAILABLE` (empty list) or `TRANSCRIPT_NOT_READY` (404/503/empty body) is stored; re-polled only on the next 21:00/23:00 cycle inside the 3-day lookback; closed as not delivered 12 h after `scheduled_end` | `graph.py`; `state.py` `NO_TRANSCRIPT_GRACE` |

**How the publisher uses them:** call `TranscriptAcquisitionService` and `TranscriptSelectionService` with its own connection, and commit per lecture-day. Pass `qa_evidence_repository=None`: it skips the `qa_doctors_sessions` parity read, whose failure would roll back the day's acquisition. Take the existing per-lecture advisory lock (`app/orchestration/locks.py`, namespace `0x4B424C31`) so the publisher and the QA orchestrator never work on the same lecture at once. QA then reads the same stored rows, so no second transcript registry is needed.

**Gaps to fix in the publisher, not in QA:**

- `acquire_day` re-downloads every artifact of the day; add a skip for `CONTENT_STORED` artifacts younger than 72 h.
- A re-fetch failure overwrites `artifact_status` on an already-stored artifact. Treat stored content as available whatever the latest fetch status says.
- For a series meeting, another occurrence's stored transcript can mark this lecture's transcript stage `COMPLETE`. The publisher must check readiness **per occurrence**, from the selection result. **\[CODE\]**, runtime impact **\[ASSUMPTION\]**.

## 7. Current production schedules and actual dependencies

Production runs exactly two scheduled cycles a day, at 21:00 and 23:00 Cairo, so every lecture waits about 8 h before anything happens to it. **\[PROD\]**

| Process | How it runs | Evidence |
| --- | --- | --- |
| QA scheduler | In-process sleep loop (`app/orchestration/daemon.py` `SchedulerDaemon.run_forever`), cron `0 21,23 * * *` Africa/Cairo, lookback 3 days (a 4-day window); cycle advisory lock `0x0C1C1E`, per-lecture locks | **\[CODE\]**; the last 12 runs all `SCHEDULED` at 21:00 and 23:00, 15–20 Oct **\[PROD\]** |
| Backfill runner | Polls `backfill_runs` every 20 s, SKIP LOCKED claim, one day per transaction | **\[CODE\]**; 94 BACKFILL runs in the last 30 days, 10:00–19:00 Cairo **\[PROD\]** |
| Media worker | `positive-moments-media.service`: `app.cli.media media-runner --poll-seconds 10`, `Restart=always`, `MemoryMax=512M`, user `www-data` | `deploy/systemd/positive-moments-media.service` **\[CODE\]** |
| Web, scheduler, backfill units | systemd on the VPS (srv1281908), git checkout `/var/www/Positive-moments` | Unit files are **not in the repo**; contents **\[ASSUMPTION\]** |
| Env loading | `app/config/settings.py` loads `backend/.env`; on the VPS the units use `backend/config/.env` or `backend/qa-core.runtime.env` via `EnvironmentFile=` | **\[CODE\]**; VPS layout per the earlier deploy record |

**Measured latencies, lectures since 20 Sep** **\[PROD\]**:

| Step | Median | p90 | n |
| --- | --- | --- | --- |
| Lecture end → first discovered | 8.0 h | 10.0 h | 102 |
| Lecture end → recording link written | 8.1 h | 50.7 h | 67 |
| Pilot: Fetch submitted → video identified | 32 s | — | 1 |
| Pilot: submitted → encoding finished (2 h 12 m, 1080p) | 17 min | — | 1 |

Transcript first-fetch latency could not be measured. `content_fetched_at` is overwritten on every re-fetch, so its median of 178 h measures re-downloads, not first availability. The publisher should record first-seen and first-stored times itself.

**The QA schedule stays exactly as it is.** The publisher runs as a separate process with its own cadence. It shares only the per-lecture advisory lock and the stored rows.

## 8. Recording / SharePoint discovery coverage

The matching logic is sound and should be reused as is. Its entry point must change, because today it starts from a legacy QA row and a transcript-derived call id. **\[CODE\]**

**Reusable pieces** (`app/recordings/`):

- `graph_lookup.RecordingMetadataGateway.lookup`: `GET /users/{lookup}/onlineMeetings/{meeting}/recordings`, filtered by `callId`. Needs `OnlineMeetingRecording.Read.All` plus the Teams application access policy.
- `drive_items`: three sources. `TenantRecordingSearch` (`/search/query`) runs only if `RECORDING_LINK_SEARCH_REGION` is set, and is off by default. `OneDriveRecordingsFolder` covers the lookup user's and the organizer's `Recordings` folders. `ChannelRecordingsFolder` covers channel meetings in the lookup user's joined teams. If any source fails, the result is `DRIVE_ITEM_DISCOVERY_FAILED`.
- `matching.match_candidates` (rule `exact_call_id_subject_timestamp_v9`): Teams file-name pattern, normalized subject equal, same date, and file-name time 0–120 s before the Graph `createdDateTime`.
- `resolution.resolve` (policy `recording_match_v2_full_recording_resolution`): it collapses copies and classifies files as FULL (at least 50% of scheduled), FRAGMENT (at most 25%) or PARTIAL. A fallback for files with no known duration requires 10× size dominance. It refuses rather than guessing.

**Production state** (100 coded rows) **\[PROD\]**: 96 `WRITTEN`; 1 `MULTIPART_RECORDING`, 1 `GRAPH_RECORDING_AMBIGUOUS` and 1 `AMBIGUOUS_RECORDING_FILES` (all review); 1 `DRIVE_ITEM_DISCOVERY_FAILED` (waiting). Retries back off 6 h → 12 h → 24 h, up to 8 attempts.

**Coverage gaps the publisher must handle:**

1. **The QA gate.** Lectures with no QA row get `NO_LEGACY_QA_ROW` and are never linked. Fix: a publisher-owned resolver that starts from `lecture_sessions` (meeting id, lookup user, scheduled window). It gets the call id from the Graph callRecordings list filtered to the occurrence window, or from `lecture_transcript_artifacts.provider_call_id` when a transcript exists.
2. **The tenant search is off.** Code comments say it was the only source that found the Sep 2026 Martech recordings. Enable it for the publisher (`RECORDING_LINK_SEARCH_REGION`) after confirming the region value. **\[ASSUMPTION\]**
3. **Only `Recordings` folders are searched.** Recordings saved elsewhere (other SharePoint sites, renamed folders) are invisible. Reconciliation must report completed lectures with a Graph callRecording but no located file as `RECORDING_FILE_NOT_FOUND`.
4. **Channel meetings** outside the lookup user's joined teams fail with `channel_not_in_joined_teams`. These need monitoring per team.
5. **Permissions are contradictory in the docs.** The driveItem routes worked in the pilot (metadata and `/content` redirect), so `Files.Read.All`-level access exists today. **\[PROD\]** (pilot)

**The download URL source is unchanged from the pilot.** `app/media/delivery.SharePointMedia.source_file` returns a fresh `@microsoft.graph.downloadUrl` or the `/content` 302 location, without following it. The Graph callRecording `/content` route is unsuitable for Bunny: it streams only with our bearer token, which must never leave the backend.

## 9. Multipart readiness

Multipart lectures are still unsupported end to end, and the earlier gap remains. 10 of 151 transcript selections have 2 parts, yet 0 `lecture_recording_parts` rows hold a SharePoint location. **\[PROD\]**

- `RECORDING_LINK` treats multipart as review: `MULTIPART_RECORDING` ("recording\_url holds one URL"). **\[CODE\]**
- `lecture_recording_parts.source_drive_id` and `source_item_id` (migration 019) are **written by no code**. The repository's column list excludes them. **\[CODE\]**
- `app/media/part_jobs.resolve_part_sources` accepts a per-part source map, but nothing passes one. **\[CODE\]**
- Only 2 lectures have more than one `lecture_recording_parts` row; those were written by one-off Phase 6A scripts. **\[PROD\] \[CODE\]**

**Proposed per-part mapping (deterministic, every link proven):**

1. Graph callRecordings for the call give one row per part: `id`, `createdDateTime`, `endDateTime`, `contentCorrelationId`.
2. Transcript part ↔ recording part: equal `contentCorrelationId`, else `createdDateTime` within 2 s. This rule is already used by `alignment.align`, and Phase 6A measured +0.000 s.
3. Recording part ↔ SharePoint file: the existing v9 rule per part (file-name time 0–120 s before that part's `createdDateTime`, subject and date equal). Then `align()` must prove the origin, using the file's `video.duration` against that part's window within max(2 s, 0.1%).
4. If every part has exactly one file and one transcript, store one row per part. Anything else → `REVIEW_REQUIRED` + alert, and **no part is published**.
5. **One Bunny video per recording part.** Bunny cannot concatenate a URL fetch, and joining parts would shift captions by the \~61–64 s overlap measured in Phase 6A. The LMS shows the parts in order under one occurrence.
6. Each part's caption is **that part's own raw VTT, unshifted**: offset = 0 when `align()` proves `graph_created_origin`. Never the combined transcript, whose later cues carry `part_offset_ms`.
7. Uniqueness: one row per (drive, item); one per (lecture, part index); one Bunny video per (library, drive, item); every part maps to the same LMS occurrence.

The LMS's own artifacts table carries `call_id` and `content_correlation_id` per recording. It is a useful independent cross-check for step 2 during rollout. **\[LMS\]**

## 10. Bunny pilot reuse assessment

Keep the pilot's client, identity and caption modules as they are. Replace its orchestration and manifest. **\[CODE\]** The pilot record (`be785a53-16c0-4d2d-b20b-dea9636f5532`, `rec-a83ab163`, publication `70aa4a0a-…`) is migrated into the tables, never re-imported.

| Module | Verdict | Why / change needed |
| --- | --- | --- |
| `app/bunny/client.py` | **Reuse** | Endpoints pinned to Bunny's OpenAPI; never-retry-uncertain-writes; DNS failures are known-not-sent; secrets scrubbed; 15 tests |
| `app/bunny/identity.py` | **Reuse + extend** | Add the LMS occurrence id and `part n/N` to `build_title`; uuid5 ids already match migration 024 |
| `app/bunny/captions.py` | **Reuse** | Strips voice tags; cue-by-cue proof against the source; published hash `cdc19671…` was byte-identical to the validated file |
| `app/bunny/source.py` | **Replace** | Reads one lecture through `lecture_recording_links`; the publisher needs per-part sources and the per-occurrence selection |
| `app/bunny/pilot.py` | **Split** | Its preflight becomes the startup and per-job guards; each stage becomes a job handler; the 15 checks stay |
| `app/bunny/manifest.py` | **Retire** | It is a stand-in for the tables; the pilot's records are its only content |

**Weak points found in review:**

- **Identification by title search assumes one video per token.** A confirmed failed import (Bunny status 6 `UploadFailed`) leaves a dead video with the same token. The retry must exclude guids recorded as failed, or the failed video must be deleted first. That deletion needs approval (section 22).
- **Fetch acceptance is not the end of the import.** The pilot already waits for the video to appear and for status ≥ 1, then 3, then 4. The publisher keeps these as distinct states (section 13).
- **The pilot polled in-process for up to 3 h.** The publisher schedules a poll job instead, so encoding never blocks other work.
- **Media verification read the encoding tier from library settings.** It needs the account key. The publisher reads library settings once per startup, alerts on drift, and never makes that check per video.
- **DNS on the developer machine was unreliable** (5 of 6 runs failed before succeeding). The bounded read-only retries added during the pilot stay. Production runs on the VPS, whose DNS was not tested. **\[ASSUMPTION\]**

## 11. Updated database design and migration proposal

All publisher state lives in the Lecture Intelligence database (`DATABASE_URL`). It reuses `lecture_sessions`, `lecture_transcript_*` and `lecture_recording_parts`, and adds five small tables plus one view. The LMS database stays read-only. LMS ids are stored as plain text with their provenance, never as cross-database foreign keys.

| Table | New / changed | Purpose | Keys and constraints |
| --- | --- | --- | --- |
| `lecture_lms_matches` | **new** (024b) | Lecture ↔ LMS occurrence, with history | PK `match_id` (uuid5); `lecture_id` FK; `lms_occurrence_id`, `lms_live_session_id`, `lms_session_number`, `lms_scheduled_start` (text/int/ts); `match_status` CHECK (`PENDING`, `VERIFIED`, `NO_LMS_SESSION`, `AMBIGUOUS`, `RESCHEDULED_CANDIDATE`, `CONFLICT`, `STALE`); `rule_version`, `evidence jsonb`, `checked_at`; **partial unique** `(lecture_id) WHERE is_current`; partial unique `(lms_occurrence_id) WHERE is_current AND match_status='VERIFIED'` |
| `lecture_recording_parts` | **changed**: additive | One row per recording part with its SharePoint file | Starts writing the existing `source_drive_id/item_id/located_at`; adds `graph_recording_id`, `locate_status`, `locate_rule`; **widens** the `media_duration_source` CHECK to allow `drive_item_video_facet`; partial unique `(source_drive_id, source_item_id)` |
| `bunny_videos` | 024, **amended** | One Bunny video per part file | Drop `UNIQUE (library, drive, item)` in favour of a partial unique index `WHERE retired_at IS NULL`, so a confirmed-failed import can be retired and retried; add `retired_at`, `retire_reason`, `bunny_status_history jsonb`, `visibility_state` CHECK (`HIDDEN`, `READY_FOR_LMS`, `WITHDRAWN`) |
| `bunny_caption_publications` / `_current` | 024, unchanged | Caption history + the live pointer | As proposed and tested |
| `bunny_publisher_jobs` | **new** | The durable queue | PK `job_id`; `job_kind` CHECK; `subject_key` text (for example `lecture:<id>:part:2`); **partial unique** `(job_kind, subject_key) WHERE status IN ('QUEUED','RUNNING','RETRY_WAIT')` = one live job per thing; `priority` (live 100 / backfill 10); `status`, `attempt_count`, `max_attempts`, `next_attempt_at`, `locked_by`, `locked_at`, `last_error_code`, `last_error`; index `(status, priority DESC, next_attempt_at)` |
| `bunny_publisher_events` | **new** | Append-only audit | PK bigserial; `job_id`, `lecture_id`, `event`, `detail jsonb` (no URLs or keys; same guard as the manifest), `at` |
| `bunny_publisher_heartbeats` | **new** | Liveness per worker | PK `worker_id`; `seen_at`, `version`, `current_job` |
| `lecture_publication_status_v` | **new view** | Coverage and exception queue | Computed from the tables above. A view, so it can never drift from the facts |

**Job claim** (the pattern already proven in `app/db/repositories/media_platform.py`):

```sql
UPDATE bunny_publisher_jobs SET status='RUNNING', locked_by=%(w)s, locked_at=now(), attempt_count=attempt_count+1
 WHERE job_id = (SELECT job_id FROM bunny_publisher_jobs
                  WHERE (status='QUEUED' OR (status='RETRY_WAIT' AND next_attempt_at<=now())
                         OR (status='RUNNING' AND locked_at < now() - interval '15 minutes'))
                    AND job_kind = ANY(%(kinds)s)
                  ORDER BY priority DESC, next_attempt_at NULLS FIRST, job_id
                  LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING *;
```

**Transaction boundaries.** Each transaction is one of three things: a claim, one external step's *intent* (for example `SUBMITTING`, committed **before** the Bunny call), or that step's *result*. External calls happen between transactions. No transaction is held open across Graph or Bunny calls. This is the lesson of the 2026-10-15 idle-timeout rollback.

**Migration plan** (manual psql, as today; nothing applied yet):

1. `024_create_bunny_stream_tables.sql`, amended as above, plus `025_create_publisher_tables.sql` (`lecture_lms_matches`, `bunny_publisher_jobs`, `bunny_publisher_events`, `bunny_publisher_heartbeats`, the view) and `026_recording_parts_source_location.sql` (the additive `lecture_recording_parts` changes).
2. Each file is idempotent (`IF NOT EXISTS`), with a guarded rollback that refuses when rows exist, as in the current 024. Each is tested on `kbc_qa_integration_test` with the release gate.
3. Moving a file into `app/db/migrations/` is the approval step. The production audit contract reads that folder.
4. **Pilot backfill**: a one-shot script reads `output/bunny_pilot/manifest.json` and inserts 1 video, 1 publication and 1 current pointer with the same uuid5 primary keys, inside a transaction that verifies counts.

**Rollback.** Stop the publisher unit; the tables stay; QA is untouched. The `lecture_recording_parts` CHECK widening is reversible only while no `drive_item_video_facet` rows exist.

## 12. Continuous publisher architecture

The publisher is one new systemd service running one long-lived Python process. It has a scheduler thread that enqueues work and a small fixed pool of worker threads that claim jobs. There is no Redis, Celery or cron. It is modeled on `positive-moments-media.service`, which already runs this way. **\[CODE\]**

| Component | Cadence | Does | Reuses |
| --- | --- | --- | --- |
| Discovery planner | every 5 min | Calls `discover_day` for today and yesterday (Cairo) with `persist=True`. Enqueues `MATCH_LMS`, `ACQUIRE_TRANSCRIPT` and `RESOLVE_RECORDING` for lectures past T\_end | `LectureDiscoveryService` (unchanged; it needs Aptem/LMS only for eligibility) |
| LMS matcher | per job | Reads occurrences for the lecture's date (read-only LMS connection) and applies the section 5 rules | `readonly_database_connection`, `canonicalize_join_url` |
| Transcript worker | per job | `acquire_day` for one day plus selection; commits | `TranscriptAcquisitionService`, `TranscriptSelectionService` |
| Recording worker | per job | Graph callRecordings plus DriveItem discovery, then per-part resolution; writes `lecture_recording_parts` | `app/recordings/*` behind a new QA-free precondition adapter |
| Import worker | per job, ≤ 2 videos in flight | Fresh URL → Fetch → identify → tag | `app/bunny/client.py`, `SharePointMedia.source_file` |
| Encoding poller | every 60 s per video in flight | Get Video until finished or failed, then media checks | pilot `_await_encoding`/`_verify_media` logic |
| Caption worker | per job | Build, validate, upload, read back, make current | `app/bunny/captions.py` |
| Reconciler | 05:00 Cairo daily + every 6 h light pass | Coverage across LMS, LI and Bunny (section 15) | `lecture_publication_status_v` |
| Heartbeat | every 30 s | Writes `bunny_publisher_heartbeats` | — |

**Unit:** `positive-moments-publisher.service`.

- `ExecStart=…/backend/venv/bin/python -m app.cli.bunny publisher-daemon`, as `User=www-data`.
- `Restart=always`, `RestartSec=10`, `TimeoutStopSec=120`, `MemoryMax=512M`.
- Its own `EnvironmentFile=backend/config/publisher.env`, containing only: `DATABASE_URL`, `LMS_DATABASE_URL`, `APTEM_DATABASE_URL`, the `MICROSOFT_GRAPH_*` settings, `KBC_LECTURE_CALENDAR_USER_UPN`, the `BUNNY_*` settings and `PUBLISHER_*` tunables.
- **No `QA_MODEL_API_KEY`**. That proves OpenAI independence by construction.
- SIGTERM finishes the current external step, persists, then exits. A step cut short is recovered by its lease (15 min) and its uncertain-state reconciliation.

**Why this shape:** the polling is bounded. Each tick touches only due jobs (an indexed `next_attempt_at`) and today's and yesterday's calendar, never all systems. Back-pressure is a capacity check: a video import is not claimed while two are in flight. Discovery, transcripts and captions never wait on an encode, because an encode is a poll job, not a blocked thread.

## 13. State machines

There are five orthogonal tracks plus visibility, each stored where its facts live. The 16 conceptual states you proposed are a **derived** status computed by `lecture_publication_status_v`, never a column anyone writes.

| Track (stored in) | States → allowed transitions | Needs a verified external fact | On crash |
| --- | --- | --- | --- |
| LMS match (`lecture_lms_matches`) | `PENDING` → `VERIFIED` \| `NO_LMS_SESSION` \| `AMBIGUOUS`† \| `RESCHEDULED_CANDIDATE`† \| `CONFLICT`†; `VERIFIED` → `STALE` → re-evaluated | LMS rows read in the same check | Re-run (pure read) |
| Transcript (artifact + selection tables) | `PENDING` → `STORED` → `SELECTED` \| `NO_TRANSCRIPT` (after 72 h) \| `BLOCKED_POLICY`† | Graph content stored with sha256 | Re-run; idempotent upserts |
| Recording part (`lecture_recording_parts`) | `PENDING` → `LOCATED` \| `MULTIPART_UNRESOLVED`† \| `AMBIGUOUS`† \| `NOT_FOUND` (after 7 days)† | Graph callRecording + DriveItem metadata | Re-run (reads only) |
| Video (`bunny_videos`) | `PENDING` → `SUBMITTING` → `SUBMITTED` \| `SUBMISSION_UNCERTAIN` → `IDENTIFIED` → `UPLOADED` (status 1) → `TRANSCODING` (2–3) → `FINISHED` (4) → `VERIFIED`; `FAILED` (5–6) → `RETIRED` → a new row | Fetch accepted; video found by token; Bunny status; our media checks | `SUBMITTING`/`UNCERTAIN` → **reconcile only**, never re-fetch |
| Caption (`_publications` + `_current`) | `PREPARED` → `UPLOADING` \| `UPLOAD_UNCERTAIN` → `PUBLISHED` → `VERIFIED` (current); `REJECTED`†; `VERIFIED` → `SUPERSEDED` when a newer one becomes current | Bunny validation `valid=true`; read-back registers `en` | `UPLOADING`/`UNCERTAIN` → read back first |
| Visibility (`bunny_videos.visibility_state`) | `HIDDEN` → `READY_FOR_LMS` (video `VERIFIED` + caption `VERIFIED` + match `VERIFIED`) → `WITHDRAWN` (manual) | All three tracks | Recomputed |

† = `REVIEW_REQUIRED`: it alerts, stays in the exception queue, and leaves only by an operator action recorded in `bunny_publisher_events`.

**Derived lecture status (first match wins):** `REVIEW_REQUIRED` → `PERMANENT_FAILURE` (attempts exhausted on a non-retryable error) → `RETRY_SCHEDULED` → `LMS_MATCH_PENDING` → `RECORDING_PENDING` → `IMPORT_*` / `ENCODING` → `TRANSCRIPT_PENDING` → `CAPTION_PENDING` → `FULLY_READY`. Transcript and recording run in parallel, so a lecture can be `ENCODING` while its transcript is still pending.

**Detecting source changes:**

- A new transcript content sha → a new content version (existing behaviour) → the caption builds to a different sha → a new publication. If a caption is current, replacing it requires `PUBLISHER_AUTO_REPLACE_CAPTIONS=true`; otherwise it is review.
- A DriveItem `lastModifiedDateTime` or size change after import → review, never a silent re-import.
- A Bunny caption `version` that differs from the one recorded → changed outside the publisher → review.

**No silent drop:** every lecture in `lecture_sessions` inside the publisher's horizon has exactly one derived status. The reconciler alerts on any lecture with no job and no final state.

## 14. Temporary URL and concurrency strategy

A download URL exists only inside one import job's stack frame, for the few seconds between Graph and Bunny. It is the same design the pilot proved. **\[CODE\] \[PROD\]**

1. Discovery, matching and recording resolution persist file **identity** (drive id, item id, size, duration), never a URL. The manifest guard that refuses URL-like values carries over to `bunny_publisher_events.detail` and job payloads.
2. An import job is claimed only when fewer than `PUBLISHER_MAX_IMPORTS_IN_FLIGHT` (start at **2**) videos are in `SUBMITTING` to `TRANSCODING`. Encodes in flight do not hold a thread.
3. Inside the job, in order: commit `SUBMITTING` → fetch a fresh URL (`SharePointMedia.source_file`; the item id and size must equal the stored identity) → `POST /videos/fetch` straight away → drop the reference → commit the result.
4. **Four distinct facts, four states:** Fetch accepted (HTTP success) → video exists (found by token, `IDENTIFIED`) → source downloaded (status ≥ 1 `Uploaded`) → transcoded (status 4, progress 100) → verified by our checks. The pilot took 32 s from submission to identification and 17 min to finish. **\[PROD\]**
5. **Uncertain submission** (timeout or 5xx after sending): `SUBMISSION_UNCERTAIN` → reconcile by token for up to 30 min. Found → continue. Not found after 30 min → `REVIEW_REQUIRED`, **never an automatic re-fetch**, because Bunny may still be queueing it.
6. **Confirmed failure** (Bunny 400/422 on Fetch, or status 5/6 after identification): the video row is retired, then a new attempt is queued with a **new** URL after backoff, at most 3 attempts.
7. **URL expiry:** Bunny's docs ask for an `AuthorizationExpire` of at least 1 h. The Graph pre-authenticated URL lifetime is about 1 h **\[ASSUMPTION\]**. A 1.06 GB file was fully pulled within 32 s. Expiry therefore shows up as status 6 and is handled by step 6. Alert if more than 1 in 20 imports fail this way.
8. **Rate limits:** Bunny 429 on Fetch is a definite refusal and is retried with backoff (existing client). Graph 429 and 5xx are retried with bounded backoff on reads only.

Transcript and caption workers run concurrently with imports, each limited to 2 jobs at once. The only shared bottleneck is the database connection: one short connection per job, `prepare_threshold=None`, as the media runner does for the pooled Neon server.

## 15. Reliability, retries and reconciliation

Every failure leaves the lecture in a visible state with a next action and a time. Nothing removes a lecture from monitoring except a final state an operator can see.

| Failure | Class | Backoff | Gives up after |
| --- | --- | --- | --- |
| DNS, connection refused, Graph or Bunny 429/5xx on reads | transient | 1 min, 5 min, 15 min, 1 h, then every 3 h | 24 h → `REVIEW_REQUIRED` |
| Graph 401/403, application access policy, `accessDenied` | configuration | none; circuit opens for that job kind and alerts | immediately |
| Transcript not ready | waiting | 15 min to 24 h, then hourly | 72 h → `NO_TRANSCRIPT` |
| Recording or file not found | waiting | 15 min to 12 h, hourly to 48 h, then 6 h | 7 days → `NOT_FOUND` (review) |
| Bunny Fetch uncertain | reconcile | every 1 min | 30 min → review |
| Bunny import failed (status 5/6) | confirmed | 15 min, 1 h, 4 h, new URL each time | 3 attempts → `PERMANENT_FAILURE` |
| Encoding stalled (no progress change) | watch | poll every 60 s | 6 h without progress → review |
| Caption rejected (`valid=false`) | data | none | immediately → review |
| LMS database unavailable | transient | as reads | the import waits; nothing is published without a verified match |
| Publisher down for a day | recovery | on restart, the discovery planner also scans the last 7 days | — |

**Duplicate prevention, by layer:**

- **Database:** a partial unique index allows one live job per `(job_kind, subject_key)`.
- **Claim:** `FOR UPDATE SKIP LOCKED` plus a 15-minute lease.
- **Source:** one active Bunny video per (library, drive, item).
- **Intent before side effect:** `SUBMITTING` is committed before every Fetch, and `UPLOADING` before every caption upload.
- **Identity:** the identity token, plus the `metaTags` that hold the record id.
- **Concurrency:** two workers can never both submit, because the second sees `SUBMITTING` and reconciles.

**Reconciliation checks** (daily 05:00 Cairo, light pass every 6 h). Each check lists its rows in the exception queue:

1. LMS `completed` occurrences in the last 14 days with no LI lecture (today 11 of 56, mostly tests): reported as `LMS_SESSION_NOT_DISCOVERED`. Real `week-additional` events signal a discovery gap (they are not in the discovery mailbox).
2. LI lectures with no LMS match after T\_end + 24 h.
3. Completed lectures with a Graph callRecording but no located file; lectures with neither 24 h after the end.
4. Videos verified but with no current caption, and captions pending more than 24 h after their transcript was stored.
5. **Bunny inventory diff:** the library list against `bunny_videos`. A Bunny video with no row is an orphan, two videos with one token are duplicates, and a row whose video is missing is lost.
6. Jobs in `RUNNING` past their lease, `RETRY_WAIT` overdue by more than 1 h, and encodes stalled more than 6 h.
7. Two current LMS matches for one occurrence, or one file mapped to two lectures.
8. A Graph or Drive source that failed on every lecture in 24 h, signalling missing permission or site coverage.
9. A heartbeat older than 5 min.

Weekends and holidays need no special case. The planner is calendar-driven, so a day with no lectures is simply empty, and the reconciler checks every calendar day.

## 16. LMS playback integration contract

The LMS asks the publisher "which videos belong to occurrence X?" through a small authenticated read API. It then checks enrolment and signs a short-lived Bunny embed token itself. The LMS never parses a title, and never holds a Bunny management key.

**Current Bunny library security** (library 786045, read-only) **\[CONFIG\]**:

| Setting | Value | Required for students |
| --- | --- | --- |
| `PlayerTokenAuthenticationEnabled` | **false** | **true**: embeds then need a signed, expiring token |
| `AllowDirectPlay` | **true** | false |
| `AllowedReferrers` / `BlockNoneReferrer` | 1 entry / true | LMS domain(s) only |
| CDN token authentication (pull zone) | not readable via this API; the pilot fetched the caption file from the CDN with **no token or referrer** | enabled on the pull zone |
| `KeepOriginalFiles` | true (+1.06 GB per pilot lecture) | business decision |
| DRM, geo-blocking, watermark | off | optional |

**API** (Django `backend/`, mirroring the existing service-token pattern of `api/social-media/internal/*`):

- `GET /api/publisher/v1/occurrences/{lms_occurrence_id}/videos` → `200 {occurrence_id, live_session_id, lecture_id, status, videos:[{part, part_count, bunny_library_id, bunny_video_id, duration_s, captions:[{lang, label, version}], visibility}]}`; `404` if unknown; `409 {status, reason}` if not `READY_FOR_LMS`.
- `GET /api/publisher/v1/occurrences?changed_since=<ts>`: an incremental sync feed, so the LMS can cache mappings in its own tables if it prefers.
- Auth: `Authorization: Bearer <PUBLISHER_LMS_SERVICE_TOKEN>`, a dedicated rotating secret, read-only, over TLS, from the LMS backend only. Every request is audited.

**LMS responsibilities** (no LMS change is made in this audit):

1. Resolve learner → enrolment → occurrence in its own data, and refuse if not enrolled.
2. Call the API server-side.
3. Sign the embed URL with the library's **token-authentication key**: SHA-256 over key + video id + expiry, 5–15 min validity. This is not the API key.
4. Respect `visibility`. The LMS may also keep its own `lmsHiddenFromLearners` flag.

**Readiness vs visibility.** The publisher only declares `READY_FOR_LMS`, meaning the video is verified, the caption is verified and the match is verified. Release to learners is the LMS's decision. **Recommendation:** show only after captions are verified. Encoding to caption took seconds in the pilot, so waiting costs nothing, and it avoids an uncaptioned first view.

**Secrets:**

- Graph, Bunny API, account and LMS credentials stay in the publisher's env file only.
- The Bunny account key is used read-only, once per startup; prefer a subuser key with Stream read scope.
- Run a quarterly rotation with overlap.
- `backend/.env` holds plaintext Neon credentials on disk, which is expected for an env file; on the VPS the files must be `0600 www-data`.
- Public caption files carry no speaker names (verified in the pilot: 0 label strings in the output).

## 17. Monitoring and coverage metrics

One view, `lecture_publication_status_v`, feeds a daily report and a new operations endpoint (`/api/operations/publisher/`). Counts are kept separately for lectures, recording parts and Bunny videos.

**Baseline today, 1–20 Oct 2026** (what the publisher would inherit) **\[PROD\] \[LMS\]**:

| Metric | Count | Note |
| --- | --- | --- |
| Lectures discovered by LI | 81 | 0 marked cancelled (the flag is never set) |
| … matched to exactly one LMS occurrence | 59 | 73% |
| … LMS ambiguous | 4 | test modules + a cancelled twin |
| … no LMS session | 18 | 6 series |
| Transcript selected | 55 | 4 of them have 2 parts |
| No transcript (`NO_CANDIDATES` / `NO_SAME_DAY`) | 21 | many are "not delivered" sessions |
| Recording link `COMPLETE` | 51 | 1 review (multipart), 1 waiting |
| Imported to Bunny / verified | 1 / 1 | the pilot (16 Oct) |
| Captions verified | 1 | the pilot |
| LMS completed occurrences with no LI lecture | 11 of 56 | mostly tests; 2 `week-additional` |

**Standing metrics** (each by lecture, by part and by video):

- Eligible completed lectures; LMS matched / pending / ambiguous / none.
- Recordings located; imports in flight / verified / failed; transcripts stored / selected / none.
- Captions verified; `FULLY_READY`; review-required; permanent failures.
- Latency p50/p90: lecture end → fully ready (target p90 under 3 h); end → transcript stored; end → file located; submit → verified.
- Health: heartbeat age, oldest due job, Bunny 429 rate, Graph error rate by source.

**Exception queue** = every lecture not `FULLY_READY`, with `lecture`, `date`, `LMS occurrence`, `derived status`, `reason code`, `next action at` and `since`, newest first. It is shown in the operations console beside the existing day view.

**Alerts:**

- Heartbeat older than 5 min.
- A review-required item older than 24 h.
- No `FULLY_READY` lecture for 2 days with lectures delivered in between.
- Bunny orphan or duplicate found.
- Configuration circuit open.

The repo has no alerting channel today **\[CODE\]**. Start with an email or Teams webhook from the reconciler, decided in section 22.

## 18. Historical backfill compatibility

Backfill is the same publisher with a different job source and lower priority. It shares every table, identity and dedupe rule, so a lecture can never be imported twice whichever path reaches it first.

- **Scope:** a `publisher_backfill_runs` row (date range, filters, cap per day) enqueues jobs at `priority=10`. Live jobs are at `priority=100` and are always claimed first.
- **Admission:** backfill imports count against their own cap (`PUBLISHER_BACKFILL_IMPORTS_IN_FLIGHT`, start at 1). They are not claimed when any live import is waiting.
- **Same rules:** LMS match policy A, multipart refusal until mapped, the same caption pipeline. Historical lectures that predate the registry (the July legacy sessions) first need discovery via the existing backfill runner, which is already restricted to 120 days.
- **Forecast inputs (pilot):** 4.43 GB stored per 2 h 12 m lecture, of which 1.06 GB is the kept original, at 5 renditions with 3 replication regions; 17 min free encode. Multiply by the eligible count from `backfill-preview` before approving a run. Bunny prices are **\[ASSUMPTION\]**; read them from the account before forecasting.
- **Progress:** the same status view filtered by run id. Missing recordings, transcripts and LMS matches land in the same exception queue, tagged `backfill`.

## 19. Production rollout and rollback plan

The rollout has four stages, each gated by numbers from the previous one. Every stage can be stopped with `systemctl stop positive-moments-publisher` without touching QA.

| Stage | What runs | Bunny writes | Exit criteria |
| --- | --- | --- | --- |
| 0. Migrations | 024 (amended), 025, 026 applied after approval; pilot manifest backfilled | none | release gate green; pilot rows present; QA cycles at 21:00 and 23:00 unchanged for 2 days |
| 1. Shadow (7 days) | discovery, LMS match, transcript, recording resolution, **plan-only** import and caption | none | ≥ 95% of LMS-matched lectures reach "would import"; 0 duplicate plans; exception queue reviewed daily |
| 2. Staging library 786045 (7 days) | full publisher, cap 5 imports a day | staging only | all imports verified; 0 orphans or duplicates; 3 manual playback checks per day (start, middle, end); end to fully ready p90 under 3 h |
| 3. Production library | new library with token auth + CDN token auth; LMS integration behind an LMS flag | production | LMS team sign-off; enrolment checks tested; caption spot checks for 1 week |

**Rollback.**

- Stop the unit. Its jobs stay queued, and nothing is lost.
- To withdraw videos from learners, set `visibility_state='WITHDRAWN'`; the API then returns 409, and the Bunny videos stay.
- Code rollback is a git checkout of the previous version, as with every other service.
- The schema is additive and stays. A table rollback uses the guarded rollback files, and only while they are empty.

**Deployment specifics:**

- Add the unit file to `deploy/systemd/`, unlike the three units that exist only on the VPS today.
- Restart with `systemctl restart --no-block`.
- Keep the publisher on its own env file so a QA key rotation can never stop it.

## 20. Implementation tasks in dependency order

P0 tasks are needed for the shadow stage, P1 for staging imports, and P2 for production and scale. Each task depends only on tasks above it.

| ID | Task | Depends on | Size |
| --- | --- | --- | --- |
| P0-1 | Decisions in section 22 (ownership, `NO_LMS_SESSION` rule, deleting failed imports, alert channel) | — | meeting |
| P0-2 | Migrations 024 (amended), 025, 026 + rollbacks + integration tests on the test DB | P0-1 | M |
| P0-3 | `app/publisher/queue.py`: claim/lease/complete/retry repository (media-runner pattern) + events + heartbeat | P0-2 | M |
| P0-4 | `app/publisher/lms.py`: read-only occurrence loader + matching rules v1 + `lecture_lms_matches` writer | P0-2 | M |
| P0-5 | Transcript job: direct `acquire_day` / selection with own commit, `qa_evidence_repository=None`, per-lecture advisory lock, skip-fresh re-fetch | P0-3 | S |
| P0-6 | Recording job: QA-free precondition adapter (call id from Graph recordings or the artifact `provider_call_id`) + the existing discovery/matching/resolution; single-part only | P0-3 | M |
| P0-7 | `publisher-daemon` CLI + scheduler thread + shadow mode (plan-only import/caption) + systemd unit in `deploy/systemd/` | P0-3 … P0-6 | M |
| P0-8 | Status view, exception queue, daily reconciler (checks 1–9), operations endpoint | P0-4 … P0-7 | M |
| P1-1 | Import job: split `pilot.py` into submit / identify / poll / verify handlers; capacity admission; retire + retry | P0-7 | M |
| P1-2 | Caption job: per part, from the selected part's raw VTT; replacement guard | P1-1 | S |
| P1-3 | Pilot manifest → tables backfill script | P0-2 | S |
| P1-4 | Title with LMS occurrence id + `metaTags`; retitle on match change | P0-4, P1-1 | S |
| P1-5 | Alerts (heartbeat, review age, orphans, circuit) | P0-8 | S |
| P2-1 | Multipart mapping (section 9) + one video per part | P1-1 | L |
| P2-2 | Production Bunny library with token auth and CDN token auth; secret rotation runbook | P0-1 | S |
| P2-3 | LMS read API + service token + audit (Django) | P0-8 | M |
| P2-4 | Enable tenant search for recordings; per-source coverage alerts | P0-6 | S |
| P2-5 | Backfill runs (lower priority) + storage forecast | P1-1 | M |
| P2-6 | Optional: Bunny webhook to cut encode polling | P1-1 | S |

**Required tests** (automated unless marked):

- **Unit, with fakes:**
  - new lecture, both artifacts ready;
  - recording before transcript, and transcript before recording;
  - OpenAI key absent or QA failed (the publisher never imports `app/qa`);
  - missing, ambiguous, rescheduled and cancelled-twin LMS matches;
  - multipart refused, then mapped;
  - URL expired (status 6 → retry with a new URL);
  - Graph 401/403/429/5xx and DNS;
  - Bunny 429/5xx and Fetch accepted but the client timed out;
  - encoding stalled; caption timeout and caption replacement;
  - crash after `SUBMITTING`;
  - two workers racing for one lecture;
  - a day offline;
  - DriveItem changed; recurring series across dates; duplicate titles on one date;
  - no transcript; no supported file identity;
  - LMS database down.
- **Integration** on `kbc_qa_integration_test`: the claim/lease SQL under two concurrent connections, unique-index duplicate refusal, migrations and their rollbacks.
- **Staging soak:** 7 days of shadow, then 7 days of staging imports.
- **Manual:** playback and caption sync at start, middle and end on 3 videos a day during staging.

## 21. GO/NO-GO checklist

Production GO needs every box ticked. Today none are, because nothing beyond the pilot has been built.

- [ ] Video-hosting ownership agreed with the LMS team (Bunny publisher vs the LMS blob archive)
- [ ] Migrations 024/025/026 approved, applied, and the pilot record migrated with matching ids
- [ ] Publisher unit running in its own env file with **no** OpenAI key; QA 21:00/23:00 cycles unchanged for 7 days
- [ ] 7-day shadow: ≥ 95% of LMS-matched lectures planned correctly; 0 duplicate plans; every exception has a reason code
- [ ] 7-day staging: all imports verified; 0 orphan or duplicate Bunny videos; manual sync checks passed
- [ ] End to fully ready p90 under 3 h on staging
- [ ] Multipart either mapped and verified on at least 2 real lectures, or explicitly held in `REVIEW_REQUIRED` with an alert
- [ ] Production library: player token auth on, direct play off, CDN token auth on, referrers restricted to LMS domains
- [ ] LMS read API live with service token; LMS enrolment check and token signing tested by the LMS team
- [ ] Alerts firing to an agreed channel (heartbeat, review age, orphans)
- [ ] Runbook: stop, withdraw a video, rotate keys, replay a failed job
- [ ] Release gate green, including the new concurrency integration tests

## 22. Remaining risks, unknowns and questions

**Decisions needed before building (P0-1):**

1. **Who hosts lecture video?** The LMS already syncs Teams recording and transcript artifacts, uploads some to blob storage, and tracks learner viewing per `artifact_id`. Running both pipelines doubles Graph downloads and storage. Recommendation: Bunny becomes the playback source; the LMS keeps its viewing analytics, re-keyed to the publisher's video mapping.
2. **`NO_LMS_SESSION` lectures** (18 of 81, including the pilot's own lecture): skip, or import as "LMS unassigned"?
3. **May the publisher delete a Bunny video?** Only a confirmed-failed empty import, before a retry. Otherwise those stay as recorded orphans.
4. **Alert channel:** email, a Teams webhook, or the operations console only?
5. **Keep original files on Bunny?** +1.06 GB per lecture today.

**Unknowns to validate:**

- 213 recording artifacts for 111 completed LMS occurrences: multipart, re-recordings or duplicates? **\[LMS\]**
- Graph pre-authenticated URL lifetime and Bunny's behaviour on expiry mid-download. **\[ASSUMPTION\]**
- VPS unit files and DNS reliability on srv1281908 (not in the repo; the developer machine's DNS failed 5 of 6 runs). **\[ASSUMPTION\]**
- That each recurring occurrence carries its own iCalUId, and that event ids are mailbox-scoped. **\[ASSUMPTION\]**
- Tenant search region value, and whether it finds recordings outside `Recordings` folders. **\[ASSUMPTION\]**
- Whether the scheduler daemon's missing `prepare_threshold=None` matters on the pooler (not set in `daemon.py` or `backfill._open`). **\[CODE\]**

**Risks:**

| Risk | Likelihood | Effect | Mitigation |
| --- | --- | --- | --- |
| Publisher and QA write the same transcript day at once | medium | one transaction fails and retries | shared per-lecture advisory lock; unique constraints already present |
| A series meeting marks a lecture's transcript complete using another date's artifact | medium | captions from the wrong lecture | per-occurrence readiness from selection only; never "any artifact" |
| LMS edits an occurrence after the video is shown | low | mapping points at a moved session | `STALE` + review; never auto re-point a visible video |
| Bunny CDN open without token auth | high today | anyone with a GUID can fetch media | P2-2 before any learner access |
| Free-encoding queue delays at peak | medium | late availability | measured 17 min; alert at 6 h stalled; premium is a setting, not a redesign |

**Recommended implementation sequence for the new-lecture publisher:**

1. Decisions (P0-1).
2. Migrations on the test DB (P0-2).
3. Queue (P0-3).
4. LMS matcher (P0-4).
5. Transcript and recording jobs (P0-5, P0-6).
6. Daemon in shadow (P0-7).
7. Status and reconciler (P0-8).
8. 7-day shadow.
9. Import and caption jobs (P1-1, P1-2).
10. Staging soak.
11. Production library + LMS API (P2-2, P2-3).
12. Multipart (P2-1).
13. Backfill (P2-5).

**Nothing has been implemented, migrated or deployed in this phase; approval is needed to start P0.**
