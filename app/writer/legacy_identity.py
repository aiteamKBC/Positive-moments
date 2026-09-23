"""
Which existing `qa_doctors_sessions` row, if any, already represents THIS
lecture occurrence.

THE INVARIANT
-------------
One canonical lecture occurrence <-> at most one `qa_doctors_sessions` row.

Before this module, nothing enforced it. The writer looked for its target by
exact `session_id`, and the `session_id` is the raw Graph transcript id. When
Graph re-serialized its transcript ids on 2026-09-22, the same lecture arrived
with a different `session_id`, the exact lookup found nothing, the writer
decided WOULD_INSERT, and three lectures gained a second row beside n8n's.

WHY ONE MODULE
--------------
Two callers must reach the same answer: the writer, which decides what to
write, and `PipelineStateResolver._legacy_qa_sync`, which decides whether the
scheduler should even ask. If they disagree, the scheduler offers work the
writer refuses (noise) or the resolver hides work the writer would do
(corruption). Both call `LegacyOccurrenceGuard.evaluate`, so they cannot.

HOW AN EXISTING ROW IS MATCHED
------------------------------
Candidates are the legacy rows on this lecture's dates - the canonical session
date and every legacy UTC date its renders carry - plus any row the coded
writer already owns for this lecture on any date. Each is classified:

  SAME        its transcript is one of THIS lecture's transcripts, compared by
              canonical identity (both Graph serializations match), or the
              coded writer already owns it for THIS lecture_id;
  DIFFERENT   a different meeting, or a row the coded writer owns for ANOTHER
              lecture;
  UNRESOLVED  the same meeting on the same date, but no proof either way, or a
              transcript match that another lecture already owns.

Subject is never used: two lectures can share a subject, and one lecture's
subject can be rewritten.

THE VERDICT
-----------
  CLEAR                    nothing represents this occurrence: insert is safe;
  FOREIGN_SAME_OCCURRENCE  exactly one foreign row does: PROTECTED;
  OWNED_SAME_OCCURRENCE    exactly one coded row owned by this lecture does:
                           that row is the target - never a second row;
  AMBIGUOUS                anything else: a human decides.

Every lookup here is a SELECT.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.common.errors import DATABASE_ERROR, PlatformError
from app.transcripts.identity import canonical_key


SAME = "SAME"
DIFFERENT = "DIFFERENT"
UNRESOLVED = "UNRESOLVED"

CLEAR = "CLEAR"
FOREIGN_SAME_OCCURRENCE = "FOREIGN_SAME_OCCURRENCE"
OWNED_SAME_OCCURRENCE = "OWNED_SAME_OCCURRENCE"
AMBIGUOUS = "AMBIGUOUS"

# Why a candidate was classified the way it was. Reported, so an operator
# looking at a refusal can see the evidence rather than just the conclusion.
BASIS_TRANSCRIPT_IDENTITY = "TRANSCRIPT_IDENTITY"
BASIS_CODED_OWNERSHIP = "CODED_OWNERSHIP_THIS_LECTURE"
BASIS_OWNED_BY_OTHER_LECTURE = "CODED_OWNERSHIP_OTHER_LECTURE"
BASIS_IDENTITY_OWNED_ELSEWHERE = "TRANSCRIPT_IDENTITY_OWNED_BY_OTHER_LECTURE"
BASIS_SAME_MEETING_UNPROVEN = "SAME_MEETING_SAME_DATE_UNPROVEN"
BASIS_DIFFERENT_MEETING = "DIFFERENT_MEETING"


@dataclass(frozen=True)
class Candidate:
    session_id: str
    meeting_id: str | None
    date: object
    owner_lecture_id: str | None


@dataclass(frozen=True)
class Match:
    session_id: str
    relation: str
    basis: str
    owner_lecture_id: str | None


@dataclass
class OccurrenceVerdict:
    verdict: str
    target_session_id: str | None = None
    matches: list = field(default_factory=list)

    @property
    def blocks_insert(self) -> bool:
        return self.verdict != CLEAR

    def as_dict(self) -> dict:
        # Session ids are reported by their last 12 characters only: enough to
        # tell rows apart in a log, and they are long provider identifiers.
        return {
            "same_occurrence_verdict": self.verdict,
            "same_occurrence_session_id": self.target_session_id,
            "same_occurrence_matches": [
                {"session_id_tail": m.session_id[-12:], "relation": m.relation,
                 "basis": m.basis, "coded_owner_lecture_id": m.owner_lecture_id}
                for m in self.matches if m.relation != DIFFERENT],
        }


def classify(*, lecture_id, meeting_id, own_transcript_keys, candidates) -> OccurrenceVerdict:
    """
    Pure decision over already-loaded facts. No I/O, so it can be tested
    exhaustively and shared by the writer and the resolver unchanged.
    """
    lecture_id = str(lecture_id)
    matches = []
    for candidate in candidates:
        owner = str(candidate.owner_lecture_id) if candidate.owner_lecture_id else None
        identity_match = canonical_key(candidate.session_id) in own_transcript_keys
        if identity_match and owner and owner != lecture_id:
            relation, basis = UNRESOLVED, BASIS_IDENTITY_OWNED_ELSEWHERE
        elif identity_match:
            relation, basis = SAME, BASIS_TRANSCRIPT_IDENTITY
        elif owner == lecture_id:
            relation, basis = SAME, BASIS_CODED_OWNERSHIP
        elif owner:
            relation, basis = DIFFERENT, BASIS_OWNED_BY_OTHER_LECTURE
        elif meeting_id and candidate.meeting_id and candidate.meeting_id == meeting_id:
            relation, basis = UNRESOLVED, BASIS_SAME_MEETING_UNPROVEN
        else:
            relation, basis = DIFFERENT, BASIS_DIFFERENT_MEETING
        matches.append(Match(candidate.session_id, relation, basis, owner))

    same = [m for m in matches if m.relation == SAME]
    unresolved = [m for m in matches if m.relation == UNRESOLVED]
    if not same and not unresolved:
        return OccurrenceVerdict(CLEAR, None, matches)
    if len(same) == 1 and not unresolved:
        only = same[0]
        verdict = (OWNED_SAME_OCCURRENCE if only.owner_lecture_id == lecture_id
                   else FOREIGN_SAME_OCCURRENCE)
        return OccurrenceVerdict(verdict, only.session_id, matches)
    return OccurrenceVerdict(AMBIGUOUS, None, matches)


# ---------------------------------------------------------------------------
# persistence (READ ONLY)
# ---------------------------------------------------------------------------

LOAD_LECTURE = """
SELECT l.meeting_id, l.session_date
  FROM public.lecture_sessions l
 WHERE l.lecture_id = %s
