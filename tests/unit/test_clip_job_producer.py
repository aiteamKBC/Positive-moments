"""
Phase 6B: the Positive Clip job producer.

These cover the two properties that decide whether a replay is safe - identity
stability and idempotency - and the downstream metadata contract whose absence
the first real pilot exposed: the clip uploaded successfully and then created
no asset row at all, because the asset-sync statement reads `original_start`
and `original_end` out of job metadata and v2 had renamed them.

A tiny fake connection stands in for psycopg. It is not a database; it records
the statements the producer issues and answers the three lookups the producer
makes, which is exactly enough to test the decisions.
"""
import json

import pytest

from app.media.clip_jobs import (
    PRODUCED_BY,
    PositiveClipJobProducer,
    analysis_fields,
    moments_from_analysis,
    output_filename,
    seconds,
    source_fingerprint,
)
from app.media.coordinates import MediaTimeline, RecordingPart
from app.media.positive_clips import POSITIVE_CLIP_PLANNER_VERSION


DESTINATION = {"destination_drive_id": "drive-1",
               "destination_folder_item_id": "folder-1"}

SESSION = "session-femi"

TIMELINE = MediaTimeline([RecordingPart(
    part_index=1, canonical_offset_seconds=0.0, media_duration_seconds=8783.0,
    recording_id="rec-1", user_id="user", meeting_id="meeting")])

CLIPS = [
    {"start": "00:04:14.320", "end": "00:04:35.000", "start_cue": 41,
     "end_cue": 45, "speaker": "Kelly Chan", "category": "learning_experience",
     "positive_quote": "It was good.", "reason": "praised the session",
     "confidence": 0.86, "semantic_verification": {"verdict": "accept"}},
    {"start": "00:06:04.800", "end": "00:06:56.000", "start_cue": 60,
     "end_cue": 64, "speaker": "Kelly Chan", "category": "trainer",
     "quote": "Really helpful."},
]


class FakeConnection:
    """
    Answers the producer's three lookups and records its writes.

    `existing_jobs` maps job_key -> (job_id, status, metadata).
    `assets` maps clip_key -> (trim_status, start, end, url).
    """

    def __init__(self, *, clips=CLIPS, existing_jobs=None, assets=None):
        self.clips = clips
        self.existing_jobs = existing_jobs or {}
        self.assets = assets or {}
        self.inserts = []
        self.replans = []

    def execute(self, statement, params=None):
        text = " ".join(statement.split())
        if "FROM public.qa_doctors_sessions" in text and "positive_clips" in text:
            return _Result((SESSION, "G3 - Femi", "2026-07-24", "Femi Falodun",
                            self.clips, "drive-src", "item-src",
                            "positive_clips_v5_final"))
        if "FROM public.qa_positive_clip_assets" in text:
            return _Result(self.assets.get(params[0]))
        if "FROM public.qa_media_jobs" in text and "WHERE job_key" in text:
            return _Result(self.existing_jobs.get(params[0]))
        if text.startswith("INSERT INTO public.qa_media_jobs"):
            self.inserts.append(params)
            return _Result((len(self.inserts),))
        if text.startswith("UPDATE public.qa_media_jobs"):
            self.replans.append(params)
            return _Result((1,))
        raise AssertionError(f"unexpected statement: {text[:80]}")


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


def produce(connection, dry_run=False):
    return PositiveClipJobProducer(destination=DESTINATION).produce(
        connection, SESSION, TIMELINE, dry_run=dry_run)


# --- reading the stored analysis --------------------------------------------

def test_timestamps_are_parsed_strictly():
    assert seconds("00:04:14.320") == pytest.approx(254.32)
    assert seconds("01:54:05.761") == pytest.approx(6845.761)
    for bad in ("4:14", "00:04:14", "", None, "00:99:14.320"):
        assert seconds(bad) is None


def test_a_moment_with_unreadable_timestamps_is_skipped_not_guessed():
    moments = moments_from_analysis([
        {"start": "00:04:14.320", "end": "00:04:35.000", "start_cue": 1, "end_cue": 2},
        {"start": "nonsense", "end": "00:05:00.000", "start_cue": 3, "end_cue": 4},
        {"start": "00:06:00.000", "end": "00:06:30.000"},          # no cues
    ])
    assert [m.clip_index for m in moments] == [1]


def test_the_filename_convention_is_unchanged():
    assert output_filename("abc", "G3 - Femi - Customer Journey", "2026-07-24",
                           1, 41, 45).endswith("_clip-01_cue-41-45.mp4")
    assert output_filename("abc", None, None, 2, 7, 9).startswith("00000000_lecture_")


# --- the downstream contract the pilot broke --------------------------------

def test_the_metadata_keeps_the_keys_the_asset_sync_requires():
    """
    `020_complete_endpoint_asset_sync.sql` refuses a job whose metadata has no
    original_start/original_end. The first v2 job uploaded fine and produced no
    asset because of exactly this.
    """
    connection = FakeConnection()
    produce(connection)
    assert connection.inserts
    metadata = json.loads(connection.inserts[0]["metadata"])
    for required in ("original_start", "original_end", "clip_index", "speaker",
                     "category", "reason", "confidence"):
        assert required in metadata, required
    assert metadata["original_start"] == "00:04:14.320"
    assert metadata["original_end"] == "00:04:35.000"


