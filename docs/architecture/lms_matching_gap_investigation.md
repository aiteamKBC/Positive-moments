# LMS Matching Gap Investigation — 1–20 October 2026

Targeted, read-only follow-up to [kent_lms_bunny_publisher_audit.md](kent_lms_bunny_publisher_audit.md) (sections 5, 8 and 22). This report resolves the 18 unmatched lectures, the 4 ambiguous lectures and the 11 LMS occurrences missing from Lecture Intelligence (LI), and states the matching and discovery changes the Continuous Bunny Publisher needs.

Evidence labels: **[LMS]** read from the LMS database · **[PROD]** read from the LI production database · **[CODE]** verified in this repository · **[ASSUMPTION]** not verified.

Companion files (git-ignored):

- `output/lms_matching/unmatched_lectures_20261001_20261020.csv` (18 rows)
- `output/lms_matching/missing_lms_occurrences_20261001_20261020.csv` (11 rows)

## 1. Method and safeguards

- LMS: the `LMS_DATABASE_URL` connection, opened inside `SET TRANSACTION READ ONLY` after a write probe was refused. LI: `tools.audit_qa_core_production.connect_read_only` (refused write probe, `transaction_read_only = on`), client-side binding only (pooled server).
- Every query had a `statement_timeout` (30–120 s). No writes, migrations, Graph calls, Bunny calls or OpenAI calls were made. No code in the repository was changed.
- Nothing personal is exported: no attendee, learner or viewer data was read into the outputs; organizer emails were compared only as booleans or pseudonyms; join URLs, passcodes and Graph meeting ids are not written anywhere (Teams thread keys appear only as hashes in scratch evidence).
- Teams identity was compared three ways: canonical join URL (`app/graph/meetings.canonicalize_join_url`), the Teams **meeting key** (thread id + message id, taken from the join-URL path or decoded from the Graph online-meeting id `1*<organizer>*<messageId>**<thread>`), and the raw online-meeting id.

## 2. Reproduced baseline

Window: LI `session_date` 2026-10-01 … 2026-10-20; LMS `scheduled_start` in the same dates (naive timestamps are UTC). Rule: canonical occurrence join URL + exact UTC start. **[PROD] [LMS]**

| Measure | Audit | Reproduced |
| --- | --- | --- |
| LI lectures | 81 | 81 |
| Exactly one occurrence | 59 | 59 |
| Ambiguous (>1 occurrence) | 4 | 4 |
| No occurrence | 18 | 18 |
| LMS occurrences in window | 158 | 158 (96 scheduled, 56 completed, 6 cancelled) |
| Completed occurrences with no LI lecture | 11 of 56 | 11 of 56 |

The baseline reproduces exactly. Matching on the Teams meeting key instead of the canonical URL gives the same result for all 81 lectures (59 single, 4 double, 18 none), so the gap is not caused by URL formatting.

## 3. Tables and relationships investigated

A catalog sweep of all 31 LMS schemas found every column whose name refers to meetings, events, calls, calendars, recordings, transcripts or occurrences. The relevant ones:

