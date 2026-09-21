# Target state: Phase 1

The only active Phase 1 flow is:

```text
Microsoft Graph /users/{configured-user}/calendarView
                         |
                         v
       eligible Teams calendar occurrences
                         |
              exact canonical JoinWebUrl
                         |
                         v
Microsoft Graph /users/{configured-user}/onlineMeetings
                         |
                         +---- Aptem source database (APTEM_DATABASE_URL)
                         |       public.aptem_auto_extracting (SELECT only)
                         |       exact normalized Group match
                         v
              Python discovery service
                    |             |
                    v             v
       public.lecture_sessions   public.lecture_discovery_runs
```

No downstream consumer is connected. The service is manual, app-only, explicitly user-scoped, and in shadow mode. It never treats a Graph failure as an empty calendar result.

## Identity and idempotency

`lecture_id` is UUIDv5 in a fixed application namespace over:

`microsoft_graph_calendar + case-folded calendar owner + iCalUId`

Graph documents `iCalUId` as different for each recurring-series occurrence, so it is the preferred stable occurrence key. If it is absent, the event `id` is the fallback. Subject, time, meeting ID, transcript ID, recording ID, and legacy `session_id` are not identity inputs. Thus duplicate titles remain distinct, reruns update the same row, and an edited occurrence retains identity. Database uniqueness independently protects both `(source_system, calendar_user_upn, calendar_event_id)` and non-null `(source_system, lower(calendar_user_upn), i_cal_uid)`.

## Safety boundary

The KBC application connection (`DATABASE_URL`) owns `lecture_sessions`, `lecture_discovery_runs`, and QA validation reads. A physically separate, transaction-enforced read-only connection (`APTEM_DATABASE_URL`) is required for Aptem. There is no fallback between roles. The only write repositories target the two new KBC tables. Aptem is queried with the preserved active-program SQL. Calendar entries that are cancelled or lack a Teams Join URL are excluded, and retrieval requests Outlook immutable IDs. Online meetings are accepted only when exactly one returned record has an exact canonical URL match; zero and multiple exact matches are review statuses.
