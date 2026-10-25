"""
Learner delivery: the LMS read model and the publication lifecycle.

THE READ MODEL (what the LMS backend sees, contract v1)
    The LMS asks by ITS identity - curriculum.live_session_occurrences.id - and
    never by the publisher's lecture id. One answer per occurrence, computed LIVE
    on every request from the same `progress.readiness()` the planner, the
    import gate and the reconciler use. The historical LECTURE_FULLY_READY event
    is never consulted: it is written once and never withdrawn.

    state (one, in this precedence)
      NOT_DISCOVERED   the LMS has the occurrence; no lecture is linked to it
      NOT_APPLICABLE   cancelled lecture / test or inactive LMS occurrence
      BLOCKED          duplicate calendar event, or every recording part excluded
      WITHDRAWN        an operator withdrew it (overrides everything below)
      REVIEW_REQUIRED  waiting for a person (fragment, ambiguous / stale match, ...)
      PROCESSING       recording, import, encoding or caption still under way
      READY            every included part has a VERIFIED video and a VERIFIED,
                       content-checked caption, and the LMS link is current

    playback.available is true ONLY when state is READY, the lecture is
    RELEASED, and delivery is switched on (PUBLISHER_LMS_PLAYBACK_ENABLED).
    Anything else - a withdrawal, a caption superseded by an unverified one, a
    match gone STALE - makes it false on the very next request.

THE PUBLICATION LIFECYCLE (audited operator actions)
    RELEASE   HIDDEN | WITHDRAWN -> READY_FOR_LMS on every INCLUDED part's live
              video, only when the lecture is verified ready right now AND the
              library passes the learner-delivery security check.
    WITHDRAW  any -> WITHDRAWN on every live video of the lecture.
    Both run in ONE transaction under a per-lecture transaction advisory lock
    with the video rows locked FOR UPDATE: two operators, or an operator and a
    worker, can never interleave; repeating an action is a no-op (no event).
    A new recording part imported after a release starts HIDDEN, so the lecture
    reads NOT_RELEASED until someone releases it again - never silently public.

Nothing returned here carries a Teams join URL or meeting id, a SharePoint
drive / item id or file name, a download or CDN URL, a key, transcript or
caption text, or job information.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timezone

import psycopg

from app.bunny import captions as cap
from app.db.repositories.publisher_queue import PublisherQueueRepository
from app.publisher.operations import OperatorActionRefused
from app.publisher.progress import (
    DUPLICATE_AMBIGUOUS, DUPLICATE_LOSER, EXCLUDE, INCLUDE, NEEDS_DECISION, load_facts, readiness,
)

CONTRACT_VERSION = "1"
READY, PROCESSING, REVIEW_REQUIRED, BLOCKED, WITHDRAWN, NOT_DISCOVERED, NOT_APPLICABLE = (
    "READY", "PROCESSING", "REVIEW_REQUIRED", "BLOCKED", "WITHDRAWN", "NOT_DISCOVERED", "NOT_APPLICABLE")
RELEASED, NOT_RELEASED = "RELEASED", "NOT_RELEASED"
HIDDEN, READY_FOR_LMS = "HIDDEN", "READY_FOR_LMS"
RELEASE_EVENT, WITHDRAW_EVENT = "PUBLICATION_RELEASED", "PUBLICATION_WITHDRAWN"
PUBLICATION_LOCK_NAMESPACE = 0x4B50_5231         # "KPR1": one publication change per lecture at a time
CONTENT_VERIFIED = (cap.IDENTICAL, cap.EQUIVALENT)
OCCURRENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
URLISH = re.compile(r"(https?://|tempauth|[?&]sig=)", re.IGNORECASE)
REVIEW_BLOCKS = ("LMS_AMBIGUOUS", "LMS_CONFLICT", "LMS_STALE", "LMS_RESCHEDULED_CANDIDATE", "LMS_NO_LMS_SESSION",
                 "LMS_NO_MEETING_KEY", "LMS_LMS_RECORD_INACTIVE", "LMS_LINK_NOT_CURRENT",
                 "RECORDING_REVIEW_REQUIRED", "RECORDING_PERMANENT_FAILURE")
COVERAGE_STATE = {"NOT_YET_DUE": NOT_DISCOVERED, "LMS_ONLY_PENDING": NOT_DISCOVERED,
                  "LMS_SESSION_NOT_DISCOVERED": NOT_DISCOVERED, "MATCHED": NOT_DISCOVERED,
                  "TEST_EXCLUDED": NOT_APPLICABLE, "INACTIVE": NOT_APPLICABLE}

LINK = """
SELECT k.lecture_id, k.lms_occurrence_id, k.lms_live_session_id, k.lms_module_catalogue_id, k.lms_session_number,
       k.link_role, k.is_current, k.lms_scheduled_start
  FROM public.bunny_lms_occurrence_links k
 WHERE k.lms_occurrence_id = %s
 ORDER BY k.is_current DESC, k.created_at DESC
 LIMIT 1
