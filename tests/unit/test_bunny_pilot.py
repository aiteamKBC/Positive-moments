"""The Bunny single-recording pilot: preflight gates, idempotency, reconciliation."""
from __future__ import annotations

import json

import pytest

from app.bunny.manifest import PilotManifest
from app.bunny.pilot import BLOCKED, COMPLETED, REVIEW_REQUIRED, WAITING, BunnyPilot, PilotConfig
from tests.unit.bunny_fakes import (
    LECTURE_ID,
    LIBRARY_ID,
    TEMP_URL,
    FakeBunny,
    FakeCdn,
    FakeCore,
    FakeGraph,
    FakeMedia,
    lecture_source,
    uncertain,
)

CREDS = {"BUNNY_STAGING_LIBRARY_ID": True, "BUNNY_STAGING_API_KEY": True,
         "BUNNY_STAGING_CDN_HOSTNAME": True, "BUNNY_STAGING_ACCOUNT_API_KEY": True}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def make(tmp_path, *, bunny=None, core="default", source=None, graph=None, library_id=LIBRARY_ID,
         dry_run=False, cdn=None, **config):
    bunny = bunny or FakeBunny()
    clock = Clock()
    pilot = BunnyPilot(
        config=PilotConfig(lecture_id=LECTURE_ID, library_id=library_id, cdn_hostname="vz-test.b-cdn.net",
                           output_dir=str(tmp_path / "out"), dry_run=dry_run,
                           poll_interval_seconds=10, identify_timeout_seconds=60,
                           encoding_timeout_seconds=600, **config),
        load_source=lambda: source or lecture_source(), graph=graph or FakeGraph(), media=FakeMedia(),
        bunny=bunny, core=FakeCore() if core == "default" else core,
        manifest=PilotManifest(tmp_path / "out" / "manifest.json"), credentials=CREDS,
        sleep=clock.sleep, clock=clock, open_url=cdn or FakeCdn(bunny))
    return pilot, bunny


def statuses(report):
    return {c["name"]: c["status"] for c in report["preflight"]}


def test_preflight_blocks_when_library_settings_cannot_be_verified(tmp_path):
    pilot, bunny = make(tmp_path, core=None)
    report = pilot.run()
    assert report["outcome"] == BLOCKED
    assert {statuses(report)[n] for n in ("free_encoding_enabled", "ai_transcribing_disabled",
                                          "h264_output_enabled", "resolutions_appropriate")} == {"UNVERIFIABLE"}
    assert bunny.fetch_calls == [] and bunny.caption_calls == [] and bunny.meta_calls == []
    assert pilot.manifest.videos() == []


def test_old_library_is_refused_before_any_bunny_call(tmp_path):
    bunny = FakeBunny(library_id=785080)
    pilot, _ = make(tmp_path, bunny=bunny, library_id=785080)
    report = pilot.run()
    assert report["outcome"] == BLOCKED
    assert statuses(report)["destination_is_staging_library"] == "FAIL"
    assert bunny.list_calls == 0 and bunny.fetch_calls == []


@pytest.mark.parametrize("setting", [{"EnableTranscribing": True}, {"EncodingTier": "Premium"},
                                     {"OutputCodecs": "vp9"}, {"TranscribingCaptionLanguages": ["en"]}])
def test_paid_or_ai_settings_block(tmp_path, setting):
    pilot, bunny = make(tmp_path, core=FakeCore(**setting))
    assert pilot.run()["outcome"] == BLOCKED
    assert bunny.fetch_calls == []


def test_dry_run_passes_preflight_and_writes_nothing(tmp_path):
    pilot, bunny = make(tmp_path, dry_run=True)
    report = pilot.run()
    assert report["outcome"] == "READY", report.get("blocking")
    assert all(c["status"] == "PASS" for c in report["preflight"])
    assert bunny.fetch_calls == [] and pilot.manifest.videos() == []


