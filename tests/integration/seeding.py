"""
Synthetic fixtures for the self-contained integration suite.

Every release-gate integration test builds its own evidence with these helpers
and rolls it back. Nothing here reads, copies or reproduces production data:

  * no learner PII - names are obviously invented ("Synthetic Learner 1");
  * no production transcript text - the WebVTT below is generated;
  * no production identifiers - lecture ids are fresh uuid4s and transcript
    ids are generated envelopes. The one thing deliberately reproduced is the
    Graph transcript id FORMAT, because that format is itself the subject of
    the F-02 / transcript-identity contracts;
  * no production dates - everything happens on a date far outside the
    platform's real history.

`insert()` fills any NOT NULL column the caller did not name, so a test states
only the columns that carry meaning for it and the schema supplies the rest.
That keeps the fixtures minimal and the tests readable, and it means a new NOT
NULL column cannot silently break every test in the suite.
"""
from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from psycopg.types.json import Jsonb

from app.attendance.resolver import RESOLVER_VERSION
from app.attendance.roles import ROLE_ALGORITHM_VERSION
from app.engagement.calculator import ENGAGEMENT_ALGORITHM_VERSION
from app.qa.checklist import CHECKLIST_ITEMS
from app.qa.inputs import REQUIRED_ATTENDANCE_ROSTER_VERSION
from app.rendering.evidence import RENDERER_VERSION
from app.transcripts.selection import SELECTION_VERSION
from app.transcripts.webvtt import PARSER_VERSION


# A date far outside the platform's real history, so a fixture can never be
# confused with - or accidentally join to - a real lecture.
DAY = date(2031, 3, 4)
START = datetime(2031, 3, 4, 9, tzinfo=timezone.utc)
SPEAKER_INVENTORY_VERSION = "speaker_inventory_v1"