| Table | Rows (approx.) | Role for matching | Finding |
| --- | --- | --- | --- |
| `curriculum.live_sessions` | 107 | Series: module link, organizer, series join URL, `status`, `meeting_type` | Statuses: active 87 (84 `live-session`, 3 `linked-external`), `week-meeting` 9 (`week-additional`), `superseded` 7, `cancelled` 3, `feedback-test` 1. 2 series have `module_catalogue_id IS NULL`. **[LMS]** |
| `curriculum.live_session_occurrences` | 1,146 | **Per-lecture identity** (`id`), start/end, occurrence join URL, `status` | FK `live_session_id → live_sessions(id)` `NOT NULL … ON DELETE CASCADE`; **0 orphans**. Occurrence join URL differs from the series URL in 6 rows; occurrence `online_meeting_id` is empty in 1,081 rows. **[LMS]** |
| `curriculum.live_session_artifacts` | 350 | Recordings (216) and transcripts (134), each with `call_id` and `content_correlation_id` | FK to occurrences. No call id or correlation id of the 18 unmatched lectures appears here. **[LMS] [PROD]** |
| `curriculum.modules` | 327 | Title, programme, cohort, schedule, `source_type` (`module_authoring`, `mba-legacy`, …), `teams_shared_source_module_id` | Used as LI's second eligibility source (`app/db/repositories/lms.py`). Includes test programmes ("Final Test", "Test Lms", "Test"). **[LMS] [CODE]** |
| `curriculum.module_teams_shares` | 5 | Module → source module whose Teams meeting it reuses | The LMS mechanism for several modules sharing one meeting. **[LMS]** |
| `curriculum.teams_calendar_integrity_events` | 56,928 | LMS calendar audit (`lifecycle_*`, `verification_*`, `session_reassociated`, `calendar_reconciled`) | 17 `session_reassociated` events on 19 Oct moved Ray MSP session numbers to different occurrences (section 6.7). **[LMS]** |
| `curriculum.teams_calendar_sync_state` | 64 | Per-series snapshot of the Outlook series | No snapshot mentions any unmatched meeting. **[LMS]** |
| `curriculum.teams_calendar_create_claims` | 26 | LMS meeting-creation leases | 24 `done`, 2 `teams_calendar_already_exists`; none for the unmatched modules. **[LMS]** |
| `curriculum.teams_graph_failures` | 0 | Graph failures per meeting | Empty. **[LMS]** |
| `curriculum.live_session_recording_views`, `_join_launches`, `_attendance*`, `session_result_archive` | 81–1,760 | Learner-facing usage keyed by occurrence id | Confirms occurrence id as the LMS playback key; not needed for matching (learner data, not read). **[LMS]** |
| `Coach.coach_calendar_event`, `Coach.coach_meeting_artifacts`, `enrolment.Enrolment_Reviews` | 10–295 | Coaching and enrolment-review meetings (separate domain) | Not lectures; no hit for the unmatched meetings. **[LMS]** |
| `Learner.verified_teams_attendance`, `kbc_sync.*`, `public.kbc_combined_attendance` | — | Attendance keyed by `occurrence_id` | Downstream of occurrences; not an identity source. **[LMS]** |

LI side: `lecture_sessions` (discovery registry; one discovery mailbox), `lecture_transcript_candidates` → `lecture_transcript_artifacts` (`provider_call_id`, `content_correlation_id`), `lecture_recording_parts`, `lecture_recording_links`, `lecture_discovery_runs`. **[PROD]**

**Full-text check.**

- **What was searched:** the 6 Teams thread ids (raw and URL-encoded) and the 5 Graph meeting ids of the 18 unmatched lectures.
- **Where:** every text, varchar and json column of every base table in 11 schemas (`curriculum`, `Coach`, `Learner`, `enrolment`, `Engagement`, `Feedback`, `kbc_sync`, `public`, `LMS`, `knowledge`, `chat`).
- **Result: 0 hits in 332 tables.** **[LMS]**
- **Skipped (9 tables):** 5 tables over 300k rows (`curriculum.record_revisions`, `curriculum.quiz_answers`, `Learner.learner_progress_ksbs`, `Learner.learner_training_plan_components`, `Learner.source_lms_activity_results`); 1 timeout (`Learner.learner_month_restore_points`); 3 errors on unusual column types (`Aptem_users` ×2, `LMS.non_active_users`). None of the 9 is a meeting or calendar table.
- **Not scanned:** the other 20 schemas, which are audit, migration, backup and reporting copies.
- **Positive control:** the same method found the matched Ray MSP meeting in 1 series and its 16 occurrences.

## 4. Root causes

