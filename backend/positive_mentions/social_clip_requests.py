"""
Create Clip for Social Media reels, through the existing clip-on-demand webhook.

The browser asks Django; Django validates the opportunity and POSTs
`{session_id, opportunity_id}` to CLIP_ON_DEMAND_WEBHOOK_URL - the same webhook
and the same sender (`views.send_clip_request`) Positive Moments uses. The n8n
workflow looks the opportunity up itself, cuts its exact saved times with zero
padding, uploads it and saves the result in
`opportunity_data.social_clip` = {trim_status, clip_url, clip_filename, ...}.

REQUESTED vs PRODUCED
---------------------
The webhook answers as soon as it receives a request and n8n writes nothing
until the clip is finished or has failed. So that a reload still knows a clip
was asked for, Django records the request beside the result, in
`opportunity_data.social_clip_request` = {requested_at, requested_by}. n8n
never reads or writes that key, and Django never writes `social_clip`.

  ready      social_clip completed with a SharePoint link     -> View Clip
  requested  asked for, no newer result yet, < REQUEST_WINDOW  -> no second request
  stale      asked for, no result after REQUEST_WINDOW         -> may ask again
  failed     social_clip failed (or unusable)                  -> may ask again
  available  eligible, never asked                             -> Create Clip
  unavailable  not clippable; `reason` says why

The checks below are the workflow's own (Select Requested Clip Only), so a
request Django accepts is one n8n will not refuse for the same reason.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from django.db import connection, transaction

from .models import DoctorSession, SocialMediaOpportunity
from .views import ClipRequestFailed, send_clip_request

# n8n polls the render for up to 40 x 15 s, then downloads and uploads.
REQUEST_WINDOW = timedelta(minutes=30)
MIN_SECONDS, MAX_SECONDS = 5, 180
TIMESTAMP = re.compile(r"^(\d{1,3}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?$")
SHAREPOINT_URL = re.compile(r"^https://[^/\s]+\.sharepoint\.com/[^\s]*$")
RESULT_KEY, REQUEST_KEY = "social_clip", "social_clip_request"


class ClipRefused(Exception):
    def __init__(self, code, detail, status=409, clip=None):
        super().__init__(detail)
        self.code, self.detail, self.status, self.clip = code, detail, status, clip


def _seconds(value):
    match = TIMESTAMP.fullmatch(str(value or "").strip())
    if not match:
        return None
    hours, minutes, secs, fraction = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(secs) + float("0." + (fraction or "0"))


def _when(value):
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _data(opportunity) -> dict:
    return opportunity.opportunity_data if isinstance(opportunity.opportunity_data, dict) else {}


def is_reel(opportunity) -> bool:
    return isinstance(opportunity.formats, list) and "reel" in opportunity.formats


def refusal(opportunity, lecture):
    """Why n8n would refuse this opportunity, or None."""
    if opportunity.verification_status != "accepted":
        return "not_accepted", "Only accepted opportunities can be clipped."
    if not is_reel(opportunity):
        return "not_a_reel", "Only opportunities verified as a reel can be clipped."
    if not (lecture.recording_drive_id or "").strip() or not (lecture.recording_item_id or "").strip():
        return "recording_missing", "The source recording has not been linked yet."
    start, end = _seconds(opportunity.start_time), _seconds(opportunity.end_time)
    if start is None or end is None or end <= start:
        return "invalid_timestamps", "The saved start and end times are missing or invalid."
    if not MIN_SECONDS <= end - start <= MAX_SECONDS:
        return "invalid_duration", f"Clips must be {MIN_SECONDS}-{MAX_SECONDS} seconds long."
    if opportunity.start_cue > opportunity.end_cue:
        return "invalid_cues", "The saved cue range is invalid."
    segments = _data(opportunity).get("reel_segments")
    if isinstance(segments, list) and segments:
        only = segments[0] if isinstance(segments[0], dict) else {}
        if len(segments) != 1 or (only.get("start_cue"), only.get("end_cue")) != (
                opportunity.start_cue, opportunity.end_cue):
            return "unsupported_segments", "This reel's segments are not one interval matching its saved times."
    return None


def clip_state(opportunity, lecture, now=None) -> dict | None:
    """What the page may show for this opportunity's clip. None for non-reels."""
    if not is_reel(opportunity):
        return None
    now = now or datetime.now(timezone.utc)
    data = _data(opportunity)
    result = data.get(RESULT_KEY) if isinstance(data.get(RESULT_KEY), dict) else {}
    request = data.get(REQUEST_KEY) if isinstance(data.get(REQUEST_KEY), dict) else {}
    requested_at, result_at = _when(request.get("requested_at")), _when(result.get("updated_at"))
    url = str(result.get("clip_url") or "").strip()
    state = {"state": "available", "can_request": True, "reason": None, "clip_url": None,
             "clip_filename": None, "error": None,
             "requested_at": requested_at.isoformat() if requested_at else None,
             "updated_at": result_at.isoformat() if result_at else None}

    if result.get("trim_status") == "completed" and SHAREPOINT_URL.fullmatch(url):
        return {**state, "state": "ready", "can_request": False, "clip_url": url,
                "clip_filename": result.get("clip_filename")}
    if requested_at and (result_at is None or result_at < requested_at):
        if now - requested_at < REQUEST_WINDOW:
            return {**state, "state": "requested", "can_request": False}
        return {**state, "state": "stale", "reason": "No clip result arrived for the last request."}
    if result:
        error = str(result.get("trim_error") or "").strip().splitlines()
        return {**state, "state": "failed",
                "error": (error[0][:200] if error else "The clip was not completed.")}
    refused = refusal(opportunity, lecture)
    if refused:
        return {**state, "state": "unavailable", "can_request": False, "reason": refused[1]}
    return state


