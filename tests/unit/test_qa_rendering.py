"""
Phase 3B unit tests: the legacy evidence renderer, the LMS snapshot rules and
the compatibility payload shapes.

Pure functions only - no database, no model, no provider. Every personal name
is an invented fixture value.
"""
import uuid
from datetime import date, datetime, timezone

import pytest

from app.qa.checklist import CHECKLIST_ITEMS, MET, NOT_MET, PARTIALLY_MET
from app.rendering.compatibility import (
    SEVERITY,
    counts_from_statuses,
    item7_evidence,
    ksbs_to_object,
    legacy_date,
    render_fingerprint,
    session_id_match,
    titled_items_to_object,
)
from app.rendering.evidence import (
    MAX_BLOCKS_PER_CLIP,
    MAX_BLOCK_GAP_MS,
    MAX_CHARS_PER_BLOCK,
    NO_EVIDENCE,
    NO_EXTRACTABLE_FOR_RANGE,
    RENDERER_VERSION,
    blocks_from_range,
    clip_text,
    format_clips,
    legacy_evidence_field,
    norm_speaker,
)
from app.rendering.lms import (
    LEGACY_ROW_CAP,
    LMS_SNAPSHOT_VERSION,
    compare_with_legacy,
    legacy_students_payload,
    lms_fingerprint,
    normalize_module,
)


def cue(index, start_ms, end_ms, speaker, text):
    return {"cue_id": uuid.UUID(int=index), "cue_index": index, "start_ms": start_ms,
            "end_ms": end_ms, "speaker_label_raw": speaker, "text": text}