| # | Root cause | Lectures | Evidence |
| --- | --- | --- | --- |
| R1 | **The lecture runs in a Teams *channel* meeting that the LMS never linked.** The module exists in the LMS, but it has no live session, so no occurrence exists. | 15 | All 15 use `…@thread.tacv2/<messageId>` channel-meeting URLs. LMS-created meetings are private `19:meeting_…@thread.v2/0` meetings. Each module has 0 live sessions. The LMS can link channel meetings — 3 `linked-external` series were created on 14–15 Oct and match LI exactly (7 lectures). **[LMS] [PROD]** |
| R2 | **LI duplicate calendar entries.** A second Outlook event (subject with trailing spaces, different channel meeting, never resolved) for a slot whose real lecture already matches. | 3 | `metadata.duplicate_suppression` names a winner lecture that matches the LMS `linked-external` occurrence exactly. **[PROD]** |
| R3 | **Matcher does not exclude inactive LMS rows.** A cancelled occurrence, or a superseded series sharing the same meeting, creates a second candidate at the same start. | 4 (ambiguous) | Aya Modual: same series, one `scheduled` + one `cancelled` occurrence at each start. Test Teams Emails: active + `superseded` series share one meeting. All 7 same-series/same-start pairs in the LMS include a cancelled row. **[LMS]** |
| R4 | **LI discovery only registers events whose subject equals an Aptem group or LMS module title.** LMS week-additional events are not discovered. | 2 (LMS side) | Both genuine missing occurrences are `week-additional`. Their organizer *is* the LI discovery mailbox, so calendar coverage exists; the exclusion is at subject eligibility. **[LMS] [PROD] [CODE]** |
| R5 | **Test programmes leak into both systems.** LI eligibility reads all non-deleted LMS module titles, including test modules. | 9 of 11 missing; 4 ambiguous; 14 of the 59 exact | Programmes "Final Test", "Test Lms", "Test", series `feedback-test`. **[LMS] [PROD]** |

## 5. The 18 unmatched lectures

No unmatched lecture was recovered as an exact match. None of their meeting keys, online-meeting ids, call ids or correlation ids appears anywhere in the LMS.

### 5.1 Channel meetings with no LMS live session — 15 lectures, 5 series

| LI series | Lectures (dates) | LMS module evidence | Classification |
| --- | --- | --- | --- |
| Advanced Project and Logistics Management | 5 (5, 7, 12, 14, 19 Oct) | `MOD-2026091423231353355366EC83E5D5C3`, identical title, OTHM L7 — Al Fanar, Mon/Wed 11:00 UK, 16 Sep–18 Nov: fits every lecture; **0 live sessions** | `NOT_PRESENT_IN_LMS` ×5 |
| Stephen-Portfolio Management 2026 | 3 (1, 8, 15 Oct) | `mba-legacy` module with the identical title (no cohort, no schedule) and `module_authoring` "Stephen-Portfolio Management" (Oct 2025 cohort, Thu 09:00 UK, 23 Jul–29 Oct); **neither has a live session** | `NOT_PRESENT_IN_LMS` ×3 |
| AI in Project Control 2026 | 3 (2, 9, 16 Oct; 16 Oct is the pilot) | No module with this title. Two "AI in Project Control" modules (Project Controls L6, October 2026 cohort) are scheduled Jun–Sep **2027**, no live session | `NOT_PRESENT_IN_LMS` ×3 |
| Ray \| PMP – June 2026 | 2 (7, 14 Oct) | Closest is `mba-legacy` "Ray-Project Management Professional-June 2026", which ended 16 Aug; no live session. LI eligibility came from Aptem | `NOT_PRESENT_IN_LMS` ×2 |
| Sufian-Strategic Leadership | 2 (3, 10 Oct) | `mba-legacy` module, identical title, no live session. A **superseded** series "MBA - Strategic Leadership" (no module link) has occurrence #11 at the 10 Oct start, `scheduled`, 0 participants, a different Teams meeting; it has no 3 Oct occurrence | 3 Oct `NOT_PRESENT_IN_LMS`; 10 Oct `LMS_RECORD_INACTIVE_OR_CANCELLED` (candidate `OCC-9FB88A1844E74E4D8C93359031E58222`, review only) |

These are genuine lectures. 11 of the 15 have a written recording link, and 1 more is waiting on drive-item discovery; organizers are 5 different trainers, none of them the discovery mailbox (LI sees them as invited events). Same-start LMS occurrences in other series were checked and are unrelated modules with different meetings. They are listed in the CSV only where they carry a real signal.

