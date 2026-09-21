"""
Phase 3C2.3B unit tests: cross-part seam deduplication.

Pure functions only - no database, no Graph, no model. Every fixture is a
hand-written WebVTT part, so the geometry under test is explicit.
"""
import pytest

from app.transcripts.seam import (
    DEDUP_POLICY_VERSION,
    SEAM_PARSER_VERSION,
    TranscriptPart,
    parse_parts_with_seam_dedup,
    seam_fingerprint,
)
from app.transcripts.webvtt import PARSED, PARSED_WITH_WARNINGS, parse_combined_webvtt


def vtt(*cues) -> str:
    """Build a WebVTT part from (start_ms, end_ms, speaker, text) tuples."""
    def stamp(value):
        h, rest = divmod(value, 3600000)
        m, rest = divmod(rest, 60000)
        s, ms = divmod(rest, 1000)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
    blocks = [f"{stamp(a)} --> {stamp(b)}\n<v {who}>{text}</v>" for a, b, who, text in cues]
    return "WEBVTT\n\n" + "\n\n".join(blocks)


def part(index, offset, *cues, artifact=None):
    return TranscriptPart(part_index=index, offset_ms=offset, content=vtt(*cues),
                          artifact_id=artifact or f"artifact-{index}",
                          provider_transcript_id=f"ptid-{index}")


# --- 1. the Andrew shape: a later part starting inside the earlier coverage ----------

def test_a_later_part_starting_inside_earlier_coverage_loses_the_overlap():
    first = part(1, 0,
                 (10_000, 20_000, "Trainer", "one"),
                 (20_000, 30_000, "Trainer", "two"),
                 (30_000, 61_005, "Trainer", "three"))
    # Offset places part 2's first cue 61,005 ms before part 1's coverage end.
    second = part(2, 0,
                  (0, 5_000, "Trainer", "dup a"),
                  (10_000, 20_000, "Trainer", "dup b"),
                  (61_005, 70_000, "Trainer", "new"))
    document = parse_parts_with_seam_dedup([first, second])

    assert document.dropped_cue_count == 2
    assert [cue.start_ms for cue in document.cues] == [10_000, 20_000, 30_000, 61_005]
    seam = document.seams[0]
    assert seam.earlier_coverage_end_ms == 61_005
    assert seam.seam_overlap_ms == 61_005
    assert seam.later_cues_examined == 3
    assert seam.kept_cue_count == 1
    assert seam.boundary_straddling_dropped_count == 0


def test_the_kept_cues_are_renumbered_contiguously_from_one():
    document = parse_parts_with_seam_dedup([
        part(1, 0, (0, 10_000, "A", "x"), (10_000, 20_000, "A", "y")),
        part(2, 0, (5_000, 9_000, "A", "dropped"), (20_000, 30_000, "A", "kept")),
    ])
    assert [cue.cue_index for cue in document.cues] == [1, 2, 3]


# --- 2-4. the other geometries ------------------------------------------------------

def test_two_parts_that_do_not_overlap_lose_nothing():
    document = parse_parts_with_seam_dedup([
        part(1, 0, (0, 10_000, "A", "one")),
        part(2, 50_000, (0, 10_000, "A", "two")),
    ])
    assert document.dropped_cue_count == 0
    assert [cue.start_ms for cue in document.cues] == [0, 50_000]
    assert document.seams[0].seam_overlap_ms == 0


def test_a_second_part_starting_exactly_on_the_boundary_is_kept():
    # start_ms == coverage_end_ms is NOT inside the covered timeline.
    document = parse_parts_with_seam_dedup([
        part(1, 0, (0, 10_000, "A", "one")),
        part(2, 10_000, (0, 5_000, "A", "two")),
    ])
    assert document.dropped_cue_count == 0
    assert [cue.start_ms for cue in document.cues] == [0, 10_000]


