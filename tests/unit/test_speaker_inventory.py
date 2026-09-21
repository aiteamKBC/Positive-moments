"""
Phase 2C2 canonical speaker inventory.

A speaker row is "one distinct RAW label inside one document". It is not a
person: no role, attendance, LMS, alias, fuzzy match, or cross-document
identity exists at this layer, and these tests pin that boundary down.
"""
import logging
import uuid
from datetime import date

import pytest

from app.transcripts.speakers import (
    SPEAKER_INVENTORY_VERSION,
    find_normalization_collisions,
    normalize_speaker_label,
    speaker_identity,
)
from app.transcripts.speaker_service import SpeakerInventoryService


DOC_A = uuid.UUID("11111111-1111-5111-8111-111111111111")
DOC_B = uuid.UUID("22222222-2222-5222-8222-222222222222")
TARGET = date(2026, 9, 4)


# --- normalization ----------------------------------------------------------------


def test_outer_whitespace_is_trimmed_and_internal_whitespace_collapsed():
    assert normalize_speaker_label("  Jane   Doe  ") == "jane doe"
    assert normalize_speaker_label("Jane\tDoe") == "jane doe"


def test_casefold_normalization():
    assert normalize_speaker_label("JANE DOE") == "jane doe"
    assert normalize_speaker_label("Jane Doe") == "jane doe"
    # Casefold, not lower(): the German sharp s folds to 'ss'.
    assert normalize_speaker_label("Straße") == "strasse"


def test_unicode_nfkc_normalization():
    # Full-width and compatibility forms normalize; the label still means the same.
    assert normalize_speaker_label("Ｊane Doe") == "jane doe"
    assert normalize_speaker_label("ﬁona Reid") == "fiona reid"


def test_normalization_is_conservative_and_never_guesses_identity():
    """Accents, initials, order and nicknames are all left alone."""
    assert normalize_speaker_label("Zoë Dodd") == "zoë dodd"      # accent kept
    assert normalize_speaker_label("Zoë Dodd") != normalize_speaker_label("Zoe Dodd")
    assert normalize_speaker_label("J. R. Smith") == "j. r. smith"  # initials kept
    assert normalize_speaker_label("Doe, Jane") != normalize_speaker_label("Jane Doe")
    assert normalize_speaker_label("Chris Earle") != normalize_speaker_label("Christopher Earle")
    assert normalize_speaker_label("Ali") != normalize_speaker_label("Ali Mohamedin")


def test_normalization_handles_missing_values():
    assert normalize_speaker_label(None) == ""
    assert normalize_speaker_label("   ") == ""


# --- identity ------------------------------------------------------------------------


def test_speaker_id_is_deterministic():
    first = speaker_identity(document_id=DOC_A, speaker_label_raw="Jane Doe")
    assert first == speaker_identity(document_id=DOC_A, speaker_label_raw="Jane Doe")


def test_the_same_label_in_two_documents_gets_different_ids():
    """No human is ever shared across documents at this layer."""
    assert speaker_identity(document_id=DOC_A, speaker_label_raw="Jane Doe") != \
           speaker_identity(document_id=DOC_B, speaker_label_raw="Jane Doe")


def test_identity_uses_the_exact_raw_label_not_the_normalized_form():
    """Labels that merely look alike can never collapse into one row."""
    assert speaker_identity(document_id=DOC_A, speaker_label_raw="Jane Doe") != \
           speaker_identity(document_id=DOC_A, speaker_label_raw="jane  doe")


def test_inventory_version_is_part_of_the_identity():
    assert speaker_identity(document_id=DOC_A, speaker_label_raw="Jane Doe") != \
           speaker_identity(document_id=DOC_A, speaker_label_raw="Jane Doe",
                            inventory_version="speaker_inventory_v2")
    assert SPEAKER_INVENTORY_VERSION == "speaker_inventory_v1"


def test_a_blank_label_cannot_become_a_speaker():
    for value in (None, "", "   "):
        with pytest.raises(ValueError, match="speaker_label_raw is required"):
            speaker_identity(document_id=DOC_A, speaker_label_raw=value)


# --- collisions -------------------------------------------------------------------------


def test_normalized_collisions_are_reported_not_merged():
    collisions = find_normalization_collisions(["Jane Doe", "jane  doe", "Ali Hassan"])
    assert len(collisions) == 1
    assert collisions[0]["speaker_label_normalized"] == "jane doe"
    assert collisions[0]["raw_label_count"] == 2
    # The two raw labels keep separate identities regardless.
    assert speaker_identity(document_id=DOC_A, speaker_label_raw="Jane Doe") != \
           speaker_identity(document_id=DOC_A, speaker_label_raw="jane  doe")


