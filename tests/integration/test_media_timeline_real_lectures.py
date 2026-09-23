"""
Phase 6A: the measured media timeline, against the real registry.

These read `lecture_recording_parts` - the durations Phase 6A measured from the
recordings themselves - and check that every canonical cue of a real lecture
lands inside real media. That is the property the whole transform exists to
guarantee, and it is the one the legacy identity mapping silently broke.

Read-only. No Graph call, no media download, no write.
"""
import pytest

from app.config.settings import Settings
from app.db.connection import readonly_database_connection
from app.db.repositories.recording_parts import RecordingPartRepository
from app.media.coordinates import MediaCoordinateError
from app.media.recordings import timeline_from_rows

# ---------------------------------------------------------------------------
# PRODUCTION-DATA ACCEPTANCE SUITE
#
# Every test in this module asserts behaviour against KBC's real historical
# evidence: named lectures, real session dates, real transcripts, the real
# legacy dataset. It is NOT part of the RC release gate and is deselected by
#     pytest tests/integration -m "not production_data"
# because on a database without that evidence it can only fail or pass
# vacuously - neither of which validates anything.
#
# The contracts in here that never needed real history have been moved to the
# self-contained gate modules (test_pipeline_contracts.py,
# test_platform_invariants.py, test_safety_fixes_integration.py).
#
# To run this suite, an approved acceptance dataset must be configured - never
# production. See docs/audits/QA_CORE_RC4_TEST_GATE_FINAL_2026-09-22.md.
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.production_data


LECTURES = """
SELECT l.lecture_id, l.subject
  FROM public.lecture_sessions l
 WHERE l.subject ILIKE %s
 LIMIT 1
"""

CUES = """
SELECT min(c.start_ms), max(c.end_ms), count(*)
  FROM public.lecture_transcript_cues c
  JOIN public.lecture_transcript_documents d ON d.document_id = c.document_id
 WHERE d.lecture_id = %s AND d.parser_version = 'webvtt_canonical_v1'
"""

MEASURED = "SELECT count(*) FROM public.lecture_recording_parts"


@pytest.fixture(scope="module")
def connection():
    settings = Settings.from_environment()
    with readonly_database_connection(settings.database_url) as handle:
        yield handle


def load(connection, pattern):
    row = connection.execute(LECTURES, (pattern,)).fetchone()
    assert row, f"no lecture matched {pattern}"
    lecture_id, subject = row
    first_ms, last_ms, count = connection.execute(CUES, (str(lecture_id),)).fetchone()
    rows = RecordingPartRepository().for_lecture(connection, lecture_id)
    return subject, first_ms / 1000.0, last_ms / 1000.0, count, rows


ANDREW = "%Andrew-Scheduling%"
RISK = "%Risk Management%"
PPC = "%Project Planning & Control%"


def test_the_measurement_has_been_run(connection):
    """Everything below is meaningless if the table is empty."""
    assert connection.execute(MEASURED).fetchone()[0] > 0


@pytest.mark.parametrize("pattern", [ANDREW, RISK, PPC])
def test_every_cue_of_a_real_lecture_lands_inside_real_media(connection, pattern):
    subject, first, last, count, rows = load(connection, pattern)
    assert rows, f"{subject} has no measured recording parts"
    timeline = timeline_from_rows(rows)
    assert count > 0

    for canonical in (first, (first + last) / 2, last):
        point = timeline.locate(canonical)
        assert 0.0 <= point.media_seconds
        part = next(p for p in timeline.parts if p.part_index == point.part_index)
        assert point.media_seconds <= part.media_duration_seconds


@pytest.mark.parametrize("pattern", [ANDREW, RISK, PPC])
def test_the_whole_lecture_is_covered_by_segments(connection, pattern):
    """
    Not just the endpoints: the span between them has to be continuously
    covered, or a part cut across a gap would silently lose time.
    """
    _subject, first, last, _count, rows = load(connection, pattern)
    segments = timeline_from_rows(rows).segments(first, last)
    assert segments
    total = sum(segment.duration_seconds for segment in segments)
    assert total == pytest.approx(last - first, abs=0.01)
    for earlier, later in zip(segments, segments[1:]):
        assert earlier.canonical_end_seconds == pytest.approx(
            later.canonical_start_seconds)


def test_the_multipart_lecture_really_does_span_two_recordings(connection):
    """
    Andrew is the case the identity mapping cannot express: its last cue is
    past the end of its first recording, so a correct answer MUST name a
    second one.
    """
    _subject, first, last, _count, rows = load(connection, ANDREW)
    assert len(rows) == 2, "Andrew-Scheduling should have two measured parts"
    timeline = timeline_from_rows(rows)

    assert timeline.locate(first).part_index == 1
    assert timeline.locate(last).part_index == 2
    assert timeline.crosses_recording_boundary(first, last)

    first_recording = timeline.parts[0].media_duration_seconds
    assert last > first_recording, (
        "the last cue should sit beyond the first recording; if it does not, "
        "this lecture no longer tests what it was chosen to test")


def test_the_identity_mapping_would_have_failed_on_the_multipart_lecture(connection):
    """
    State the old bug as an assertion. Asking for the last cue as if the whole
    lecture were in recording one is a request for a moment that file does not
    contain.
    """
    _subject, _first, last, _count, rows = load(connection, ANDREW)
    single = [row for row in rows if row["part_index"] == 1]
    only_first_recording = timeline_from_rows(single)
    with pytest.raises(MediaCoordinateError):
        only_first_recording.locate(last)


def test_a_single_part_lecture_is_still_the_identity(connection):
    _subject, first, last, _count, rows = load(connection, PPC)
    assert len(rows) == 1
    timeline = timeline_from_rows(rows)
    for canonical in (first, last):
        assert timeline.locate(canonical).media_seconds == pytest.approx(canonical)


def test_every_measured_lecture_covers_its_own_transcript(connection):
    """
    The sweep. Any lecture whose cues run past its measured media is a lecture
    whose clips would be cut from the wrong place, so the whole registry is
    checked rather than the three that were investigated by hand.
    """
    lectures = connection.execute("""
        SELECT DISTINCT r.lecture_id, l.subject
          FROM public.lecture_recording_parts r
          JOIN public.lecture_sessions l ON l.lecture_id = r.lecture_id
    """).fetchall()
    assert lectures

    repository = RecordingPartRepository()
    failures = []
    for lecture_id, subject in lectures:
        first_ms, last_ms, count = connection.execute(
            CUES, (str(lecture_id),)).fetchone()
        if not count:
            continue
        timeline = timeline_from_rows(repository.for_lecture(connection, lecture_id))
        try:
            timeline.segments(first_ms / 1000.0, last_ms / 1000.0)
        except MediaCoordinateError as exc:
            failures.append(f"{subject}: {exc}")
    assert not failures, "lectures whose transcript is not covered by media:\n" + \
                         "\n".join(failures)
