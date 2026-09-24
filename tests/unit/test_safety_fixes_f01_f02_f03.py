"""
QA Core safety fixes for F-01, F-02 and F-03.

See docs/audits/QA_CORE_F01_F02_F03_ROOT_CAUSE_2026-09-22.md for the defects
and docs/audits/QA_CORE_SAFETY_FIX_VALIDATION_2026-09-22.md for the fixes.

  F-02  one lecture occurrence must never gain a second qa_doctors_sessions row
        because Graph re-serialized its transcript id;
  F-01  a Perfect policy with no attendance requirement must never publish;
  F-03  shadow QA must never PUBLISH attendance it does not have. Superseded
        on 2026-09-24 by attendance-optional QA (attendance_optional_qa_v1):
        missing attendance no longer blocks QA, but everything attendance
        alone can supply stays UNKNOWN - never the empty snapshot's zero -
        and Item 7 stays the model's answer.

The first group reproduces REAL Graph ids byte-for-byte from a test-side
encoder. That is deliberately stronger than "the decoder returns something":
if the model of the format were wrong, the encoder could not regenerate the
ids production actually stores.
"""
import base64
import uuid
from datetime import date

import pytest

from app.cli.main import build_parser, guard_write_command
from app.common.errors import PlatformError
from app.orchestration.state import PipelineStateResolver
from app.orchestration.stages import (
    COMPLETE,
    MISSING,
    NOT_APPLICABLE,
    REVIEW_REQUIRED,
    STALE,
    SYNC_LEGACY_QA,
)
from app.orchestration.sync_safety import classify_plan, classify_qa
from app.qa.perfect import (
    PERFECT_ELIGIBILITY_VERSION as PERFECT_V1,
    PERFECT_ELIGIBILITY_VERSION_V2 as PERFECT_V2,
    PUBLISHABLE_PERFECT_ELIGIBILITY_VERSIONS,
    may_publish,
)
from app.qa.service import (
    COMPLETED,
    WAITING_FOR_ATTENDANCE_SOURCE,
    package_attendance_coverage,
)
from app.transcripts.identity import (
    SOURCE_DECODED,
    SOURCE_RAW,
    IdentityDecodeError,
    canonical_key,
    canonical_transcript_identity,
    lz4_block_decompress,
    same_transcript,
)
from app.writer.legacy_identity import (
    AMBIGUOUS,
    CLEAR,
    FOREIGN_SAME_OCCURRENCE,
    OWNED_SAME_OCCURRENCE,
    Candidate,
    LegacyOccurrenceGuard,
    classify,
)
from app.writer.modes import (
    BLOCKED_AMBIGUOUS_LEGACY_IDENTITY,
    DRY_RUN,
    EXPLICIT_BACKFILL,
    PRODUCTION_NEW_ONLY,
    PROTECTED_EXISTING_LEGACY_ROW,
    WOULD_INSERT,
    WOULD_SKIP_IDENTICAL,
    WOULD_UPDATE,
    WriterModeError,
)

from test_backfill_legacy_compatibility import (
    FakeLegacyTarget,
    FakeOccurrenceRepository,
    FakeOwnership,
    sync_one,
    writer_for,
)
from test_legacy_writer import rendered
from test_shadow_qa import StubEvaluations, StubProvider, good_output, package, run_one


# ---------------------------------------------------------------------------
# a test-side encoder for Graph transcript ids
# ---------------------------------------------------------------------------

def _mp_uint(n):
    if n < 0x80:
        return bytes([n])
    if n < 0x100:
        return b"\xcc" + bytes([n])
    if n < 0x10000:
        return b"\xcd" + n.to_bytes(2, "big")
    return b"\xce" + n.to_bytes(4, "big")


def _mp_str(text):
    raw = text.encode("utf-8")
    if len(raw) < 32:
        return bytes([0xA0 | len(raw)]) + raw
    if len(raw) < 256:
        return b"\xd9" + bytes([len(raw)]) + raw
    return b"\xda" + len(raw).to_bytes(2, "big") + raw


def _mp_array(parts):
    if len(parts) < 16:
        return bytes([0x90 | len(parts)]) + b"".join(parts)
    return b"\xdc" + len(parts).to_bytes(2, "big") + b"".join(parts)


def _lz4_literal_block(payload):
    """A literals-only LZ4 block, exactly the shape Graph emits."""
    n = len(payload)
    if n < 15:
        return bytes([n << 4]) + payload
    rest, extra = n - 15, bytearray()
    while rest >= 255:
        extra.append(255)
        rest -= 255
    extra.append(rest)
    return bytes([0xF0]) + bytes(extra) + payload