def test_a_later_part_entirely_inside_earlier_coverage_is_dropped_completely():
    document = parse_parts_with_seam_dedup([
        part(1, 0, (0, 100_000, "A", "long")),
        part(2, 10_000, (0, 5_000, "A", "a"), (5_000, 10_000, "A", "b")),
    ])
    assert document.dropped_cue_count == 2
    assert [cue.start_ms for cue in document.cues] == [0]
    assert document.seams[0].kept_cue_count == 0


# --- 5. boundary-straddling ----------------------------------------------------------

def test_a_boundary_straddling_cue_is_dropped_whole_and_its_tail_is_reported():
    # Part 2's cue starts 1 s before coverage ends and runs 4 s past it.
    document = parse_parts_with_seam_dedup([
        part(1, 0, (0, 10_000, "A", "one")),
        part(2, 9_000, (0, 5_000, "A", "straddles")),
    ])
    seam = document.seams[0]
    assert seam.boundary_straddling_dropped_count == 1
    assert seam.maximum_dropped_tail_ms == 4_000
    assert seam.dropped_cue_count == 1
    # The text is never split at a timestamp.
    assert [cue.start_ms for cue in document.cues] == [0]
    assert any(w["code"] == "SEAM_BOUNDARY_STRADDLING_CUE_DROPPED" for w in document.warnings)


# --- 6. three parts, two seams -------------------------------------------------------

def test_three_parts_resolve_two_seams_against_cumulative_coverage():
    document = parse_parts_with_seam_dedup([
        part(1, 0, (0, 30_000, "A", "p1")),
        part(2, 25_000, (0, 4_000, "A", "p2 dup"), (5_000, 20_000, "A", "p2 new")),
        part(3, 40_000, (0, 3_000, "A", "p3 dup"), (10_000, 20_000, "A", "p3 new")),
    ])
    assert len(document.seams) == 2
    assert document.seams[0].dropped_cue_count == 1     # 25,000 < 30,000
    assert document.seams[1].dropped_cue_count == 1     # 40,000 < 45,000
    assert [cue.start_ms for cue in document.cues] == [0, 30_000, 50_000]
    assert document.metrics["part_count"] == 3
    assert document.metrics["seam_count"] == 2


def test_coverage_is_cumulative_so_a_short_middle_part_cannot_rewind_it():
    document = parse_parts_with_seam_dedup([
        part(1, 0, (0, 100_000, "A", "long")),
        part(2, 10_000, (0, 1_000, "A", "swallowed")),
        part(3, 20_000, (0, 1_000, "A", "also swallowed")),
    ])
    assert document.dropped_cue_count == 2
    assert [cue.start_ms for cue in document.cues] == [0]


# --- 7. single part unchanged --------------------------------------------------------

def test_a_single_part_is_untouched_by_v2():
    cues = ((1_000, 5_000, "A", "one"), (4_000, 9_000, "B", "two"), (9_000, 12_000, "A", "three"))
    only = part(1, 0, *cues)
    v2 = parse_parts_with_seam_dedup([only])
    v1 = parse_combined_webvtt(only.content)
    assert v2.seams == []
    assert v2.dropped_cue_count == 0
    assert len(v2.cues) == len(v1.cues)
    for new, old in zip(v2.cues, v1.cues):
        assert (new.start_ms, new.end_ms) == (old.start_ms, old.end_ms)
        assert new.cue_text_sha256 == old.cue_text_sha256
        assert new.speaker_label_raw == old.speaker_label_raw
        assert new.cue_index == old.cue_index
    assert v2.metrics["duration_ms"] == v1.metrics["duration_ms"]


# --- 8-9. ordering and legitimate overlap --------------------------------------------

def test_cues_with_the_same_start_are_ordered_deterministically():
    document = parse_parts_with_seam_dedup([
        part(1, 0, (0, 5_000, "A", "first"), (5_000, 6_000, "A", "second"),
             (5_000, 7_000, "B", "third")),
    ])
    assert [cue.start_ms for cue in document.cues] == [0, 5_000, 5_000]
    # Equal starts fall back to the source ordinal, so the result is stable.
    assert [cue.metadata["source_cue_index"] for cue in document.cues] == [1, 2, 3]


