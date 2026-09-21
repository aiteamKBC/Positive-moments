"""
Phase 2A acquisition service.

Covers the Phase 1 input contract, artifact identity and idempotency, content
hashing and versioning provenance, legacy QA session_id parity, and the rule
that no transcript text or secret ever reaches a log record.
"""
import logging
from datetime import date, datetime, timezone
from uuid import UUID

from app.common.hashing import content_sha256, provider_transcript_identity
from app.transcripts.models import (
    BLOCKED_TRANSCRIPT_ACCESS,
    CONTENT_STORED,
    CONTENT_STORED_NO_SPEAKER_ATTRIBUTION,
    ERROR,
    GRAPH_ACCESS_TO_TRANSCRIPTS_DISABLED,
    NO_TRANSCRIPTS_AVAILABLE,
    PROVIDER_ERROR,
    PROVIDER_MICROSOFT_GRAPH,
    SPEAKER_ATTRIBUTION_NOT_ALLOWED,
    TRANSCRIPTS_FOUND,
    VTT,
    ReadyLecture,
    TranscriptArtifact,
    TranscriptContent,
    TranscriptListing,
)
from app.transcripts.service import TranscriptAcquisitionService


TARGET = date(2026, 9, 4)
USER_ID = "737679b4-8eac-4fe9-a491-76d8cdf65f6d"
ATTRIBUTED = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Jane Doe>Highly confidential lecture speech.</v>\n"
UNATTRIBUTED = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHighly confidential lecture speech.\n"


def _lecture(subject="Ray-Project Management Office (PMO)", meeting="meeting-1",
             lecture_id="852e175e-3782-5659-961d-0d435fa6baee"):
    return ReadyLecture(
        lecture_id=UUID(lecture_id), meeting_id=meeting, meeting_lookup_user_id=USER_ID,
        scheduled_start=datetime(2026, 9, 4, 8, tzinfo=timezone.utc),
        scheduled_end=datetime(2026, 9, 4, 10, tzinfo=timezone.utc),
        session_date=TARGET, subject=subject, module=subject,
        meeting_lookup_context_source="DISCOVERY_MAILBOX_OBJECT_ID",
        join_url_oid_hint_status="JOIN_URL_OID_MATCHES_CONTEXT",
    )


def _artifact(transcript_id, hour=7):
    return TranscriptArtifact(
        provider_transcript_id=transcript_id,
        provider_created_at=datetime(2026, 9, 4, hour, 58, tzinfo=timezone.utc),
        provider_end_at=datetime(2026, 9, 4, hour + 2, 11, tzinfo=timezone.utc),
        provider_call_id="call-1", content_correlation_id="corr-20260904",
        provider_meeting_id="meeting-1",
    )


class Lectures:
    """Stands in for the read-only Phase 1 input repository."""

    def __init__(self, ready=None, skipped=None, considered=None):
        self.ready = ready if ready is not None else [_lecture()]
        self.skipped = skipped or []
        self.considered = considered if considered is not None else len(self.ready) + len(self.skipped)

    def load_day(self, connection, target_date):
        return {"considered": self.considered, "ready": self.ready, "skipped": self.skipped}


class Gateway:
    def __init__(self, listing=None, content=None):
        self._listing = listing
        self._content = content if content is not None else ATTRIBUTED
        self.listed = []
        self.fetched = []

    def list_transcripts(self, user_object_id, meeting_id):
        self.listed.append((user_object_id, meeting_id))
        if isinstance(self._listing, TranscriptListing):
            return self._listing
        artifacts = self._listing if self._listing is not None else [_artifact("t-1")]
        if not artifacts:
            return TranscriptListing(status=NO_TRANSCRIPTS_AVAILABLE)
        return TranscriptListing(status=TRANSCRIPTS_FOUND, artifacts=artifacts)

    def fetch_content(self, user_object_id, meeting_id, provider_transcript_id):
        self.fetched.append((user_object_id, meeting_id, provider_transcript_id))
        payload = self._content
        if isinstance(payload, dict):
            payload = payload.get(provider_transcript_id, payload.get("*"))
        if isinstance(payload, TranscriptContent):
            return payload
        return TranscriptContent(
            status="FETCHED", raw=payload, content_format=VTT,
            speaker_attribution=b"<v " in payload,
        )


