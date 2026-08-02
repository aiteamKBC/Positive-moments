from django.db import connection
from django.db.models import Count, Exists, IntegerField, OuterRef, Q, QuerySet, Subquery, Value
from django.db.models.expressions import RawSQL
from django.db.models.functions import Coalesce

from .models import DoctorSession, PositiveClipAsset

V5_FINAL = "positive_clips_v5_final"
CLIP_READY_STATUS = "completed"


def v5_lectures() -> QuerySet:
    return DoctorSession.objects.filter(clips_analysis_completeness=V5_FINAL)


def with_positive_clips_length(queryset: QuerySet) -> QuerySet:
    if connection.vendor == "postgresql":
        sql = (
            "CASE WHEN jsonb_typeof(positive_clips) = 'array' "
            "THEN jsonb_array_length(positive_clips) ELSE 0 END"
        )
    else:
        sql = (
            "CASE WHEN JSON_TYPE(positive_clips) = 'array' "
            "THEN JSON_ARRAY_LENGTH(positive_clips) ELSE 0 END"
        )
    return queryset.annotate(
        positive_clips_length=RawSQL(sql, (), output_field=IntegerField())
    )


def ready_clip_assets() -> QuerySet:
    return PositiveClipAsset.objects.filter(
        trim_status=CLIP_READY_STATUS,
        clip_url__regex=r"^\s*https://[^/\s]+\.sharepoint\.com(?:/[^\s]*)?\s*$",
    )


def with_clip_production_status(queryset: QuerySet) -> QuerySet:
    assets = ready_clip_assets().filter(session_id=OuterRef("session_id"))
    asset_counts = (
        assets.values("session_id")
        .annotate(total=Count("clip_key", distinct=True))
        .values("total")
    )
    return queryset.annotate(
        has_ready_clips=Exists(assets),
        ready_clips_count=Coalesce(
            Subquery(asset_counts, output_field=IntegerField()),
            Value(0),
        ),
    )


def filtered_lectures(params) -> QuerySet:
    queryset = with_clip_production_status(v5_lectures())
    search = params.get("search", "").strip()
    if search:
        queryset = queryset.filter(Q(subject__icontains=search) | Q(trainer__icontains=search))
    if params.get("trainer"):
        queryset = queryset.filter(trainer=params["trainer"])
    if params.get("date_from"):
        queryset = queryset.filter(date__gte=params["date_from"])
    if params.get("date_to"):
        queryset = queryset.filter(date__lte=params["date_to"])
    if params.get("clips_status"):
        queryset = queryset.filter(clips_status=params["clips_status"])
    positive = params.get("has_positive_clips")
    if positive in {"true", "false"}:
        queryset = with_positive_clips_length(queryset)
        lookup = "positive_clips_length__gt" if positive == "true" else "positive_clips_length"
        queryset = queryset.filter(**{lookup: 0})
    production = params.get("clip_production_status", "all")
    if production == "ready":
        queryset = queryset.filter(has_ready_clips=True)
    elif production in {"pending", "no_positive_moments"}:
        queryset = with_positive_clips_length(queryset)
        if production == "pending":
            queryset = queryset.filter(
                positive_clips_length__gt=0,
                has_ready_clips=False,
            )
        else:
            queryset = queryset.filter(positive_clips_length=0)
    recording = params.get("recording_status")
    if recording == "available":
        queryset = queryset.exclude(recording_url__isnull=True).exclude(recording_url="")
    elif recording == "missing":
        queryset = queryset.filter(Q(recording_url__isnull=True) | Q(recording_url=""))
    return queryset
