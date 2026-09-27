"""Safe, flat views of a lecture outcome for read-only previews. No URL, no id of Graph."""
from __future__ import annotations


def alignment_row(outcome) -> dict:
    state, alignment = outcome.state, outcome.detail.get("alignment") or {}
    return {
        "lecture_id": outcome.lecture_id,
        "date": str(state.get("session_date")), "subject": state.get("subject"),
        "transcript_state": state.get("transcript_state"),
        "analysis_state": state.get("analysis_state"),
        "recording_state": state.get("recording_state"),
        "recording_status": state.get("recording_status"),
        "recording_rule": state.get("recording_rule"),
        "recording_duration_seconds": state.get("recording_duration_seconds"),
        "alignment_status": alignment.get("status"),
        "alignment_code": alignment.get("code"),
        "alignment_method": alignment.get("method"),
        "media_offset_seconds": alignment.get("media_offset_seconds"),
        "file_name_lead_seconds": alignment.get("file_name_lead_seconds"),
        "reference_duration_seconds": alignment.get("reference_duration_seconds"),
        "reference_source": alignment.get("reference_source"),
        "pairing": alignment.get("pairing"),
        "recording_facts": outcome.detail.get("recording_facts"),
    }


class PreMigrationMoments:
    """Read-only preview before migration 023: no analyses exist yet."""

    def __init__(self, inner):
        self.inner = inner

    def transcript(self, connection, document_id):
        return self.inner.transcript(connection, document_id)

    def latest_analysis(self, *args, **kwargs):
        return None

    def current_analysis(self, *args, **kwargs):
        return None

    def moments(self, *args, **kwargs):
        return []


class PreMigrationMedia:
    """Read-only preview before migration 023: no media jobs exist yet."""

    def __init__(self, inner):
        self.inner = inner

    def job_counts_by_lecture(self, *args, **kwargs):
        return {}

    def __getattr__(self, name):
        return getattr(self.inner, name)
