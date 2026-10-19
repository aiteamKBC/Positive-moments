import uuid

from django.db import models
from django.utils import timezone


class DoctorSession(models.Model):
    session_id = models.TextField(primary_key=True)
    meeting_id = models.TextField(null=True, blank=True)
    date = models.DateField(null=True, blank=True)
    subject = models.TextField(null=True, blank=True)
    trainer = models.TextField(null=True, blank=True)
    positive_clips = models.JSONField(null=True, blank=True)
    positive_clips_count = models.IntegerField(null=True, blank=True)
    clips_status = models.TextField(null=True, blank=True)
    clips_analyzed_at = models.DateTimeField(null=True, blank=True)
    clips_analysis_completeness = models.TextField(null=True, blank=True)
    clips_error = models.TextField(null=True, blank=True)
    has_positive_clips = models.BooleanField(null=True, blank=True)
    recording_url = models.TextField(null=True, blank=True)
    recording_item_id = models.TextField(null=True, blank=True)
    recording_drive_id = models.TextField(null=True, blank=True)
    recording_filename = models.TextField(null=True, blank=True)
    recording_link_status = models.TextField(null=True, blank=True)
    recording_link_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "qa_doctors_sessions"
        ordering = ("-date", "session_id")


class PositiveClipAsset(models.Model):
    clip_key = models.TextField(primary_key=True)
    session_id = models.TextField()
    clip_index = models.IntegerField()
    source_start = models.TextField()
    source_end = models.TextField()
    trim_start_seconds = models.DecimalField(max_digits=12, decimal_places=3)
    trim_end_seconds = models.DecimalField(max_digits=12, decimal_places=3)
    duration_seconds = models.DecimalField(max_digits=12, decimal_places=3)
    clip_filename = models.TextField(null=True, blank=True)
    clip_drive_id = models.TextField(null=True, blank=True)
    clip_item_id = models.TextField(null=True, blank=True)
    clip_url = models.TextField(null=True, blank=True)
    trim_status = models.TextField()
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "qa_positive_clip_assets"


class SocialMediaOpportunity(models.Model):
    """
    One AI-suggested social media opportunity found in a lecture.

    Written by the Social Media pipeline, read here only. `session_id` points at
    qa_doctors_sessions; it is a plain column rather than a ForeignKey so the
    existing lecture model gains no reverse relation.
    """
    opportunity_id = models.BigIntegerField(primary_key=True)
    session_id = models.TextField()
    opportunity_key = models.TextField()
    start_cue = models.IntegerField()
    end_cue = models.IntegerField()
    start_time = models.TextField(null=True, blank=True)
    end_time = models.TextField(null=True, blank=True)
    duration_seconds = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    speaker = models.TextField(null=True, blank=True)
    quote = models.TextField(null=True, blank=True)
    content_angle = models.TextField(null=True, blank=True)
    why_selected = models.TextField(null=True, blank=True)
    primary_format = models.TextField(null=True, blank=True)
    formats = models.JSONField(default=list)
    verification_status = models.TextField()
    verification_reason = models.TextField(null=True, blank=True)
    discovery_source = models.TextField()
    evidence_type = models.TextField(null=True, blank=True)
    opportunity_data = models.JSONField(default=dict)
    analyzed_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "qa_social_media_opportunities"


# --- Social Media operations (owned by Django; migration 0001) -----------------
#
# Prompt history, analysis runs and OpenAI usage. Nothing here reads or writes
# qa_social_media_opportunities except a COUNT when a run reports completion.
# `auth_user` is shared with other college systems, so user links carry no
# database constraint and every row also keeps the person's name.

PROMPT_KEYS = ("social_discovery", "social_verification")
RUN_STATUSES = ("queued", "running", "completed", "failed")
ACTIVE_RUN_STATUSES = ("queued", "running")
RUN_TRIGGERS = ("on_demand", "scheduled")
USAGE_STAGES = ("discovery", "verification")
USAGE_SOURCES = ("reported", "estimated", "unavailable")
COST_STATUSES = ("measured", "estimated", "unknown")


def _choices(values):
    return [(value, value) for value in values]