def _ext(ext_type, data):
    fixed = {1: 0xD4, 2: 0xD5, 4: 0xD6, 8: 0xD7, 16: 0xD8}
    if len(data) in fixed:
        return bytes([fixed[len(data)], ext_type]) + data
    return b"\xc7" + bytes([len(data), ext_type]) + data


def graph_id(thread, meeting_ts, transcript, *, trailing_nil=False, version=4,
             extra=None):
    fields = [_mp_uint(version), _mp_str(thread), _mp_str(meeting_ts),
              _mp_str(transcript)]
    if extra is not None:
        fields.append(extra)
    if trailing_nil:
        fields.append(b"\xc0")
    payload = _mp_array(fields)
    block = _lz4_literal_block(payload)
    envelope = (b"\x92" + _ext(98, _mp_uint(len(payload)))
                + b"\xc6" + len(block).to_bytes(4, "big") + block)
    return base64.urlsafe_b64encode(envelope).decode("ascii")


# Two real ids from production (the same transcript, both serializations).
REAL_CANONICAL = (
    "ktVizIDGAAAAgvBxlATZMDE5OjYxMjU3NDVhMTc0NjRjYTA4MDFiMTM5NzY4MTZiZGUxQHRocmVh"
    "ZC50YWN2Mq0xNzgxODc5ODEzMTUy2TwzMjVhNWVhNy1jMzVmLTRlODQtOWQ2MS04M2RlOTEwNTBm"
    "NjItMTc5MDA2NTY1MS1UcmFuc2NyaXB0VjI=")
REAL_VARIANT = (
    "ktVizIHGAAAAg_BylQTZMDE5OjYxMjU3NDVhMTc0NjRjYTA4MDFiMTM5NzY4MTZiZGUxQHRocmVh"
    "ZC50YWN2Mq0xNzgxODc5ODEzMTUy2TwzMjVhNWVhNy1jMzVmLTRlODQtOWQ2MS04M2RlOTEwNTBm"
    "NjItMTc5MDA2NTY1MS1UcmFuc2NyaXB0VjLA")
REAL_TRIPLE = ("19:6125745a17464ca0801b13976816bde1@thread.tacv2", "1781879813152",
               "325a5ea7-c35f-4e84-9d61-83de91050f62-1790065651-TranscriptV2")


def synthetic_triple(i):
    return (f"19:{uuid.uuid5(uuid.NAMESPACE_URL, f't{i}').hex}@thread.tacv2",
            str(1781879813152 + i * 1000),
            f"{uuid.uuid5(uuid.NAMESPACE_URL, f'g{i}')}-{1790000000 + i}-TranscriptV2")


# ---------------------------------------------------------------------------
# Fix C - canonical transcript identity
# ---------------------------------------------------------------------------

def test_the_encoder_reproduces_both_real_production_ids_byte_for_byte():
    """
    The format model is proven, not assumed: regenerating the stored ids from
    their decoded fields must give back exactly what Graph produced.
    """
    assert graph_id(*REAL_TRIPLE) == REAL_CANONICAL
    assert graph_id(*REAL_TRIPLE, trailing_nil=True) == REAL_VARIANT


def test_both_real_serializations_resolve_to_one_identity():
    canonical = canonical_transcript_identity(REAL_CANONICAL)
    variant = canonical_transcript_identity(REAL_VARIANT)
    assert canonical.source == variant.source == SOURCE_DECODED
    assert canonical.key == variant.key
    assert (canonical.thread_id, canonical.meeting_timestamp,
            canonical.transcript_id) == REAL_TRIPLE
    assert same_transcript(REAL_CANONICAL, REAL_VARIANT)


def test_ninety_four_synthetic_pairs_canonicalize_ninety_four_of_ninety_four():
    """Test 7, structurally. Production's own 94 pairs are validated read-only."""
    for i in range(94):
        triple = synthetic_triple(i)
        a, b = graph_id(*triple), graph_id(*triple, trailing_nil=True)
        assert a != b
        assert canonical_key(a) == canonical_key(b)


def test_distinct_transcripts_never_collapse():
    """Test 8. 500 distinct transcripts, in both spellings, give 500 identities."""
    keys = set()
    for i in range(500):
        triple = synthetic_triple(i)
        keys.add(canonical_key(graph_id(*triple)))
        keys.add(canonical_key(graph_id(*triple, trailing_nil=True)))
    assert len(keys) == 500


@pytest.mark.parametrize("change", ["thread", "timestamp", "transcript"])
def test_one_changed_field_is_a_different_transcript(change):
    thread, ts, guid = REAL_TRIPLE
    altered = {"thread": (thread.replace("6125", "6126"), ts, guid),
               "timestamp": (thread, str(int(ts) + 1), guid),
               "transcript": (thread, ts, guid.replace("325a", "325b"))}[change]
    assert canonical_key(graph_id(*altered)) != canonical_key(REAL_CANONICAL)