Two cases need a human eye:

- **AI in Project Control, 2 Oct:** LI has no recording link. The same morning, the LMS ran a week-additional event "Stakeholder Management & Reporting" for the same programme (`OCC-7167…`, 08:00–12:00 UTC, 26 participants, recorded) in a different meeting. The lecture may have been replaced by that event, but this is not identity evidence.
- **Sufian, 10 Oct:** the superseded series looks like an abandoned LMS plan for this cohort. It must never become the identity, because it is superseded and its meeting was not the one used.

### 5.2 LI duplicate calendar entries — 3 lectures (Ray MSP, 2, 9, 16 Oct)

| Suppressed LI lecture | Winner LI lecture | Winner's exact LMS occurrence |
| --- | --- | --- |
| `d39b547f…` (2 Oct) | `a79f844b…` | `OCC-002B76DDFF4749BB8F48DE4BD0274BAB` |
| `b1ff02a2…` (9 Oct) | `7863f971…` | `OCC-2F48B538401E4DA3AB11AD4296299BA5` |
| `c752ce33…` (16 Oct) | `6eb9f25e…` | `OCC-F1820478BA8D4573B125B1FDD8A5E742` |

The suppressed rows are `ONLINE_MEETING_NOT_FOUND` / `REVIEW`, have no transcript and no recording link, and their channel meeting is absent from the LMS. **[PROD] [LMS]** They are classified `NOT_PRESENT_IN_LMS` with the sub-reason `LI_SUPPRESSED_DUPLICATE`, because the classification list has no duplicate category. They need no LMS action and are not lectures to publish.

## 6. Matching rules

### 6.1 The 4 ambiguous lectures

| LI lecture | Candidates at the same meeting key + start | Resolution with rule changes |
| --- | --- | --- |
| Aya Modual, 9 Oct | `OCC-69ED…` scheduled; `OCC-5C9E…` **cancelled** (same series) | `OCC-69ED12FA849742FAB5BB2A15F11FF444` |
| Aya Modual, 16 Oct | `OCC-6A51…` scheduled; `OCC-842C…` **cancelled** | `OCC-6A51FC5293BC40B482F82482734EEB6F` |
| Test Teams Emails, 7 Oct | `OCC-6478…` (active series); `OCC-063F…` (**superseded** series) | `OCC-6478D6A164D0475A9DDBE677317EB734` |
| Test Teams Emails, 14 Oct | `OCC-FE31…` (active, completed); `OCC-320F…` (**superseded**) | `OCC-FE3135F25F234FDAAC4901D15142EB60` |

All 4 become exact matches once cancelled occurrences and superseded series are excluded. All 4 belong to the "Final Test" programme, so the publisher would then exclude them as tests.

### 6.2 Recommended rule `meeting_key_start_v2`

1. **Key** = Teams meeting key (thread id + message id) from the occurrence join URL, the series join URL, or the decoded online-meeting id. The canonical join URL is kept as a confirming field. For private meetings the message id is `0`; for channel meetings it identifies the meeting within the channel, so a channel's different meetings never collide.
2. **Candidates** = occurrences with that key whose `status <> 'cancelled'`, whose series `status IN ('active','week-meeting')` (excluding `superseded`, `cancelled`, `feedback-test`), and whose module, when present, is not deleted.
3. **Start** must equal the LI `scheduled_start` exactly (naive LMS timestamp read as UTC; verified 67/67 in the audit).
4. Exactly one → `VERIFIED`. More than one → `AMBIGUOUS` (review, never take the first). Key match with a different start within ±7 days → `RESCHEDULED_CANDIDATE` (review). No key match → `NO_LMS_SESSION`.
5. Re-check until T_end + 24 h, then daily for 14 days. A verified occurrence that becomes cancelled, changes start, or whose series becomes superseded turns `STALE` and goes to review; a video already visible to learners is never re-pointed automatically.

On 1–20 Oct this gives **63 VERIFIED, 0 AMBIGUOUS, 18 NO_LMS_SESSION**, replacing the baseline's 59 / 4 / 18. 18 of the 63 are test programmes and are excluded by the publisher's scope rule (6.3).