class Artifacts:
    """In-memory stand-in mirroring the real repository's version semantics."""

    def __init__(self):
        self.rows = {}
        self.contents = {}
        self.failures = []

    def upsert(self, connection, artifact_id, lecture_id, **fields):
        outcome = "existing" if artifact_id in self.rows else "created"
        self.rows.setdefault(artifact_id, {"lecture_id": lecture_id, "sha": None, "version": 0})
        self.rows[artifact_id].update({k: v for k, v in fields.items()})
        return outcome

    def current_content_hash(self, connection, artifact_id):
        return self.rows.get(artifact_id, {}).get("sha")

    def store_content(self, connection, artifact_id, *, raw_text, content_sha256,
                      content_bytes, content_format, speaker_attribution):
        versions = self.contents.setdefault(artifact_id, [])
        for version in versions:
            if version["sha"] == content_sha256:
                self.rows[artifact_id].update({"sha": content_sha256, "version": version["number"]})
                return {"version_number": version["number"], "version_created": False}
        number = len(versions) + 1
        versions.append({"number": number, "sha": content_sha256, "raw": raw_text,
                         "speaker_attribution": speaker_attribution})
        self.rows[artifact_id].update({"sha": content_sha256, "version": number})
        return {"version_number": number, "version_created": True}

    def mark_content_failure(self, connection, artifact_id, artifact_status, reason):
        self.failures.append((artifact_id, artifact_status, reason))


class Runs:
    def __init__(self):
        self.started = 0
        self.summaries = []

    def start(self, connection, target_date):
        import uuid
        self.started += 1
        return uuid.uuid4()

    def complete(self, connection, run_id, summary):
        self.summaries.append(summary)


def service(lectures=None, gateway=None, artifacts=None, runs=None, qa=None):
    return TranscriptAcquisitionService(
        gateway=gateway or Gateway(),
        lecture_repository=lectures or Lectures(),
        artifact_repository=artifacts or Artifacts(),
        run_repository=runs or Runs(),
        qa_evidence_repository=qa,
    )


# --- Phase 1 input contract ---------------------------------------------------


def test_only_downstream_ready_lectures_are_acquired():
    gateway = Gateway()
    summary = service(lectures=Lectures(ready=[_lecture()], considered=8), gateway=gateway).acquire_day(
        object(), TARGET)
    assert summary["canonical_lectures"] == 8
    assert summary["lectures_ready"] == 1
    assert gateway.listed == [(USER_ID, "meeting-1")]


def test_unresolved_lecture_is_skipped_explicitly_and_reported():
    skipped = [{"lecture_id": "22abbd06", "subject": "Ray-MSP Jan 2026  ",
                "skip_reason": "NOT_DOWNSTREAM_READY", "downstream_ready": False,
                "has_meeting_id": False}]
    gateway = Gateway()
    summary = service(lectures=Lectures(ready=[_lecture()], skipped=skipped, considered=8),
                      gateway=gateway).acquire_day(object(), TARGET)
    assert summary["lectures_skipped_not_ready"] == 1
    assert summary["skipped_lectures"][0]["skip_reason"] == "NOT_DOWNSTREAM_READY"
    # The skipped lecture never reached Graph.
    assert len(gateway.listed) == 1


def test_the_persisted_user_object_id_is_the_graph_context():
    gateway = Gateway()
    service(gateway=gateway).acquire_day(object(), TARGET, persist=False)
    assert gateway.listed[0][0] == USER_ID
    assert gateway.fetched[0][0] == USER_ID


# --- artifact counts ----------------------------------------------------------


def test_zero_artifacts_is_reported_as_a_successful_empty_answer():
    summary = service(gateway=Gateway(listing=[])).acquire_day(object(), TARGET)
    assert summary["lectures"][0]["status"] == NO_TRANSCRIPTS_AVAILABLE
    assert summary["meetings_with_zero_artifacts"] == 1
    assert summary["error_count"] == 0


def test_one_artifact_is_stored():
    artifacts = Artifacts()
    summary = service(artifacts=artifacts, gateway=Gateway(listing=[_artifact("t-1")])).acquire_day(
        object(), TARGET)
    assert summary["artifacts_discovered"] == 1
    assert summary["meetings_with_one_artifact"] == 1
    assert len(artifacts.rows) == 1


def test_multiple_artifacts_are_all_stored_without_selecting_a_primary():
    listing = [_artifact(f"t-{index}", hour=7) for index in range(14)]
    artifacts = Artifacts()
    summary = service(artifacts=artifacts, gateway=Gateway(listing=listing)).acquire_day(
        object(), TARGET)
    assert summary["artifacts_discovered"] == 14
    assert summary["meetings_with_multiple_artifacts"] == 1
    assert len(artifacts.rows) == 14
    # Nothing in the result marks one artifact as chosen.
    assert not any("primary" in key for item in summary["lectures"]
                   for artifact in item["artifacts"] for key in artifact)
    assert summary["metadata"]["selection_performed"] is False


