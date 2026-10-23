"""
The Teams meeting key: thread id + message id.

A Teams join URL has the shape

    https://teams.microsoft.com/l/meetup-join/<thread>/<messageId>?context=...

and a Graph online-meeting id is base64 of "1*<organizer>*<messageId>**<thread>".
Private meetings use thread `19:meeting_...@thread.v2` with message id 0;
channel meetings use the channel's `19:...@thread.tacv2` with the id of the
meeting's message, so meetings in one channel never share a key.

The key is compared, never stored: persisted rows carry `meeting_key_hash`.
The query string (tenant/organizer context) is deliberately ignored - it is
not part of the meeting's identity and differs between copies of one link.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import urllib.parse

_THREAD_IN_PATH = re.compile(r"(19:[^/@*\s]+@thread\.(?:v2|tacv2|skype))/(\d+)")
_ONLINE_MEETING = re.compile(r"^1\*[^*]+\*(\d+)\*\*(19:[^@*\s]+@thread\.(?:v2|tacv2|skype))$")


def from_join_url(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        path = urllib.parse.urlsplit(value.strip()).path
    except ValueError:
        return None
    # Links are seen single- and double-encoded; decoding twice is idempotent
    # for the characters a thread id can contain.
    match = _THREAD_IN_PATH.search(urllib.parse.unquote(urllib.parse.unquote(path)))
    return f"{match.group(1)}/{int(match.group(2))}" if match else None


def from_online_meeting_id(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        decoded = base64.b64decode(text + "=" * (-len(text) % 4), validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    match = _ONLINE_MEETING.match(decoded)
    return f"{match.group(2)}/{int(match.group(1))}" if match else None


def keys_for(*, join_urls=(), online_meeting_ids=()) -> frozenset[str]:
    keys = {from_join_url(u) for u in join_urls} | {from_online_meeting_id(m) for m in online_meeting_ids}
    return frozenset(k for k in keys if k)


def key_hash(key: str | None) -> str | None:
    """16-hex sha256 prefix: enough to compare and audit, useless to join a meeting."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] if key else None
