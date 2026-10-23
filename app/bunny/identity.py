"""
Deterministic identity and titles for Bunny videos.

The authoritative source identity is (library, drive_id, item_id). Everything
here is a pure function of it, so a retry, a restart or a second machine
derives exactly the same record id, identity token and title.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from datetime import date

# Namespace for uuidv5 record ids; fixed forever once rows exist.
BUNNY_NAMESPACE = uuid.UUID("5d6c1f0e-4b7a-5c3e-9a51-6b756e6e7953")

# The Bunny OpenAPI spec sets minLength 1 and documents no maximum; 200 is a
# conservative cap (also enforced by migration 024).
TITLE_MAX_LENGTH = 200
IDENTITY_PATTERN = re.compile(r"^rec-[0-9a-f]{8}$")

# Letters/digits in any script, plus a small set of harmless punctuation. The
# pipe is excluded because it is the title's field separator.
_ALLOWED_PUNCTUATION = set(" -_.,&()'+:#/")
_SPACES = re.compile(r"\s+")


def recording_identity(drive_id: str, item_id: str) -> str:
    drive, item = str(drive_id or "").strip(), str(item_id or "").strip()
    if not drive or not item:
        raise ValueError("drive_id and item_id are both required")
    digest = hashlib.sha256(f"{drive}\x1f{item}".encode("utf-8")).hexdigest()
    return f"rec-{digest[:8]}"


def video_record_id(library_id: int | str, drive_id: str, item_id: str, generation: int = 1) -> uuid.UUID:
    """
    Generation 1 is the pilot's id, unchanged. A re-import after a CONFIRMED
    failed import (the failed row is retired, never reused) is generation 2, 3...
    """
    base = f"bunny-video|{int(library_id)}|{drive_id}|{item_id}"
    if int(generation) < 1:
        raise ValueError("generation starts at 1")
    return uuid.uuid5(BUNNY_NAMESPACE, base if int(generation) == 1 else f"{base}|gen{int(generation)}")


def caption_publication_id(library_id: int | str, video_id: str, language: str,
                           caption_sha256: str, transform_version: str) -> uuid.UUID:
    return uuid.uuid5(BUNNY_NAMESPACE, f"bunny-caption|{int(library_id)}|{str(video_id).lower()}|"
                                       f"{language}|{caption_sha256}|{transform_version}")


def clean_subject(subject: str) -> str:
    text = unicodedata.normalize("NFKC", str(subject or "")).replace("&amp;", "&")
    kept = []
    for char in text:
        category = unicodedata.category(char)
        if category[0] in "LN" or char in _ALLOWED_PUNCTUATION:
            kept.append(char)
        elif category[0] in "ZC" or char == "|":
            kept.append(" ")
    cleaned = _SPACES.sub(" ", "".join(kept)).strip(" -_.,:/")
    return cleaned or "Lecture"


def build_title(subject: str, session_date: date | str, lecture_id: str,
                identity: str) -> str:
    if not IDENTITY_PATTERN.match(identity or ""):
        raise ValueError("identity must look like rec-xxxxxxxx")
    day = session_date.isoformat() if isinstance(session_date, date) else str(session_date)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("session_date must be YYYY-MM-DD")
    short_lecture = str(lecture_id).replace("-", "")[:8].lower()
    if not re.fullmatch(r"[0-9a-f]{8}", short_lecture):
        raise ValueError("lecture_id must be a UUID")
    suffix = f" | {day} | {short_lecture} | {identity}"
    budget = TITLE_MAX_LENGTH - len(suffix)
    subject_part = clean_subject(subject)[:budget].rstrip(" -_.,:/") or "Lecture"
    return subject_part + suffix


_OCCURRENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def build_publisher_title(subject: str, session_date: date | str, occurrence_id: str, identity: str, *,
                          part_index: int = 1, part_count: int = 1) -> str:
    """
    "{subject} | {date} | lms {occurrence} | part {n}/{m} | {rec-xxxxxxxx}".

    The verified LMS occurrence id (curriculum.live_session_occurrences.id) is
    the per-lecture identity; the part appears only for a multipart lecture.
    A label for humans and for Fetch reconciliation - never a key: the
    PostgreSQL mapping is authoritative.
    """
    if not IDENTITY_PATTERN.match(identity or ""):
        raise ValueError("identity must look like rec-xxxxxxxx")
    day = session_date.isoformat() if isinstance(session_date, date) else str(session_date)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("session_date must be YYYY-MM-DD")
    occurrence = str(occurrence_id or "").strip()
    if not _OCCURRENCE.match(occurrence):
        raise ValueError("occurrence_id must be a plain LMS identifier")
    if not 1 <= int(part_index) <= int(part_count):
        raise ValueError("part_index must be within 1..part_count")
    part = f" | part {int(part_index)}/{int(part_count)}" if int(part_count) > 1 else ""
    suffix = f" | {day} | lms {occurrence}{part} | {identity}"
    budget = TITLE_MAX_LENGTH - len(suffix)
    subject_part = clean_subject(subject)[:budget].rstrip(" -_.,:/") or "Lecture"
    return subject_part + suffix