# --- identity and idempotency -------------------------------------------------


def test_artifact_identity_is_the_provider_identity_alone():
    """
    Phase 2B normalization: the lecture is NOT part of the physical key, so a
    recurring-series transcript reachable from two lectures is ONE artifact row
    with one copy of the raw bytes.
    """
    first = provider_transcript_identity(
        provider=PROVIDER_MICROSOFT_GRAPH, provider_transcript_id="t-1")
    assert first == provider_transcript_identity(
        provider="microsoft_graph", provider_transcript_id="t-1")
    assert first != provider_transcript_identity(
        provider=PROVIDER_MICROSOFT_GRAPH, provider_transcript_id="t-2")


def test_rerunning_acquisition_creates_no_duplicate_artifact_rows():
    listing = [_artifact("t-1"), _artifact("t-2")]
    artifacts, runs = Artifacts(), Runs()
    acquire = service(artifacts=artifacts, runs=runs, gateway=Gateway(listing=listing))
    first = acquire.acquire_day(object(), TARGET)
    second = acquire.acquire_day(object(), TARGET)

    assert (first["artifacts_created"], first["artifacts_existing"]) == (2, 0)
    assert (second["artifacts_created"], second["artifacts_existing"]) == (0, 2)
    assert len(artifacts.rows) == 2
    assert second["artifacts_discovered"] == 2


# --- content hashing and versioning -------------------------------------------


def test_raw_content_sha256_is_the_hash_of_the_provider_bytes():
    artifacts = Artifacts()
    summary = service(artifacts=artifacts, gateway=Gateway(content=ATTRIBUTED)).acquire_day(
        object(), TARGET)
    digest = content_sha256(ATTRIBUTED)
    stored = summary["lectures"][0]["artifacts"][0]
    assert stored["content_sha256_prefix"] == digest[:12]
    assert stored["content_bytes"] == len(ATTRIBUTED)
    version = next(iter(artifacts.contents.values()))[0]
    assert version["sha"] == digest
    # Stored verbatim: not cleaned, re-timed, or normalized.
    assert version["raw"] == ATTRIBUTED.decode("utf-8")


def test_identical_content_does_not_create_duplicate_evidence():
    artifacts = Artifacts()
    acquire = service(artifacts=artifacts, gateway=Gateway(content=ATTRIBUTED))
    first = acquire.acquire_day(object(), TARGET)
    second = acquire.acquire_day(object(), TARGET)

    assert first["content_versions_created"] == 1
    assert second["content_versions_created"] == 0
    assert second["content_unchanged"] == 1
    assert len(next(iter(artifacts.contents.values()))) == 1


def test_changed_content_appends_a_version_and_preserves_the_old_evidence():
    artifacts = Artifacts()
    gateway = Gateway(content=ATTRIBUTED)
    acquire = service(artifacts=artifacts, gateway=gateway)
    acquire.acquire_day(object(), TARGET)

    revised = ATTRIBUTED + b"\n00:00:03.000 --> 00:00:04.000\n<v Jane Doe>And more.</v>\n"
    gateway._content = revised
    second = acquire.acquire_day(object(), TARGET)

    versions = next(iter(artifacts.contents.values()))
    assert second["content_versions_created"] == 1
    assert [v["number"] for v in versions] == [1, 2]
    # Version 1 is untouched: nothing is overwritten or destroyed.
    assert versions[0]["sha"] == content_sha256(ATTRIBUTED)
    assert versions[1]["sha"] == content_sha256(revised)
    assert second["lectures"][0]["artifacts"][0]["content_changed"] is True


# --- provider failure states --------------------------------------------------


def test_transcripts_disabled_is_blocked_not_zero_transcripts():
    listing = TranscriptListing(status=BLOCKED_TRANSCRIPT_ACCESS,
                                reason=GRAPH_ACCESS_TO_TRANSCRIPTS_DISABLED,
                                http_status=403, provider_code="GraphAccessToTranscriptsDisabled")
    summary = service(gateway=Gateway(listing=listing)).acquire_day(object(), TARGET)
    assert summary["lectures"][0]["status"] == BLOCKED_TRANSCRIPT_ACCESS
    assert summary["admin_blocked_count"] == 1
    assert summary["meetings_with_zero_artifacts"] == 0