MARK_REQUESTED = """
UPDATE qa_social_media_opportunities
   SET opportunity_data = jsonb_set(COALESCE(opportunity_data, '{}'::jsonb), '{social_clip_request}',
                                    %(request)s::jsonb, true)
 WHERE opportunity_id = %(opportunity_id)s
"""

# Only the marker this request wrote, so a later request is never undone.
CLEAR_REQUESTED = """
UPDATE qa_social_media_opportunities
   SET opportunity_data = opportunity_data - 'social_clip_request'
 WHERE opportunity_id = %(opportunity_id)s
   AND opportunity_data -> 'social_clip_request' ->> 'requested_at' = %(requested_at)s
"""


def request_clip(session_id: str, opportunity_id: int, username: str):
    """Validate, record the request, then ask n8n. Returns the new clip state."""
    with transaction.atomic():
        opportunity = (SocialMediaOpportunity.objects.select_for_update()
                       .filter(opportunity_id=opportunity_id, session_id=session_id,
                               verification_status="accepted").first())
        lecture = (DoctorSession.objects.defer("positive_clips").filter(session_id=session_id).first()
                   if opportunity else None)
        if opportunity is None or lecture is None:
            raise ClipRefused("not_found", "This accepted opportunity is not part of the lecture.", 404)
        refused = refusal(opportunity, lecture)
        if refused:
            raise ClipRefused(*refused, status=422 if refused[0] != "not_a_reel" else 409)
        current = clip_state(opportunity, lecture)
        if current["state"] == "ready":
            raise ClipRefused("clip_already_produced", "This clip has already been produced.", clip=current)
        if current["state"] == "requested":
            raise ClipRefused("clip_in_progress", "This clip has already been requested.", clip=current)
        requested_at = datetime.now(timezone.utc).isoformat()
        with connection.cursor() as cursor:
            cursor.execute(MARK_REQUESTED, {
                "opportunity_id": opportunity_id,
                "request": json.dumps({"requested_at": requested_at, "requested_by": username}),
            })
    try:
        send_clip_request({"session_id": session_id, "opportunity_id": opportunity_id})
    except ClipRequestFailed:
        with connection.cursor() as cursor:
            cursor.execute(CLEAR_REQUESTED, {"opportunity_id": opportunity_id, "requested_at": requested_at})
        raise ClipRefused("clip_request_failed", "The clip service did not accept the request. Try again.", 502)
    opportunity.refresh_from_db(fields=["opportunity_data"])
    return clip_state(opportunity, lecture)