def test_normal_overlap_inside_one_part_is_preserved_not_deduplicated():
    document = parse_parts_with_seam_dedup([
        part(1, 0, (0, 10_000, "A", "one"), (5_000, 15_000, "B", "overlapping"),
             (12_000, 20_000, "A", "three")),
    ])
    assert len(document.cues) == 3
    assert document.dropped_cue_count == 0
    assert document.metrics["overlapping_cue_count"] >= 1


def test_the_final_timeline_never_jumps_backwards():
    document = parse_parts_with_seam_dedup([
        part(1, 0, (10_026_775, 14_403_937, "A", "part one")),
        part(2, 14_336_082, (6_850, 7_170, "A", "dup"), (67_855, 2_858_410, "A", "rest")),
    ])
    starts = [cue.start_ms for cue in document.cues]
    assert starts == sorted(starts)
    assert document.metrics["non_monotonic_cue_count"] == 0


# --- 10. the decision is geometric, never textual -------------------------------------

def test_dedup_does_not_depend_on_text_or_speaker_similarity():
    """Identical geometry, completely different wording and speakers."""
    same_words = parse_parts_with_seam_dedup([
        part(1, 0, (0, 10_000, "A", "hello world")),
        part(2, 5_000, (0, 2_000, "A", "hello world")),
    ])
    different_words = parse_parts_with_seam_dedup([
        part(1, 0, (0, 10_000, "A", "hello world")),
        part(2, 5_000, (0, 2_000, "Z", "utterly unrelated phrasing")),
    ])
    assert same_words.dropped_cue_count == different_words.dropped_cue_count == 1


def test_the_module_contains_no_similarity_or_model_machinery():
    import pathlib
    source = (pathlib.Path(__file__).resolve().parents[2]
              / "app" / "transcripts" / "seam.py").read_text(encoding="utf-8").lower()
    for forbidden in ("difflib", "levenshtein", "embedding", "openai", "similarity(",
                      "jaccard", "fuzzy"):
        assert forbidden not in source, forbidden


# --- 11-12. timeline semantics ---------------------------------------------------------

def test_the_call_relative_origin_is_preserved():
    document = parse_parts_with_seam_dedup([
        part(1, 0, (10_026_775, 10_031_735, "A", "first"),
             (14_395_777, 14_403_937, "B", "last")),
        part(2, 14_336_082, (6_850, 7_170, "A", "dropped"),
             (2_857_770, 2_858_410, "A", "final")),
    ])
    assert document.cues[0].start_ms == 10_026_775, "must not rebase to zero"
    assert document.metrics["first_cue_start_ms"] == 10_026_775


def test_duration_is_last_end_minus_first_start_not_measured_from_zero():
    document = parse_parts_with_seam_dedup([
        part(1, 0, (10_000_000, 10_100_000, "A", "one")),
        part(2, 10_100_000, (0, 50_000, "A", "two")),
    ])
    assert document.metrics["first_cue_start_ms"] == 10_000_000
    assert document.metrics["last_cue_end_ms"] == 10_150_000
    assert document.metrics["duration_ms"] == 150_000


# --- 13. provenance ----------------------------------------------------------------------

def test_every_cue_records_the_part_artifact_ordinal_and_offset_it_came_from():
    document = parse_parts_with_seam_dedup([
        part(1, 0, (0, 10_000, "A", "one"), artifact="art-1"),
        part(2, 20_000, (0, 5_000, "A", "two"), artifact="art-2"),
    ])
    first, second = document.cues
    assert first.metadata["source_part_index"] == 1
    assert first.metadata["source_artifact_id"] == "art-1"
    assert first.metadata["applied_offset_ms"] == 0
    assert first.metadata["raw_start_ms"] == 0
    assert second.metadata["source_part_index"] == 2
    assert second.metadata["source_artifact_id"] == "art-2"
    assert second.metadata["source_provider_transcript_id"] == "ptid-2"
    assert second.metadata["applied_offset_ms"] == 20_000
    assert second.metadata["raw_start_ms"] == 0, "the raw ordinal timestamp survives"
    assert second.start_ms == 20_000, "the shifted timestamp is what is stored"