def clip(start_ms, end_ms, index=1, clip_id=None):
    def stamp(ms):
        hours, rest = divmod(ms, 3600000)
        minutes, rest = divmod(rest, 60000)
        seconds, millis = divmod(rest, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
    return {"clip_id": clip_id or uuid.uuid4(), "clip_index": index,
            "start_text": stamp(start_ms), "end_text": stamp(end_ms),
            "start_ms": start_ms, "end_ms": end_ms}


# --- 4-8. cue selection, merging and ordering ---------------------------------

def test_only_cues_overlapping_the_range_are_selected():
    cues = [cue(1, 0, 1000, "A", "before"), cue(2, 2000, 3000, "A", "inside"),
            cue(3, 9000, 9500, "A", "after")]
    blocks = blocks_from_range(1500, 4000, cues)
    assert [block["cue_ids"] for block in blocks] == [[uuid.UUID(int=2)]]


def test_a_cue_touching_the_boundary_is_excluded():
    # Legacy used strict inequalities: cue.start < end AND cue.end > start.
    cues = [cue(1, 4000, 5000, "A", "starts at the end")]
    assert blocks_from_range(1000, 4000, cues) == []
    assert blocks_from_range(5000, 6000, cues) == []


def test_empty_text_cues_are_ignored():
    cues = [cue(1, 0, 1000, "A", "   "), cue(2, 1000, 2000, "A", "real")]
    blocks = blocks_from_range(0, 3000, cues)
    assert [block["cue_ids"] for block in blocks] == [[uuid.UUID(int=2)]]


def test_same_speaker_within_the_gap_merges():
    cues = [cue(1, 0, 1000, "Dana", "one"), cue(2, 2000, 3000, "Dana", "two")]
    blocks = blocks_from_range(0, 4000, cues)
    assert len(blocks) == 1
    assert blocks[0]["texts"] == ["one", "two"]
    assert blocks[0]["end_ms"] == 3000


def test_merge_boundary_is_inclusive_at_1500ms():
    assert MAX_BLOCK_GAP_MS == 1500
    exactly = [cue(1, 0, 1000, "Dana", "one"), cue(2, 2500, 3000, "Dana", "two")]
    assert len(blocks_from_range(0, 4000, exactly)) == 1
    over = [cue(1, 0, 1000, "Dana", "one"), cue(2, 2501, 3000, "Dana", "two")]
    assert len(blocks_from_range(0, 4000, over)) == 2


def test_a_different_speaker_always_splits_the_block():
    cues = [cue(1, 0, 1000, "Dana", "one"), cue(2, 1100, 2000, "Elias", "two")]
    assert len(blocks_from_range(0, 3000, cues)) == 2


def test_speaker_comparison_is_trim_and_lowercase_only():
    assert norm_speaker("  Dana Whitfield ") == "dana whitfield"
    cues = [cue(1, 0, 1000, "Dana Whitfield", "one"),
            cue(2, 1100, 2000, " dana whitfield ", "two")]
    assert len(blocks_from_range(0, 3000, cues)) == 1
    # Not fuzzy: a different name is a different speaker.
    other = [cue(1, 0, 1000, "Dana Whitfield", "one"),
             cue(2, 1100, 2000, "Dana Whitfeld", "two")]
    assert len(blocks_from_range(0, 3000, other)) == 2


def test_blocks_are_ordered_chronologically():
    cues = [cue(3, 4000, 5000, "C", "third"), cue(1, 0, 500, "A", "first"),
            cue(2, 2000, 2500, "B", "second")]
    blocks = blocks_from_range(0, 9000, cues)
    assert [block["texts"][0] for block in blocks] == ["first", "second", "third"]


def test_an_inverted_or_missing_range_yields_no_blocks():
    cues = [cue(1, 0, 1000, "A", "text")]
    assert blocks_from_range(2000, 2000, cues) == []
    assert blocks_from_range(3000, 1000, cues) == []
    assert blocks_from_range(None, 1000, cues) == []


# --- 9-13. formatting limits and shape ----------------------------------------

def test_at_most_eight_blocks_are_rendered_per_clip():
    assert MAX_BLOCKS_PER_CLIP == 8
    cues = [cue(index, index * 10_000, index * 10_000 + 1000, f"S{index}", f"line {index}")
            for index in range(1, 13)]
    rendered = format_clips([clip(0, 200_000)], cues)
    assert rendered["block_count"] == 8
    assert len(rendered["text"].split("\n")) == 8
    assert "[1.8]" in rendered["text"] and "[1.9]" not in rendered["text"]


def test_quotes_are_truncated_at_260_characters_with_an_ellipsis():
    assert MAX_CHARS_PER_BLOCK == 260
    long_text = "x" * 400
    assert clip_text(long_text).endswith("...")
    assert len(clip_text(long_text)) == 263
    assert clip_text("short") == "short"


def test_whitespace_in_a_quote_is_collapsed():
    assert clip_text("  a\n\n b   c ") == "a b c"


def test_a_block_with_a_speaker_uses_the_parenthesised_form():
    cues = [cue(1, 1000, 3000, "Dana Whitfield", "hello there")]
    text = format_clips([clip(0, 5000)], cues)["text"]
    assert text == "[1.1] 00:00:01.000 --> 00:00:03.000 (Dana Whitfield): hello there"


def test_a_block_without_a_speaker_omits_the_parentheses():
    cues = [cue(1, 1000, 3000, None, "hello there")]
    text = format_clips([clip(0, 5000)], cues)["text"]
    assert text == "[1.1] 00:00:01.000 --> 00:00:03.000: hello there"


def test_clip_and_block_indexes_are_one_based_and_per_clip():
    cues = [cue(1, 0, 1000, "A", "one"), cue(2, 5000, 6000, "B", "two")]
    text = format_clips([clip(0, 2000, index=1), clip(4000, 7000, index=2)], cues)["text"]
    assert text.split("\n")[0].startswith("[1.1]")
    assert text.split("\n")[1].startswith("[2.1]")


# --- 14-15. fallbacks ----------------------------------------------------------

def test_a_range_with_no_usable_cue_reports_no_extractable_quote():
    cues = [cue(1, 50_000, 51_000, "A", "elsewhere")]
    rendered = format_clips([clip(1000, 4000)], cues)
    assert rendered["text"] == f"[1] 00:00:01.000 --> 00:00:04.000: {NO_EXTRACTABLE_FOR_RANGE}"
    assert rendered["block_count"] == 0


def test_no_clips_at_all_reports_no_evidence_found():
    assert format_clips([], [cue(1, 0, 1000, "A", "x")])["text"] == NO_EVIDENCE


def test_a_clip_missing_an_endpoint_is_skipped_like_legacy():
    cues = [cue(1, 0, 1000, "A", "x")]
    broken = clip(0, 1000)
    broken["end_text"] = ""
    assert format_clips([broken], cues)["text"] == NO_EVIDENCE


# --- 16-17. the single legacy evidence field ----------------------------------

def test_evidence_and_reasoning_are_joined_with_a_newline():
    assert legacy_evidence_field("[1.1] a", "because") == "[1.1] a\nbecause"


def test_evidence_without_reasoning_is_unchanged():
    assert legacy_evidence_field("[1.1] a", "") == "[1.1] a"
    assert legacy_evidence_field("[1.1] a", None) == "[1.1] a"


def test_no_evidence_found_is_replaced_by_the_reasoning_alone():
    assert legacy_evidence_field(NO_EVIDENCE, "durationMinutes = 144.") == "durationMinutes = 144."
    assert legacy_evidence_field(NO_EVIDENCE, None) == ""


# --- 18. deterministic Item 7 evidence -----------------------------------------

def test_item7_evidence_matches_the_legacy_sentence_shape():
    text = item7_evidence(spoke_count=5, attended_count=14, engagement_percentage="35.71",
                          spoke_labels=["Avery Holt", "Blake Quinlan"],
                          silent_names=["Casey Ormond"])
    lines = text.split("\n")
    assert lines[0] == "Engagement score = 5 / 14 = 35.71%."
    assert lines[1] == "Students who spoke (2): Avery Holt, Blake Quinlan."
    assert lines[2] == "Students who attended but did not speak (1): Casey Ormond."


def test_item7_evidence_prints_a_whole_percentage_without_trailing_zeros():
    text = item7_evidence(spoke_count=1, attended_count=1, engagement_percentage="100.00",
                          spoke_labels=["Avery Holt"], silent_names=[])
    assert text.startswith("Engagement score = 1 / 1 = 100%.")
    assert "(0): (none)." in text


# --- 20-23. sections ------------------------------------------------------------

def test_strengths_and_areas_are_numbered_from_one():
    calls = []

    def render(position):
        calls.append(position)
        return {"text": f"rendered {position}", "cue_ids": [uuid.UUID(int=position)]}

    rendered = titled_items_to_object([{"title": "a"}, {"title": "b"}], "strength", render)
    assert list(rendered) == ["strength_1", "strength_2"]
    assert calls == [1, 2]
    assert rendered["strength_2"]["evidence"] == "rendered 2"
    assert rendered["strength_1"]["title"] == "a"


def test_ksbs_keep_type_and_title_untouched():
    rendered = ksbs_to_object(
        [{"type": "Skill", "title": "Planning"}],
        lambda position: {"text": "rendered", "cue_ids": []})
    assert rendered["ksb_1"]["type"] == "Skill"
    assert rendered["ksb_1"]["title"] == "Planning"


def test_empty_sections_render_as_an_empty_object():
    assert titled_items_to_object(None, "area", lambda position: None) == {}
    assert ksbs_to_object([], lambda position: None) == {}


# --- 24-29. LMS query semantics --------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Project Planning &amp; Control", "project planning & control"),
    ("  Ray-MSP   Jan  2026 ", "ray-msp jan 2026"),
    ("UPPER case", "upper case"),
    (None, ""),
])
def test_module_normalization_matches_the_legacy_sql(raw, expected):
    assert normalize_module(raw) == expected


