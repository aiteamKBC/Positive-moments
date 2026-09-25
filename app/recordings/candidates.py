"""
Candidate normalization: raw Graph driveItems in, comparable candidates out.

Pure functions. The subject normalization and the Teams file-name grammar are
exactly those of the n8n v8/v9 branch that produced every existing recording
link, so a lecture that matched there normalizes identically here.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app.recordings.models import DriveItemCandidate


_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SPACES = re.compile(r"\s+")
_TITLE = re.compile(r"^(dr|doctor|prof|professor|mr|mrs|ms)\s+")
# "<subject>-YYYYMMDD_HHMMSS[UTC]-Meeting Recording.mp4". [0-9], not \d: the
# JavaScript original matched ASCII digits only.
_TEAMS_RECORDING = re.compile(
    r"^(.*)-([0-9]{8})_([0-9]{6})(?:UTC)?-Meeting Recording\.mp4$", re.IGNORECASE)
_MP4 = re.compile(r"\.mp4$", re.IGNORECASE)


def normalize_subject(value) -> str:
    """
    Lower-case, decode `&amp;`, drop one leading title, keep only [a-z0-9].
    It deliberately treats "Dr.Femi ..." and "Femi ..." as the same subject.
    """
    text = str(value or "").lower().replace("&amp;", "&")
    text = _SPACES.sub(" ", _NON_ALNUM.sub(" ", text)).strip()
    text = _TITLE.sub("", text)
    return _NON_ALNUM.sub("", text)


def parse_recording_name(name) -> tuple[str, str, float] | None:
    """(normalized subject, YYYY-MM-DD, epoch seconds) or None. Times are UTC."""
    match = _TEAMS_RECORDING.match(str(name or ""))
    if not match:
        return None
    try:
        moment = datetime.strptime(match.group(2) + match.group(3), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    moment = moment.replace(tzinfo=timezone.utc)
    return normalize_subject(match.group(1)), moment.date().isoformat(), moment.timestamp()


def candidate_from_drive_item(item: dict, source: str) -> DriveItemCandidate | None:
    """A Graph driveItem (listing or search hit) as a candidate, or None."""
    if not isinstance(item, dict):
        return None
    item_id = item.get("id")
    name = item.get("name")
    drive_id = ((item.get("parentReference") or {}).get("driveId")
                or item.get("driveId"))
    if not item_id or not name or not drive_id or not _MP4.search(str(name)):
        return None
    if "folder" in item and "file" not in item:
        return None
    parsed = parse_recording_name(name)
    return DriveItemCandidate(
        item_id=str(item_id), drive_id=str(drive_id), name=str(name),
        web_url=item.get("webUrl"), source=source,
        subject_key=parsed[0] if parsed else None,
        file_date=parsed[1] if parsed else None,
        file_timestamp=parsed[2] if parsed else None)


def deduplicate(candidates) -> list[DriveItemCandidate]:
    """One candidate per (drive, item): several sources can see the same file."""
    seen: dict[tuple[str, str], DriveItemCandidate] = {}
    for candidate in candidates:
        if candidate is None:
            continue
        seen.setdefault((candidate.drive_id, candidate.item_id), candidate)
    return list(seen.values())
