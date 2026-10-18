"""
Social Media opportunities, read only.

The source of truth is `qa_social_media_opportunities`, never the
`positive_clips` JSON: a lecture is on the Social Media page because it has
accepted rows there. The lecture table only supplies date, subject, trainer
and recording metadata.

Every count uses `primary_format`, so each opportunity is counted exactly once
and the per-format counts add up to the total. The full `formats` list (an
opportunity can suit a reel AND a carousel) is shown on each opportunity.
"""
from datetime import date

from django.db.models import Count, F, Max, Q, QuerySet

from .models import DoctorSession, SocialMediaOpportunity

ACCEPTED = "accepted"
FORMATS = ("reel", "carousel", "quote_post", "educational_post")


class InvalidFilter(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def accepted_opportunities() -> QuerySet:
    return SocialMediaOpportunity.objects.filter(verification_status=ACCEPTED)


def requested_format(params) -> str:
    value = (params.get("content_format") or "").strip().lower()
    if value and value not in FORMATS:
        raise InvalidFilter("invalid_format", f"content_format must be one of: {', '.join(FORMATS)}.")
    return value


def _requested_date(params, name: str) -> date | None:
    value = (params.get(name) or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InvalidFilter("invalid_date", f"{name} must be a date in YYYY-MM-DD form.") from None


def social_lectures(params) -> QuerySet:
    """Lectures with at least one accepted opportunity (of `content_format`, when chosen)."""
    opportunities = accepted_opportunities()
    chosen = requested_format(params)
    if chosen:
        opportunities = opportunities.filter(primary_format=chosen)
    queryset = DoctorSession.objects.filter(
        session_id__in=opportunities.values("session_id"),
    ).only("session_id", "date", "subject", "trainer", "recording_url")
    search = (params.get("search") or "").strip()
    if search:
        queryset = queryset.filter(Q(subject__icontains=search) | Q(trainer__icontains=search))
    date_from = _requested_date(params, "date_from")
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    date_to = _requested_date(params, "date_to")
    if date_to:
        queryset = queryset.filter(date__lte=date_to)
    # Newest first; session_id breaks ties so pages never overlap or skip.
    return queryset.order_by(F("date").desc(nulls_last=True), "session_id")


def _format_counts(row: dict) -> dict:
    counts = {name: row[name] for name in FORMATS}
    # A primary format outside the four known ones, or none at all.
    counts["other"] = row["total"] - sum(counts.values())
    return counts


def opportunity_stats(session_ids) -> dict[str, dict]:
    """Counts for many lectures in ONE grouped query, keyed by session_id."""
    rows = (
        accepted_opportunities()
        .filter(session_id__in=list(session_ids))
        .order_by()
        .values("session_id")
        .annotate(
            total=Count("opportunity_id"),
            last_analyzed_at=Max("analyzed_at"),
            **{name: Count("opportunity_id", filter=Q(primary_format=name)) for name in FORMATS},
        )
    )
    return {
        row["session_id"]: {
            "opportunity_count": row["total"],
            "format_counts": _format_counts(row),
            "last_analyzed_at": row["last_analyzed_at"],
        }
        for row in rows
    }


def social_summary(params) -> dict:
    """Totals across every lecture the list would show (all pages)."""
    lectures = social_lectures(params).order_by().values("session_id")
    totals = accepted_opportunities().filter(session_id__in=lectures).aggregate(
        lectures=Count("session_id", distinct=True),
        total=Count("opportunity_id"),
        **{name: Count("opportunity_id", filter=Q(primary_format=name)) for name in FORMATS},
    )
    return {
        "lectures": totals["lectures"],
        "opportunity_count": totals["total"],
        "format_counts": _format_counts(totals),
    }