def test_the_legacy_row_cap_is_five_hundred():
    assert LEGACY_ROW_CAP == 500


def test_lms_query_contract_is_ported_verbatim():
    from app.db.repositories.qa_rendering import LOAD_LMS_STUDENTS
    sql = " ".join(LOAD_LMS_STUDENTS.split())
    assert "public.kbc_users_data" in sql
    assert "'&amp;', '&'" in sql
    assert "lower(coalesce(k.\"Program-Status\", '')) = 'active'" in sql
    assert 'ORDER BY k."FullName"' in sql
    assert "LIMIT 500" in sql
    assert 'btrim(f.full_name) <> \'\'' in sql
    assert "f.id IS NOT NULL" in sql
    for verb in ("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE", "DROP", "TRIGGER"):
        assert verb not in sql.upper()


def test_students_payload_is_ordered_by_full_name():
    members = [{"external_learner_id": 2, "full_name": "Zoe Abernethy"},
               {"external_learner_id": 1, "full_name": "Avery Holt"}]
    payload = legacy_students_payload(members)
    assert payload == {"students": [{"ID": 1, "FullName": "Avery Holt"},
                                    {"ID": 2, "FullName": "Zoe Abernethy"}]}


# --- 30-32. LMS snapshot provenance -----------------------------------------------

