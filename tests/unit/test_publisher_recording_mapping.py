"""P0-C: the pure multipart recording mapping rules (no database, no Graph)."""
from __future__ import annotations

from datetime import timedelta

from app.publisher import recording_mapping as rm
from app.recordings.candidates import candidate_from_drive_item
from tests.unit.recording_fakes import END, START, SUBJECT, FakeRecordingGraph, iso

DATE = START.date()
EXPECTED = (END - START).total_seconds()


def _select(graph, start=START, end=END):
    return rm.select_occurrence(graph.recordings, scheduled_start=start, scheduled_end=end, session_date=DATE)


def _candidates(*metas, source="onedrive_recordings"):
    return [candidate_from_drive_item(m, source) for m in metas]


def _map(occurrence, candidates):
    assigned, diagnostics = rm.assign_files(occurrence.parts, candidates, subject=SUBJECT,
                                            session_date=DATE.isoformat())
    mappings = [rm.map_part(i, p, kept, shared, expected_duration=EXPECTED)
                for i, (p, kept, shared) in enumerate(assigned, start=1)]
    return rm.claimed_twice(mappings), diagnostics


# -- occurrence selection ---------------------------------------------------------------------

def test_single_part_selected_and_last_weeks_recording_ignored():
    g = FakeRecordingGraph()
    g.recording(START - timedelta(days=7), END - timedelta(days=7), call_id="old")
    this = g.recording(START + timedelta(minutes=1), END - timedelta(minutes=2))
    occurrence = _select(g)
    assert occurrence.status == rm.SELECTED and [p.recording_id for p in occurrence.parts] == [this["id"]]
    assert len(occurrence.all_recordings) == 2 and not occurrence.in_progress


def test_parts_of_one_call_are_kept_in_recording_order():
    g = FakeRecordingGraph()
    second = g.recording(START + timedelta(minutes=61), END, call_id="c")
    first = g.recording(START, START + timedelta(minutes=62), call_id="c")      # overlaps the next by a minute
    occurrence = _select(g)
    assert [p.recording_id for p in occurrence.parts] == [first["id"], second["id"]]


def test_restarted_call_within_twenty_minutes_is_one_occurrence():
    g = FakeRecordingGraph()
    g.recording(START, START + timedelta(minutes=50), call_id="a")
    g.recording(START + timedelta(minutes=62), END, call_id="b")                 # new call id after a drop
    assert len(_select(g).parts) == 2


def test_unattached_recording_overlapping_the_slot_is_ambiguous():
    g = FakeRecordingGraph()
    g.recording(START, START + timedelta(minutes=30), call_id="a")
    g.recording(START + timedelta(minutes=60), END, call_id="b")                 # 30 min gap, other call
    occurrence = _select(g)
    assert occurrence.status == rm.AMBIGUOUS_OCCURRENCE_RECORDINGS


def test_no_recording_in_the_window_and_unreadable_rows():
    g = FakeRecordingGraph()
    g.recording(START + timedelta(hours=8), START + timedelta(hours=9))          # same day, far outside
    assert _select(g).status == rm.NO_RECORDING_FOR_OCCURRENCE
    g.recordings.append({"id": "bad", "createdDateTime": "not a time"})
    assert _select(g).status == rm.GRAPH_RESPONSE_INVALID


def test_recording_without_end_time_is_in_progress():
    g = FakeRecordingGraph()
    g.recording(START, None)
    assert _select(g).in_progress


def test_duplicate_listing_of_one_recording_collapses():
    g = FakeRecordingGraph()
    row = g.recording(START, END)
    g.recordings.append(dict(row))
    assert len(_select(g).parts) == 1


# -- file per part ---------------------------------------------------------------------------------

def test_one_file_per_part_with_graph_origin_duration():
    g = FakeRecordingGraph()
    a = g.recording(START, START + timedelta(minutes=62), call_id="c")
    b = g.recording(START + timedelta(minutes=61), END, call_id="c")
    files = [g.recorded_file(a, lead=40), g.recorded_file(b, lead=20)]
    mappings, _ = _map(_select(g), _candidates(*files))
    assert [m.locate_status for m in mappings] == [rm.LOCATED, rm.LOCATED]
    assert [m.candidate.item_id for m in mappings] == [files[0]["id"], files[1]["id"]]
    assert {m.media_origin for m in mappings} == {"graph_created_origin"}
    assert [m.part_index for m in mappings] == [1, 2]