@pytest.mark.parametrize("raw", [
    None, "", "   ", "not a transcript id", "!!!***", "ktVizIDG",
    base64.urlsafe_b64encode(b"\x92\xd5\x62\xcc\x80").decode(),       # truncated
])
def test_anything_undecodable_keeps_its_raw_identity(raw):
    identity = canonical_transcript_identity(raw)
    assert identity.source == SOURCE_RAW


def test_a_raw_identity_only_ever_equals_the_identical_string():
    assert canonical_key("opaque-1") != canonical_key("opaque-2")
    assert canonical_key("opaque-1") == canonical_key("opaque-1")
    assert canonical_key("opaque-1") != canonical_key(REAL_CANONICAL)


def test_an_unknown_format_version_is_refused_not_interpreted():
    assert canonical_transcript_identity(
        graph_id(*REAL_TRIPLE, version=5)).source == SOURCE_RAW


def test_an_extra_non_nil_field_is_refused_rather_than_collapsed():
    """
    Only trailing nils are tolerated. A new non-nil field is information we do
    not understand, so collapsing it away could merge two transcripts.
    """
    extra = graph_id(*REAL_TRIPLE, extra=_mp_str("something new"))
    assert canonical_transcript_identity(extra).source == SOURCE_RAW
    assert canonical_key(extra) != canonical_key(REAL_CANONICAL)


def test_trailing_bytes_after_the_envelope_are_refused():
    body = base64.urlsafe_b64decode(REAL_CANONICAL) + b"\x00"
    assert canonical_transcript_identity(
        base64.urlsafe_b64encode(body).decode()).source == SOURCE_RAW


def test_lz4_back_references_including_overlapping_copies_decode():
    # "abc" then a match (offset 3, length 6) then a final literal "d".
    block = bytes([0x32]) + b"abc" + bytes([3, 0]) + bytes([0x10]) + b"d"
    assert lz4_block_decompress(block, 10) == b"abcabcabcd"


@pytest.mark.parametrize("block,size", [
    (bytes([0x32]) + b"abc" + bytes([9, 0]) + bytes([0x10]) + b"d", 10),  # offset too far
    (bytes([0x50]) + b"abc", 5),                                            # truncated
    (bytes([0x30]) + b"abc", 4),                                            # size mismatch
])
def test_a_malformed_lz4_block_raises(block, size):
    with pytest.raises(IdentityDecodeError):
        lz4_block_decompress(block, size)


def test_selection_collapses_twins_to_the_first_seen_spelling():
    """
    One transcript seen under two spellings is one candidate, and it is the
    spelling seen FIRST - so a later re-serialization can never displace the
    id a lecture is already published under.
    """
    from datetime import datetime, timezone
    from app.db.repositories.transcript_selections import collapse_equivalent_transcripts

    early = datetime(2026, 9, 17, tzinfo=timezone.utc)
    late = datetime(2026, 9, 22, tzinfo=timezone.utc)
    other = graph_id(*synthetic_triple(7))
    rows = [
        ("a-variant", REAL_VARIANT, None, None, None, None, None, None, late),
        ("a-canonical", REAL_CANONICAL, None, None, None, None, None, None, early),
        ("b", other, None, None, None, None, None, None, late),
    ]
    kept = [row[0] for row in collapse_equivalent_transcripts(rows)]
    assert kept == ["a-canonical", "b"]


# ---------------------------------------------------------------------------
# Fix A - the writer's same-occurrence guard
# ---------------------------------------------------------------------------

LECTURE = "00000000-0000-0000-0000-000000000002"
MEETING = "MEETING-1"
DAY = "2026-09-04"


def n8n_row(session_id, *, meeting=MEETING, day=DAY, subject="Test Lecture"):
    return {"session_id": session_id, "meeting_id": meeting, "date": day,
            "subject": subject, "trainer": "Somebody", "met_count": 10}


def context(*transcripts, meeting=MEETING, dates=(DAY,)):
    return {LECTURE: {"meeting_id": meeting, "dates": list(dates),
                      "transcripts": list(transcripts)}}


def plan(payload_id, legacy, ownership, lectures, **kw):
    payload = rendered(session_id=payload_id, meeting_id=MEETING,
                       legacy_date=DAY, canonical_session_date=DAY)
    return sync_one(writer_for(legacy=legacy, ownership=ownership, payload=payload,
                               lectures=lectures, **kw))


def test_1_same_lecture_canonical_id_existing_n8n_row_is_not_inserted():
    legacy = FakeLegacyTarget(sessions={REAL_CANONICAL: n8n_row(REAL_CANONICAL)})
    result = plan(REAL_CANONICAL, legacy, FakeOwnership(), context(REAL_CANONICAL))
    assert result["decision"] == PROTECTED_EXISTING_LEGACY_ROW
    assert legacy.writes == [] and len(legacy.sessions) == 1