def test_lms_fingerprint_is_order_independent_and_deterministic():
    members = [{"external_learner_id": 2, "full_name": "Zoe Abernethy"},
               {"external_learner_id": 1, "full_name": "Avery Holt"}]
    forward = lms_fingerprint(module_normalized="m", members=members)
    backward = lms_fingerprint(module_normalized="m", members=list(reversed(members)))
    assert forward == backward and len(forward) == 64


@pytest.mark.parametrize("change", ["add", "remove", "rename", "module", "version"])
def test_lms_fingerprint_changes_when_the_effective_roster_changes(change):
    members = [{"external_learner_id": 1, "full_name": "Avery Holt"},
               {"external_learner_id": 2, "full_name": "Zoe Abernethy"}]
    base = lms_fingerprint(module_normalized="m", members=members)
    if change == "add":
        other = lms_fingerprint(module_normalized="m", members=members + [
            {"external_learner_id": 3, "full_name": "Kit Lowell"}])
    elif change == "remove":
        other = lms_fingerprint(module_normalized="m", members=members[:1])
    elif change == "rename":
        other = lms_fingerprint(module_normalized="m", members=[
            {"external_learner_id": 1, "full_name": "Avery Holt-Brown"}, members[1]])
    elif change == "module":
        other = lms_fingerprint(module_normalized="other", members=members)
    else:
        other = lms_fingerprint(module_normalized="m", members=members,
                                snapshot_version="other_version")
    assert other != base


def test_lms_snapshot_version_is_stable():
    assert LMS_SNAPSHOT_VERSION == "legacy_qa_v8_active_lms_roster_v1"


def test_lms_drift_is_classified_not_forced():
    members = [{"external_learner_id": 1, "full_name": "Avery Holt"}]
    same = compare_with_legacy(members, {"students": [{"ID": 1, "FullName": "Avery Holt"}]}, 1)
    assert same["status"] == "LMS_ROSTER_MATCH"
    drift = compare_with_legacy(members, {"students": [
        {"ID": 1, "FullName": "Avery Holt"}, {"ID": 9, "FullName": "Gone Learner"}]}, 2)
    assert drift["status"] == "LMS_ROSTER_DRIFT"
    assert drift["only_legacy_id_count"] == 1
    missing = compare_with_legacy(members, None, None)
    assert missing["status"] == "LEGACY_LMS_DATA_MISSING"


def test_lms_drift_report_carries_ids_not_names():
    drift = compare_with_legacy([{"external_learner_id": 1, "full_name": "Avery Holt"}],
                                {"students": [{"ID": 9, "FullName": "Gone Learner"}]}, 1)
    import json
    assert "Avery Holt" not in json.dumps(drift)
    assert "Gone Learner" not in json.dumps(drift)


# --- 35-40. compatibility shape ----------------------------------------------------

def test_session_id_match_is_session_id_underscore_order():
    assert session_id_match("TRANSCRIPT-1", 7) == "TRANSCRIPT-1_7"


