"""Synthetic LMS occurrences and lectures for the matching tests. No production identifiers."""
from __future__ import annotations

import base64
import itertools
from datetime import date, datetime, timedelta, timezone

from app.lms import meeting_key
from app.lms.sources import LectureFacts, LmsOccurrence, LmsSnapshot

START = datetime(2031, 3, 4, 9, tzinfo=timezone.utc)
ORGANIZER = "00000000-0000-4000-8000-000000000001"
_ids = itertools.count(1)


def private_url(thread="19:meeting_SYNTHETICa1@thread.v2", message=0):
    return f"https://teams.microsoft.com/l/meetup-join/{thread.replace(':', '%3a').replace('@', '%40')}/{message}?context=%7b%22Tid%22%3a%22x%22%7d"


def channel_url(channel="19:SYNTHchannel01@thread.tacv2", message=1789000000001):
    return f"https://teams.microsoft.com/l/meetup-join/{channel.replace(':', '%3a').replace('@', '%40')}/{message}?context=%7b%7d"


def online_meeting_id(thread="19:meeting_SYNTHETICa1@thread.v2", message=0, organizer=ORGANIZER):
    return base64.b64encode(f"1*{organizer}*{message}**{thread}".encode()).decode().rstrip("=")


def occurrence(*, url=None, mid=None, start=START, status="scheduled", series_status="active",
               meeting_type="live-session", module="MOD-A", programme="Synthetic Programme",
               module_deleted=False, oid=None, sid=None, number=1, minutes=120) -> LmsOccurrence:
    n = next(_ids)
    url = private_url() if url is None else url
    return LmsOccurrence(
        occurrence_id=oid or f"OCC-SYN{n:04d}", live_session_id=sid or f"LIVE-SYN{n:04d}", session_number=number,
        status=status, scheduled_start=start, scheduled_end=start + timedelta(minutes=minutes),
        meeting_keys=meeting_key.keys_for(join_urls=(url,), online_meeting_ids=(mid,)),
        online_meeting_keys=meeting_key.keys_for(online_meeting_ids=(mid,)),
        series_status=series_status, meeting_type=meeting_type, module_catalogue_id=module,
        module_exists=module is not None, module_deleted=module_deleted, programme_name=programme)


def lecture(*, url=None, mid=None, start=START, suppressed=False, lecture_id=None) -> LectureFacts:
    url = private_url() if url is None else url
    return LectureFacts(
        lecture_id=lecture_id or f"00000000-0000-4000-9000-{next(_ids):012d}", session_date=start.date(),
        scheduled_start=start, scheduled_end=start + timedelta(hours=2),
        meeting_keys=meeting_key.keys_for(join_urls=(url,), online_meeting_ids=(mid,)),
        online_meeting_key=meeting_key.from_online_meeting_id(mid), duplicate_suppressed=suppressed)


def snapshot(*occurrences, shares=None) -> LmsSnapshot:
    return LmsSnapshot(occurrences=tuple(occurrences), shares=dict(shares or {}),
                       window_start=START - timedelta(days=8), window_end=START + timedelta(days=8))
