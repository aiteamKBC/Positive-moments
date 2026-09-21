"""
Phase 3C2.3F unit tests: mapping-aware idempotency (Gate F).

The defect these exist for is generic, not Andrew's: a coded-owned target was
treated as identical purely because the SOURCE fingerprint had not changed.
That is wrong whenever the MAPPING changes, because the same frozen source
then produces a different target row - which is exactly how a wrong
attended_count reached production and then survived a second "identical" run.

Nothing here touches a database or a network.
"""
import pytest

from app.writer.mapping import PayloadInvariantError
from app.writer.modes import (
    CANARY_NEW_ONLY,
    DRY_RUN,
    EXPLICIT_BACKFILL,
    PERFECT_BLOCKED_BACKFILL_NOT_AUTHORISED,
    PERFECT_PROTECTED_EXISTING_LEGACY_ROW,
    PERFECT_UPDATE_MAPPING_OUTPUT_CHANGED,
    PERFECT_UPDATE_SOURCE_CHANGED,
    PERFECT_WOULD_INSERT,
    PERFECT_WOULD_SKIP_IDENTICAL,
    PERFECT_WOULD_UPDATE,
    plan_perfect_decision,
)
from app.writer.perfect_mapping import (
    CODED_OWNED_COLUMNS,
    FOREIGN_OWNED_COLUMNS,
    PERFECT_MAPPING_VERSION,
    RECORDING_OWNED_COLUMNS,
    eligibility_identity,
    perfect_digest,
    perfect_mapped_digest,
    perfect_row,
)


def mapped(**overrides) -> dict:
    row = {
        "lecture_key": "2026-09-16|Subject", "session_date": "2026-09-16",
        "subject": "Subject", "module": "Module", "trainer": "A Trainer",
        "engagement": 100, "attended_count": 7, "met_count": 11,
        "recording_url": None, "meeting_id": "MEETING-1", "session_id": "SESSION-1",
    }
    row.update(overrides)
    return row


def _decide(**overrides) -> str:
    kwargs = {"mode": DRY_RUN, "render_status": "RENDERED", "qa_status": "COMPLETED",
              "payload_valid": True, "is_perfect": True, "target_exists": True,
              "coded_owned": True, "foreign_session_on_key": False,
              "fingerprint_matches": True, "mapped_output_matches": True}
    kwargs.update(overrides)
    return plan_perfect_decision(**kwargs)


# --------------------------------------------------------------------------
# 1-4: the decision matrix
# --------------------------------------------------------------------------

def test_same_source_and_same_mapped_output_is_a_noop():
    assert _decide() == PERFECT_WOULD_SKIP_IDENTICAL


def test_same_source_but_changed_mapped_output_updates_a_coded_owned_target():
    """The regression that matters: an unchanged fingerprint is not enough."""
    assert _decide(mapped_output_matches=False) == PERFECT_WOULD_UPDATE


def test_a_changed_mapping_can_never_bypass_ownership_protection():
    for mode in (DRY_RUN, CANARY_NEW_ONLY, "PRODUCTION_NEW_ONLY", EXPLICIT_BACKFILL):
        assert _decide(mode=mode, coded_owned=False, mapped_output_matches=False) \
            == PERFECT_PROTECTED_EXISTING_LEGACY_ROW
    # And not even with the backfill authorisation flag.
    assert _decide(mode=EXPLICIT_BACKFILL, coded_owned=False,
                   mapped_output_matches=False, allow_update_existing=True) \
        == PERFECT_PROTECTED_EXISTING_LEGACY_ROW


def test_a_changed_source_still_follows_the_existing_controlled_path():
    assert _decide(fingerprint_matches=False) == PERFECT_WOULD_UPDATE
    assert _decide(fingerprint_matches=False, coded_owned=False) \
        == PERFECT_PROTECTED_EXISTING_LEGACY_ROW
    # A backfill must still be authorised explicitly.
    assert _decide(mode=EXPLICIT_BACKFILL, fingerprint_matches=False) \
        == PERFECT_BLOCKED_BACKFILL_NOT_AUTHORISED
    assert _decide(mode=EXPLICIT_BACKFILL, fingerprint_matches=False,
                   allow_update_existing=True) == PERFECT_WOULD_UPDATE


def test_a_backfill_rewrite_needs_explicit_authorisation():
    assert _decide(mode=EXPLICIT_BACKFILL, mapped_output_matches=False) \
        == PERFECT_BLOCKED_BACKFILL_NOT_AUTHORISED
    assert _decide(mode=EXPLICIT_BACKFILL, mapped_output_matches=False,
                   allow_update_existing=True) == PERFECT_WOULD_UPDATE
    # An authorised backfill of an unchanged target is still a no-op.
    assert _decide(mode=EXPLICIT_BACKFILL, allow_update_existing=True) \
        == PERFECT_WOULD_SKIP_IDENTICAL


def test_an_absent_target_is_an_insert_regardless_of_mapping_state():
    assert _decide(target_exists=False, coded_owned=False,
                   fingerprint_matches=False,
                   mapped_output_matches=False) == PERFECT_WOULD_INSERT


# --------------------------------------------------------------------------
# 5-9: what participates in the mapped digest
# --------------------------------------------------------------------------

def test_attended_count_participates_in_the_mapped_digest():
    assert perfect_mapped_digest(mapped(attended_count=7)) \
        != perfect_mapped_digest(mapped(attended_count=None))
    assert perfect_mapped_digest(mapped(attended_count=7)) \
        != perfect_mapped_digest(mapped(attended_count=8))