def test_no_collision_when_labels_are_genuinely_distinct():
    assert find_normalization_collisions(["Jane Doe", "Ali Hassan", "Zoë Dodd"]) == []


# --- aggregation via the service ------------------------------------------------------------


class Speakers:
    """Stands in for the read-only aggregation repository."""

    def __init__(self, documents, aggregates, unassigned=None):
        self.documents = documents
        self.aggregates = aggregates
        self.unassigned = unassigned or {}
        self.written = {}

    def load_documents(self, connection, target_date, parser_version):
        return self.documents

    def aggregate_speakers(self, connection, document_ids):
        return {k: v for k, v in self.aggregates.items() if k in set(document_ids)}

    def count_unassigned_cues(self, connection, document_ids):
        return self.unassigned

    def existing_speaker_ids(self, connection, document_id, inventory_version):
        return set(self.written.get(document_id, {}))

    def upsert_speakers(self, connection, document_id, inventory_version, speakers):
        before = set(self.written.get(document_id, {}))
        incoming = {row["speaker_id"] for row in speakers}
        self.written[document_id] = {row["speaker_id"]: row for row in speakers}
        created = len(incoming - before)
        return {"created": created, "updated": len(incoming) - created,
                "removed": len(before - incoming)}


class Runs:
    def start(self, connection, target_date, inventory_version, mode="SHADOW"):
        return uuid.uuid4()

    def complete(self, connection, run_id, summary):
        self.summary = summary


def _document(document_id=DOC_A, cue_count=5, duration_ms=100_000, subject="Lecture A"):
    return {"document_id": document_id, "lecture_id": uuid.uuid4(), "subject": subject,
            "cue_count": cue_count, "duration_ms": duration_ms,
            "parser_version": "webvtt_canonical_v1",
            "source_content_sha256": "a" * 64, "parse_status": "PARSED"}


def _aggregate(label, cue_count, first_index, last_index, first_ms, last_ms, gross_ms):
    return {"speaker_label_raw": label, "cue_count": cue_count,
            "first_cue_index": first_index, "last_cue_index": last_index,
            "first_spoken_start_ms": first_ms, "last_spoken_end_ms": last_ms,
            "gross_spoken_ms": gross_ms}


def _service(repository, **kwargs):
    return SpeakerInventoryService(
        speaker_repository=repository, run_repository=Runs(), **kwargs)


def test_one_label_across_many_cues_yields_one_speaker_row():
    repository = Speakers(
        [_document(cue_count=668)],
        {DOC_A: [_aggregate("Jane Doe", 668, 1, 668, 1000, 900_000, 500_000)]})
    summary = _service(repository).build_day(object(), TARGET)
    assert summary["documents"][0]["distinct_raw_speaker_count"] == 1
    assert summary["speakers_created"] == 1
    assert summary["cues_aggregated"] == 668


def test_multiple_labels_yield_one_row_each_not_one_per_cue():
    aggregates = [
        _aggregate(f"Speaker {n}", 60, n, 600 + n, n * 1000, 700_000, 40_000)
        for n in range(1, 11)
    ]
    repository = Speakers([_document(cue_count=600)], {DOC_A: aggregates})
    summary = _service(repository).build_day(object(), TARGET)
    assert summary["documents"][0]["distinct_raw_speaker_count"] == 10
    assert summary["speakers_created"] == 10
    assert len(repository.written[DOC_A]) == 10


def test_aggregated_fields_are_carried_through_unchanged():
    repository = Speakers(
        [_document()],
        {DOC_A: [_aggregate("Jane Doe", 12, 3, 97, 4_500, 88_000, 31_250)]})
    _service(repository).build_day(object(), TARGET)
    row = next(iter(repository.written[DOC_A].values()))
    assert (row["cue_count"], row["first_cue_index"], row["last_cue_index"]) == (12, 3, 97)
    assert (row["first_spoken_start_ms"], row["last_spoken_end_ms"]) == (4_500, 88_000)
    assert row["gross_spoken_ms"] == 31_250
    assert row["speaker_label_raw"] == "Jane Doe"          # raw preserved exactly
    assert row["speaker_label_normalized"] == "jane doe"