def test_the_metadata_also_carries_the_new_coordinate_provenance():
    connection = FakeConnection()
    produce(connection)
    metadata = json.loads(connection.inserts[0]["metadata"])
    assert metadata["planner_version"] == POSITIVE_CLIP_PLANNER_VERSION
    assert metadata["media_coordinate_version"] == "call_relative_part_media_v1"
    assert metadata["produced_by"] == PRODUCED_BY
    assert metadata["recording_id"] == "rec-1"
    assert "source_fingerprint" in metadata


def test_analysis_fields_do_not_invent_values():
    assert analysis_fields({})["original_start"] is None
    assert analysis_fields({"start": "x"})["original_start"] == "x"


# --- what gets queued --------------------------------------------------------

def test_both_moments_are_queued_with_the_planned_media_range():
    connection = FakeConnection()
    outcome = produce(connection)
    assert outcome.planned == 2
    assert outcome.inserted == 2
    first = connection.inserts[0]
    assert first["start_seconds"] == pytest.approx(254.32 - 60)
    assert first["end_seconds"] == pytest.approx(275.0 + 60)
    assert first["cut_mode"] == "precise"


def test_the_destination_is_the_configured_one():
    connection = FakeConnection()
    produce(connection)
    assert connection.inserts[0]["destination_drive_id"] == "drive-1"
    assert connection.inserts[0]["destination_folder_item_id"] == "folder-1"


def test_a_destination_is_required():
    with pytest.raises(ValueError):
        PositiveClipJobProducer(destination={})


# --- idempotency -------------------------------------------------------------

def test_a_delivered_clip_is_never_re_queued():
    """Re-cutting would hand somebody a second copy of a clip they already have."""
    connection = FakeConnection(assets={
        f"{SESSION}:41:45": ("completed", 194.32, 335.76, "https://x")})
    outcome = produce(connection)
    assert outcome.already_delivered == 1
    assert outcome.inserted == 1                    # only the second moment
    assert len(connection.inserts) == 1


def test_an_unchanged_plan_is_a_noop():
    fingerprint = source_fingerprint(SESSION, CLIPS, TIMELINE)
    existing = {
        f"positive:{_short()}:41:45": (1, "pending", {
            "source_fingerprint": fingerprint,
            "planner_version": POSITIVE_CLIP_PLANNER_VERSION}),
        f"positive:{_short()}:60:64": (2, "pending", {
            "source_fingerprint": fingerprint,
            "planner_version": POSITIVE_CLIP_PLANNER_VERSION}),
    }
    connection = FakeConnection(existing_jobs=existing)
    outcome = produce(connection)
    assert outcome.inserted == 0
    assert outcome.replanned == 0
    assert outcome.unchanged == 2
    assert connection.inserts == [] and connection.replans == []


def test_a_changed_fingerprint_replans_a_pending_job():
    existing = {f"positive:{_short()}:41:45": (1, "pending", {
        "source_fingerprint": "stale",
        "planner_version": POSITIVE_CLIP_PLANNER_VERSION})}
    connection = FakeConnection(existing_jobs=existing)
    outcome = produce(connection)
    assert outcome.replanned == 1
    assert len(connection.replans) == 1


def test_a_running_or_finished_job_is_left_alone():
    """
    The queue owns it. Rewriting the boundaries of a job a worker is already
    cutting would change what it produces half way through.
    """
    for status in ("processing", "completed"):
        existing = {f"positive:{_short()}:41:45": (1, status, {
            "source_fingerprint": "stale",
            "planner_version": POSITIVE_CLIP_PLANNER_VERSION})}
        connection = FakeConnection(existing_jobs=existing)
        outcome = produce(connection)
        assert outcome.replanned == 0, status
        assert connection.replans == [], status
        assert any(job["state"] == f"left_alone_{status}" for job in outcome.jobs)


def test_a_dry_run_writes_nothing():
    connection = FakeConnection()
    outcome = produce(connection, dry_run=True)
    assert outcome.inserted == 2
    assert connection.inserts == [] and connection.replans == []


# --- the fingerprint ---------------------------------------------------------

def test_the_fingerprint_changes_with_the_analysis():
    first = source_fingerprint(SESSION, CLIPS, TIMELINE)
    changed = source_fingerprint(
        SESSION, [{**CLIPS[0], "start": "00:04:15.000"}, CLIPS[1]], TIMELINE)
    assert first != changed


def test_the_fingerprint_changes_with_a_re_measured_recording():
    """
    A different measured duration means different padding bounds, so a pending
    plan built on the old measurement is stale.
    """
    other = MediaTimeline([RecordingPart(
        part_index=1, canonical_offset_seconds=0.0, media_duration_seconds=9000.0,
        recording_id="rec-1", user_id="user", meeting_id="meeting")])
    assert source_fingerprint(SESSION, CLIPS, TIMELINE) != \
        source_fingerprint(SESSION, CLIPS, other)


def test_the_fingerprint_is_stable_across_runs():
    assert source_fingerprint(SESSION, CLIPS, TIMELINE) == \
        source_fingerprint(SESSION, CLIPS, TIMELINE)


def _short():
    import hashlib
    return hashlib.md5(SESSION.encode()).hexdigest()[:8]
