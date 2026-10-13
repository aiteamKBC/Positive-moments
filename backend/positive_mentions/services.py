from django.db import connection
from django.db.models import Count, Exists, IntegerField, JSONField, OuterRef, Q, QuerySet, Subquery, Value
from django.db.models.expressions import RawSQL
from django.db.models.functions import Coalesce

from .models import DoctorSession, PositiveClipAsset

V5_FINAL = "positive_clips_v5_final"
CLIP_READY_STATUS = "completed"


def v5_lectures() -> QuerySet:
    return DoctorSession.objects.filter(clips_analysis_completeness=V5_FINAL)


def clip_category(params) -> str:
    """The requested moment category, as the analysis spells it."""
    return (params.get("category") or "").strip().lower()


def _moments_in_category_sql():
    """How many of a row's moments carry one category (one parameter)."""
    if connection.vendor == "postgresql":
        return (
            "CASE WHEN jsonb_typeof(positive_clips) = 'array' THEN ("
            "SELECT count(*) FROM jsonb_array_elements(positive_clips) AS clip "
            "WHERE lower(btrim(clip ->> 'category')) = %s) ELSE 0 END"
        )
    return (
        "CASE WHEN JSON_TYPE(positive_clips) = 'array' THEN ("
        "SELECT count(*) FROM json_each(positive_clips) "
        "WHERE lower(trim(json_extract(value, '$.category'))) = %s) ELSE 0 END"
    )


def with_positive_clips_length(queryset: QuerySet, category: str = "") -> QuerySet:
    """Moments per lecture - only those of `category` when one is chosen."""
    if category:
        return queryset.annotate(positive_clips_length=RawSQL(
            _moments_in_category_sql(), (category,), output_field=IntegerField()))
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


# The lecture LIST needs two things from `positive_clips`: how many moments
# there are and which categories they carry. The column also holds every
# moment's quotes, dialogue and verification, ~37 KB per lecture, so the list
# asks PostgreSQL for the same structure with only `category` left in each
# moment. `normalize_clips` reads it exactly as it reads the full value.
_SLIM_ELEMENTS = (
    "(SELECT COALESCE(jsonb_agg(CASE WHEN jsonb_typeof(e) = 'object' "
    "THEN jsonb_build_object('category', e -> 'category') ELSE 'null'::jsonb END "
    "ORDER BY i), '[]'::jsonb) FROM jsonb_array_elements({0}) WITH ORDINALITY AS t(e, i))"
)
_SLIM_CLIPS_SQL = (
    "CASE jsonb_typeof(positive_clips) "
    "WHEN 'array' THEN " + _SLIM_ELEMENTS.format("positive_clips") + " "
    "WHEN 'object' THEN (SELECT CASE WHEN jsonb_typeof(c) = 'array' THEN "
    + _SLIM_ELEMENTS.format("c") + " ELSE 'null'::jsonb END FROM (SELECT CASE "
    "WHEN positive_clips ? 'clips' THEN positive_clips -> 'clips' "
    "ELSE COALESCE(positive_clips -> 'positive_clips', '[]'::jsonb) END AS c) AS picked) "
    "ELSE 'null'::jsonb END"
)


def with_slim_positive_clips(queryset: QuerySet) -> QuerySet:
    """List rows without the bulky moment detail; other databases keep the column."""
    if connection.vendor != "postgresql":
        return queryset
    return queryset.defer("positive_clips").annotate(
        positive_clips_slim=RawSQL(_SLIM_CLIPS_SQL, (), output_field=JSONField()))


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
    category = clip_category(params)
    if category:
        # Only lectures holding at least one moment of this category.
        queryset = queryset.annotate(category_moments=RawSQL(
            _moments_in_category_sql(), (category,), output_field=IntegerField(),
        )).filter(category_moments__gt=0)
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
