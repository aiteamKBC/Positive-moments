# Phase 1 implementation and validation report

Validation date: 2026-09-14. Historical target: 2026-09-04.

## Before changes

The repository contained a Django Positive Mentions dashboard, a verified Python Graph app-only client and lecture-parts planner, Positive Clips n8n/SQL automation, a Node media worker, and the existing QA/media/splitting schemas. It did not contain a coded calendar discovery service or canonical lecture registry. The working tree already had user changes (`.gitignore`) and untracked `automation/` and `services/`; they were preserved.

## Created architecture

- `app/config`, `app/common`, `app/db`, `app/graph`, `app/lectures`, and `app/cli` implement the Phase 0/1 boundary.
- `docs/architecture/current_state.md`, `docs/architecture/target_state.md`, and `docs/migration/phase_plan.md` document the baseline, target, and phases 0-7.
- `automation/legacy_n8n/README.md` documents the sanitized legacy path and references existing exports.
- `tests/unit` and `tests/integration` cover discovery rules and rollback-isolated database behavior.

## Database migration and final schemas

`app/db/migrations/001_create_lecture_discovery_tables.sql` was applied successfully. It created only:

- `public.lecture_sessions`: UUID `lecture_id`; source/calendar identities; meeting and Join URL fields; subject/normalized subject/module; scheduled and Graph meeting timestamps; Cairo business date; timezone/organizer/type; mapping, match, and discovery statuses; cancellation flag; discovery/audit timestamps; JSONB metadata. It has occurrence uniqueness, status/time checks, and date/meeting indexes.
- `public.lecture_discovery_runs`: UUID `run_id`; target date; start/completion timestamps; shadow mode; event/resolution/match/unmatched/error counters; status; JSONB metadata. It has non-negative counter and enum checks plus a date index.

Post-migration and post-integration-test counts were zero registry rows and zero run rows for 2026-09-04. The integration test inserts and updates only the two new tables inside a transaction and rolls it back.

## Discovery behavior

- Identity is deterministic UUIDv5 from the case-folded explicit calendar owner and occurrence `iCalUId`, falling back to `calendar_event_id` only when needed. Titles, meeting/transcript/recording IDs, and legacy `session_id` are excluded.
- The existing `GraphAppClient` is reused through an adapter. It uses OAuth client credentials, `.default` scope, a refresh-before-expiry memory cache, sanitized errors, and no `/me` endpoint.
- A Cairo local date is converted to consecutive timezone-aware local midnights and then UTC; database operational times are `timestamptz`; `session_date` is derived in `Africa/Cairo`.
- Cancelled events and events without a Teams Join URL are excluded. Eligible malformed events fail explicitly.
- Online meeting candidates are never correlated by position. Scheme/host/default port, path encoding/trailing slash, query ordering, and fragment are canonicalized; exactly one canonical equality is required. Zero and multiple matches remain distinct review statuses.
- Aptem access preserves the supplied active-group SQL. Matching is lowercase + HTML entity decode + whitespace collapse + trim, followed by exact equality. No fuzzy matching exists.
- `--dry-run` performs discovery but invokes neither write repository. `select-lecture` returns one exact result, rejects zero, and requires Cairo `--start HH:MM` when duplicates remain.

## Historical shadow validation

The earlier Phase 1 command stopped safely before Graph access because `KBC_LECTURE_CALENDAR_USER_UPN` was not configured. Phase 1.5 subsequently established that Aptem uses a separate physical database. The earlier zero-active-group result came from the KBC connection and is non-authoritative for Aptem validation. `APTEM_DATABASE_URL` is now required, has no fallback to `DATABASE_URL`, and is opened with a database-enforced read-only transaction.

Read-only evidence currently shows:

| Metric | Result |
| --- | ---: |
| Calendar events | Blocked by missing calendar owner |
| Eligible Teams events | Not queried |
| Online meetings resolved | Not queried |
| Active Aptem groups under preserved rule | Not validated against the correct Aptem database |
| Active Aptem matches | Not computable |
| Registry rows for 2026-09-04 | 0 |
| Discovery run rows for 2026-09-04 | 0 |
| Existing QA rows for 2026-09-04 | 6 |
| Existing QA rows with meeting ID | 6 |
| Distinct QA meeting IDs | 6 |
| Distinct normalized QA subjects | 6 |
| Meeting-ID / subject matches | Not computable without calendar results |

The QA rows are validation evidence only and may omit calendar events that never reached QA. Two local settings remain required for Phase 1.5 real validation: `KBC_LECTURE_CALENDAR_USER_UPN` and `APTEM_DATABASE_URL`. No discrepancy was automatically changed.

## Test evidence

`python -m pytest -q` reports **22 passed**. Coverage includes token caching, Cairo windows, aware timestamps, calendar filtering, JoinWebUrl canonicalization, exact/not-found/ambiguous resolution, Graph auth/permission taxonomy, Aptem normalization/exact matching, duplicate-title identity, recurring occurrences, rerun identity, dry-run no-write, registry idempotency/update, transactional repository rollback, and 0/1/many single-lecture selection.

## Tables and external effects

Production objects changed: `public.lecture_sessions` and `public.lecture_discovery_runs` were created. Test DML against them was rolled back. `public.aptem_auto_extracting` and `public.qa_doctors_sessions` were queried read-only. `information_schema` was queried for inspection. No other production table was read for discovery or changed.

Existing n8n workflows, QA behavior, Positive Clips, lecture-parts behavior, and the media worker are unchanged. No media job or transcript was created; no transcript processing, AI analysis, FFmpeg, SharePoint upload, scheduler, or downstream workflow ran. Aptem, attendance, and users source tables are unchanged. No secret, access token, Authorization header, or database password was logged.

## Exact Phase 2 entry point

After configuring the calendar owner and reviewing successful Phase 1 shadow comparisons, start with an additive `public.lecture_transcript_artifacts` table whose artifact identity is independent and whose `lecture_id` foreign key points to `public.lecture_sessions`. Implement acquisition as shadow-only storage first; do not write `qa_doctors_sessions` or reinterpret `session_id`.