def test_the_seam_audit_names_both_sides():
    document = parse_parts_with_seam_dedup([
        part(1, 0, (0, 10_000, "A", "one"), artifact="art-1"),
        part(2, 5_000, (0, 2_000, "A", "two"), artifact="art-2"),
    ])
    seam = document.seams[0].as_dict()
    assert seam["earlier_artifact_id"] == "art-1"
    assert seam["later_artifact_id"] == "art-2"
    assert seam["later_offset_ms"] == 5_000
    assert seam["later_raw_first_start_ms"] == 0
    assert seam["later_shifted_first_start_ms"] == 5_000
    assert seam["dedup_policy_version"] == DEDUP_POLICY_VERSION


# --- 16-17. determinism -------------------------------------------------------------------

def test_the_same_parts_always_produce_the_same_result():
    parts = [part(1, 0, (0, 10_000, "A", "one")), part(2, 5_000, (0, 9_000, "A", "two"))]
    a = parse_parts_with_seam_dedup(parts)
    b = parse_parts_with_seam_dedup(list(reversed(parts)))   # order must not matter
    assert [(c.cue_index, c.start_ms, c.cue_text_sha256) for c in a.cues] == \
           [(c.cue_index, c.start_ms, c.cue_text_sha256) for c in b.cues]
    assert a.dropped_cue_count == b.dropped_cue_count


def test_the_fingerprint_is_stable_and_sensitive_to_every_input():
    base = [part(1, 0, (0, 10_000, "A", "one")), part(2, 20_000, (0, 5_000, "A", "two"))]
    assert seam_fingerprint(base) == seam_fingerprint(list(reversed(base)))
    moved = [base[0], TranscriptPart(2, 20_001, base[1].content, "artifact-2", "ptid-2")]
    assert seam_fingerprint(moved) != seam_fingerprint(base)
    retexted = [base[0], part(2, 20_000, (0, 5_000, "A", "different"))]
    assert seam_fingerprint(retexted) != seam_fingerprint(base)
    assert seam_fingerprint(base, policy_version="other") != seam_fingerprint(base)


# --- 18. version identity -------------------------------------------------------------------

def test_the_version_names_are_explicit_and_distinct_from_v1():
    from app.transcripts.webvtt import PARSER_VERSION
    assert SEAM_PARSER_VERSION == "webvtt_canonical_v2_seam_dedup"
    assert SEAM_PARSER_VERSION != PARSER_VERSION
    assert DEDUP_POLICY_VERSION == "earlier_part_wins_v1"


# --- status and edge cases ---------------------------------------------------------------

def test_a_seam_that_drops_cues_is_reported_as_parsed_with_warnings():
    document = parse_parts_with_seam_dedup([
        part(1, 0, (0, 10_000, "A", "one")),
        part(2, 5_000, (0, 2_000, "A", "two")),
    ])
    assert document.status == PARSED_WITH_WARNINGS
    assert any(w["code"] == "SEAM_DUPLICATE_COVERAGE_DROPPED" for w in document.warnings)


def test_a_clean_multi_part_join_is_plain_parsed():
    document = parse_parts_with_seam_dedup([
        part(1, 0, (0, 10_000, "A", "one")),
        part(2, 20_000, (0, 5_000, "A", "two")),
    ])
    assert document.status == PARSED


def test_no_parts_is_an_empty_transcript_rather_than_an_error():
    document = parse_parts_with_seam_dedup([])
    assert document.cues == []
    assert document.metrics["cue_count"] == 0


def test_part_warnings_keep_their_own_part_provenance():
    broken = TranscriptPart(2, 20_000, "WEBVTT\n\nno timing here\n", "art-2", "ptid-2")
    document = parse_parts_with_seam_dedup([part(1, 0, (0, 10_000, "A", "one")), broken])
    offending = [w for w in document.warnings if w["code"] == "BLOCK_WITHOUT_TIMING"]
    assert offending and offending[0]["part_index"] == 2