def test_file_name_origin_also_proves_the_duration():
    g = FakeRecordingGraph()
    r = g.recording(START, END)
    f = g.recorded_file(r, lead=50, duration=EXPECTED + 50)
    [m], _ = _map(_select(g), _candidates(f))
    assert (m.locate_status, m.media_origin) == (rm.LOCATED, "file_name_origin")


def test_duration_mismatch_pending_and_origin_ambiguity():
    g = FakeRecordingGraph()
    r = g.recording(START, END)
    [m], _ = _map(_select(g), _candidates(g.recorded_file(r, duration=EXPECTED - 600)))
    assert m.locate_status == rm.DURATION_MISMATCH
    g2 = FakeRecordingGraph()
    r2 = g2.recording(START, END)
    unindexed = g2.mp4(started=START - timedelta(seconds=35), duration_seconds=None)
    [m2], _ = _map(_select(g2), _candidates(unindexed))
    assert (m2.locate_status, m2.reason) == (rm.DURATION_PENDING, "FILE_DURATION_UNAVAILABLE")
    g3 = FakeRecordingGraph()
    r3 = g3.recording(START, END)
    [m3], _ = _map(_select(g3), _candidates(g3.recorded_file(r3, lead=3)))     # both origins fit, 3 s apart
    assert (m3.locate_status, m3.reason) == (rm.AMBIGUOUS, "MEDIA_ORIGIN_AMBIGUOUS")


def test_wrong_subject_and_wrong_date_never_match():
    g = FakeRecordingGraph()
    r = g.recording(START, END)
    other_subject = g.recorded_file(r, subject="Another Cohort Lecture")
    wrong_day = g.mp4(started=START - timedelta(days=1, seconds=30), duration_seconds=EXPECTED)
    [m], diagnostics = _map(_select(g), _candidates(other_subject, wrong_day))
    assert m.locate_status == rm.NOT_FOUND and diagnostics["subject_mismatch_in_window"] == 1


def test_file_fitting_two_parts_is_ambiguous_for_both():
    g = FakeRecordingGraph()
    g.recording(START, START + timedelta(minutes=1), call_id="c")
    g.recording(START + timedelta(seconds=90), END, call_id="c")
    f = g.mp4(started=START - timedelta(seconds=10), duration_seconds=EXPECTED)  # within 120 s of both
    mappings, diagnostics = _map(_select(g), _candidates(f))
    assert {m.locate_status for m in mappings} == {rm.AMBIGUOUS}
    assert diagnostics["files_fitting_several_parts"] == 1


def test_two_files_for_one_part_is_ambiguous_but_copies_collapse():
    g = FakeRecordingGraph()
    r = g.recording(START, END)
    f1 = g.recorded_file(r, lead=30)
    f2 = g.recorded_file(r, lead=31, size=123)
    [m], _ = _map(_select(g), _candidates(f1, f2))
    assert (m.locate_status, m.reason) == (rm.AMBIGUOUS, "SEVERAL_FILES_FOR_PART")
    copy = dict(f1, id="ITEM-COPY", parentReference={"driveId": "b!site", "id": "x"})
    [m2], _ = _map(_select(g), _candidates(f1, copy))
    assert m2.locate_status == rm.LOCATED


def test_false_start_is_its_own_classified_part():
    g = FakeRecordingGraph()
    g.recording(START, START + timedelta(minutes=2), call_id="c")
    g.recording(START + timedelta(minutes=5), END, call_id="c")
    files = [g.recorded_file(r) for r in g.recordings]
    mappings, _ = _map(_select(g), _candidates(*files))
    assert [m.classification for m in mappings] == ["FRAGMENT", "FULL"]
    assert all(m.located for m in mappings)


def test_same_file_resolved_for_two_parts_is_refused():
    g = FakeRecordingGraph()
    r = g.recording(START, END)
    f = _candidates(g.recorded_file(r))[0]
    one = rm.map_part(1, rm.parse_recordings([r])[0][0], [rm.Exact(f, 35.0, None)], 0, expected_duration=EXPECTED)
    two = rm.replace(one, part_index=2)
    assert {m.locate_status for m in rm.claimed_twice([one, two])} == {rm.AMBIGUOUS}


# -- transcript corroboration ------------------------------------------------------------------------