### 6.3 Answers to the matching questions

1. **Additional LMS tables.** `live_sessions.status` and `meeting_type` (to drop superseded, cancelled and test series), `modules.programme_name` plus a test-programme list (scope), `module_teams_shares` (shared meetings), and `teams_calendar_integrity_events` (`session_reassociated`, used as a re-verification trigger). Artifacts, sync state and claims add no identity beyond the occurrence.
2. **Reliable keys.** Occurrence id (LMS identity); Teams meeting key + exact UTC start (join). The Graph online-meeting id is only a confirmation, because it is empty on 1,081 of 1,146 occurrences. Not reliable: `graph_event_id` (mailbox-scoped), `session_number` (re-associated on 19 Oct), and titles.
3. **Is join URL + UTC start sufficient?** Yes, as the join key, once it is restricted to active rows. Normalising to the meeting key protects against URL query-string variants: it agreed with the canonical URL in all 63 matched cases. It cannot find lectures the LMS never recorded; that is a coverage problem, not a matching problem.
4. **Safe fallbacks.** Only `RESCHEDULED_CANDIDATE` (same key, different start) and the LI duplicate winner rule. Title, module, cohort, trainer and same-time evidence are attached to `NO_LMS_SESSION` items as **review candidates** and never verify on their own, because same-start collisions are common: 14 of the 18 unmatched lectures share their start with unrelated occurrences, and the 8 Oct Stephen lecture with 14 of them.
5. **Recurring meetings.** One series join URL is shared by every occurrence, so the start time is what selects the occurrence. Never key on `session_number`: on 19 Oct the LMS re-bound 14 of the 16 Ray MSP session numbers to occurrences one week later and unbound one occurrence (17 `session_reassociated` events, `microsoftChanged=false`). Treat that event as a re-verification trigger.
6. **Several cohorts sharing one meeting.** The LMS records this through `module_teams_shares` (5 rows): the sharing module reuses the source module's meeting. None was observed producing two occurrences with the same key and start in this window. Recommended: one recording may map to N occurrences only when they share key and start *and* are linked through `module_teams_shares`; otherwise `AMBIGUOUS`. Video identity stays per recording part; LMS mapping is many-to-many.
7. **Cancellation and rescheduling.** A cancelled occurrence is never a candidate. Rescheduling in Teams changes the start, so the old match goes `STALE` and the new start is re-evaluated. A replaced series appears as `superseded` and must be excluded (it shares the active series' meeting in 4 module pairs).
8. **Occurrence without a parent series?** No: `live_session_id` is `NOT NULL` with an FK (`ON DELETE CASCADE`), and there are 0 orphans. A series without a module does exist (2 series, 19 scheduled occurrences), so the matcher must not require a module.
9. **Additional discovery sources?** Yes. LMS occurrences must become a second discovery input (section 7), because 2 genuine LMS sessions in this window never reached LI.
10. **A genuine lecture outside LMS scope.** It stays in `NO_LMS_SESSION` and is held under Policy A, reported daily to the LMS team with its review candidates, and escalated after 24 h. It leaves that state only through an explicit decision: the LMS links the meeting (then it verifies), or the series is put on an explicit out-of-scope list (then `OUT_OF_LMS_SCOPE`, published or not according to decision 2 of the audit). It is never silently dropped. No lecture in this window was *confirmed* out of scope: every one belongs to a programme the LMS already holds.

## 7. The 11 completed LMS occurrences missing from LI

| Classification | Count | Occurrences |
| --- | --- | --- |
| Test or non-production occurrence | 8 | M4, Test-Replace_Link, Test-Replac, test-main-additional, Final Teams Mo, M3 (programme "Final Test"); 2 × TEST QR feedback (series `feedback-test`, no join URL) |
| Existing lecture under another identity | 1 | Test Teams Emails 14 Oct (`OCC-FE31…`): LI lecture `6b39ffe1…` exists but was ambiguous (section 6.1); test programme |
| Additional/week-additional session — **genuine** | 2 | Stakeholder Management & Reporting, 2 Oct (`OCC-7167…`, Project Controls L6, 26 participants, 4 recordings); London Master Class Event, 2 Oct (`OCC-E47E…`, Marketing Executive L4, 47 participants, recorded) |

For the 2 genuine sessions:

- **Organizer:** both are organized by the LI discovery mailbox, so they were in the calendar LI reads. **[LMS] [PROD]**
- **Stakeholder Management & Reporting:** the title equals an active LMS module title, but LI never registered it. The Outlook subject that discovery actually saw could not be verified: the window's `lecture_discovery_runs` rows are all `SHADOW` and hold no non-canonical event list, and no Graph call was made. **[ASSUMPTION]** The subject differed from the LMS title.
- **London Master Class Event:** the title is neither an Aptem group nor a module title, so LI eligibility excludes it by design. **[CODE]**

Whether a masterclass should be published is a business decision (question 4 in section 10).

## 8. Summary

Lecture side (LI lectures, 1–20 Oct; each lecture counted once):

| Outcome | Lectures | Of which test programmes |
| --- | --- | --- |
| Original exact matches | 59 | 14 |
| Newly recovered exact matches (rule change, section 6.1) | 4 | 4 |
| Remaining ambiguous | 0 | — |
| LI duplicate calendar entries (slot already covered by an exact winner) | 3 | 0 |
| Confirmed missing LMS records (genuine lecture, no LMS occurrence) | 14 | 0 |
| LMS record exists but inactive (superseded series; review) | 1 | 0 |
| Confirmed outside LMS scope | 0 | — |
| **Total** | **81** | 18 |

**Unresolved: 15** — the 14 missing LMS records plus the 1 inactive record. All 15 need an LMS-team decision. They are the rows with `requires_lms_team_review = yes`; they are already counted above and not added again.

LMS side (completed occurrences, 1–20 Oct):

| Outcome | Occurrences |
| --- | --- |
| Matched to an LI lecture (baseline) | 45 |
| Matched after the rule change (Test Teams Emails, 14 Oct) | 1 |
| Test or non-production, no LI lecture | 8 |
| **Genuine LMS sessions missing from LI** | **2** |
| **Total** | **56** |

The 45 + 1 occurrences are among the 63 matched lectures, which map to 63 distinct occurrences. The other 17 matched lectures map to occurrences still marked `scheduled` after the session date, because LMS completion status lags. The publisher must not gate on occurrence `status`.

## 9. Discovery changes

1. **Keep LI calendar discovery as the primary source.** It found every LMS-matched lecture and the 15 channel-meeting lectures the LMS lacks.
2. **Add LMS occurrences as a second input.** Each publisher tick reads non-test occurrences whose start has passed. An occurrence without a verified LI lecture after T_end + 2 h becomes `LMS_ONLY_PENDING` and is reported. This would have caught both week-additional events. Turning such occurrences into lectures needs an LI discovery change: register a lecture from the LMS occurrence's meeting key and organizer, then let the normal transcript and recording stages run. That change goes in the implementation plan as a separate task, not a matching rule.
3. **Scope rule.** Exclude programmes on an explicit test list (`Final Test`, `Test Lms`, `Test`) and series `feedback-test`, plus deleted modules. Keep the list in configuration and review it with the LMS team, rather than matching names like "test" in code.
4. **Honour LI duplicate suppression.** Never treat a suppressed row as a lecture or a gap. **[CODE]** `app/orchestration/state.py` already short-circuits these rows.
5. **Coverage reconciliation, daily.** Every LI lecture and every in-scope past LMS occurrence must be in exactly one state: `VERIFIED`, `AMBIGUOUS`, `RESCHEDULED_CANDIDATE`, `NO_LMS_SESSION`, `LMS_ONLY_PENDING`, `OUT_OF_LMS_SCOPE`, `TEST_EXCLUDED` or `LI_DUPLICATE`. Each state's count is published, so nothing disappears silently.

## 10. Production implications

- **The current 59/81 understates real coverage.** Among *production* lectures, 45 of 63 verify (71%). The remaining 18 production lectures are 3 duplicates plus 15 lectures in 5 series whose meetings the LMS never linked. LI already has a written recording link for 11 of those 15.
- **The fix is mostly on the LMS side and is cheap.** Linking the 5 channel meetings as `linked-external` series (the 14–15 Oct procedure) would have verified all 15 in this window, with no fuzzy logic in the publisher.
- **Without the scope rule, the publisher would import test lectures.** 18 of the 63 verified lectures in this window are tests.
- **Migration 024b (`lecture_lms_matches`)** needs `match_rule` `meeting_key_start_v2`, the new states above, `lms_series_status` at match time, and a stored `meeting_key_hash`. A table `lms_coverage_reconciliation` (or a view) is needed for the LMS-only side. These are design notes only: nothing has been migrated.
- **No change to transcript or recording stages.** This investigation changes only matching, discovery scope and reporting.

## 11. Questions for the LMS team

1. Will you link the 5 channel-meeting series as `linked-external` live sessions? For Stephen Portfolio Management, which of the two modules is authoritative?
2. Which module and cohort do "AI in Project Control 2026" and "Ray | PMP – June 2026" belong to? The LMS modules with those names are scheduled for 2027 or ended in August.
3. Was the superseded "MBA - Strategic Leadership" series the plan for the Sufian cohort, and can it be retired?
4. Should week-additional events (e.g. London Master Class Event) be published to learners?
5. Can you confirm the test-programme list (`Final Test`, `Test Lms`, `Test`, `feedback-test`), or provide a flag on `modules` or `live_sessions`?
6. When two modules share a meeting (`module_teams_shares`), should one recording appear in both modules?
7. Does the LMS ever set occurrences to `completed` reliably? The occurrences for 17 matched lectures still show `scheduled`.

## 12. Final answer

**The safest strategy** is a deterministic, two-sided reconciliation, not a smarter matcher:

1. **Identity.** Use LMS `live_session_occurrences.id`, joined on the Teams meeting key (thread + message id) plus exact UTC start, over active rows only: no cancelled occurrences, no superseded, cancelled or test series. Exactly one candidate verifies. Anything else goes to an explicit review state, and the first candidate is never chosen.
2. **Discovery from both sides.**
   - **LI calendar discovery** remains the primary source.
   - **In-scope LMS occurrences** are a second source, so LMS-only sessions such as week-additional events surface as `LMS_ONLY_PENDING`.
   - **Coverage reconciliation:** every lecture and occurrence is accounted for in exactly one published state.
3. **No silent fallbacks.** Title, module, trainer and same-time evidence only ever produce review candidates. Lectures without an LMS occurrence are held as `NO_LMS_SESSION` and reported until the LMS links the meeting or the series is explicitly declared out of scope.

**Justifying findings:**

- **Unmatched lectures are a coverage problem, not a matching problem.** All 18 unmatched lectures are lectures the LMS does not hold (15 lectures in 5 series) or LI duplicates (3). None of their meeting identifiers appears in any of the 332 LMS tables scanned (positive control passed), so no matcher could have recovered them.
- **The precise rule produced no false positives.** Every exact match on key + start was unique once inactive rows were excluded (63/63). Same-start collisions with unrelated modules happened on 14 of the 18 unmatched lectures, so title or time matching would have produced wrong identities.
- **Ambiguity came from inactive LMS rows.** All 4 ambiguous cases, and all 7 same-series duplicate starts in the LMS, involve a cancelled occurrence or a superseded series.
- **LI misses LMS-only sessions.** 2 genuine LMS sessions (73 participants in total) never reached LI. Only an LMS-side input closes that gap.
- **Session numbers are not stable.** The LMS re-bound 14 session numbers in one series on 19 Oct, so the key must be the occurrence plus the meeting, never `session_number`.
- **Tests leak into both systems.** 18 of the 63 verified lectures and 9 of the 11 missing occurrences are test-programme rows, so an explicit scope rule is required.

Nothing was implemented, migrated or deployed. Approval is needed before changing the matcher, discovery or migration 024b.