"""
COVERAGE = """
SELECT lms_occurrence_id, lms_live_session_id, lms_module_catalogue_id, lms_scheduled_start, lms_scheduled_end,
       coverage_status, lms_occurrence_status
  FROM public.bunny_lms_occurrence_coverage WHERE lms_occurrence_id = %s
"""
MATCH = """
SELECT match_status, rule_version FROM public.bunny_lms_match_evaluations WHERE lecture_id = %s AND is_current
"""
VIDEOS = """
SELECT bunny_video_record_id, recording_part_index, bunny_video_id, import_status, encoding_status,
       bunny_length_seconds, source_duration_seconds, available_resolutions, visibility_state
  FROM public.bunny_videos
 WHERE bunny_library_id = %s AND lecture_id = %s AND retired_at IS NULL
"""
CAPTIONS = """
SELECT c.bunny_video_id, p.language_code, p.caption_label, p.publication_status, p.bunny_caption_version,
       p.readback ->> 'content_check'
  FROM public.bunny_caption_current c
  JOIN public.bunny_caption_publications p ON p.publication_id = c.publication_id
 WHERE c.bunny_library_id = %s AND c.bunny_video_id = ANY(%s::uuid[])
 ORDER BY p.language_code
"""
LIFECYCLE = """
SELECT DISTINCT ON (event) event, at FROM public.bunny_publisher_events
 WHERE lecture_id = %s AND event IN ('PUBLICATION_RELEASED', 'PUBLICATION_WITHDRAWN')
   AND detail ->> 'library_id' = %s
 ORDER BY event, event_id DESC
"""
# One changed_at per LMS occurrence the publisher knows (linked, or seen on the LMS side): the newest
# timestamp of anything the v1 answer is computed from. GREATEST ignores NULLs. At-least-once by design.
CHANGED = """
WITH occ AS (
  SELECT o.lms_occurrence_id,
         (SELECT k.lecture_id FROM public.bunny_lms_occurrence_links k WHERE k.lms_occurrence_id = o.lms_occurrence_id
           ORDER BY k.is_current DESC, k.created_at DESC LIMIT 1) AS lecture_id,
         (SELECT max(k.created_at) FROM public.bunny_lms_occurrence_links k
           WHERE k.lms_occurrence_id = o.lms_occurrence_id) AS link_at,
         (SELECT c.status_changed_at FROM public.bunny_lms_occurrence_coverage c
           WHERE c.lms_occurrence_id = o.lms_occurrence_id) AS coverage_at
    FROM (SELECT lms_occurrence_id FROM public.bunny_lms_occurrence_links
          UNION SELECT lms_occurrence_id FROM public.bunny_lms_occurrence_coverage) o
   WHERE %(occurrence)s::text IS NULL OR o.lms_occurrence_id = %(occurrence)s::text
), changed AS (
  SELECT occ.lms_occurrence_id, GREATEST(
           occ.link_at, occ.coverage_at, l.updated_at,
           (SELECT max(COALESCE(e.superseded_at, e.first_evaluated_at)) FROM public.bunny_lms_match_evaluations e
             WHERE e.lecture_id = occ.lecture_id),
           (SELECT d.updated_at FROM public.bunny_recording_discoveries d WHERE d.lecture_id = occ.lecture_id),
           (SELECT max(s.updated_at) FROM public.bunny_recording_sources s WHERE s.lecture_id = occ.lecture_id),
           (SELECT max(pd.decided_at) FROM public.bunny_publisher_part_decisions pd
             WHERE pd.lecture_id = occ.lecture_id),
           (SELECT max(v.updated_at) FROM public.bunny_videos v
             WHERE v.lecture_id = occ.lecture_id AND v.bunny_library_id = %(library)s),
           (SELECT max(GREATEST(p.updated_at, cc.made_current_at)) FROM public.bunny_caption_publications p
              LEFT JOIN public.bunny_caption_current cc ON cc.publication_id = p.publication_id
             WHERE p.lecture_id = occ.lecture_id AND p.bunny_library_id = %(library)s),
           (SELECT max(ev.at) FROM public.bunny_publisher_events ev
             WHERE ev.lecture_id = occ.lecture_id AND ev.event IN ('PUBLICATION_RELEASED', 'PUBLICATION_WITHDRAWN')
               AND ev.detail ->> 'library_id' = %(library)s::text)
         ) AS changed_at
    FROM occ LEFT JOIN public.lecture_sessions l ON l.lecture_id = occ.lecture_id
)
SELECT lms_occurrence_id, changed_at FROM changed
 WHERE (%(since)s::timestamptz IS NULL OR changed_at > %(since)s::timestamptz)
   AND (%(after_at)s::timestamptz IS NULL OR (changed_at, lms_occurrence_id) > (%(after_at)s::timestamptz, %(after_id)s::text))
 ORDER BY changed_at, lms_occurrence_id
 LIMIT %(limit)s