def test_end_to_end_then_rerun_is_idempotent(tmp_path):
    pilot, bunny = make(tmp_path)
    report = pilot.run()
    assert report["outcome"] == COMPLETED, report
    assert len(bunny.fetch_calls) == 1 and len(bunny.caption_calls) == 1
    video = report["video"]
    assert video["import_status"] == "IDENTIFIED" and video["encoding_status"] == "VERIFIED"
    assert video["video_title"] == "AI in Project Control 2026 | 2026-10-16 | bb2d676b | " + video["recording_identity"]
    caption = report["caption"]
    assert caption["publication_status"] == "VERIFIED"
    assert caption["readback"]["content_check"] == "IDENTICAL"
    assert caption["applied_offset_ms"] == 0 and caption["removed_voice_tag_count"] == 12
    uploaded = bunny.caption_files[(video["bunny_video_id"], "en")]
    assert "<v" not in uploaded and "Synthetic Trainer" not in uploaded

    again = make(tmp_path, bunny=bunny)[0].run()
    assert again["outcome"] == COMPLETED
    assert len(bunny.fetch_calls) == 1 and len(bunny.caption_calls) == 1

    persisted = (tmp_path / "out" / "manifest.json").read_text(encoding="utf-8")
    assert "tempauth" not in persisted and TEMP_URL not in persisted
    assert TEMP_URL not in json.dumps(report, default=str)


def test_uncertain_fetch_is_reconciled_not_resubmitted(tmp_path):
    bunny = FakeBunny(fetch_error=uncertain(), create_on_error=True)
    pilot, _ = make(tmp_path, bunny=bunny)
    report = pilot.run()
    assert report["outcome"] == COMPLETED, report
    assert len(bunny.fetch_calls) == 1 and len(bunny.videos) == 1


def test_uncertain_fetch_that_never_appears_waits_and_rerun_never_refetches(tmp_path):
    bunny = FakeBunny(fetch_error=uncertain(), create_on_error=False)
    pilot, _ = make(tmp_path, bunny=bunny)
    first = pilot.run()
    assert first["outcome"] == WAITING
    assert first["video"]["import_status"] == "SUBMISSION_UNCERTAIN"
    second = make(tmp_path, bunny=bunny)[0].run()
    assert second["outcome"] == WAITING
    assert len(bunny.fetch_calls) == 1


def test_crash_after_submitting_is_persisted_before_the_fetch(tmp_path):
    class Crash(Exception):
        pass

    bunny = FakeBunny()
    pilot, _ = make(tmp_path, bunny=bunny)

    def crash(*a, **k):
        bunny.fetch_calls.append("crashed")
        bunny._new_video(pilot.manifest.videos()[0]["video_title"])
        raise Crash()
    bunny.fetch_video = crash
    with pytest.raises(Crash):
        pilot.run()
    record = PilotManifest(tmp_path / "out" / "manifest.json").videos()[0]
    assert record["import_status"] == "SUBMITTING"

    del bunny.fetch_video                      # restore the class method
    report = make(tmp_path, bunny=bunny)[0].run()
    assert report["outcome"] == COMPLETED
    assert bunny.fetch_calls == ["crashed"] and len(bunny.videos) == 1


def test_media_length_mismatch_stops_before_captions(tmp_path):
    bunny = FakeBunny(length=7900)
    pilot, _ = make(tmp_path, bunny=bunny)
    report = pilot.run()
    assert report["outcome"] == REVIEW_REQUIRED
    assert "length_within_tolerance" in report["failed_checks"]
    assert bunny.caption_calls == []


def test_file_name_origin_alignment_is_refused(tmp_path):
    # A file 71.8 s longer than the transcript window means media zero is NOT the
    # transcript start: no offset is assumed, the run stops before importing.
    pilot, bunny = make(tmp_path, graph=FakeGraph(duration_ms=8_013_367))
    report = pilot.run()
    assert report["outcome"] == BLOCKED
    assert statuses(report)["transcript_aligned"] == "FAIL"
    assert bunny.fetch_calls == []


