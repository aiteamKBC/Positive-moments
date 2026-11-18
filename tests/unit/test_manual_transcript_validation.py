"""
Manual transcript upload - the parts that need no database.

  * the file validator (app/transcripts/manual_upload.inspect_vtt): what a Teams .vtt looks like,
    what is refused and why, and that the refusals never depend on the caller;
  * the lecture scope that keeps a manual recovery to ONE lecture: the runner refuses the day-wide
    Graph transcript sweep and hands the day-scoped services the lecture filter, and those services
    narrow to it.

Every transcript below is synthetic.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.orchestration.runner import StageRunner
from app.orchestration.stages import ACQUIRE_TRANSCRIPT, BUILD_CANONICAL_CUES, RESOLVE_SPEAKERS, SELECT_TRANSCRIPT
from app.transcripts import manual_upload as mu
from app.transcripts.document_service import CanonicalTranscriptService
from app.transcripts.models import ReadyLecture
from app.transcripts.selection_service import TranscriptSelectionService
from app.transcripts.speaker_service import SpeakerInventoryService


def _stamp(ms):
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def teams_vtt(cues=None, *, newline="\n", bom=False, speakers=True) -> bytes:
    """The shape Teams' 'Download as .vtt' produces: header, cue ids, <v Name> voice tags."""
    cues = cues if cues is not None else [(i * 600_000, i * 600_000 + 540_000, f"Line {i}.")
                                          for i in range(12)]
    lines = ["WEBVTT", ""]
    for index, (start, end, text) in enumerate(cues):
        lines += [f"5d0c6a1e-{index:04d}/{index}-0", f"{_stamp(start)} --> {_stamp(end)}",
                  f"<v Synthetic Trainer>{text}</v>" if speakers else text, ""]
    body = newline.join(lines)
    return ("﻿" if bom else "").encode("utf-8") + body.encode("utf-8")


def codes(items):
    return [item["code"] for item in items]


# --- the file validator ---------------------------------------------------------------------------

def test_a_teams_vtt_is_accepted_with_its_facts():
    raw = teams_vtt()
    vtt, errors, warnings = mu.inspect_vtt("Ray MSP.vtt", raw)
    assert errors == [] and warnings == []
    facts = vtt.facts()
    assert facts["cue_count"] == 12 and facts["spoken_cue_count"] == 12
    assert facts["last_cue_end_ms"] == 11 * 600_000 + 540_000
    assert facts["speaker_label_count"] == 1 and facts["speaker_attribution"] is True
    assert facts["content_bytes"] == len(raw) and len(facts["content_sha256"]) == 64
    # Facts only: no transcript text, no speaker name.
    assert "Line 0." not in str(facts) and "Synthetic Trainer" not in str(facts)


def test_a_windows_download_with_bom_and_crlf_is_accepted():
    vtt, errors, _ = mu.inspect_vtt("t.VTT", teams_vtt(newline="\r\n", bom=True))
    assert errors == [] and vtt.cue_count == 12


@pytest.mark.parametrize("filename, raw, code", [
    ("transcript.docx", teams_vtt(), mu.NOT_A_VTT_FILE),
    ("t.vtt", b"", mu.EMPTY_FILE),
    ("t.vtt", b"WEBVTT\n\n" + b"\x00\x01" * 10, mu.NOT_A_TEXT_FILE),
    ("t.vtt", "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\ncafé".encode("latin-1"), mu.NOT_UTF8),
    ("t.vtt", b"00:00:01.000 --> 00:00:02.000\nhello\n", mu.INVALID_WEBVTT_FILE),
    ("t.vtt", b"WEBVTT\n\n", mu.NO_CUES),
    ("t.vtt", b"WEBVTT\n\nNOTE only a note\n", mu.NO_CUES),
], ids=["extension", "empty", "binary", "not-utf8", "no-header", "header-only", "note-only"])
def test_unsupported_files_are_refused_with_a_reason(filename, raw, code):
    _vtt, errors, _ = mu.inspect_vtt(filename, raw)
    assert code in codes(errors)


def test_a_file_over_the_limit_is_refused_without_being_parsed(monkeypatch):
    monkeypatch.setattr(mu, "MAX_UPLOAD_BYTES", 100)
    vtt, errors, _ = mu.inspect_vtt("t.vtt", teams_vtt())
    assert vtt is None and codes(errors) == [mu.FILE_TOO_LARGE]