def test_gross_spoken_time_may_exceed_the_document_duration_with_overlaps():
    """Two people talking over each other is valid; it is reported, not fixed."""
    repository = Speakers(
        [_document(duration_ms=60_000)],
        {DOC_A: [_aggregate("Jane Doe", 5, 1, 5, 0, 60_000, 50_000),
                 _aggregate("Ali Hassan", 4, 2, 6, 1_000, 59_000, 30_000)]})
    summary = _service(repository).build_day(object(), TARGET)
    document = summary["documents"][0]
    assert document["gross_spoken_ms_total"] == 80_000 > document["document_duration_ms"]
    assert document["gross_exceeds_document_duration"] is True
    assert summary["documents_with_overlap_excess"] == 1
    # Neither speaker's total was scaled down to make the sum fit.
    assert {row["gross_spoken_ms"] for row in repository.written[DOC_A].values()} == {50_000, 30_000}


def test_gross_below_duration_is_equally_valid_and_not_flagged():
    repository = Speakers(
        [_document(duration_ms=100_000)],
        {DOC_A: [_aggregate("Jane Doe", 3, 1, 3, 0, 90_000, 40_000)]})
    summary = _service(repository).build_day(object(), TARGET)
    assert summary["documents"][0]["gross_exceeds_document_duration"] is False
    assert summary["documents_with_overlap_excess"] == 0


def test_cues_without_a_speaker_are_counted_not_given_a_fake_speaker():
    repository = Speakers(
        [_document(cue_count=10)],
        {DOC_A: [_aggregate("Jane Doe", 8, 1, 9, 0, 50_000, 20_000)]},
        unassigned={DOC_A: 2})
    summary = _service(repository).build_day(object(), TARGET)
    assert summary["unassigned_cue_count"] == 2
    assert summary["documents"][0]["unassigned_speaker_cue_count"] == 2
    assert summary["documents"][0]["distinct_raw_speaker_count"] == 1
    labels = {row["speaker_label_raw"] for row in repository.written[DOC_A].values()}
    assert labels == {"Jane Doe"}
    assert not any(label in ("", None, "UNKNOWN") for label in labels)


def test_colliding_labels_are_flagged_on_the_rows_but_still_separate():
    repository = Speakers(
        [_document()],
        {DOC_A: [_aggregate("Jane Doe", 3, 1, 3, 0, 10_000, 5_000),
                 _aggregate("jane  doe", 2, 4, 5, 11_000, 20_000, 4_000)]})
    summary = _service(repository).build_day(object(), TARGET)
    assert summary["normalization_collision_count"] == 1
    rows = list(repository.written[DOC_A].values())
    assert len(rows) == 2                                   # never merged
    assert all(row["metadata"]["normalized_label_collision"] for row in rows)


def test_two_documents_never_share_a_speaker_row():
    repository = Speakers(
        [_document(DOC_A, subject="Lecture A"), _document(DOC_B, subject="Lecture B")],
        {DOC_A: [_aggregate("Jane Doe", 3, 1, 3, 0, 10_000, 5_000)],
         DOC_B: [_aggregate("Jane Doe", 4, 1, 4, 0, 20_000, 8_000)]})
    _service(repository).build_day(object(), TARGET)
    assert set(repository.written[DOC_A]) & set(repository.written[DOC_B]) == set()


def test_a_new_document_version_gets_its_own_inventory():
    """Provenance stays safe: no speaker ID is reused across document versions."""
    reparsed = uuid.UUID("33333333-3333-5333-8333-333333333333")
    repository = Speakers(
        [_document(DOC_A), _document(reparsed)],
        {DOC_A: [_aggregate("Jane Doe", 3, 1, 3, 0, 10_000, 5_000)],
         reparsed: [_aggregate("Jane Doe", 3, 1, 3, 0, 10_000, 5_000)]})
    _service(repository).build_day(object(), TARGET)
    assert set(repository.written[DOC_A]) != set(repository.written[reparsed])


def test_rerun_creates_no_duplicate_speaker_rows():
    repository = Speakers(
        [_document()],
        {DOC_A: [_aggregate("Jane Doe", 3, 1, 3, 0, 10_000, 5_000),
                 _aggregate("Ali Hassan", 2, 4, 5, 11_000, 20_000, 4_000)]})
    service = _service(repository)
    first = service.build_day(object(), TARGET)
    second = service.build_day(object(), TARGET)
    assert (first["speakers_created"], first["speakers_updated"]) == (2, 0)
    assert (second["speakers_created"], second["speakers_updated"]) == (0, 2)
    assert len(repository.written[DOC_A]) == 2


# --- boundary: nothing downstream happens here ---------------------------------------------


