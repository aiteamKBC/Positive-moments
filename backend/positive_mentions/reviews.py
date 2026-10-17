"""
Human review of positive clips: who may decide, and what they decided.

A review is keyed to the clip identity the platform already uses for media
(session + start cue + end cue), never to the clip's position in the array.
A clip with no review row is `pending`. Reviewing never writes to
`qa_doctors_sessions.positive_clips`.
"""
from django.conf import settings

from .models import ClipReview


def can_review(user) -> bool:
    if not (user and user.is_authenticated and user.is_active):
        return False
    if user.is_staff:
        return True
    email = (getattr(user, "email", "") or "").strip().lower()
    return bool(email) and email in settings.POSITIVE_CLIP_REVIEWERS


def reviewer_name(user) -> str:
    return (user.get_full_name() or "").strip() or user.email or user.get_username()


def clip_cues(clip):
    """The (start_cue, end_cue) a review is keyed on, or None if the clip has no whole cues."""
    start, end = clip.get("start_cue"), clip.get("end_cue")
    for value in (start, end):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
            return None
    return int(start), int(end)


def payload(review) -> dict:
    if review is None:
        return {"status": ClipReview.PENDING, "reviewer": None, "reviewed_at": None, "note": ""}
    return {"status": review.status, "reviewer": review.reviewer_name,
            "reviewed_at": review.reviewed_at, "note": review.note}


def attach_reviews(session_id, clips):
    """Each clip gets `review`; a clip without cues cannot be reviewed and gets None."""
    reviews = {(row.start_cue, row.end_cue): row
               for row in ClipReview.objects.filter(session_id=session_id)}
    for clip in clips:
        cues = clip_cues(clip)
        clip["review"] = None if cues is None else payload(reviews.get(cues))
    return clips


def summarise(clips) -> dict:
    statuses = [clip["review"]["status"] for clip in clips if clip.get("review")]
    return {status: statuses.count(status) for status in ClipReview.STATUSES}