def test_existing_foreign_english_caption_is_never_overwritten(tmp_path):
    bunny = FakeBunny()
    pilot, _ = make(tmp_path, bunny=bunny)
    title = None
    # Pre-existing video with this exact title and a caption we did not publish.
    from app.bunny.identity import build_title, recording_identity
    from tests.unit.bunny_fakes import DRIVE_ID, ITEM_ID
    title = build_title("AI in Project Control 2026", "2026-10-16", LECTURE_ID,
                        recording_identity(DRIVE_ID, ITEM_ID))
    guid = bunny._new_video(title)
    bunny.videos[guid]["captions"] = [{"srclang": "en", "label": "English", "version": 1}]
    report = pilot.run()
    assert report["outcome"] == BLOCKED
    assert statuses(report)["no_existing_caption_overwritten"] == "FAIL"
    assert bunny.caption_calls == [] and bunny.fetch_calls == []


def test_ambiguous_identity_token_blocks(tmp_path):
    bunny = FakeBunny()
    from app.bunny.identity import recording_identity
    from tests.unit.bunny_fakes import DRIVE_ID, ITEM_ID
    token = recording_identity(DRIVE_ID, ITEM_ID)
    bunny._new_video(f"Something | {token}")
    bunny._new_video(f"Other | {token}")
    pilot, _ = make(tmp_path, bunny=bunny)
    assert pilot.run()["outcome"] == BLOCKED
    assert bunny.fetch_calls == []


def test_only_one_pilot_video_per_library(tmp_path):
    pilot, bunny = make(tmp_path)
    assert pilot.run()["outcome"] == COMPLETED
    other = lecture_source(recording_drive_id="b!other", recording_item_id="01OTHER",
                           legacy_drive_id="b!other", legacy_item_id="01OTHER")
    report = make(tmp_path, bunny=bunny, source=other)[0].run()
    assert report["outcome"] == BLOCKED
    assert statuses(report)["no_duplicate_import"] == "FAIL"
    assert len(bunny.fetch_calls) == 1


def test_rejected_caption_is_recorded_and_not_made_current(tmp_path):
    bunny = FakeBunny(caption_result={"http_status": 400, "success": False, "valid": False,
                                      "errorList": ["bad cue"], "warningList": []})
    pilot, _ = make(tmp_path, bunny=bunny)
    report = pilot.run()
    assert report["outcome"] == REVIEW_REQUIRED
    assert report["caption"]["publication_status"] == "REJECTED"
    assert pilot.manifest.data["current_captions"] == {}


def test_transient_graph_read_failures_are_retried(tmp_path):
    from app.graph.transport import GraphError
    graph = FakeGraph()
    real, failures = graph.get_json, [GraphError("Microsoft Graph", None, "gaierror", "dns")] * 2
    graph.get_json = lambda path: (_ for _ in ()).throw(failures.pop()) if failures else real(path)
    pilot, bunny = make(tmp_path, graph=graph)
    report = pilot.run()
    assert report["outcome"] == COMPLETED, report.get("blocking")
    assert len(bunny.fetch_calls) == 1


def test_permanent_graph_failure_is_not_retried(tmp_path):
    from app.graph.transport import GraphError
    graph = FakeGraph()
    calls = []
    def denied(path):
        calls.append(path)
        raise GraphError("Microsoft Graph", 403, "accessDenied", "no")
    graph.get_json = denied
    pilot, bunny = make(tmp_path, graph=graph)
    assert pilot.run()["outcome"] == BLOCKED
    assert len(calls) == 1 and bunny.fetch_calls == []


def test_cdn_readback_unavailable_still_verifies_registration(tmp_path):
    bunny = FakeBunny()
    pilot, _ = make(tmp_path, bunny=bunny, cdn=FakeCdn(bunny, status=403))
    report = pilot.run()
    assert report["outcome"] == COMPLETED
    assert report["caption"]["readback"]["content_check"] == "CDN_UNAVAILABLE"
    assert report["caption"]["readback"]["registered"] is True
