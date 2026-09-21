import json

from app.common.errors import DATABASE_ERROR, PlatformError


class LectureSessionRepository:
    """
    Persistence for the canonical KBC lecture registry.

    Only Aptem-matched Teams occurrences reach this repository; the service
    filters non-canonical calendar events out before persistence, and the
    table's own `lecture_sessions_canonical_scope` CHECK enforces it.
    """

    def upsert(self, connection, lecture) -> str:
        if lecture.group_match_status != "MATCHED":
            raise PlatformError(
                DATABASE_ERROR,
                "only active-Aptem-matched occurrences belong in the canonical lecture registry",
            )
        try:
            row = connection.execute("""
            INSERT INTO public.lecture_sessions (
                lecture_id, source_system, calendar_user_upn, calendar_event_id, i_cal_uid,
                meeting_id, join_url, subject, normalized_subject, module,
                scheduled_start, scheduled_end, session_date, calendar_timezone,
                calendar_organizer_address, meeting_organizer_user_id,
                organizer_validation_status, meeting_lookup_user_id,
                meeting_lookup_context_source, join_url_oid_hint_status,
                graph_meeting_subject, graph_meeting_start,
                graph_meeting_end, meeting_type, calendar_mapping_status,
                group_match_status, discovery_status, is_cancelled,
                downstream_ready, metadata
            ) VALUES (
                %(lecture_id)s, 'MICROSOFT_GRAPH_CALENDAR', %(calendar_user_upn)s,
                %(calendar_event_id)s, %(i_cal_uid)s, %(meeting_id)s, %(join_url)s,
                %(subject)s, %(normalized_subject)s, %(module)s, %(scheduled_start)s,
                %(scheduled_end)s, %(session_date)s, %(calendar_timezone)s,
                %(calendar_organizer_address)s, %(meeting_organizer_user_id)s,
                %(organizer_validation_status)s, %(meeting_lookup_user_id)s,
                %(meeting_lookup_context_source)s, %(join_url_oid_hint_status)s,
                %(graph_meeting_subject)s, %(graph_meeting_start)s,
                %(graph_meeting_end)s, %(meeting_type)s, %(calendar_mapping_status)s,
                %(group_match_status)s, %(discovery_status)s, %(is_cancelled)s,
                %(downstream_ready)s, %(metadata)s::jsonb
            )
            ON CONFLICT (lecture_id) DO UPDATE SET
                calendar_event_id = EXCLUDED.calendar_event_id,
                i_cal_uid = EXCLUDED.i_cal_uid,
                meeting_id = EXCLUDED.meeting_id,
                join_url = EXCLUDED.join_url,
                subject = EXCLUDED.subject,
                normalized_subject = EXCLUDED.normalized_subject,
                module = EXCLUDED.module,
                scheduled_start = EXCLUDED.scheduled_start,
                scheduled_end = EXCLUDED.scheduled_end,
                session_date = EXCLUDED.session_date,
                calendar_timezone = EXCLUDED.calendar_timezone,
                calendar_organizer_address = EXCLUDED.calendar_organizer_address,
                meeting_organizer_user_id = EXCLUDED.meeting_organizer_user_id,
                organizer_validation_status = EXCLUDED.organizer_validation_status,
                meeting_lookup_user_id = EXCLUDED.meeting_lookup_user_id,
                meeting_lookup_context_source = EXCLUDED.meeting_lookup_context_source,
                join_url_oid_hint_status = EXCLUDED.join_url_oid_hint_status,
                graph_meeting_subject = EXCLUDED.graph_meeting_subject,
                graph_meeting_start = EXCLUDED.graph_meeting_start,
                graph_meeting_end = EXCLUDED.graph_meeting_end,
                meeting_type = EXCLUDED.meeting_type,
                calendar_mapping_status = EXCLUDED.calendar_mapping_status,
                group_match_status = EXCLUDED.group_match_status,
                discovery_status = EXCLUDED.discovery_status,
                is_cancelled = EXCLUDED.is_cancelled,
                downstream_ready = EXCLUDED.downstream_ready,
                last_discovered_at = now(), updated_at = now(),
                -- Discovery owns its own diagnostics and rewrites them every
                -- run. The Phase 4C1 duplicate suppression annotation is NOT
                -- a discovery diagnostic: it is a decision made about this
                -- occurrence afterwards, and a nightly rediscovery must not
                -- quietly erase it. Carried across explicitly, by name, so
                -- the exception is visible rather than a merge that silently
                -- preserves whatever else happens to be there.
                metadata = EXCLUDED.metadata || CASE
                    WHEN public.lecture_sessions.metadata ? 'duplicate_suppression'
                    THEN jsonb_build_object('duplicate_suppression',
                             public.lecture_sessions.metadata -> 'duplicate_suppression')
                    ELSE '{}'::jsonb END
            RETURNING (xmax = 0) AS inserted
            """,
                {**lecture.as_record(), "metadata": json.dumps(lecture.metadata)},
            ).fetchone()
        except PlatformError:
            raise
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "lecture registry upsert failed") from exc
        return "created" if row[0] else "updated"

    def select_exact(self, connection, target_date, subject: str) -> list[dict]:
        try:
            rows = connection.execute("""
            SELECT lecture_id, subject, scheduled_start, meeting_id, discovery_status, downstream_ready
              FROM public.lecture_sessions
             WHERE session_date = %s AND subject = %s AND NOT is_cancelled
             ORDER BY scheduled_start, lecture_id
            """,
                (target_date, subject),
            ).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "lecture registry selection failed") from exc
        return [
            dict(lecture_id=str(row[0]), subject=row[1], scheduled_start=row[2],
                 meeting_id=row[3], discovery_status=row[4], downstream_ready=row[5])
            for row in rows
        ]

    def count_day(self, connection, target_date) -> int:
        try:
            return connection.execute(
                "SELECT count(*) FROM public.lecture_sessions WHERE session_date = %s",
                (target_date,),
            ).fetchone()[0]
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "lecture registry count failed") from exc