def test_2_same_lecture_variant_id_existing_n8n_row_is_not_inserted():
    """The F-02 defect itself: the exact lookup misses, the guard does not."""
    legacy = FakeLegacyTarget(sessions={REAL_CANONICAL: n8n_row(REAL_CANONICAL)})
    result = plan(REAL_VARIANT, legacy, FakeOwnership(), context(REAL_VARIANT))
    assert result["decision"] == PROTECTED_EXISTING_LEGACY_ROW
    assert result["same_occurrence_verdict"] == FOREIGN_SAME_OCCURRENCE
    assert result["legacy_session_id"] == REAL_CANONICAL
    assert legacy.writes == [] and len(legacy.sessions) == 1
    assert classify_qa(result["decision"])["auto_approved"] is False


@pytest.mark.parametrize("mode,allow", [(DRY_RUN, False), (PRODUCTION_NEW_ONLY, False),
                                        (EXPLICIT_BACKFILL, True)])
def test_2b_the_variant_is_protected_in_every_mode(mode, allow):
    legacy = FakeLegacyTarget(sessions={REAL_CANONICAL: n8n_row(REAL_CANONICAL)})
    result = plan(REAL_VARIANT, legacy, FakeOwnership(), context(REAL_VARIANT),
                  mode=mode, allow_update_existing=allow)
    assert result["decision"] == PROTECTED_EXISTING_LEGACY_ROW
    assert legacy.writes == []


def test_3_coded_row_under_the_old_id_is_the_target_never_a_second_row():
    legacy = FakeLegacyTarget(sessions={REAL_CANONICAL: n8n_row(REAL_CANONICAL)})
    ownership = FakeOwnership(owned={REAL_CANONICAL: {
        "source_fingerprint": "a" * 64, "lecture_id": LECTURE}})
    result = plan(REAL_VARIANT, legacy, ownership, context(REAL_VARIANT))
    assert result["same_occurrence_verdict"] == OWNED_SAME_OCCURRENCE
    assert result["decision"] == WOULD_SKIP_IDENTICAL
    assert result["legacy_session_id"] == REAL_CANONICAL
    assert list(legacy.sessions) == [REAL_CANONICAL]


def test_3b_an_approved_update_lands_on_the_published_id_with_its_checklist():
    legacy = FakeLegacyTarget(sessions={REAL_CANONICAL: n8n_row(REAL_CANONICAL)})
    ownership = FakeOwnership(owned={REAL_CANONICAL: {
        "source_fingerprint": "b" * 64, "lecture_id": LECTURE}})
    result = plan(REAL_VARIANT, legacy, ownership, context(REAL_VARIANT),
                  mode=EXPLICIT_BACKFILL, allow_update_existing=True)
    assert result["decision"] == WOULD_UPDATE
    assert legacy.writes == [REAL_CANONICAL]
    assert list(legacy.sessions) == [REAL_CANONICAL]
    checklist = legacy.checklists[REAL_CANONICAL]
    assert {row["session_id_match"] for row in checklist} == {
        f"{REAL_CANONICAL}_{order}" for order in range(1, 12)}
    assert REAL_VARIANT not in legacy.checklists


def test_4_same_date_different_meeting_does_not_collide():
    other = graph_id(*synthetic_triple(1))
    legacy = FakeLegacyTarget(sessions={other: n8n_row(other, meeting="MEETING-2")})
    result = plan(REAL_CANONICAL, legacy, FakeOwnership(), context(REAL_CANONICAL))
    assert result["same_occurrence_verdict"] == CLEAR
    assert result["decision"] == WOULD_INSERT


def test_5_an_identical_subject_alone_never_collides():
    other = graph_id(*synthetic_triple(2))
    legacy = FakeLegacyTarget(sessions={other: n8n_row(
        other, meeting="MEETING-2", subject="Test Lecture")})
    result = plan(REAL_CANONICAL, legacy, FakeOwnership(), context(REAL_CANONICAL))
    assert result["decision"] == WOULD_INSERT


def test_6a_two_rows_for_one_occurrence_is_ambiguous_and_never_written():
    """Both spellings already exist as foreign rows: picking one would be a guess."""
    legacy = FakeLegacyTarget(sessions={
        REAL_CANONICAL: n8n_row(REAL_CANONICAL),
        REAL_VARIANT: n8n_row(REAL_VARIANT)})
    fresh = REAL_CANONICAL + "x"  # a spelling of the lecture no row carries
    result = plan(fresh, legacy, FakeOwnership(),
                  context(fresh, REAL_CANONICAL, REAL_VARIANT))
    assert result["same_occurrence_verdict"] == AMBIGUOUS
    assert result["decision"] == BLOCKED_AMBIGUOUS_LEGACY_IDENTITY
    assert legacy.writes == []
    verdict = classify_plan({"decision": result["decision"]})
    assert verdict["may_write"] is False
    assert verdict["reason_codes"] == ["LEGACY_IDENTITY_AMBIGUOUS"]