def test_cues_without_any_spoken_text_are_refused():
    raw = b"WEBVTT\n\n00:00:01.000 --> 00:10:00.000\n<v A></v>\n\n00:10:01.000 --> 00:20:00.000\n \n"
    _vtt, errors, _ = mu.inspect_vtt("t.vtt", raw)
    assert mu.NO_SPOKEN_TEXT in codes(errors)


def test_a_transcript_too_short_for_the_pipeline_is_refused():
    """The real Phase 2B combiner refuses a sub-30-second transcript; so does the upload."""
    _vtt, errors, _ = mu.inspect_vtt("t.vtt", teams_vtt([(0, 10_000, "Hi.")]))
    assert mu.NOT_COMBINABLE in codes(errors)


def test_a_damaged_file_is_refused_and_a_few_bad_blocks_are_only_reported():
    good = teams_vtt().decode()
    damaged = good + "\n\n" + "\n\n".join("garbage block without timing" for _ in range(5))
    _vtt, errors, _ = mu.inspect_vtt("t.vtt", damaged.encode())
    assert mu.MALFORMED_CUES in codes(errors)
    _vtt, errors, warnings = mu.inspect_vtt("t.vtt", (good + "\n\nstray line\n").encode())
    assert errors == [] and mu.SOME_MALFORMED_BLOCKS in codes(warnings)


def test_no_speaker_labels_is_a_warning_not_a_refusal():
    vtt, errors, warnings = mu.inspect_vtt("t.vtt", teams_vtt(speakers=False))
    assert errors == [] and mu.NO_SPEAKER_LABELS in codes(warnings)
    assert vtt.speaker_attribution is False


def test_the_filename_is_reduced_to_a_base_name():
    assert mu.clean_filename("..\\..\\etc/passwd.vtt") == "passwd.vtt"
    assert mu.clean_filename("") == "transcript.vtt"
    assert len(mu.clean_filename("a" * 500 + ".vtt")) == 200


def test_every_code_has_a_message_an_operator_can_act_on():
    for name in dir(mu):
        value = getattr(mu, name)
        if name.isupper() and isinstance(value, str) and value == name and name not in (
                "MANUAL_UPLOAD", "SELECTED", "SELECTION_VERSION", "VTT", "VALIDATOR_VERSION",
                "ALLOWED_EXTENSION", "EMPTY_TRANSCRIPT", "INVALID_WEBVTT", "UNSUPPORTED_STRUCTURE"):
            assert value in mu.MESSAGES, name


def test_the_console_wiring_never_allows_the_provider_and_the_cli_does(monkeypatch):
    from app.orchestration import factory
    built = []
    monkeypatch.setattr(factory, "build_orchestrator",
                        lambda settings, **kwargs: built.append(kwargs) or object())
    monkeypatch.setattr(factory, "build_resolver", lambda **kwargs: object())
    console = factory.build_manual_transcript_service(SimpleNamespace(recording_link_mode="observe"))
    assert console.allow_provider is False
    console.orchestrator_factory("L1")
    assert built[-1]["allow_provider"] is False and built[-1]["lecture_scope"] == ["L1"]
    cli = factory.build_manual_transcript_service(SimpleNamespace(recording_link_mode="observe"),
                                                  allow_provider=True)
    cli.orchestrator_factory("L1")
    assert cli.allow_provider is True and built[-1]["allow_provider"] is True


def test_the_web_view_builds_the_console_wiring(monkeypatch):
    """The HTTP Process endpoint must never be able to buy a generation."""
    import pathlib
    source = pathlib.Path("backend/operations/transcript_upload_views.py").read_text(encoding="utf-8")
    assert "build_manual_transcript_service(settings(), allow_provider=False)" in source
    assert "allow_provider=True" not in source


def test_the_server_command_is_registered():
    from app.cli.main import build_parser
    args = build_parser().parse_args(["process-manual-transcript", "--lecture-id", "L1", "--plan"])
    assert (args.command, args.lecture_id, args.plan, args.no_provider) == (
        "process-manual-transcript", "L1", True, False)


# --- lecture scope: one lecture, never its day ------------------------------------------------------