def _located_two_parts():
    g = FakeRecordingGraph()
    a = g.recording(START, START + timedelta(minutes=62), call_id="c", correlation="corr-a")
    b = g.recording(START + timedelta(minutes=61), END, call_id="c", correlation="corr-b")
    occurrence = _select(g)
    mappings, _ = _map(occurrence, _candidates(g.recorded_file(a), g.recorded_file(b)))
    return g, occurrence, mappings


def test_transcript_parts_pair_by_correlation_then_time():
    _, occurrence, mappings = _located_two_parts()
    transcripts = [rm.TranscriptPartRef("art-1", 1, "c", "corr-a", None),
                   rm.TranscriptPartRef("art-2", 2, "c", None, occurrence.parts[1].created_at)]
    paired, status, conflict = rm.pair_transcripts(mappings, occurrence.all_recordings, transcripts)
    assert (status, conflict) == (rm.PAIRED, None)
    assert [(m.transcript_artifact_id, m.transcript_pairing) for m in paired] == [
        ("art-1", "content_correlation_id"), ("art-2", "created_at")]


def test_no_transcript_and_partial_transcript_are_not_failures():
    _, occurrence, mappings = _located_two_parts()
    assert rm.pair_transcripts(mappings, occurrence.all_recordings, [])[1] == rm.NO_TRANSCRIPT
    one = [rm.TranscriptPartRef("art-1", 1, "c", "corr-a", None)]
    assert rm.pair_transcripts(mappings, occurrence.all_recordings, one)[1:] == (rm.PARTIAL, None)


def test_transcript_conflicts_are_detected():
    g, occurrence, mappings = _located_two_parts()
    g.recording(START - timedelta(days=7), END - timedelta(days=7), call_id="old", correlation="corr-old")
    occurrence = _select(g)
    other = [rm.TranscriptPartRef("art-x", 1, "old", "corr-old", None)]
    assert rm.pair_transcripts(mappings, occurrence.all_recordings, other)[2] == \
        "TRANSCRIPT_PART_BELONGS_TO_ANOTHER_RECORDING"
    calls = [rm.TranscriptPartRef("art-y", 1, "zzz", None, None)]
    assert rm.pair_transcripts(mappings, occurrence.all_recordings, calls)[2] == "CALL_ID_CONFLICT"
    twice = [rm.TranscriptPartRef("t1", 1, "c", "corr-a", None), rm.TranscriptPartRef("t2", 2, "c", "corr-a", None)]
    assert rm.pair_transcripts(mappings, occurrence.all_recordings, twice)[2] == \
        "SEVERAL_TRANSCRIPT_PARTS_FOR_ONE_RECORDING"


# -- previously verified parts ----------------------------------------------------------------------------

def _stored(mapping, **override):
    row = {"part_index": mapping.part_index, "locate_status": rm.LOCATED,
           "source_drive_id": mapping.candidate.drive_id, "source_item_id": mapping.candidate.item_id}
    return {**row, **override}


def test_a_verified_part_is_kept_when_a_later_read_misses_it():
    _, occurrence, mappings = _located_two_parts()
    stored = {mappings[0].recording.recording_id: _stored(mappings[0])}
    lost = [rm.replace(mappings[0], locate_status=rm.NOT_FOUND, candidate=None), mappings[1]]
    merged, conflict = rm.merge_with_stored(lost, stored)
    assert conflict is None and merged[0].located and merged[0].reason == "PREVIOUSLY_VERIFIED_SOURCE_KEPT"


def test_a_verified_part_is_never_re_pointed():
    _, occurrence, mappings = _located_two_parts()
    stored = {mappings[0].recording.recording_id: _stored(mappings[0], source_item_id="OTHER")}
    assert rm.merge_with_stored(mappings, stored)[1] == "SOURCE_CONFLICT_WITH_VERIFIED_FILE"
    moved = {mappings[0].recording.recording_id: _stored(mappings[0], part_index=2)}
    assert rm.merge_with_stored(mappings, moved)[1] == "RECORDING_SET_CHANGED"
    gone = {"rec-gone": _stored(mappings[0])}
    assert rm.merge_with_stored(mappings, gone)[1] == "RECORDING_SET_CHANGED"


