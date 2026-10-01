import base64
import json
import re
from datetime import date
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

TIMESTAMP_PATTERN = re.compile(
    r"^(?P<hours>\d+):(?P<minutes>[0-5]\d):(?P<seconds>[0-5]\d(?:\.\d+)?)$"
)


def timestamp_to_seconds(value) -> float | None:
    if not isinstance(value, str):
        return None
    match = TIMESTAMP_PATTERN.fullmatch(value.strip())
    if not match:
        return None
    return (
        int(match.group("hours")) * 3600
        + int(match.group("minutes")) * 60
        + float(match.group("seconds"))
    )


def encode_session_id(session_id: str) -> str:
    return base64.urlsafe_b64encode(session_id.encode()).decode().rstrip("=")


def decode_session_id(session_key: str) -> str:
    padding = "=" * (-len(session_key) % 4)
    try:
        return base64.urlsafe_b64decode(session_key + padding).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid session identifier") from exc


def _decode_nav(value: str) -> dict:
    # Stream writes standard base64 with padding; older links from this app
    # used the URL-safe alphabet without it. Accept both.
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded.replace("-", "+").replace("_", "/")).decode("utf-8")
        payload = json.loads(decoded)
        return payload if isinstance(payload, dict) else {}
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


# A SharePoint site, team site or OneDrive root at the start of a file path.
SITE_ROOT_PATTERN = re.compile(r"^/(?:sites|teams|personal)/[^/]+")


def _stream_player_parts(parts):
    """
    The Stream player URL for a direct file link, or None to keep the link.

    Only the player page (`/_layouts/15/stream.aspx?id=<file>`) reads the `nav`
    start time. A direct `.../Recordings/<file>.mp4` web URL is redirected to
    the player and the redirect drops `nav`, so the video starts at 0:00.
    Sharing links (`/:v:/...`) and links already on the player keep their
    shape: Stream honours `nav` on both.
    """
    path = unquote(parts.path)
    if not path.lower().endswith(".mp4"):
        return None
    root = SITE_ROOT_PATTERN.match(path)
    if root is None:
        return None
    return parts.scheme, parts.netloc, f"{root.group(0)}/_layouts/15/stream.aspx", {"id": path}


def timestamped_sharepoint_url(recording_url: str, start_seconds: float) -> str:
    parts = urlsplit(recording_url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    scheme, netloc, path = parts.scheme, parts.netloc, parts.path
    player = _stream_player_parts(parts)
    if player is not None:
        scheme, netloc, path, params = player
    nav = _decode_nav(params.get("nav", ""))
    playback = nav.get("playbackOptions")
    if not isinstance(playback, dict):
        playback = {}
    playback["startTimeInSeconds"] = start_seconds
    nav["playbackOptions"] = playback
    # The same encoding Stream's own "share at time" links use.
    params["nav"] = base64.b64encode(json.dumps(nav, separators=(",", ":")).encode()).decode()
    return urlunsplit((scheme, netloc, path, urlencode(params, quote_via=quote), parts.fragment))


def four_months_ago(today: date) -> date:
    month_index = today.year * 12 + today.month - 1 - 4
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    month_lengths = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
                     else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return date(year, month, min(today.day, month_lengths[month - 1]))


def normalize_clips(value) -> list[dict]:
    if isinstance(value, dict):
        candidates = value.get("clips", value.get("positive_clips", []))
    else:
        candidates = value
    if not isinstance(candidates, list):
        return []

    allowed = (
        "start", "end", "positive_quote", "quote", "speaker",
        "positive_speakers", "other_speakers", "category", "feedback_target",
        "reason", "confidence", "duration_seconds", "dialogue",
        "semantic_verification", "start_cue", "end_cue",
    )
    normalized = []
    for original_index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            continue
        clip = {key: candidate.get(key) for key in allowed}
        for list_key in ("positive_speakers", "other_speakers", "dialogue"):
            if not isinstance(clip[list_key], list):
                clip[list_key] = []
        if not isinstance(clip["semantic_verification"], dict):
            clip["semantic_verification"] = {}
        clip["original_index"] = original_index
        normalized.append(clip)
    return sorted(
        normalized,
        key=lambda clip: (
            timestamp_to_seconds(clip.get("start")) is None,
            timestamp_to_seconds(clip.get("start")) or 0,
        ),
    )