def test_6b_same_meeting_same_day_without_proof_is_ambiguous():
    unrelated = graph_id(*synthetic_triple(3))
    legacy = FakeLegacyTarget(sessions={unrelated: n8n_row(unrelated)})
    result = plan(REAL_CANONICAL, legacy, FakeOwnership(), context(REAL_CANONICAL))
    assert result["decision"] == BLOCKED_AMBIGUOUS_LEGACY_IDENTITY
    assert legacy.writes == []


def test_6c_readiness_refusals_still_take_precedence_over_identity():
    legacy = FakeLegacyTarget(sessions={REAL_CANONICAL: n8n_row(REAL_CANONICAL)})
    payload = rendered(session_id=REAL_VARIANT, qa_status="REVIEW_REQUIRED",
                       meeting_id=MEETING, legacy_date=DAY,
                       canonical_session_date=DAY)
    result = sync_one(writer_for(legacy=legacy, ownership=FakeOwnership(),
                                 payload=payload, lectures=context(REAL_VARIANT)))
    assert result["decision"] == "BLOCKED_NOT_READY"


def test_a_transcript_match_owned_by_another_lecture_is_never_resolved_silently():
    verdict = classify(lecture_id=LECTURE, meeting_id=MEETING,
                       own_transcript_keys={canonical_key(REAL_VARIANT)},
                       candidates=[Candidate(REAL_CANONICAL, MEETING, DAY, "someone-else")])
    assert verdict.verdict == AMBIGUOUS


def test_9_the_f02_lectures_are_no_longer_inserted():
    """
    Test 9, reproduced: n8n's row under the canonical spelling, the coded render
    under the variant, no coded ownership - exactly 2026-09-02's two lectures.
    """
    legacy = FakeLegacyTarget(sessions={REAL_CANONICAL: n8n_row(REAL_CANONICAL)})
    result = plan(REAL_VARIANT, legacy, FakeOwnership(), context(REAL_VARIANT))
    assert result["decision"] != WOULD_INSERT
    assert result["decision"] == PROTECTED_EXISTING_LEGACY_ROW


@pytest.mark.parametrize("payload_spelling", ["variant", "canonical"])
def test_10_an_already_duplicated_lecture_never_gains_a_third_row(payload_spelling):
    """
    Test 10: n8n's canonical row AND the coded variant row both exist, as on
    2026-09-03 and 2026-09-08. Whichever spelling the lecture is rendered
    under next, no third row may appear.
    """
    legacy = FakeLegacyTarget(sessions={
        REAL_CANONICAL: n8n_row(REAL_CANONICAL),
        REAL_VARIANT: n8n_row(REAL_VARIANT)})
    ownership = FakeOwnership(owned={REAL_VARIANT: {
        "source_fingerprint": "a" * 64, "lecture_id": LECTURE}})
    payload = REAL_VARIANT if payload_spelling == "variant" else REAL_CANONICAL
    for mode, allow in ((DRY_RUN, False), (PRODUCTION_NEW_ONLY, False),
                        (EXPLICIT_BACKFILL, True)):
        result = plan(payload, legacy, ownership,
                      context(REAL_VARIANT, REAL_CANONICAL), mode=mode,
                      allow_update_existing=allow)
        assert result["decision"] != WOULD_INSERT
        assert set(legacy.sessions) == {REAL_CANONICAL, REAL_VARIANT}


def test_a_genuinely_new_lecture_still_inserts_normally():
    result = plan(REAL_CANONICAL, FakeLegacyTarget(), FakeOwnership(),
                  context(REAL_CANONICAL))
    assert result["same_occurrence_verdict"] == CLEAR
    assert result["decision"] == WOULD_INSERT
    assert classify_qa(result["decision"])["action"] == "WRITE"


# ---------------------------------------------------------------------------
# Fix B - the resolver and the writer never disagree
# ---------------------------------------------------------------------------

class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class ResolverConnection:
    """Answers the resolver's ownership query from the same fake ledger."""

    def __init__(self, ownership):
        self.ownership = ownership

    def execute(self, sql, params=()):
        if "FROM public.lecture_qa_legacy_writes w" in sql and "w.lecture_id = %s" in sql:
            rows = [(uuid.uuid4(), "rendered-1", "evaluation-1", "WRITTEN",
                     params[1], sid, None)
                    for sid, rec in self.ownership.owned.items()
                    if str(rec.get("lecture_id")) == str(params[0])]
            return _Rows(rows)
        return _Rows([])