@pytest.mark.parametrize("column,value", [
    ("recording_url", "https://example.invalid/recording"),
    ("recap_url", "https://example.invalid/recap"),
    ("excel_synced_at", "2026-09-18T00:00:00Z"),
    ("detected_at", "2026-09-18T00:00:00Z"),
    ("id", 695),
])
def test_a_foreign_owned_column_does_not_participate(column, value):
    """
    A recording link or an Excel sync stamp is another system doing its job.
    If it moved the mapped digest, every enrichment would look like drift and
    provoke a pointless rewrite - or, worse, an endless one.
    """
    assert perfect_mapped_digest(mapped(**{column: value})) \
        == perfect_mapped_digest(mapped())


def test_a_foreign_field_change_alone_never_produces_a_mapping_update():
    enriched = mapped(recording_url="https://example.invalid/x",
                      recap_url="https://example.invalid/y",
                      excel_synced_at="2026-09-18T00:00:00Z")
    proposed = mapped()
    assert perfect_mapped_digest(enriched) == perfect_mapped_digest(proposed)
    assert _decide(mapped_output_matches=True) == PERFECT_WOULD_SKIP_IDENTICAL
    # The whole-row digest still records the enrichment, as audit evidence.
    assert perfect_digest(enriched) != perfect_digest(proposed)


def test_every_coded_owned_column_participates_in_the_mapped_digest():
    baseline = perfect_mapped_digest(mapped())
    for column in CODED_OWNED_COLUMNS:
        changed = mapped(**{column: "CHANGED-VALUE"})
        assert perfect_mapped_digest(changed) != baseline, column


def test_the_ownership_boundary_lists_do_not_overlap():
    coded = set(CODED_OWNED_COLUMNS)
    assert coded.isdisjoint(RECORDING_OWNED_COLUMNS)
    assert coded.isdisjoint(FOREIGN_OWNED_COLUMNS)


# --------------------------------------------------------------------------
# 10: mapping version provenance
# --------------------------------------------------------------------------

def test_the_mapping_version_participates_and_is_reported():
    assert perfect_mapped_digest(mapped(), mapping_version="other") \
        != perfect_mapped_digest(mapped(), mapping_version=PERFECT_MAPPING_VERSION)
    assert eligibility_identity()["mapping_version"] == PERFECT_MAPPING_VERSION


def test_the_mapping_version_is_separate_from_the_writer_version():
    """
    Ownership is keyed on (lecture_key, writer_version). If the mapping
    version were folded into the writer version, correcting a mapping would
    mint a SECOND ownership row for a lecture the platform already owns, and
    the first row would become unattributable.
    """
    from app.writer.perfect_mapping import PERFECT_WRITER_VERSION
    assert PERFECT_MAPPING_VERSION != PERFECT_WRITER_VERSION
    assert PERFECT_MAPPING_VERSION not in PERFECT_WRITER_VERSION


def test_an_absent_row_digests_distinctly():
    assert perfect_mapped_digest(None) != perfect_mapped_digest(mapped())


# --------------------------------------------------------------------------
# the update reason
# --------------------------------------------------------------------------

def test_the_two_update_reasons_are_distinct_and_named():
    assert PERFECT_UPDATE_MAPPING_OUTPUT_CHANGED == "MAPPING_OUTPUT_CHANGED"
    assert PERFECT_UPDATE_SOURCE_CHANGED == "SOURCE_CHANGED"
    assert PERFECT_UPDATE_MAPPING_OUTPUT_CHANGED != PERFECT_UPDATE_SOURCE_CHANGED


# --------------------------------------------------------------------------
# the mapping itself still refuses to invent a value
# --------------------------------------------------------------------------

def test_the_mapping_reads_attended_count_from_the_rendered_payload():
    rendered = {
        "session_id": "SESSION-1", "meeting_id": "MEETING-1",
        "legacy_date": "2026-09-16", "subject": "Subject", "lms_module": "Module",
        "trainer": "A Trainer", "engagement": 100, "attended_count": 7,
        "met_count": 11,
    }
    assert perfect_row(rendered)["attended_count"] == 7
    # Absent stays absent rather than becoming a guess - but the loader
    # regression test is what guarantees it is never absent by accident.
    del rendered["attended_count"]
    assert perfect_row(rendered)["attended_count"] is None


def test_the_mapping_still_never_supplies_a_recording_url():
    rendered = {
        "session_id": "SESSION-1", "meeting_id": "MEETING-1",
        "legacy_date": "2026-09-16", "subject": "Subject", "lms_module": "Module",
        "trainer": "A Trainer", "engagement": 100, "attended_count": 7,
        "met_count": 11, "recording_url": "https://example.invalid/should-be-ignored",
    }
    assert perfect_row(rendered)["recording_url"] is None


def test_the_loader_query_selects_every_column_the_mapping_reads():
    """
    Static guard on the exact defect: `perfect_row` reads these columns from
    the rendered payload, so LOAD_RENDERED must fetch every one of them. A
    column read with .get() but never selected silently becomes NULL.
    """
    from app.db.repositories.qa_writer import LOAD_RENDERED
    for column in ("attended_count", "engagement", "met_count", "trainer",
                   "lms_module", "meeting_id", "session_id", "legacy_date", "subject"):
        assert f"rs.{column}" in LOAD_RENDERED, column