def sha(label: str) -> str:
    """A deterministic 64-hex value for the many sha256/fingerprint columns."""
    return hashlib.sha256(str(label).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Graph transcript ids, in both serializations
# ---------------------------------------------------------------------------

def _mp_uint(n):
    if n < 0x80:
        return bytes([n])
    if n < 0x100:
        return b"\xcc" + bytes([n])
    return b"\xcd" + n.to_bytes(2, "big")


def _mp_str(text):
    raw = text.encode("utf-8")
    if len(raw) < 32:
        return bytes([0xA0 | len(raw)]) + raw
    return b"\xd9" + bytes([len(raw)]) + raw


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


def graph_id(n: int, *, variant: bool = False) -> str:
    """
    Canonical (`…VjI=`) or variant (trailing nil, `…VjLA`) spelling of
    synthetic transcript `n` - the same envelope as the two real production
    spellings the unit suite reproduces byte for byte. The FORMAT is the
    subject under test; the contents are invented.
    """
    thread = f"19:{uuid.uuid5(uuid.NAMESPACE_URL, f'thread{n}').hex}@thread.tacv2"
    fields = [_mp_uint(4), _mp_str(thread), _mp_str(str(1_900_000_000_000 + n)),
              _mp_str(f"{uuid.uuid5(uuid.NAMESPACE_URL, f'tr{n}')}-{1790000000 + n}"
                      "-TranscriptV2")]
    if variant:
        fields.append(b"\xc0")
    payload = bytes([0x90 | len(fields)]) + b"".join(fields)
    block = _lz4_literal_block(payload)
    envelope = (b"\x92" + _ext(98, _mp_uint(len(payload)))
                + b"\xc6" + len(block).to_bytes(4, "big") + block)
    return base64.urlsafe_b64encode(envelope).decode("ascii")


# ---------------------------------------------------------------------------
# synthetic WebVTT
# ---------------------------------------------------------------------------

def _stamp(ms):
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def webvtt(cues) -> str:
    """`cues` is [(start_ms, end_ms, speaker, text)]. No production text."""
    blocks = ["WEBVTT", ""]
    for index, (start, end, speaker, text) in enumerate(cues, start=1):
        blocks += [str(index), f"{_stamp(start)} --> {_stamp(end)}",
                   f"<v {speaker}>{text}</v>", ""]
    return "\n".join(blocks)


TRAINER = "Trainer Synthetic"


def default_cues(count=6, speaker=TRAINER):
    return [(i * 60_000, i * 60_000 + 30_000, speaker if i % 3 else f"Synthetic Learner {i}",
             f"Synthetic line {i}.") for i in range(count)]


# ---------------------------------------------------------------------------
# the generic inserter
# ---------------------------------------------------------------------------

_COLUMNS: dict = {}


def columns(connection, table):
    if table not in _COLUMNS:
        _COLUMNS[table] = connection.execute("""
            SELECT column_name, data_type, is_nullable = 'NO', column_default IS NOT NULL
              FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = %s""", (table,)).fetchall()
    return _COLUMNS[table]


def _filler(name, data_type):
    if name.endswith("sha256") or name.endswith("fingerprint"):
        return sha(f"{name}{uuid.uuid4()}")
    return {
        "uuid": lambda: uuid.uuid4(), "text": lambda: "seed",
        "integer": lambda: 0, "bigint": lambda: 0, "smallint": lambda: 0,
        "numeric": lambda: 0, "boolean": lambda: False, "date": lambda: DAY,
        "timestamp with time zone": lambda: START,
        "jsonb": lambda: {}, "ARRAY": lambda: [],
    }[data_type]()


def insert(connection, table, **values):
    """INSERT, filling every NOT NULL column the caller did not name."""
    cols = columns(connection, table)
    assert cols, f"no such table {table}"
    types = {name: data_type for name, data_type, _, _ in cols}
    for name, data_type, required, has_default in cols:
        if required and not has_default and name not in values:
            values[name] = _filler(name, data_type)
    names = list(values)
    params = [Jsonb(values[n]) if types[n] == "jsonb" else values[n] for n in names]
    connection.execute(
        f"INSERT INTO public.{table} ({', '.join(chr(34) + n + chr(34) for n in names)}) "
        f"VALUES ({', '.join(['%s'] * len(names))})", params)
    return values


# ---------------------------------------------------------------------------
# a lecture and its transcript evidence
# ---------------------------------------------------------------------------

def seed_lecture(connection, *, transcript_id=None, meeting_id=None, subject=None,
                 artifacts=None, session_date=DAY, start=None, downstream_ready=True,
                 select=True, n=None):
    """One canonical lecture with its artifact(s) and, by default, a selection."""
    n = uuid.uuid4().int % 100000 if n is None else n
    transcript_id = transcript_id or graph_id(n)
    meeting_id = meeting_id or f"MTG-{n}-{uuid.uuid4().hex[:8]}"
    subject = subject or f"Synthetic Lecture {n}"
    start = start or datetime.combine(session_date, START.timetz())
    lecture_id = uuid.uuid4()
    insert(connection, "lecture_sessions", lecture_id=lecture_id,
           calendar_user_upn="calendar@example.invalid",
           calendar_event_id=f"evt-{lecture_id}",
           join_url=f"https://example.invalid/{lecture_id}",
           subject=subject, normalized_subject=subject.lower(), module=subject,
           scheduled_start=start, scheduled_end=start + timedelta(hours=2),
           session_date=session_date, calendar_mapping_status="RESOLVED",
           group_match_status="MATCHED", discovery_status="READY",
           meeting_id=meeting_id, downstream_ready=downstream_ready,
           meeting_lookup_user_id="organizer",
           meeting_lookup_context_source="DISCOVERY_MAILBOX_OBJECT_ID")
    artifact_ids = []
    for raw in (artifacts or [transcript_id]):
        artifact_id = uuid.uuid4()
        insert(connection, "lecture_transcript_artifacts", artifact_id=artifact_id,
               provider_transcript_id=raw, meeting_id=meeting_id,
               meeting_lookup_user_id="organizer", artifact_status="DISCOVERED",
               provider_created_at=start, provider_end_at=start + timedelta(hours=2))
        insert(connection, "lecture_transcript_candidates", candidate_id=uuid.uuid4(),
               lecture_id=lecture_id, artifact_id=artifact_id, meeting_id=meeting_id,
               meeting_lookup_user_id="organizer")
        artifact_ids.append(artifact_id)
    lecture = {"lecture_id": lecture_id, "transcript_id": transcript_id,
               "meeting_id": meeting_id, "subject": subject, "start": start,
               "session_date": session_date, "artifact_ids": artifact_ids,
               "selection_id": None}
    if select:
        lecture["selection_id"] = seed_selection(connection, lecture)
    return lecture


def seed_selection(connection, lecture, *, status="SELECTED"):
    selection_id = uuid.uuid4()
    selected = status == "SELECTED"
    insert(connection, "lecture_transcript_selections", selection_id=selection_id,
           lecture_id=lecture["lecture_id"], selection_version=SELECTION_VERSION,
           selection_status=status,
           primary_artifact_id=lecture["artifact_ids"][0] if selected else None,
           primary_provider_transcript_id=lecture["transcript_id"] if selected else None,
           selected_part_count=1 if selected else 0,
           actual_start=lecture["start"],
           actual_end=lecture["start"] + timedelta(hours=2),
           start_difference_minutes=0, end_difference_minutes=0)
    if selected:
        insert(connection, "lecture_transcript_selection_parts", selection_id=selection_id,
               artifact_id=lecture["artifact_ids"][0], part_index=1,
               provider_transcript_id=lecture["transcript_id"])
    return selection_id


def seed_transcript_document(connection, lecture, *, cues=None, parser_version=PARSER_VERSION,
                             label="d1"):
    """Combined transcript + canonical document + cues + one trainer speaker."""
    cues = cues if cues is not None else default_cues()
    content = webvtt(cues)
    combined_id, document_id = uuid.uuid4(), uuid.uuid4()
    insert(connection, "lecture_combined_transcripts", combined_id=combined_id,
           selection_id=lecture["selection_id"], lecture_id=lecture["lecture_id"],
           selection_version=SELECTION_VERSION, combined_content=content,
           content_sha256=sha(content), content_bytes=len(content),
           duration_seconds=7200, duration_minutes=120, parts_combined=1)
    insert(connection, "lecture_transcript_documents", document_id=document_id,
           lecture_id=lecture["lecture_id"], selection_id=lecture["selection_id"],
           combined_id=combined_id, parser_version=parser_version,
           source_content_sha256=sha(content), parse_status="PARSED",
           cue_count=len(cues), first_cue_start_ms=cues[0][0],
           last_cue_end_ms=cues[-1][1], duration_ms=cues[-1][1] - cues[0][0],
           source_fingerprint=sha(f"doc{lecture['lecture_id']}{label}{parser_version}"))
    for index, (start, end, speaker, text) in enumerate(cues, start=1):
        insert(connection, "lecture_transcript_cues", cue_id=uuid.uuid4(),
               document_id=document_id, cue_index=index, start_ms=start, end_ms=end,
               text=text, speaker_label_raw=speaker, cue_text_sha256=sha(f"{text}{index}"))
    speaker_id = uuid.uuid4()
    insert(connection, "lecture_transcript_speakers", speaker_id=speaker_id,
           document_id=document_id, speaker_inventory_version=SPEAKER_INVENTORY_VERSION,
           speaker_label_raw=TRAINER, speaker_label_normalized=TRAINER.lower(),
           cue_count=len(cues), first_cue_index=1, last_cue_index=len(cues),
           first_spoken_start_ms=cues[0][0], last_spoken_end_ms=cues[-1][1],
           gross_spoken_ms=30_000 * len(cues))
    return {"combined_id": combined_id, "document_id": document_id,
            "speaker_id": speaker_id, "content": content, "cues": cues}


# ---------------------------------------------------------------------------
# attendance, engagement and the external sources
# ---------------------------------------------------------------------------

def seed_attendance_source(connection, lecture, *, present=3, absent=0,
                           status="Attended"):
    """Rows in the externally owned public.kbc_attendance. Invented people."""
    for i in range(present + absent):
        insert(connection, "kbc_attendance", ID=f"L{i + 1}",
               FullName=f"Synthetic Learner {i + 1}",
               Email=f"learner{i + 1}@example.invalid",
               Attendance=1 if i < present else 0, module=lecture["subject"],
               date=lecture["session_date"], attendance_status=status)


def seed_lms_source(connection, lecture, *, students=2, program_status="Active"):
    """Rows in the externally owned public.kbc_users_data."""
    for i in range(students):
        insert(connection, "kbc_users_data", ID=f"S{i + 1}",
               FullName=f"Synthetic Student {i + 1}", Group=lecture["subject"],
               **{"Program-Status": program_status})


def seed_attendance_snapshot(connection, lecture, *, source_rows=3, present_rows=3,
                             members=3, any_status=None, label="s1"):
    snapshot_id = uuid.uuid4()
    insert(connection, "lecture_attendance_snapshots", snapshot_id=snapshot_id,
           lecture_id=lecture["lecture_id"],
           attendance_resolution_version=REQUIRED_ATTENDANCE_ROSTER_VERSION,
           session_date=lecture["session_date"], module=lecture["subject"],
           module_normalized=lecture["subject"].lower(), source_row_count=source_rows,
           present_row_count=present_rows, effective_member_count=members,
           source_fingerprint=sha(f"snap{lecture['lecture_id']}{label}"),
           metadata=({"source_rows_any_status": any_status}
                     if any_status is not None else {}))
    return snapshot_id


def seed_engagement(connection, lecture, document, snapshot_id, *, attended=3, spoke=1):
    engagement_id = uuid.uuid4()
    insert(connection, "lecture_transcript_speaker_roles", role_id=uuid.uuid4(),
           speaker_id=document["speaker_id"], role_algorithm_version=ROLE_ALGORITHM_VERSION,
           resolver_version=RESOLVER_VERSION, attendance_snapshot_id=snapshot_id,
           role="TRAINER_CANDIDATE", role_source="TOP_SPEAKER", role_rank=1)
    insert(connection, "lecture_engagement_metrics", engagement_id=engagement_id,
           lecture_id=lecture["lecture_id"], document_id=document["document_id"],
           attendance_snapshot_id=snapshot_id,
           engagement_algorithm_version=ENGAGEMENT_ALGORITHM_VERSION,
           trainer_exclusion_version="seed", resolver_version=RESOLVER_VERSION,
           role_algorithm_version=ROLE_ALGORITHM_VERSION,
           calculation_status="CALCULATED" if attended else "NO_ATTENDED_LEARNERS",
           trainer_exclusion_status="TRAINER_NOT_IN_ATTENDANCE",
           attendance_before_trainer_exclusion=attended, attended_count=attended,
           spoke_count=min(spoke, attended), silent_count=attended - min(spoke, attended),
           resolved_learner_speaker_count=min(spoke, attended),
           engagement_percentage=(Decimal(spoke * 100) / attended if attended
                                  else Decimal("0.00")),
           engagement_score=5 if attended else None,
           learner_engagement_status="Met" if attended else None,
           item7_override_applied=bool(attended),
           trainer_speaker_id=document["speaker_id"],
           source_fingerprint=sha(f"eng{lecture['lecture_id']}{snapshot_id}"))
    return engagement_id


def seed_qa_inputs(connection, *, source_rows=3, present_rows=3, members=3,
                   any_status=None, **lecture_kwargs):
    """Everything Phase 3A's input loader reads, end to end."""
    lecture = seed_lecture(connection, **lecture_kwargs)
    document = seed_transcript_document(connection, lecture)
    snapshot_id = seed_attendance_snapshot(
        connection, lecture, source_rows=source_rows, present_rows=present_rows,
        members=members, any_status=any_status)
    engagement_id = seed_engagement(connection, lecture, document, snapshot_id,
                                    attended=members)
    return {**lecture, **document, "snapshot_id": snapshot_id,
            "engagement_id": engagement_id}


# ---------------------------------------------------------------------------
# QA evaluations and the rendered legacy payload
# ---------------------------------------------------------------------------

def seed_evaluation(connection, lecture, *, qa_status="COMPLETED",
                    delivery_status="DELIVERED", label="e1", **extra):
    evaluation_id = uuid.uuid4()
    insert(connection, "lecture_qa_evaluations", evaluation_id=evaluation_id,
           lecture_id=lecture["lecture_id"], qa_status=qa_status,
           delivery_status=delivery_status,
           attendance_roster_version=REQUIRED_ATTENDANCE_ROSTER_VERSION,
           source_fingerprint=sha(f"eval{lecture['lecture_id']}{label}"), **extra)
    return evaluation_id


def seed_render(connection, lecture, *, session_id=None, label="r1", evaluation_id=None,
                renderer_version=RENDERER_VERSION, render_status="RENDERED",
                statuses=None, **overrides):
    """A rendered legacy payload with its eleven checklist rows."""
    session_id = session_id or lecture["transcript_id"]
    evaluation_id = evaluation_id or seed_evaluation(connection, lecture, label=label)
    rendered_id = uuid.uuid4()
    fields = dict(
        session_id=session_id, meeting_id=lecture["meeting_id"],
        subject=lecture["subject"], trainer=TRAINER,
        legacy_date=lecture["session_date"],
        canonical_session_date=lecture["session_date"],
        duration="2 hours 0 minutes", duration_score=5,
        engagement=Decimal("35.71"), engagement_score=2, attended_count=3,
        met_count=10, partial_count=0, not_met_count=1, teaching_quality_rating=4,
        teaching_quality_comments="Clear.", overall_judgement="Solid.",
        cancelled_session=False, lms_module=lecture["subject"], lms_students_count=1,
        lms_students={"students": [{"ID": "S1", "FullName": "Synthetic Student 1"}]},
        strengths={"strength_1": {"title": "a"}},
        areas_for_development={"area_1": {"title": "b"}},
        ksb_coverage={"ksb_1": {"type": "Skill", "title": "c"}})
    fields.update(overrides)
    insert(connection, "lecture_qa_rendered_sessions", rendered_session_id=rendered_id,
           lecture_id=lecture["lecture_id"], evaluation_id=evaluation_id,
           renderer_version=renderer_version, render_status=render_status,
           source_fingerprint=sha(f"render{lecture['lecture_id']}{label}"), **fields)
    statuses = {11: "Not Met"} if statuses is None else statuses
    for order in range(1, 12):
        insert(connection, "lecture_qa_rendered_checklist_items",
               rendered_item_id=uuid.uuid4(), rendered_session_id=rendered_id,
               checklist_order=order, checklist_item=CHECKLIST_ITEMS[order - 1],
               status=statuses.get(order, "Met"), status_source="AI",
               session_id=session_id, session_id_match=f"{session_id}_{order}",
               evidence=f"synthetic evidence {order}")
    return {"evaluation_id": evaluation_id, "rendered_session_id": rendered_id,
            "session_id": session_id}


# ---------------------------------------------------------------------------
# the legacy compatibility surface
# ---------------------------------------------------------------------------

def seed_legacy_row(connection, session_id, *, meeting_id, day=DAY,
                    subject="Synthetic Lecture", trainer="n8n trainer",
                    recording_url="https://recording.invalid/x", **extra):
    """A legacy row the coded platform did NOT write: n8n's."""
    columns_ = dict(session_id=session_id, meeting_id=meeting_id, date=day,
                    subject=subject, trainer=trainer, met_count=9,
                    recording_url=recording_url, positive_clips=[], **extra)
    return insert(connection, "qa_doctors_sessions", **columns_)


def legacy_rows(connection, meeting_id):
    return connection.execute(
        "SELECT session_id, trainer, met_count, recording_url "
        "  FROM public.qa_doctors_sessions WHERE meeting_id = %s "
        " ORDER BY session_id", (meeting_id,)).fetchall()


def legacy_fingerprint(connection):
    """Whole-table fingerprint of the legacy QA surface."""
    return connection.execute("""
        SELECT (SELECT count(*) FROM public.qa_doctors_sessions),
               (SELECT md5(coalesce(string_agg(q::text, '|' ORDER BY session_id), ''))
                  FROM public.qa_doctors_sessions q),
               (SELECT count(*) FROM public.qa_doctors_checklist_items),
               (SELECT count(*) FROM public.qa_perfect_lectures)""").fetchone()
