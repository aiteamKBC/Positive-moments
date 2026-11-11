"""
The two links an operator may open: a lecture's full recording and a delivered
clip. Returned one at a time, on an authenticated request, only when asked -
never embedded in list or detail payloads.

Only durable SharePoint links leave here: the recording link the Recording Link
stage (or the n8n v9 branch) persisted under the exact-identity rule, and a
delivered asset's webUrl. Never a Graph download URL, token or upload URL.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from app.media.source import EXACT_LINK_STATUSES

RECORDING_FOR_LECTURE = """
SELECT q.recording_url, q.recording_link_status
  FROM public.lecture_sessions l
  LEFT JOIN public.positive_moment_lecture_states s ON s.lecture_id = l.lecture_id
  JOIN public.qa_doctors_sessions q
       ON q.session_id = s.legacy_session_id
       OR (s.legacy_session_id IS NULL AND q.meeting_id = l.meeting_id
           AND q.date = l.session_date)
 WHERE l.lecture_id = %s
 ORDER BY (q.session_id = s.legacy_session_id) DESC NULLS LAST, q.session_id
 LIMIT 1
"""


def _is_sharepoint(url) -> bool:
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    host = (parts.hostname or "").casefold()
    return parts.scheme == "https" and (host.endswith(".sharepoint.com")
                                        or host.endswith(".sharepoint-df.com"))


def recording_link(connection, lecture_id) -> tuple[str | None, str]:
    """(url, code). url is None unless the link is an exact-match SharePoint link."""
    row = connection.execute(RECORDING_FOR_LECTURE, (str(lecture_id),)).fetchone()
    if row is None or not str(row[0] or "").strip():
        return None, "RECORDING_NOT_LINKED"
    if row[1] not in EXACT_LINK_STATUSES:
        return None, "RECORDING_LINK_NOT_EXACT"
    if not _is_sharepoint(row[0]):
        return None, "RECORDING_LINK_NOT_SHAREPOINT"
    return str(row[0]).strip(), "OK"


def asset_link(connection, asset_id) -> tuple[str | None, str]:
    row = connection.execute(
        "SELECT web_url FROM public.positive_moment_media_assets WHERE asset_id = %s",
        (str(asset_id),)).fetchone()
    if row is None:
        return None, "ASSET_NOT_FOUND"
    if not _is_sharepoint(row[0]):
        return None, "ASSET_LINK_NOT_SHAREPOINT"
    return row[0], "OK"
