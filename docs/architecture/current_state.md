# Current state before the coded discovery migration

## Python and web components

- `backend/` is a Django 6 + Django REST Framework dashboard. Its unmanaged models read `public.qa_doctors_sessions` and `public.qa_positive_clip_assets`; Django does not own those tables.
- `automation/lecture_parts/` contains the verified Python Microsoft Graph app-only client, transcript/recording lookup helpers, and the semantic three-part planner. Phase 1 reuses only its Graph client through `app/graph/auth.py` and does not execute the planner.
- `automation/positive_clips/run_producer.py` and the exported n8n workflows support the existing Positive Clips path.
- Before this change there was no coded canonical calendar-occurrence registry. `qa_doctors_sessions` is a processed QA result table, not lecture discovery's source.

## Relevant production database objects

Read-only inspection on 2026-09-14 confirmed these existing objects:

| Area | Objects | Current role |
| --- | --- | --- |
| QA | `qa_doctors_sessions`, `qa_doctors_checklist_items`, `qa_perfect_lectures` | Processed QA sessions, checklist results, and perfect-lecture reporting |
| Positive Clips | `qa_positive_clip_assets` | Delivered clip asset state |
| Lecture splitting | `qa_lecture_split_plans`, `qa_lecture_part_assets` | Three-part plans and delivered part assets |
| Media | `qa_media_jobs` | Shared Positive Clips / lecture-parts queue |
| External sources | `kbc_users_data`, `kbc_attendance` in KBC; authoritative `aptem_auto_extracting` in a separate Aptem database | Populated outside this migration; read-only here |

The KBC database also contains an object named `public.aptem_auto_extracting`, but legacy n8n uses a separate credential for the authoritative source. Phase 1.5 therefore prohibits using the KBC connection for Aptem. The existing migrations use `timestamptz` for operational timestamps and `jsonb` for metadata. The new registry follows those conventions and has no foreign key to a QA or media table.

## Graph and worker

`automation/lecture_parts/graph_client.py` already implements OAuth 2.0 client credentials, an in-memory token cache with pre-expiry refresh, pagination restricted to the configured Graph base URL, and sanitized exceptions. It uses no `/me` endpoint and does not log tokens. The media worker in `services/kbc-media-worker/` claims `qa_media_jobs`, invokes FFmpeg, uploads output, and records asset state; it is outside Phase 1 and remains unchanged.

## n8n compatibility points

Production orchestration remains in n8n. Repository exports exist for Positive Clips and lecture-parts completion, but the inspected repository does not contain an export of the upstream Calendar -> online meeting -> Aptem -> QA One Lecture discovery workflow. The coded application therefore introduces an independent shadow registry and does not change `session_id`, existing workflows, queue contracts, or worker state.
