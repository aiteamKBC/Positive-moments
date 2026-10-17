from django.conf import settings
from django.db import models


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


class ClipReview(models.Model):
    """
    A person's decision on one stored clip, kept apart from the AI evidence.

    The n8n-written `positive_clips` JSON is never touched: this row points at
    a clip by the identity the platform already uses for it (session + start
    cue + end cue) and records who decided what, and when. It is separate from
    the semantic verdict and from `social_media_ready` on purpose - neither is
    a human approval to publish.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    STATUSES = (PENDING, APPROVED, REJECTED)

    session_id = models.TextField()
    start_cue = models.IntegerField()
    end_cue = models.IntegerField()
    status = models.TextField(choices=[(value, value) for value in STATUSES])
    note = models.TextField(blank=True, default="")
    # No database constraint: auth_user is shared with other Django projects on
    # this database, and a foreign key here would make their user deletions fail.
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="+",
                                 db_constraint=False)
    # Kept as written at the time, so a later rename or deleted account does
    # not rewrite who made the decision.
    reviewer_name = models.TextField()
    reviewed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "positive_clip_reviews"
        constraints = [
            models.UniqueConstraint(fields=("session_id", "start_cue", "end_cue"),
                                    name="positive_clip_reviews_clip_key"),
            models.CheckConstraint(condition=models.Q(status__in=("pending", "approved", "rejected")),
                                   name="positive_clip_reviews_status"),
        ]
