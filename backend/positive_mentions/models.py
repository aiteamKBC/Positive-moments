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