def test_provider_error_is_never_reported_as_a_successful_acquisition():
    listing = TranscriptListing(status=PROVIDER_ERROR, reason=PROVIDER_ERROR,
                                http_status=500, provider_code="InternalServerError")
    summary = service(gateway=Gateway(listing=listing)).acquire_day(object(), TARGET)
    assert summary["lectures"][0]["status"] == ERROR
    assert summary["error_count"] == 1
    assert summary["artifacts_discovered"] == 0


def test_unattributed_fallback_is_recorded_honestly():
    fallback = TranscriptContent(
        status="FETCHED", raw=UNATTRIBUTED, content_format=VTT, speaker_attribution=False,
        reason=SPEAKER_ATTRIBUTION_NOT_ALLOWED, used_speaker_attribution_fallback=True)
    summary = service(gateway=Gateway(content=fallback)).acquire_day(object(), TARGET)
    artifact = summary["lectures"][0]["artifacts"][0]

    assert summary["speaker_attribution_fallback_count"] == 1
    assert artifact["speaker_attribution"] is False
    assert artifact["artifact_status"] == CONTENT_STORED_NO_SPEAKER_ATTRIBUTION
    assert summary["lectures"][0]["status"] == CONTENT_STORED_NO_SPEAKER_ATTRIBUTION


def test_content_failure_marks_the_artifact_without_inventing_success():
    failed = TranscriptContent(status=BLOCKED_TRANSCRIPT_ACCESS,
                               reason=GRAPH_ACCESS_TO_TRANSCRIPTS_DISABLED, http_status=403)
    artifacts = Artifacts()
    summary = service(artifacts=artifacts, gateway=Gateway(content=failed)).acquire_day(
        object(), TARGET)
    assert artifacts.contents == {}
    assert artifacts.failures[0][1] == BLOCKED_TRANSCRIPT_ACCESS
    assert summary["lectures"][0]["artifacts"][0]["content_available"] is False
    assert summary["lectures"][0]["status"] == BLOCKED_TRANSCRIPT_ACCESS


def test_list_only_mode_fetches_no_content():
    gateway = Gateway()
    summary = service(gateway=gateway).acquire_day(object(), TARGET, fetch_content=False)
    assert gateway.fetched == []
    assert summary["artifact_contents_fetched"] == 0
    assert summary["lectures"][0]["status"] == TRANSCRIPTS_FOUND


# --- legacy QA parity ---------------------------------------------------------


class Qa:
    def __init__(self, rows):
        self.rows = rows

    def load_day(self, connection, target_date):
        return self.rows


def test_legacy_session_id_membership_is_measured_not_reinterpreted():
    listing = [_artifact("legacy-selected-id"), _artifact("other-id")]
    qa = Qa([{"session_id": "legacy-selected-id",
              "subject": "Ray-Project Management Office (PMO)", "meeting_id": "meeting-1"}])
    summary = service(gateway=Gateway(listing=listing), qa=qa).acquire_day(object(), TARGET)
    parity = summary["legacy_qa_parity"]

    assert parity["summary"] == "1 / 1"
    assert parity["rows"][0]["legacy_session_id_present_in_discovered_artifacts"] == "YES"
    assert parity["rows"][0]["discovered_artifact_count"] == 2


def test_missing_legacy_session_id_is_reported_as_no():
    qa = Qa([{"session_id": "never-discovered",
              "subject": "Ray-Project Management Office (PMO)", "meeting_id": "meeting-1"}])
    summary = service(gateway=Gateway(listing=[_artifact("t-1")]), qa=qa).acquire_day(
        object(), TARGET)
    parity = summary["legacy_qa_parity"]
    assert parity["summary"] == "0 / 1"
    assert parity["rows"][0]["legacy_session_id_present_in_discovered_artifacts"] == "NO"


# --- security -----------------------------------------------------------------


def test_no_transcript_text_or_secret_reaches_the_logs(caplog):
    secret_phrase = "Highly confidential lecture speech"
    with caplog.at_level(logging.DEBUG):
        summary = service(gateway=Gateway(content=ATTRIBUTED)).acquire_day(object(), TARGET)

    emitted = "\n".join(
        record.getMessage() + str(getattr(record, "fields", "")) for record in caplog.records
    )
    assert emitted, "the run must actually log something"
    assert secret_phrase not in emitted
    assert "WEBVTT" not in emitted
    assert "Bearer" not in emitted
    assert "Authorization" not in emitted
    # The summary itself carries only a short hash prefix, never the content.
    serialized = str(summary)
    assert secret_phrase not in serialized
    assert "WEBVTT" not in serialized
    assert len(summary["lectures"][0]["artifacts"][0]["content_sha256_prefix"]) == 12
