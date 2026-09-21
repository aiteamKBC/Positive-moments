import urllib.parse
from datetime import date

from app.common.errors import CALENDAR_QUERY_ERROR, INVALID_CALENDAR_EVENT, PlatformError
from app.common.time import cairo_day_utc_window, parse_graph_datetime
from app.lectures.models import CalendarDiscoveryBatch, CalendarEvent
from app.graph.errors import translate_graph_error
from app.graph.transport import GraphError


class CalendarGateway:
    def __init__(self, client, calendar_user_upn: str):
        self.client = client
        self.calendar_user_upn = calendar_user_upn

    def discover_calendar_events(self, target_date: date) -> CalendarDiscoveryBatch:
        start, end = cairo_day_utc_window(target_date)
        query = urllib.parse.urlencode({
            "startDateTime": start.isoformat().replace("+00:00", "Z"),
            "endDateTime": end.isoformat().replace("+00:00", "Z"),
            "$select": "id,iCalUId,subject,start,end,isCancelled,isAllDay,type,"
                       "onlineMeeting,onlineMeetingUrl,organizer",
        })
        owner = urllib.parse.quote(self.calendar_user_upn, safe="")
        try:
            rows = self.client.get_collection(
                f"/users/{owner}/calendarView?{query}",
                headers={"Prefer": 'IdType="ImmutableId"'},
            )
        except GraphError as exc:
            raise translate_graph_error(exc, CALENDAR_QUERY_ERROR, "calendar query") from exc
        eligible: list[CalendarEvent] = []
        for row in rows:
            if row.get("isCancelled") is True:
                continue
            online = row.get("onlineMeeting") or {}
            join_url = online.get("joinUrl") or row.get("onlineMeetingUrl")
            if not isinstance(join_url, str) or not join_url.strip():
                continue
            try:
                start_data, end_data = row["start"], row["end"]
                # calendarView returns OVERLAPPING occurrences, so an event may
                # legitimately begin on the previous Cairo day. The business
                # date is derived from this aware start, never from the window.
                eligible.append(CalendarEvent(
                    calendar_event_id=str(row["id"]),
                    i_cal_uid=str(row["iCalUId"]) if row.get("iCalUId") else None,
                    subject=str(row.get("subject") or ""),
                    meeting_start=parse_graph_datetime(start_data["dateTime"], start_data.get("timeZone")),
                    meeting_end=parse_graph_datetime(end_data["dateTime"], end_data.get("timeZone")),
                    calendar_timezone=start_data.get("timeZone"),
                    join_url=join_url.strip(),
                    calendar_organizer_address=((row.get("organizer") or {}).get("emailAddress") or {}).get("address"),
                    is_all_day=bool(row.get("isAllDay")),
                    occurrence_type=row.get("type"),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise PlatformError(INVALID_CALENDAR_EVENT, "eligible calendar event is invalid") from exc
        return CalendarDiscoveryBatch(len(rows), eligible)