def test_legacy_date_is_the_utc_date_of_the_transcript_start():
    """Legacy sliced a UTC ISO string, so the UTC date wins over any offset."""
    from datetime import timedelta
    minus_five = timezone(timedelta(hours=-5))
    # 22:58 on the 3rd at UTC-5 is 03:58Z on the 4th: the UTC date is the 4th.
    assert legacy_date(datetime(2026, 9, 3, 22, 58, tzinfo=minus_five)) == "2026-09-04"
    # 01:30 on the 5th at UTC+3 is 22:30Z on the 4th.
    plus_three = timezone(timedelta(hours=3))
    assert legacy_date(datetime(2026, 9, 5, 1, 30, tzinfo=plus_three)) == "2026-09-04"
    assert legacy_date(datetime(2026, 9, 4, 7, 58, tzinfo=timezone.utc)) == "2026-09-04"
    assert legacy_date(None) == ""


def test_counts_come_from_the_final_statuses():
    final = [MET] * 9 + [PARTIALLY_MET, NOT_MET]
    assert counts_from_statuses(final) == {"met_count": 9, "partial_count": 1,
                                           "not_met_count": 1}


def test_severity_mapping_matches_legacy():
    assert SEVERITY == {MET: "pass", PARTIALLY_MET: "warning", NOT_MET: "fail"}


def test_renderer_version_is_stable():
    assert RENDERER_VERSION == "legacy_qa_v8_renderer_v1"


# --- 12. renderer fingerprint -------------------------------------------------------

def fingerprint_args(**overrides):
    args = {"renderer_version": RENDERER_VERSION, "evaluation_id": uuid.UUID(int=1),
            "evaluation_fingerprint": "a" * 64, "document_fingerprint": "b" * 64,
            "engagement_fingerprint": "c" * 64, "lms_fingerprint": "d" * 64,
            "checklist": [(order, MET) for order in range(1, 12)],
            "clip_states": [(uuid.UUID(int=5), "VALID")]}
    args.update(overrides)
    return args


def test_render_fingerprint_is_deterministic_and_order_independent():
    base = render_fingerprint(**fingerprint_args())
    shuffled = fingerprint_args(checklist=list(reversed(
        [(order, MET) for order in range(1, 12)])))
    assert render_fingerprint(**shuffled) == base
    assert len(base) == 64


@pytest.mark.parametrize("field,value", [
    ("renderer_version", "legacy_qa_v8_renderer_v2"),
    ("evaluation_fingerprint", "0" * 64),
    ("document_fingerprint", "1" * 64),
    ("engagement_fingerprint", "2" * 64),
    ("lms_fingerprint", "3" * 64),
    ("checklist", [(order, NOT_MET) for order in range(1, 12)]),
    ("clip_states", [(uuid.UUID(int=5), "OUT_OF_TRANSCRIPT_RANGE")]),
])
def test_render_fingerprint_changes_with_every_material_input(field, value):
    assert render_fingerprint(**fingerprint_args(**{field: value})) \
        != render_fingerprint(**fingerprint_args())


# --- 3 / 45. boundaries ---------------------------------------------------------------

def test_the_rendering_package_never_calls_a_model_or_writes_legacy_tables():
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    sources = [*(root / "app" / "rendering").glob("*.py"),
               root / "app" / "db" / "repositories" / "qa_rendering.py"]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for forbidden in ("openai", "anthropic", "complete_json", "graph.microsoft.com",
                          "kbc_attendance", "aptem_auto_extracting", "ffmpeg", "sharepoint"):
            assert forbidden not in lowered, (path.name, forbidden)
        tree = ast.parse(text)
        docstrings = {ast.get_docstring(node) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                statement = node.value
                if statement in docstrings:
                    continue
                upper = " ".join(statement.split()).upper()
                if any(upper.startswith(verb) for verb in
                       ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "TRUNCATE")):
                    for legacy in ("QA_DOCTORS", "QA_PERFECT", "KBC_USERS_DATA"):
                        assert legacy not in upper, (path.name, legacy)


def test_checklist_strings_used_by_the_renderer_are_the_canonical_ones():
    from app.rendering import service
    assert service.CHECKLIST_ITEMS is CHECKLIST_ITEMS