class LegacyView:
    """Exact-id existence from the fake table; occurrence from the REAL guard."""

    def __init__(self, legacy, guard):
        self.legacy = legacy
        self.guard = guard

    def legacy_qa_session(self, connection, legacy_session_id):
        row = self.legacy.sessions.get(legacy_session_id)
        return {"legacy_session_id": legacy_session_id} if row else None

    def legacy_qa_occurrence(self, connection, *, lecture_id, session_id,
                             writer_version):
        return self.guard.evaluate(connection, lecture_id=lecture_id,
                                   session_id=session_id,
                                   writer_version=writer_version)


SCENARIOS = {
    "new lecture": lambda: (FakeLegacyTarget(), FakeOwnership(), REAL_CANONICAL),
    "variant beside n8n row": lambda: (
        FakeLegacyTarget(sessions={REAL_CANONICAL: n8n_row(REAL_CANONICAL)}),
        FakeOwnership(), REAL_VARIANT),
    "exact n8n row": lambda: (
        FakeLegacyTarget(sessions={REAL_CANONICAL: n8n_row(REAL_CANONICAL)}),
        FakeOwnership(), REAL_CANONICAL),
    "ambiguous": lambda: (
        FakeLegacyTarget(sessions={REAL_CANONICAL: n8n_row(REAL_CANONICAL),
                                   REAL_VARIANT: n8n_row(REAL_VARIANT)}),
        FakeOwnership(), REAL_CANONICAL + "x"),
    "unproven same meeting": lambda: (
        FakeLegacyTarget(sessions={graph_id(*synthetic_triple(4)): n8n_row(
            graph_id(*synthetic_triple(4)))}),
        FakeOwnership(), REAL_CANONICAL),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_resolver_offers_an_automatic_insert_exactly_when_the_writer_would_insert(name):
    legacy, ownership, session = SCENARIOS[name]()
    lectures = context(session, REAL_CANONICAL, REAL_VARIANT)
    guard = LegacyOccurrenceGuard(FakeOccurrenceRepository(legacy, ownership, lectures))

    # DRY_RUN: both must judge the SAME starting state. A write-enabled plan
    # would insert first, and the resolver would then rightly see it.
    writer_decision = plan(session, legacy, ownership, lectures,
                           mode=DRY_RUN)["decision"]

    resolver = PipelineStateResolver(legacy_observations=LegacyView(legacy, guard))
    stage, _ = resolver._legacy_qa_sync(
        ResolverConnection(ownership), {"lecture_id": LECTURE},
        {"rendered": {"rendered_session_id": "rendered-1", "render_status": "RENDERED",
                      "legacy_session_id": session}})

    resolver_offers_insert = (stage["state"] == MISSING
                              and stage.get("action") == SYNC_LEGACY_QA)
    assert resolver_offers_insert == (writer_decision == WOULD_INSERT), (
        name, stage, writer_decision)


def test_resolver_reports_a_foreign_same_occurrence_row_as_protected_history():
    legacy = FakeLegacyTarget(sessions={REAL_CANONICAL: n8n_row(REAL_CANONICAL)})
    ownership = FakeOwnership()
    guard = LegacyOccurrenceGuard(FakeOccurrenceRepository(
        legacy, ownership, context(REAL_VARIANT)))
    resolver = PipelineStateResolver(legacy_observations=LegacyView(legacy, guard))
    stage, target = resolver._legacy_qa_sync(
        ResolverConnection(ownership), {"lecture_id": LECTURE},
        {"rendered": {"rendered_session_id": "r", "render_status": "RENDERED",
                      "legacy_session_id": REAL_VARIANT}})
    assert stage["state"] == NOT_APPLICABLE
    assert stage["reason"] == "LEGACY_ROW_NOT_CODED_OWNED"
    assert target == REAL_CANONICAL


def test_resolver_reports_ambiguity_as_review_not_as_work():
    name = "ambiguous"
    legacy, ownership, session = SCENARIOS[name]()
    guard = LegacyOccurrenceGuard(FakeOccurrenceRepository(
        legacy, ownership, context(session, REAL_CANONICAL, REAL_VARIANT)))
    resolver = PipelineStateResolver(legacy_observations=LegacyView(legacy, guard))
    stage, _ = resolver._legacy_qa_sync(
        ResolverConnection(ownership), {"lecture_id": LECTURE},
        {"rendered": {"rendered_session_id": "r", "render_status": "RENDERED",
                      "legacy_session_id": session}})
    assert stage["state"] == REVIEW_REQUIRED
    assert stage["reason"] == "LEGACY_IDENTITY_AMBIGUOUS"


def test_the_resolver_and_writer_share_one_implementation():
    """Structural: neither may grow a private copy of the matching rules."""
    import inspect
    from app.db.repositories import pipeline_observations
    from app.writer import service

    assert "LegacyOccurrenceGuard" in inspect.getsource(pipeline_observations)
    assert "LegacyOccurrenceGuard" in inspect.getsource(service)
    assert "canonical_key" not in inspect.getsource(service)


# ---------------------------------------------------------------------------
# Fix D - no publication under the obsolete Perfect policy
# ---------------------------------------------------------------------------

def test_only_the_attendance_requiring_policy_may_publish():
    assert PUBLISHABLE_PERFECT_ELIGIBILITY_VERSIONS == {PERFECT_V2}
    assert may_publish(PERFECT_V2) is True
    assert may_publish(PERFECT_V1) is False
    assert may_publish("some_future_policy") is False


def _planner(mode, version):
    from app.writer.perfect_service import PerfectLecturePlanner
    return PerfectLecturePlanner(
        result_repository=None, ownership_repository=None, legacy_repository=None,
        coverage_repository=object(), mode=mode, eligibility_version=version,
        lecture_ids=[LECTURE], confirmed=True)


@pytest.mark.parametrize("mode", ["CANARY_NEW_ONLY", PRODUCTION_NEW_ONLY, EXPLICIT_BACKFILL])
def test_11_the_planner_refuses_v1_in_every_write_mode(mode):
    with pytest.raises(WriterModeError, match="dry-run historical reproduction"):
        _planner(mode, PERFECT_V1)


def test_11b_a_dry_run_may_still_reproduce_v1():
    assert _planner(DRY_RUN, PERFECT_V1).eligibility_version == PERFECT_V1


@pytest.mark.parametrize("mode", [DRY_RUN, PRODUCTION_NEW_ONLY])
def test_11c_v2_is_unchanged(mode):
    assert _planner(mode, PERFECT_V2).eligibility_version == PERFECT_V2


def _cli(*extra):
    return build_parser().parse_args(["write-legacy-qa", "--date", "2026-09-17", *extra])


def test_11d_the_cli_refuses_v1_with_a_write_mode_before_connecting():
    args = _cli("--mode", "CANARY_NEW_ONLY", "--lecture-id", LECTURE,
                "--confirm-write", "--perfect-policy", PERFECT_V1)
    with pytest.raises(PlatformError, match="DRY_RUN historical reproduction"):
        guard_write_command(args)


def test_11e_the_cli_refuses_v1_even_when_perfect_is_skipped():
    args = _cli("--mode", "CANARY_NEW_ONLY", "--lecture-id", LECTURE,
                "--confirm-write", "--perfect-policy", PERFECT_V1,
                "--skip-perfect-lecture")
    with pytest.raises(PlatformError):
        guard_write_command(args)


def test_11f_the_cli_still_accepts_v1_in_a_dry_run_and_v2_in_a_write():
    guard_write_command(_cli("--perfect-policy", PERFECT_V1))
    guard_write_command(_cli("--mode", "CANARY_NEW_ONLY", "--lecture-id", LECTURE,
                             "--confirm-write"))
    assert _cli().perfect_policy == PERFECT_V2


# ---------------------------------------------------------------------------
# Fix E, as amended by attendance-optional QA: QA runs from the transcript,
# and an absent attendance source never becomes a published zero.
# ---------------------------------------------------------------------------

UNKNOWN_ATTENDANCE_FIELDS = ("attended_count", "spoke_count", "engagement_percentage",
                             "engagement_score")


def _assert_attendance_unknown(result, stored):
    assert result["attendance_source_authoritative"] is False
    assert result["attendance_flag"] == "PENDING_ATTENDANCE"
    evaluation = stored["evaluation"]
    for field in UNKNOWN_ATTENDANCE_FIELDS:
        assert evaluation[field] is None, field          # UNKNOWN != ZERO
    assert evaluation["item7_override_applied"] is False
    assert evaluation["metadata"]["attendance_flag"] == "PENDING_ATTENDANCE"

EMPTY_SNAPSHOT = {"attendance_source_row_count": 0, "attendance_present_row_count": 0,
                  "attendance_effective_member_count": 0,
                  "attendance_source_rows_any_status": None,
                  "attended_count": 0, "spoke_count": 0,
                  "engagement_percentage": "0.00"}


@pytest.mark.parametrize("lecture", ["Martech - Thur",
                                     "G2 - Keith - Strategy and Planning - June 2026"])
def test_12_the_two_f03_lectures_are_evaluated_without_a_fabricated_zero(lecture):
    """
    Test 12, reproducing 2026-09-17: an empty SOURCE_MISSING snapshot whose
    engagement row says 0 attended. QA now runs from the transcript; what it
    must never do is carry that zero into the evaluation.
    """
    provider = StubProvider(good_output())
    evaluations = StubEvaluations()
    result, stored, _ = run_one(package(subject=lecture, module=lecture, **EMPTY_SNAPSHOT),
                                provider, evaluations)
    assert result["qa_status"] == COMPLETED
    assert result["qa_status"] != WAITING_FOR_ATTENDANCE_SOURCE
    assert result["attendance_coverage_status"] == "SOURCE_MISSING"
    assert provider.calls == 1
    _assert_attendance_unknown(result, stored)


def test_12b_an_answer_finalized_on_the_fabricated_zero_is_not_reused():
    """
    G2 Keith already holds a COMPLETED evaluation on its empty snapshot, made
    before attendance was optional and carrying 0 attended / score 1. The
    attendance-pending answer is a DIFFERENT evaluation (its fingerprint says
    so), so the old one is never passed off as current. The orchestrator
    reaches it through the free deterministic refresh, not this path.
    """
    from app.qa.inputs import qa_source_fingerprint

    item = package(**EMPTY_SNAPSHOT)
    pending = run_one(item, StubProvider(good_output()))[0]["source_fingerprint"]
    old = qa_source_fingerprint(package={**item, "provider_contract_version": None},
                                model="gpt-5.2")
    assert old != pending
    existing = {old: {"evaluation_id": uuid.UUID(int=77), "qa_status": COMPLETED}}
    result, stored, _ = run_one(item, StubProvider(good_output()),
                                StubEvaluations(existing=existing))
    assert result.get("reused") is not True
    assert result["source_fingerprint"] == pending
    _assert_attendance_unknown(result, stored)


@pytest.mark.parametrize("counts,status", [
    ({"attendance_source_row_count": None}, "SOURCE_UNKNOWN"),
    ({"attendance_source_row_count": 0, "attendance_source_rows_any_status": 3},
     "SOURCE_PARTIAL_OR_INVALID"),
    ({"attendance_source_row_count": 4, "attendance_present_row_count": 4,
      "attendance_effective_member_count": 0}, "SOURCE_PARTIAL_OR_INVALID"),
])
def test_12c_every_non_authoritative_status_runs_qa_with_attendance_unknown(counts, status):
    provider = StubProvider(good_output())
    # The fixture's roster has members and an Item 7 override: on a
    # non-authoritative snapshot neither may reach the evaluation.
    result, stored, _ = run_one(package(**counts), provider)
    assert result["attendance_coverage_status"] == status
    assert result["qa_status"] == COMPLETED
    assert provider.calls == 1
    _assert_attendance_unknown(result, stored)


def test_13_authoritative_attendance_behaves_exactly_as_before():
    provider = StubProvider(good_output())
    result, stored, _ = run_one(package(), provider)
    assert result["attendance_coverage_status"] == "SOURCE_AVAILABLE_WITH_MEMBERS"
    assert result["qa_status"] == COMPLETED
    assert provider.calls == 1 and stored is not None


def test_13b_an_authoritative_confirmed_zero_is_still_evaluated():
    """A real, reported zero is a finding - unlike silence."""
    provider = StubProvider(good_output())
    result, _, _ = run_one(package(attendance_source_row_count=5,
                                   attendance_present_row_count=0,
                                   attendance_effective_member_count=0), provider)
    assert result["attendance_coverage_status"] == "SOURCE_AVAILABLE_CONFIRMED_ZERO"
    assert result["qa_status"] != WAITING_FOR_ATTENDANCE_SOURCE
    assert provider.calls == 1


def test_the_service_uses_the_orchestrator_predicate_not_a_copy():
    import inspect
    from app.qa import service

    source = inspect.getsource(service)
    assert "from app.attendance.coverage import is_authoritative" in source
    assert "from app.attendance.coverage import classify as classify_coverage" in source
    assert "AUTHORITATIVE_STATUSES" not in source
    assert package_attendance_coverage({}) == "SOURCE_UNKNOWN"


def test_14_the_rc3_pass_cap_regression_still_holds():
    from test_backfill_legacy_compatibility import (
        test_a_lecture_discovered_from_nothing_reaches_the_legacy_sync_in_one_run as rc3,
    )
    rc3()


def test_the_occurrence_guard_issues_only_selects():
    import inspect
    from app.writer import legacy_identity
    from app.transcripts import identity

    for module in (legacy_identity, identity):
        text = inspect.getsource(module).upper()
        for verb in ("INSERT INTO", "UPDATE PUBLIC.", "DELETE FROM",
                     "TRUNCATE TABLE", "TRUNCATE PUBLIC."):
            assert verb not in text, (module.__name__, verb)