def test_a_lecture_scoped_runner_refuses_the_day_wide_graph_sweep():
    scoped = StageRunner(settings=None, lecture_scope=["L1"])
    assert scoped.can_run(ACQUIRE_TRANSCRIPT) is False
    assert scoped.refusal_reason(ACQUIRE_TRANSCRIPT) == "DAY_WIDE_TRANSCRIPT_SWEEP_NOT_IN_LECTURE_SCOPE"
    for action in (SELECT_TRANSCRIPT, BUILD_CANONICAL_CUES, RESOLVE_SPEAKERS):
        assert scoped.can_run(action) is True
    # Every existing caller is unscoped and unchanged.
    assert StageRunner(settings=None).can_run(ACQUIRE_TRANSCRIPT) is True


@pytest.mark.parametrize("action, service, method", [
    (SELECT_TRANSCRIPT, TranscriptSelectionService, "select_day"),
    (BUILD_CANONICAL_CUES, CanonicalTranscriptService, "parse_day"),
    (RESOLVE_SPEAKERS, SpeakerInventoryService, "build_day"),
])
def test_the_day_scoped_services_are_handed_the_lecture_filter(monkeypatch, action, service, method):
    seen = {}

    def fake(self, connection, session_date, *, persist=True, lecture_ids=None):
        seen["lecture_ids"] = lecture_ids
        return {}

    monkeypatch.setattr(service, method, fake)
    StageRunner(settings=None, lecture_scope=["L1"]).execute(None, action, session_date=date(2031, 3, 4))
    assert seen["lecture_ids"] == ["L1"]
    StageRunner(settings=None).execute(None, action, session_date=date(2031, 3, 4))
    assert seen["lecture_ids"] is None


START = datetime(2031, 3, 4, 9, tzinfo=timezone.utc)


def _ready(n):
    return ReadyLecture(lecture_id=f"L{n}", meeting_id=f"M{n}", meeting_lookup_user_id="u",
                        scheduled_start=START, scheduled_end=START + timedelta(hours=2),
                        session_date=START.date(), subject=f"S{n}", module=None)


def test_selection_narrowed_to_a_lecture_selects_that_lecture_only():
    loaded = []
    lectures = SimpleNamespace(load_day=lambda c, d: {"considered": 3, "ready": [_ready(1), _ready(2)],
                                                       "skipped": [{"lecture_id": "L3"}]})
    selections = SimpleNamespace(load_candidates=lambda c, lid: loaded.append(lid) or [])
    service = TranscriptSelectionService(lecture_repository=lectures, selection_repository=selections,
                                         run_repository=None)
    summary = service.select_day(None, START.date(), persist=False, lecture_ids=["L2"])
    assert loaded == ["L2"]
    assert [item["lecture_id"] for item in summary["lectures"]] == ["L2"]
    assert summary["lectures_considered"] == 1 and summary["metadata"]["lecture_scope"] == ["L2"]
    service.select_day(None, START.date(), persist=False)
    assert loaded == ["L2", "L1", "L2"]


def test_canonical_parsing_narrowed_to_a_lecture_parses_that_lecture_only():
    vtt = teams_vtt().decode()
    sources = [{"lecture_id": f"L{n}", "subject": "s", "selection_id": f"S{n}", "combined_id": f"C{n}",
                "content_sha256": "a" * 64, "source_fingerprint": "b" * 64, "duration_seconds": None,
                "combined_content": vtt, "selection_version": "v", "primary_provider_transcript_id": "t"}
               for n in (1, 2)]
    repository = SimpleNamespace(load_combined_for_date=lambda c, d: sources)
    service = CanonicalTranscriptService(document_repository=repository, run_repository=None)
    summary = service.parse_day(None, START.date(), persist=False, lecture_ids=["L1"])
    assert [item["lecture_id"] for item in summary["documents"]] == ["L1"]
    assert len(service.parse_day(None, START.date(), persist=False)["documents"]) == 2


def test_the_speaker_inventory_narrowed_to_a_lecture_builds_that_lecture_only():
    documents = [{"document_id": f"D{n}", "lecture_id": f"L{n}", "subject": "s", "cue_count": 1,
                  "duration_ms": 1, "parser_version": "p"} for n in (1, 2)]
    asked = {}
    repository = SimpleNamespace(
        load_documents=lambda c, d, p: documents,
        aggregate_speakers=lambda c, ids: asked.setdefault("ids", list(ids)) and {},
        count_unassigned_cues=lambda c, ids: {})
    service = SpeakerInventoryService(speaker_repository=repository, run_repository=None)
    summary = service.build_day(None, START.date(), persist=False, lecture_ids=["L2"])
    assert asked["ids"] == ["D2"] and [item["lecture_id"] for item in summary["documents"]] == ["L2"]
