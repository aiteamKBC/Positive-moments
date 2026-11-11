"""
Caption read-back: the caption Bunny serves versus the one the publisher uploaded.

Bunny re-serialises a stored caption - observed on library 786045 on 2026-10-23: a UTF-8 BOM,
every cue numbered 1..n, one more trailing newline - so the check is a strict cue-by-cue
comparison, not a byte-for-byte hash. Only those formatting differences are tolerated.
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from app.bunny import captions as cap
from app.publisher.bunny_jobs import CaptionWorker
from tests.unit.bunny_fakes import teams_vtt

UPLOADED = ("WEBVTT\n\n"
            "00:00:01.000 --> 00:00:04.500\nFirst cue.\n\n"
            "00:00:04.500 --> 00:00:09.250 align:start\nSecond cue, line one\nand line two.\n\n"
            "00:00:09.250 --> 00:00:12.000\nThird & last cue.\n")


def bunny_serves(text: str) -> str:
    """What Bunny was observed to serve back for `text`: BOM, numbered cues, an extra trailing newline."""
    header, body = text.rstrip("\n").split("\n\n", 1)
    numbered = [f"{n}\n{block}" for n, block in enumerate(body.split("\n\n"), 1)]
    return "﻿" + header + "\n\n" + "\n\n".join(numbered) + "\n\n"


def verdict(served: str, expected: str = UPLOADED) -> dict:
    return cap.compare_published_caption(expected, served)


# -- tolerated formatting -------------------------------------------------------------------------
def test_byte_identical_is_identical():
    assert verdict(UPLOADED) == {"result": cap.IDENTICAL}


def test_bom_alone_is_equivalent():
    result = verdict("﻿" + UPLOADED)
    assert result["result"] == cap.EQUIVALENT and result["served_bom"] is True


def test_sequential_cue_numbers_are_equivalent():
    header, body = UPLOADED.rstrip("\n").split("\n\n", 1)
    numbered = header + "\n\n" + "\n\n".join(f"{n}\n{b}" for n, b in enumerate(body.split("\n\n"), 1)) + "\n"
    result = verdict(numbered)
    assert result["result"] == cap.EQUIVALENT and result["served_cue_identifiers"] == 3


@pytest.mark.parametrize("ending", ["", "\n\n", "\n\n\n"])
def test_trailing_newlines_are_equivalent(ending):
    assert verdict(UPLOADED.rstrip("\n") + ending)["result"] == cap.EQUIVALENT


def test_everything_bunny_does_at_once_is_equivalent_and_carries_no_text():
    result = verdict(bunny_serves(UPLOADED))
    assert result == {"result": cap.EQUIVALENT, "uploaded_cues": 3, "served_cues": 3, "served_bom": True,
                      "served_cue_identifiers": 3, "served_trailing_newlines": 2}
    assert all(not isinstance(v, str) for k, v in result.items() if k != "result")      # no caption text


def test_a_real_publication_copy_round_trips_through_bunny_serialisation():
    build = cap.build_caption(teams_vtt(40))
    assert verdict(bunny_serves(build.text), build.text)["result"] == cap.EQUIVALENT


# -- real differences ---------------------------------------------------------------------------
def test_changed_cue_text_is_a_mismatch():
    result = verdict(bunny_serves(UPLOADED.replace("Third & last cue.", "Third & last cue!")))
    assert result["result"] == cap.MISMATCH and result["reason"] == "CUE_TEXT" and result["first_difference_cue"] == 3


def test_changed_line_of_a_multi_line_payload_is_a_mismatch():
    result = verdict(bunny_serves(UPLOADED.replace("and line two.", "and line 2.")))
    assert (result["reason"], result["first_difference_cue"]) == ("CUE_TEXT", 2)


def test_joined_multi_line_payload_is_a_mismatch():
    result = verdict(bunny_serves(UPLOADED.replace("line one\nand", "line one and")))
    assert (result["reason"], result["first_difference_cue"]) == ("CUE_TEXT", 2)


def test_whitespace_inside_text_is_not_normalised():
    assert verdict(bunny_serves(UPLOADED.replace("First cue.", "First  cue.")))["reason"] == "CUE_TEXT"


def test_shifted_timestamp_is_a_mismatch():
    result = verdict(bunny_serves(UPLOADED.replace("00:00:09.250 --> 00:00:12.000", "00:00:09.250 --> 00:00:12.001")))
    assert (result["reason"], result["first_difference_cue"]) == ("CUE_TIMING", 3)


def test_changed_cue_settings_are_a_mismatch():
    assert verdict(bunny_serves(UPLOADED.replace(" align:start", "")))["reason"] == "CUE_SETTINGS"


def test_missing_last_cue_is_a_mismatch():
    result = verdict(bunny_serves(UPLOADED.rsplit("\n\n", 1)[0] + "\n"))
    assert (result["result"], result["reason"], result["first_difference_cue"]) == (cap.MISMATCH, "CUE_COUNT", 3)


def test_missing_middle_cue_is_a_mismatch():
    header, body = UPLOADED.rstrip("\n").split("\n\n", 1)
    blocks = body.split("\n\n")
    result = verdict(bunny_serves(header + "\n\n" + "\n\n".join([blocks[0], blocks[2]]) + "\n"))
    assert (result["result"], result["first_difference_cue"]) == (cap.MISMATCH, 2)


def test_duplicated_cue_is_a_mismatch():
    header, body = UPLOADED.rstrip("\n").split("\n\n", 1)
    blocks = body.split("\n\n")
    result = verdict(bunny_serves(header + "\n\n" + "\n\n".join([blocks[0], blocks[0], blocks[1], blocks[2]]) + "\n"))
    assert (result["result"], result["first_difference_cue"]) == (cap.MISMATCH, 2)


def test_reordered_cues_are_a_mismatch():
    header, body = UPLOADED.rstrip("\n").split("\n\n", 1)
    blocks = body.split("\n\n")
    result = verdict(bunny_serves(header + "\n\n" + "\n\n".join([blocks[1], blocks[0], blocks[2]]) + "\n"))
    assert (result["result"], result["reason"], result["first_difference_cue"]) == (cap.MISMATCH, "CUE_TIMING", 1)


# -- anything else Bunny might do is not silently accepted -----------------------------------------
@pytest.mark.parametrize("served, code", [
    ("", "SERVED_UNPARSEABLE:HEADER"),
    ("<html>error</html>", "SERVED_UNPARSEABLE:HEADER"),
    (UPLOADED.replace("WEBVTT", "WEBVTT - Kind: captions", 1), "SERVED_UNPARSEABLE:HEADER"),
    (UPLOADED.replace("\n", "\r\n"), "SERVED_UNPARSEABLE:CARRIAGE_RETURN"),
    ("﻿﻿" + UPLOADED, "SERVED_UNPARSEABLE:HEADER"),
    (UPLOADED.replace("00:00:01.000 -->", "00:01.000 -->"), "SERVED_UNPARSEABLE:CUE_IDENTIFIER_OR_TIMING"),
    (UPLOADED.replace("\n\n00:00:04.500", "\n\n\n00:00:04.500"), "SERVED_UNPARSEABLE:CUE_IDENTIFIER_OR_TIMING"),
    (UPLOADED.replace("WEBVTT\n\n", "WEBVTT\n\nNOTE generated\n\n"), "SERVED_UNPARSEABLE:CUE_IDENTIFIER_OR_TIMING"),
    (UPLOADED.replace("\n\n00:00:04.500", "\n\n7\n00:00:04.500"), "SERVED_UNPARSEABLE:CUE_IDENTIFIER_OR_TIMING"),
    (UPLOADED.replace("WEBVTT\n\n", "WEBVTT\n\nintro\n", 1), "SERVED_UNPARSEABLE:CUE_IDENTIFIER_OR_TIMING"),
    (UPLOADED.replace("First cue.", "First cue.\n00:00:02.000 --> 00:00:03.000"), "SERVED_UNPARSEABLE:CUE_PAYLOAD"),
])
def test_malformed_or_unexpected_served_vtt_is_a_mismatch(served, code):
    assert verdict(served) == {"result": cap.MISMATCH, "reason": code}


def test_identifiers_must_count_from_one():
    header, body = UPLOADED.rstrip("\n").split("\n\n", 1)
    shifted = header + "\n\n" + "\n\n".join(f"{n}\n{b}" for n, b in enumerate(body.split("\n\n"), 0)) + "\n"
    assert verdict(shifted)["result"] == cap.MISMATCH


def test_unparseable_upload_is_never_equivalent():
    assert verdict(bunny_serves(UPLOADED), "not a caption")["reason"] == "UPLOADED_UNPARSEABLE:HEADER"


# -- the publisher's read-back uses it --------------------------------------------------------------
class _Cdn:
    def __init__(self, text):
        self.text, self.urls = text, []

    def __call__(self, request, timeout):
        self.urls.append(request.full_url)
        return _Response(self.text)


class _Response(io.BytesIO):
    def __init__(self, text):
        super().__init__(text.encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def worker(served):
    config = SimpleNamespace(caption_language="en", caption_label="English", mode="STAGING_WRITE",
                             staging_cdn_hostname="cdn.invalid")
    bunny = SimpleNamespace(get_video=lambda video_id: {"captions": [{"srclang": "en", "label": "English",
                                                                       "version": 1}]})
    return CaptionWorker(SimpleNamespace(config=config, bunny=bunny, open_url=_Cdn(served)))


def test_read_back_accepts_bunny_serialisation_as_equivalent():
    build = cap.build_caption(teams_vtt(12))
    readback = worker(bunny_serves(build.text))._read_back("video-1", build)
    assert readback["registered"] and readback["label_matches"]
    assert readback["content_check"] == cap.EQUIVALENT
    assert readback["content_comparison"]["served_cues"] == build.cue_count


def test_read_back_still_rejects_altered_text():
    build = cap.build_caption(teams_vtt(12))
    altered = bunny_serves(build.text).replace("number 3.", "number 4.", 1)
    readback = worker(altered)._read_back("video-1", build)
    assert readback["content_check"] == cap.MISMATCH
    assert readback["content_comparison"]["reason"] == "CUE_TEXT"


# -- operator recovery of a caption Bunny accepted but whose read-back failed ---------------------
class _Tx:
    def transaction(self):
        import contextlib
        return contextlib.nullcontext()


class _Captions:
    def __init__(self, publication):
        self.publication, self.transitions = publication, []

    def transition(self, connection, pid, *, expect, **changes):
        self.transitions.append((expect, changes))
        if self.publication["publication_status"] != expect:
            return None
        self.publication = {**self.publication, **{k: v for k, v in changes.items() if k in ("publication_status",)}}
        return self.publication


def recovery_worker(served, *, registered=True):
    captions = [{"srclang": "en", "label": "English", "version": 1}] if registered else []
    uploads, events = [], []
    bunny = SimpleNamespace(get_video=lambda video_id: {"captions": captions},
                            add_caption=lambda *a, **k: uploads.append(a) or {"http_status": 200})
    publication = {"publication_id": "11111111-1111-1111-1111-111111111111", "lecture_id": "lecture-1",
                   "publication_status": "REVIEW_REQUIRED", "last_error_code": "READBACK_FAILED",
                   "published_at": "2026-10-23T10:34:45Z"}
    runtime = SimpleNamespace(
        config=SimpleNamespace(caption_language="en", caption_label="English", mode="STAGING_WRITE",
                               staging_cdn_hostname="cdn.invalid"),
        bunny=bunny, open_url=_Cdn(served), captions=_Captions(publication),
        queue=SimpleNamespace(event=lambda c, name, **k: events.append(name)))
    w = CaptionWorker(runtime)
    made_current = []
    w._make_current = lambda connection, pub, part: made_current.append(pub) or "MADE_CURRENT"
    return w, runtime, uploads, events, made_current


def test_operator_recovery_verifies_an_accepted_caption_without_uploading_it_again():
    build = cap.build_caption(teams_vtt(12))
    w, rt, uploads, events, made_current = recovery_worker(bunny_serves(build.text))
    outcome = w._reverify(_Tx(), rt.captions.publication, "video-1", build, 1)
    assert outcome == "MADE_CURRENT" and uploads == []
    (expect, changes), = rt.captions.transitions
    assert expect == "REVIEW_REQUIRED" and changes["publication_status"] == "VERIFIED"
    assert changes["last_error_code"] is None and changes["bunny_caption_version"] == 1
    assert changes["readback"]["content_check"] == cap.EQUIVALENT
    assert events == ["CAPTION_REVERIFIED"]
    assert made_current[0]["publication_id"] == "11111111-1111-1111-1111-111111111111"


@pytest.mark.parametrize("case", ["altered_text", "caption_missing", "cdn_html"])
def test_operator_recovery_never_approves_a_caption_it_cannot_prove(case):
    build = cap.build_caption(teams_vtt(12))
    served = {"altered_text": bunny_serves(build.text).replace("number 3.", "number 4.", 1),
              "caption_missing": bunny_serves(build.text), "cdn_html": "<html>not a caption</html>"}[case]
    w, rt, uploads, events, made_current = recovery_worker(served, registered=case != "caption_missing")
    outcome = w._reverify(_Tx(), rt.captions.publication, "video-1", build, 1)
    assert (outcome.kind, outcome.reason) == ("REVIEW", "CAPTION_REVERIFY_FAILED")
    assert uploads == [] and made_current == []
    (expect, changes), = rt.captions.transitions
    assert expect == "REVIEW_REQUIRED" and "publication_status" not in changes
    assert rt.captions.publication["publication_status"] == "REVIEW_REQUIRED"
    assert events == ["CAPTION_REVERIFY_FAILED"]