def test_no_identity_role_or_engagement_work_is_performed():
    repository = Speakers(
        [_document()], {DOC_A: [_aggregate("Jane Doe", 3, 1, 3, 0, 10_000, 5_000)]})
    summary = _service(repository).build_day(object(), TARGET)

    assert summary["attendance_lookups"] == 0
    assert summary["person_matches"] == 0
    assert summary["roles_assigned"] == 0
    assert summary["graph_calls"] == 0
    assert summary["webvtt_reparsed"] is False
    assert summary["metadata"]["identity_inference_performed"] is False
    assert summary["metadata"]["engagement_calculated"] is False

    # No persisted speaker row carries a role, person, or percentage field.
    for row in repository.written[DOC_A].values():
        assert not {"role", "is_trainer", "is_learner", "trainer", "learner",
                    "person_id", "attendance_id", "lms_user_id", "speaker_share_percent",
                    "engagement_percent"} & set(row)
        assert not {"role", "is_trainer", "person_id", "attendance_id",
                    "speaker_share_percent"} & set(row["metadata"])
    # Only inventory fields are persisted.
    assert set(next(iter(repository.written[DOC_A].values()))) == {
        "speaker_id", "speaker_label_raw", "speaker_label_normalized", "cue_count",
        "first_cue_index", "last_cue_index", "first_spoken_start_ms",
        "last_spoken_end_ms", "gross_spoken_ms", "metadata"}
    # No per-document share or percentage is emitted either.
    assert not {"speaker_share_percent", "trainer_percent", "learner_percent",
                "engagement_percent"} & set(summary["documents"][0])


def test_speaker_names_and_text_never_reach_the_logs(caplog):
    repository = Speakers(
        [_document()],
        {DOC_A: [_aggregate("Wilhelmina Quartermaine", 3, 1, 3, 0, 10_000, 5_000)]})
    with caplog.at_level(logging.DEBUG):
        _service(repository).build_day(object(), TARGET)
    emitted = "\n".join(
        record.getMessage() + str(getattr(record, "fields", "")) for record in caplog.records)
    assert emitted
    assert "Wilhelmina" not in emitted
    assert "WEBVTT" not in emitted


# --- legacy trainer diagnostic ---------------------------------------------------------------


class LegacyTrainers:
    def __init__(self, rows):
        self.rows = rows

    def load_trainers(self, connection, target_date):
        return self.rows


def test_legacy_trainer_diagnostic_is_exact_match_only_and_assigns_nothing():
    repository = Speakers(
        [_document(subject="Ray-Project Management Office (PMO)")],
        {DOC_A: [_aggregate("Ray Wilson", 9, 1, 9, 0, 60_000, 30_000),
                 _aggregate("Sam Lee", 3, 2, 8, 500, 40_000, 9_000)]})
    service = _service(repository, legacy_diagnostic_repository=LegacyTrainers(
        [{"subject": "Ray-Project Management Office (PMO)", "trainer": "Ray Wilson"}]))
    diagnostic = service.build_day(object(), TARGET)["legacy_trainer_diagnostic"]

    assert diagnostic["summary_raw"] == "1 / 1"
    assert diagnostic["rows"][0]["legacy_trainer_present_as_exact_raw_label"] == "YES"
    # The name itself is not echoed back into the report.
    assert "Ray Wilson" not in str(diagnostic)
    # And no role was assigned to anybody.
    assert all("role" not in row and "is_trainer" not in row
               for row in repository.written[DOC_A].values())


def test_legacy_trainer_absent_or_only_fuzzily_similar_reports_no():
    repository = Speakers(
        [_document(subject="Lecture A")],
        {DOC_A: [_aggregate("Christopher Earle", 9, 1, 9, 0, 60_000, 30_000)]})
    service = _service(repository, legacy_diagnostic_repository=LegacyTrainers(
        [{"subject": "Lecture A", "trainer": "Chris Earle"}]))
    diagnostic = service.build_day(object(), TARGET)["legacy_trainer_diagnostic"]
    # A nickname is NOT matched: this phase does no fuzzy matching whatsoever.
    assert diagnostic["summary_raw"] == "0 / 1"
    assert diagnostic["summary_normalized"] == "0 / 1"


def test_legacy_trainer_normalized_match_is_reported_separately():
    repository = Speakers(
        [_document(subject="Lecture A")],
        {DOC_A: [_aggregate("jane  doe", 9, 1, 9, 0, 60_000, 30_000)]})
    service = _service(repository, legacy_diagnostic_repository=LegacyTrainers(
        [{"subject": "Lecture A", "trainer": "Jane Doe"}]))
    diagnostic = service.build_day(object(), TARGET)["legacy_trainer_diagnostic"]
    assert diagnostic["summary_raw"] == "0 / 1"
    assert diagnostic["summary_normalized"] == "1 / 1"