"""

# Every transcript this lecture has ever been selected under, plus every
# transcript its renders have targeted. A superset is the SAFE direction: it
# can only make more rows count as "this occurrence", never fewer.
LOAD_OWN_TRANSCRIPTS = """
SELECT p.provider_transcript_id
  FROM public.lecture_transcript_selection_parts p
  JOIN public.lecture_transcript_selections s ON s.selection_id = p.selection_id
 WHERE s.lecture_id = %s
UNION
SELECT s.primary_provider_transcript_id
  FROM public.lecture_transcript_selections s
 WHERE s.lecture_id = %s AND s.primary_provider_transcript_id IS NOT NULL
UNION
SELECT r.session_id
  FROM public.lecture_qa_rendered_sessions r
 WHERE r.lecture_id = %s AND r.session_id IS NOT NULL
"""

LOAD_LEGACY_DATES = """
SELECT DISTINCT r.legacy_date
  FROM public.lecture_qa_rendered_sessions r
 WHERE r.lecture_id = %s AND r.legacy_date IS NOT NULL
"""

LOAD_CANDIDATES = """
SELECT q.session_id, q.meeting_id, q.date, w.lecture_id
  FROM public.qa_doctors_sessions q
  LEFT JOIN public.lecture_qa_legacy_writes w
         ON w.legacy_session_id = q.session_id
        AND w.writer_version = %s
        AND w.write_status <> 'ROLLED_BACK'
 WHERE q.date = ANY(%s::date[])
   AND q.session_id <> %s
UNION
SELECT q.session_id, q.meeting_id, q.date, w.lecture_id
  FROM public.lecture_qa_legacy_writes w
  JOIN public.qa_doctors_sessions q ON q.session_id = w.legacy_session_id
 WHERE w.lecture_id = %s
   AND w.writer_version = %s
   AND w.write_status <> 'ROLLED_BACK'
   AND q.session_id <> %s
"""


class LegacyOccurrenceRepository:
    """The facts `classify` needs, loaded with SELECTs only."""

    def load(self, connection, *, lecture_id, session_id, writer_version) -> dict:
        lecture_id = str(lecture_id)
        try:
            lecture = connection.execute(LOAD_LECTURE, (lecture_id,)).fetchone()
            own = connection.execute(
                LOAD_OWN_TRANSCRIPTS, (lecture_id, lecture_id, lecture_id)).fetchall()
            legacy_dates = connection.execute(LOAD_LEGACY_DATES, (lecture_id,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "occurrence context read failed") from exc
        meeting_id = lecture[0] if lecture else None
        dates = sorted({str(value) for value in
                        ([lecture[1]] if lecture and lecture[1] else [])
                        + [row[0] for row in legacy_dates]
                        if value})
        try:
            rows = connection.execute(LOAD_CANDIDATES, (
                writer_version, dates, session_id,
                lecture_id, writer_version, session_id)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "occurrence candidate read failed") from exc
        return {
            "meeting_id": meeting_id,
            "own_transcript_keys": ({canonical_key(row[0]) for row in own if row[0]}
                                    | {canonical_key(session_id)}),
            "candidates": [Candidate(row[0], row[1], row[2],
                                     str(row[3]) if row[3] else None)
                           for row in rows],
        }


class LegacyOccurrenceGuard:
    """The single entry point both the writer and the resolver call."""

    def __init__(self, repository=None):
        self.repository = repository or LegacyOccurrenceRepository()

    def evaluate(self, connection, *, lecture_id, session_id,
                 writer_version) -> OccurrenceVerdict:
        facts = self.repository.load(connection, lecture_id=lecture_id,
                                     session_id=session_id,
                                     writer_version=writer_version)
        return classify(lecture_id=lecture_id, meeting_id=facts["meeting_id"],
                        own_transcript_keys=facts["own_transcript_keys"],
                        candidates=facts["candidates"])