class SocialMediaPromptVersion(models.Model):
    """
    One saved System Message. Append only: the active prompt is the highest
    version of its key, a change adds a version and a rollback adds a version
    copying an older one, so every earlier text stays available.
    """
    prompt_key = models.TextField(choices=_choices(PROMPT_KEYS))
    version = models.PositiveIntegerField()
    content = models.TextField()
    content_sha256 = models.TextField()
    restored_from_version = models.PositiveIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        db_constraint=False, related_name="+")
    created_by_name = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "qa_social_media_prompt_versions"
        ordering = ("prompt_key", "-version")
        constraints = [
            models.UniqueConstraint(fields=("prompt_key", "version"),
                                    name="qa_social_media_prompt_versions_key_version"),
            models.CheckConstraint(condition=models.Q(prompt_key__in=PROMPT_KEYS),
                                   name="qa_social_media_prompt_versions_key"),
            models.CheckConstraint(condition=models.Q(version__gte=1),
                                   name="qa_social_media_prompt_versions_version"),
        ]


class SocialMediaAnalysisRun(models.Model):
    """
    One attempt to analyse one lecture for social media, on demand or scheduled.

    `completed` is written only when n8n reports completion AND the number of
    opportunity rows it reports matches what PostgreSQL holds for the lecture
    since the run was created. At most one queued/running run per lecture.
    """
    run_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session_id = models.TextField()
    trigger = models.TextField(choices=_choices(RUN_TRIGGERS))
    status = models.TextField(choices=_choices(RUN_STATUSES), default="queued")
    requested_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        db_constraint=False, related_name="+")
    requested_by_name = models.TextField(blank=True, default="")
    n8n_execution_id = models.TextField(null=True, blank=True)
    opportunities_reported = models.IntegerField(null=True, blank=True)
    opportunities_verified = models.IntegerField(null=True, blank=True)
    error_code = models.TextField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "qa_social_media_analysis_runs"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("session_id", "-created_at"),
                                name="qa_social_media_runs_session")]
        constraints = [
            models.UniqueConstraint(fields=("session_id",),
                                    condition=models.Q(status__in=ACTIVE_RUN_STATUSES),
                                    name="qa_social_media_runs_one_active"),
            models.CheckConstraint(condition=models.Q(status__in=RUN_STATUSES),
                                   name="qa_social_media_runs_status"),
            models.CheckConstraint(condition=models.Q(trigger__in=RUN_TRIGGERS),
                                   name="qa_social_media_runs_trigger"),
        ]


class SocialMediaUsageEvent(models.Model):
    """
    One OpenAI call made by a run, as n8n reported it.

    Token counts are null when the provider did not report them. Prices are
    copied from configuration when the event is recorded, so a later price
    change never rewrites history. `cost_usd` is null unless tokens AND prices
    are both known; `cost_status` says whether it is measured or estimated.
    """
    run = models.ForeignKey(SocialMediaAnalysisRun, on_delete=models.PROTECT,
                            related_name="usage_events")
    event_key = models.TextField()
    stage = models.TextField(choices=_choices(USAGE_STAGES))
    model = models.TextField()
    openai_response_id = models.TextField(null=True, blank=True)
    usage_source = models.TextField(choices=_choices(USAGE_SOURCES))
    input_tokens = models.BigIntegerField(null=True, blank=True)
    cached_input_tokens = models.BigIntegerField(null=True, blank=True)
    output_tokens = models.BigIntegerField(null=True, blank=True)
    input_price_per_million = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    cached_input_price_per_million = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    output_price_per_million = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    pricing_source = models.TextField(null=True, blank=True)
    cost_usd = models.DecimalField(max_digits=18, decimal_places=8, null=True, blank=True)
    cost_status = models.TextField(choices=_choices(COST_STATUSES))
    occurred_at = models.DateTimeField(null=True, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "qa_social_media_usage_events"
        ordering = ("recorded_at", "id")
        constraints = [
            models.UniqueConstraint(fields=("run", "event_key"),
                                    name="qa_social_media_usage_events_key"),
            models.CheckConstraint(condition=models.Q(stage__in=USAGE_STAGES),
                                   name="qa_social_media_usage_events_stage"),
            models.CheckConstraint(condition=models.Q(usage_source__in=USAGE_SOURCES),
                                   name="qa_social_media_usage_events_source"),
            models.CheckConstraint(condition=models.Q(cost_status__in=COST_STATUSES),
                                   name="qa_social_media_usage_events_cost_status"),
            # Measured/estimated costs always have a value; unknown never does.
            models.CheckConstraint(
                condition=(models.Q(cost_status="unknown", cost_usd__isnull=True)
                           | (~models.Q(cost_status="unknown") & models.Q(cost_usd__isnull=False))),
                name="qa_social_media_usage_events_cost_value"),
        ]