def test_no_url_survives_candidate_mapping():
    g = FakeRecordingGraph()
    r = g.recording(START, END)
    [m], _ = _map(_select(g), _candidates(g.recorded_file(r)))
    persisted = (m.candidate.drive_id, m.candidate.item_id, m.candidate.name, m.recording.recording_id)
    assert not any("http" in str(v) for v in persisted)
    assert iso(m.recording.created_at) == r["createdDateTime"]


# -- several files for one part: duration tie-break ---------------------------------------------------
def test_full_recording_beats_a_short_stray_file_by_duration():
    """Martech - Fri 2026-10-23: the 97-min recording named 71 s early, an 87-s stray file named 46 s early."""
    g = FakeRecordingGraph()
    r = g.recording(START, END)
    full = g.recorded_file(r, lead=71)                     # file names carry whole seconds
    stray = g.recorded_file(r, lead=46, duration=87.36, size=285_053)
    [m], diagnostics = _map(_select(g), _candidates(full, stray))
    assert (m.locate_status, m.reason) == (rm.LOCATED, "EXACT_RECORDING_FILE_MATCHED")
    assert m.candidate.item_id == full["id"] and m.candidate_count == 2
    assert m.media_origin == "graph_created_origin" and m.lead_seconds == 71.0
    assert m.classification == "FULL" and diagnostics["same_subject_date"] == 2


def test_two_files_matching_the_duration_stay_ambiguous():
    g = FakeRecordingGraph()
    r = g.recording(START, END)
    a, b = g.recorded_file(r, lead=40, size=111), g.recorded_file(r, lead=90, size=222)
    [m], _ = _map(_select(g), _candidates(a, b))
    assert (m.locate_status, m.reason, m.candidate) == (rm.AMBIGUOUS, "SEVERAL_FILES_FOR_PART", None)


def test_an_unknown_duration_decides_nothing():
    g = FakeRecordingGraph()
    r = g.recording(START, END)
    full = g.recorded_file(r, lead=71)
    unindexed = g.mp4(started=START - timedelta(seconds=46), duration_seconds=None, size=285_053)
    [m], _ = _map(_select(g), _candidates(full, unindexed))
    assert (m.locate_status, m.reason) == (rm.AMBIGUOUS, "SEVERAL_FILES_FOR_PART")
    g2 = FakeRecordingGraph()
    r2 = g2.recording(START, None)                               # the recording's own end is unknown
    f1, f2 = g2.mp4(started=START - timedelta(seconds=71), duration_seconds=EXPECTED), \
        g2.mp4(started=START - timedelta(seconds=46), duration_seconds=87.0, size=1)
    [m2], _ = _map(_select(g2), _candidates(f1, f2))
    assert (m2.locate_status, m2.reason) == (rm.AMBIGUOUS, "SEVERAL_FILES_FOR_PART")


def test_genuine_ambiguity_is_never_resolved():
    g = FakeRecordingGraph()
    r = g.recording(START, END)
    # neither file has the recording's duration
    a, b = g.recorded_file(r, lead=40, duration=EXPECTED - 900, size=1), g.recorded_file(r, lead=90, duration=87, size=2)
    [m], _ = _map(_select(g), _candidates(a, b))
    assert (m.locate_status, m.reason) == (rm.AMBIGUOUS, "SEVERAL_FILES_FOR_PART")
    # one file fits the duration but its media origin is itself ambiguous (named 3 s before the recording)
    g2 = FakeRecordingGraph()
    r2 = g2.recording(START, END)
    c, d = g2.recorded_file(r2, lead=3, size=1), g2.recorded_file(r2, lead=60, duration=87, size=2)
    [m2], _ = _map(_select(g2), _candidates(c, d))
    assert (m2.locate_status, m2.reason) == (rm.AMBIGUOUS, "SEVERAL_FILES_FOR_PART")


def test_only_duration_match_rules():
    g = FakeRecordingGraph()
    r = g.recording(START, END)
    occurrence = _select(g)
    [part] = occurrence.parts
    full, stray = _candidates(g.recorded_file(r, lead=71), g.recorded_file(r, lead=46, duration=87, size=2))
    exact = lambda c, lead: rm.Exact(c, lead, part)                                    # noqa: E731
    assert rm.only_duration_match(part, [exact(full, 71), exact(stray, 46)]).candidate is full
    assert rm.only_duration_match(part, [exact(full, 71)]) is None                     # nothing to break a tie