"""


# =========================================================================== the lecture, as delivered
def lecture_delivery(connection, lecture_id, *, library_id: int, language: str = "en",
                     link_current: bool = True) -> dict | None:
    """Everything the v1 answer and the lifecycle need about ONE lecture in ONE library, computed live."""
    facts = load_facts(connection, lecture_id)
    if not facts.exists:
        return None
    state = readiness(connection, facts, library_id, language)
    videos = {r[1]: r for r in connection.execute(VIDEOS, (library_id, facts.lecture_id)).fetchall()}
    video_ids = [str(r[2]) for r in videos.values() if r[2]]
    captions = {}
    for row in connection.execute(CAPTIONS, (library_id, video_ids)).fetchall() if video_ids else ():
        captions.setdefault(str(row[0]), []).append(
            {"language": row[1], "label": row[2], "status": row[3], "version": row[4],
             "content_verified": row[5] in CONTENT_VERIFIED})
    parts, excluded, blocking, sequence = [], [], list(state.blocking), 0
    for part in sorted(facts.parts, key=lambda p: p["part_index"]):
        policy = facts.policy(part)
        index = part["part_index"]
        if policy == EXCLUDE:
            excluded.append({"source_part_index": index, "reason": "OPERATOR_EXCLUDED"})
            continue
        if policy != INCLUDE:
            continue                                   # NOT_LOCATED / NEEDS_DECISION: already in `blocking`
        sequence += 1
        video = videos.get(index)
        video_id = str(video[2]) if video and video[2] else None
        part_captions = captions.get(video_id, []) if video_id else []
        current = next((c for c in part_captions if c["language"] == language), None)
        if current and current["status"] == "VERIFIED" and not current["content_verified"]:
            blocking.append(f"PART_{index}_CAPTION_CONTENT_UNVERIFIED")
        duration = video[5] if video and video[5] is not None else (
            round(float(part["duration_seconds"])) if part.get("duration_seconds") is not None else None)
        parts.append({
            "sequence": sequence, "source_part_index": index, "classification": part.get("classification"),
            "teams_recording_id": part.get("graph_recording_id"),
            "recorded_start": _iso(part.get("recording_created_at")),
            "recorded_end": _iso(part.get("recording_end_at")),
            "duration_seconds": duration,
            "bunny": {"library_id": int(library_id), "video_id": video_id} if video_id else None,
            "encoding": video[4] if video else "NOT_IMPORTED",
            "resolutions": [r for r in (video[7] or "").split(",") if r] if video else [],
            "captions": part_captions,
            "visibility": video[8] if video else None,
            "_record_id": str(video[0]) if video else None,
        })
    if not link_current:
        blocking.append("LMS_LINK_NOT_CURRENT")
    lifecycle = {r[0]: r[1] for r in connection.execute(LIFECYCLE, (facts.lecture_id, str(library_id))).fetchall()}
    release = _release_status(parts)
    verified_ready = state.fully_ready and not blocking       # verification only: release is a separate fact
    unreleased = [f"PART_{p['source_part_index']}_NOT_RELEASED" for p in parts
                  if release == NOT_RELEASED and any(q["visibility"] == READY_FOR_LMS for q in parts)
                  and p["visibility"] != READY_FOR_LMS]
    match = connection.execute(MATCH, (facts.lecture_id,)).fetchone()
    return {
        "facts": facts, "readiness": state, "verified_ready": verified_ready,
        "state": _state(facts, state, blocking, verified_ready, release),
        "blocking": _dedupe(blocking + unreleased), "verification_blocking": _dedupe(blocking),
        "parts": parts, "excluded_parts": excluded,
        "teams_part_count": len(facts.parts), "release": release,
        "released_at": _iso(lifecycle.get(RELEASE_EVENT)), "withdrawn_at": _iso(lifecycle.get(WITHDRAW_EVENT)),
        "match": {"status": match[0], "rule_version": match[1]} if match else {"status": "PENDING", "rule_version": None},
        "library_id": int(library_id),
    }


def _release_status(parts) -> str:
    included = [p["visibility"] for p in parts]
    if WITHDRAWN in included:
        return WITHDRAWN
    if included and all(v == READY_FOR_LMS for v in included):
        return RELEASED
    return NOT_RELEASED


def _state(facts, state, blocking, verified_ready, release) -> str:
    if facts.cancelled:
        return NOT_APPLICABLE
    if facts.suppressed or any(b in (DUPLICATE_LOSER, DUPLICATE_AMBIGUOUS, "NO_INCLUDED_PART") for b in blocking):
        return BLOCKED
    if release == WITHDRAWN:
        return WITHDRAWN
    if verified_ready:
        return READY
    if state.state == "REVIEW_REQUIRED" or any(
            b in REVIEW_BLOCKS or b.endswith(("_NEEDS_DECISION", "_CAPTION_CONTENT_UNVERIFIED")) for b in blocking):
        return REVIEW_REQUIRED
    return PROCESSING


# =========================================================================== the v1 answer
def occurrence_recording(connection, occurrence_id: str, *, library_id: int, playback_enabled: bool,
                         language: str = "en") -> dict | None:
    """The v1 answer for one LMS occurrence, or None when the publisher has never seen it (404)."""
    occurrence_id = str(occurrence_id or "")
    if not OCCURRENCE_ID.match(occurrence_id):
        raise OperatorActionRefused("INVALID_OCCURRENCE_ID", "the LMS occurrence id is malformed")
    link = connection.execute(LINK, (occurrence_id,)).fetchone()
    coverage = connection.execute(COVERAGE, (occurrence_id,)).fetchone()
    if link is None and coverage is None:
        return None
    changed = connection.execute(CHANGED, {"occurrence": occurrence_id, "library": int(library_id), "since": None,
                                           "after_at": None, "after_id": None, "limit": 1}).fetchone()
    body = {"contract_version": CONTRACT_VERSION, "lms_occurrence_id": occurrence_id,
            "updated_at": _iso(changed[1]) if changed else None}
    if link is None:
        state = COVERAGE_STATE.get(coverage[5], NOT_DISCOVERED)
        body.update({
            "lms_live_session_id": coverage[1], "lms_module_catalogue_id": coverage[2], "lms_session_number": None,
            "link_role": None, "state": state, "blocking": [f"LMS_COVERAGE_{coverage[5]}"], "lecture": None,
            "recording": None, "release": {"status": NOT_RELEASED, "released_at": None, "withdrawn_at": None},
            "playback": _playback(False, state, NOT_RELEASED, playback_enabled, library_id)})
        return body
    lecture_id, _, live_session, module, session_number, role, is_current, _ = link
    view = lecture_delivery(connection, lecture_id, library_id=library_id, language=language, link_current=is_current)
    facts = view["facts"]
    body.update({
        "lms_live_session_id": live_session, "lms_module_catalogue_id": module, "lms_session_number": session_number,
        "link_role": role, "state": view["state"], "blocking": view["blocking"],
        "lecture": {"lecture_id": facts.lecture_id, "title": facts.subject, "session_date": _iso(facts.session_date),
                    "scheduled_start": _iso(facts.scheduled_start), "scheduled_end": _iso(facts.scheduled_end),
                    "lms_match": view["match"]},
        "recording": {
            "teams_part_count": view["teams_part_count"], "published_part_count": len(view["parts"]),
            "total_duration_seconds": sum(p["duration_seconds"] or 0 for p in view["parts"]) or None,
            "parts": [{k: v for k, v in p.items() if not k.startswith("_")} for p in view["parts"]],
            "excluded_parts": view["excluded_parts"]},
        "release": {"status": view["release"], "released_at": view["released_at"],
                    "withdrawn_at": view["withdrawn_at"]},
        "playback": _playback(view["state"] == READY, view["state"], view["release"], playback_enabled, library_id),
    })
    return body


def _playback(ready, state, release, enabled, library_id) -> dict:
    if not ready:
        reason = f"STATE_{state}"
    elif release != RELEASED:
        reason = release
    elif not enabled:
        reason = "PLAYBACK_DISABLED"
    else:
        reason = None
    return {"available": reason is None, "reason": reason, "library_id": int(library_id),
            "player": "BUNNY_STREAM_EMBED", "token_authentication_required": True}


def occurrence_feed(connection, *, library_id: int, playback_enabled: bool, changed_since=None, cursor: str | None = None,
                    limit: int = 20, language: str = "en") -> dict:
    """Occurrences whose answer may have changed, oldest change first; at-least-once, cursor-paged."""
    after_at = after_id = None
    if cursor:
        after_at, after_id = decode_cursor(cursor)
    rows = connection.execute(CHANGED, {"occurrence": None, "library": int(library_id), "since": changed_since,
                                        "after_at": after_at, "after_id": after_id, "limit": int(limit) + 1}).fetchall()
    page, more = rows[:limit], len(rows) > limit
    items = [occurrence_recording(connection, r[0], library_id=library_id, playback_enabled=playback_enabled,
                                  language=language) for r in page]
    return {"contract_version": CONTRACT_VERSION, "items": [i for i in items if i is not None],
            "next_cursor": encode_cursor(page[-1][1], page[-1][0]) if more else None, "has_more": more}


def encode_cursor(changed_at: datetime, occurrence_id: str) -> str:
    raw = json.dumps({"t": changed_at.astimezone(timezone.utc).isoformat(), "o": occurrence_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8")
        data = json.loads(raw)
        at, occurrence_id = datetime.fromisoformat(data["t"]), str(data["o"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError):
        raise OperatorActionRefused("INVALID_CURSOR", "the cursor is not one this API issued") from None
    if at.tzinfo is None or not OCCURRENCE_ID.match(occurrence_id):
        raise OperatorActionRefused("INVALID_CURSOR", "the cursor is not one this API issued")
    return at, occurrence_id


# =========================================================================== RELEASE / WITHDRAW
def release(connection, lecture_id, *, library_id: int, actor: str, reason: str, security: dict | None,
            language: str = "en", require_unpublished: bool = False) -> dict:
    """
    Make a verified lecture available to the LMS. Idempotent; refuses anything not verified right now.

    require_unpublished (automatic release): additionally refuse - under the same lock - unless every live
    video of the lecture is still HIDDEN and the lecture has never been released or withdrawn. Automation
    therefore never undoes an operator's withdrawal and never re-releases a lecture whose new part an
    operator has not looked at.
    """
    actor, reason = _who(actor, reason)
    if not security or security.get("secure") is not True or int(security.get("library_id", -1)) != int(library_id):
        raise OperatorActionRefused("LIBRARY_NOT_SECURE", "the library failed the learner-delivery security check: "
                                    + "; ".join((security or {}).get("problems") or ["no check supplied"]))
    return _change(connection, lecture_id, library_id, actor, reason, language, releasing=True,
                   require_unpublished=require_unpublished)


def withdraw(connection, lecture_id, *, library_id: int, actor: str, reason: str, language: str = "en") -> dict:
    """Make a lecture unavailable immediately. Idempotent; always allowed."""
    actor, reason = _who(actor, reason)
    return _change(connection, lecture_id, library_id, actor, reason, language, releasing=False)


def _change(connection, lecture_id, library_id, actor, reason, language, *, releasing: bool,
            require_unpublished: bool = False) -> dict:
    lecture_id = str(lecture_id)
    try:
        with connection.transaction():
            connection.execute("SET LOCAL lock_timeout = '5s'")
            connection.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (PUBLICATION_LOCK_NAMESPACE, lecture_id))
            rows = connection.execute(
                "SELECT bunny_video_record_id, recording_part_index, visibility_state FROM public.bunny_videos "
                "WHERE bunny_library_id = %s AND lecture_id = %s AND retired_at IS NULL FOR UPDATE",
                (library_id, lecture_id)).fetchall()
            view = lecture_delivery(connection, lecture_id, library_id=library_id, language=language)
            if view is None:
                raise OperatorActionRefused("LECTURE_NOT_FOUND", "no such lecture")
            if require_unpublished and (any(r[2] != HIDDEN for r in rows) or connection.execute(
                    LIFECYCLE, (lecture_id, str(library_id))).fetchone() is not None):
                raise OperatorActionRefused("ALREADY_PUBLISHED", "released or withdrawn before: an operator decides")
            if releasing:
                if not view["verified_ready"]:
                    raise OperatorActionRefused(
                        "NOT_VERIFIED", "only a lecture whose every included part has a verified video and a "
                        "verified, content-checked caption can be released; blocking: "
                        + ", ".join(view["verification_blocking"] or [view["readiness"].state]))
                wanted = {p["_record_id"] for p in view["parts"]}
                target = [r for r in rows if str(r[0]) in wanted and r[2] != READY_FOR_LMS]
                new_state, event = READY_FOR_LMS, RELEASE_EVENT
            else:
                target = [r for r in rows if r[2] != WITHDRAWN]
                new_state, event = WITHDRAWN, WITHDRAW_EVENT
            if target:
                ids = [str(r[0]) for r in target]
                guard = " AND encoding_status = 'VERIFIED'" if releasing else ""
                changed = connection.execute(
                    "UPDATE public.bunny_videos SET visibility_state = %s, updated_at = now() "
                    f"WHERE bunny_video_record_id = ANY(%s::uuid[]) AND retired_at IS NULL{guard}",
                    (new_state, ids)).rowcount
                if changed != len(ids):
                    raise OperatorActionRefused("CONCURRENT_CHANGE", "a video changed underneath the action; retry")
                PublisherQueueRepository().event(
                    connection, event, lecture_id=lecture_id, actor=actor,
                    detail={"library_id": str(library_id), "reason": reason, "parts": sorted(r[1] for r in target),
                            "video_record_ids": ids, "previous": {str(r[0]): r[2] for r in target}})
    except psycopg.errors.LockNotAvailable:
        raise OperatorActionRefused("LECTURE_BUSY", "another publication change holds this lecture; retry") from None
    after = lecture_delivery(connection, lecture_id, library_id=library_id, language=language)
    return {"lecture_id": lecture_id, "library_id": int(library_id), "action": "RELEASE" if releasing else "WITHDRAW",
            "changed": bool(target), "parts_changed": sorted(r[1] for r in target),
            "release": after["release"], "state": after["state"], "blocking": after["blocking"]}


def _who(actor, reason) -> tuple[str, str]:
    actor, reason = (actor or "").strip(), (reason or "").strip()
    if not actor or not reason:
        raise OperatorActionRefused("ACTOR_AND_REASON_REQUIRED", "an actor and a reason are required")
    if URLISH.search(reason) or URLISH.search(actor):
        raise OperatorActionRefused("REASON_CONTAINS_URL", "the audit trail never stores a URL")
    return actor[:120], reason[:300]


# =========================================================================== helpers
def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _dedupe(items):
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


__all__ = ["CONTRACT_VERSION", "occurrence_recording", "occurrence_feed", "lecture_delivery", "release", "withdraw",
           "encode_cursor", "decode_cursor", "NEEDS_DECISION"]
