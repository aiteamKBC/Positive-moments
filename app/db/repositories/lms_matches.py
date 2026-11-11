"""
Persistence for LMS match decisions and LMS-side coverage (migration 025).

Decision history
----------------
* One row per DISTINCT decision; re-evaluating to the same decision only
  bumps last_evaluated_at / evaluation_count.
* A VERIFIED decision is never re-pointed automatically. If a later
  evaluation disagrees, the new current row is STALE (review), anchored to the
  verified decision; it returns to VERIFIED only when the LMS again yields the
  identical decision.
* OUT_OF_LMS_SCOPE is an operator decision: only a VERIFIED result replaces it.
* An LMS occurrence belongs to at most one lecture's current decision. A
  VERIFIED result naming an occurrence another lecture holds becomes CONFLICT.

Concurrency
-----------
`record()` runs in its own (sub)transaction, takes a transaction-scoped
advisory lock on the lecture and then on each named occurrence (sorted, so two
writers cannot deadlock), and relies on the partial unique indexes as the
backstop. Nothing here stores a URL, join link or Graph meeting id.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from app.common.errors import DATABASE_ERROR, PlatformError
from app.lms.matching import CONFLICT, OUT_OF_LMS_SCOPE, PRIMARY, STALE, VERIFIED, MatchResult
from app.orchestration.locks import lock_key

LECTURE_LOCK_NAMESPACE = 0x4B4C_4D31      # "KLM1": one LMS-match writer per lecture
OCCURRENCE_LOCK_NAMESPACE = 0x4B4C_4D32   # "KLM2": one claimant per LMS occurrence

EVALUATION_FIELDS = ("evaluation_id", "lecture_id", "rule_version", "match_status", "requires_review",
                     "is_current", "lecture_scheduled_start", "meeting_key_hash", "candidate_count",
                     "decision_sha256", "evidence", "first_evaluated_at", "last_evaluated_at",
                     "evaluation_count", "superseded_at", "superseded_by")

INSERT_EVALUATION = f"""
INSERT INTO public.bunny_lms_match_evaluations
    (evaluation_id, lecture_id, rule_version, match_status, requires_review, is_current,
     lecture_scheduled_start, meeting_key_hash, candidate_count, decision_sha256, evidence)
VALUES (%s, %s, %s, %s, %s, true, %s, %s, %s, %s, %s)
"""
INSERT_LINK = """
INSERT INTO public.bunny_lms_occurrence_links
    (evaluation_id, lecture_id, match_status, is_current, lms_occurrence_id, lms_live_session_id,
     lms_module_catalogue_id, lms_session_number, lms_scheduled_start, link_role, shared_source_module_id)
VALUES (%s, %s, 'VERIFIED', true, %s, %s, %s, %s, %s, %s, %s)
"""
UPSERT_COVERAGE = """
INSERT INTO public.bunny_lms_occurrence_coverage AS c
    (lms_occurrence_id, lms_live_session_id, lms_module_catalogue_id, lms_programme_name,
     lms_series_status, lms_meeting_type, lms_occurrence_status, lms_scheduled_start, lms_scheduled_end,
     meeting_key_hash, rule_version, scope_status, coverage_status, matched_lecture_id, evidence)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (lms_occurrence_id) DO UPDATE SET
    lms_live_session_id = EXCLUDED.lms_live_session_id,
    lms_module_catalogue_id = EXCLUDED.lms_module_catalogue_id,
    lms_programme_name = EXCLUDED.lms_programme_name,
    lms_series_status = EXCLUDED.lms_series_status,
    lms_meeting_type = EXCLUDED.lms_meeting_type,
    lms_occurrence_status = EXCLUDED.lms_occurrence_status,
    lms_scheduled_start = EXCLUDED.lms_scheduled_start,
    lms_scheduled_end = EXCLUDED.lms_scheduled_end,
    meeting_key_hash = EXCLUDED.meeting_key_hash,
    rule_version = EXCLUDED.rule_version,
    scope_status = EXCLUDED.scope_status,
    coverage_status = EXCLUDED.coverage_status,
    matched_lecture_id = EXCLUDED.matched_lecture_id,
    evidence = EXCLUDED.evidence,
    last_seen_at = now(),
    status_changed_at = CASE WHEN c.coverage_status IS DISTINCT FROM EXCLUDED.coverage_status
                             THEN now() ELSE c.status_changed_at END
RETURNING (xmax = 0)
"""


@dataclass(frozen=True)
class RecordOutcome:
    evaluation_id: str
    status: str
    changed: bool
    previous_status: str | None


def _db(message):
    return PlatformError(DATABASE_ERROR, message)


class LmsMatchRepository:

    # -- reads ---------------------------------------------------------------
    def current(self, connection, lecture_id) -> dict | None:
        row = connection.execute(
            f"SELECT {', '.join(EVALUATION_FIELDS)} FROM public.bunny_lms_match_evaluations "
            "WHERE lecture_id = %s AND is_current", (lecture_id,)).fetchone()
        return dict(zip(EVALUATION_FIELDS, row)) if row else None

    def evaluation(self, connection, evaluation_id) -> dict | None:
        row = connection.execute(
            f"SELECT {', '.join(EVALUATION_FIELDS)} FROM public.bunny_lms_match_evaluations "
            "WHERE evaluation_id = %s", (evaluation_id,)).fetchone()
        return dict(zip(EVALUATION_FIELDS, row)) if row else None

    def links(self, connection, evaluation_id) -> list[dict]:
        fields = ("lms_occurrence_id", "lms_live_session_id", "lms_module_catalogue_id", "lms_session_number",
                  "lms_scheduled_start", "link_role", "shared_source_module_id", "is_current")
        rows = connection.execute(
            f"SELECT {', '.join(fields)} FROM public.bunny_lms_occurrence_links WHERE evaluation_id = %s "
            "ORDER BY link_role DESC, lms_occurrence_id", (evaluation_id,)).fetchall()
        return [dict(zip(fields, r)) for r in rows]

    def current_occurrence_owners(self, connection, occurrence_ids=None) -> dict:
        """occurrence_id -> lecture_id for every current VERIFIED link."""
        if occurrence_ids is None:
            rows = connection.execute("SELECT lms_occurrence_id, lecture_id FROM public.bunny_lms_occurrence_links "
                                      "WHERE is_current").fetchall()
        else:
            rows = connection.execute("SELECT lms_occurrence_id, lecture_id FROM public.bunny_lms_occurrence_links "
                                      "WHERE is_current AND lms_occurrence_id = ANY(%s)",
                                      (list(occurrence_ids),)).fetchall()
        return {r[0]: str(r[1]) for r in rows}

    # -- writes --------------------------------------------------------------
    def record(self, connection, result: MatchResult, *, lecture_scheduled_start) -> RecordOutcome:
        try:
            with connection.transaction():
                return self._record(connection, result, lecture_scheduled_start)
        except psycopg.errors.UniqueViolation:
            # Lost a race the advisory locks should have prevented: the
            # occurrence is now someone else's. Record that, visibly.
            with connection.transaction():
                return self._record(connection, self._conflict(result, holders={}), lecture_scheduled_start)

    def declare_out_of_scope(self, connection, lecture_id, *, lecture_scheduled_start, actor: str,
                             reason: str) -> RecordOutcome:
        result = MatchResult(lecture_id=str(lecture_id), status=OUT_OF_LMS_SCOPE,
                             evidence={"declared_by": actor, "reason": reason})
        with connection.transaction():
            self._lock(connection, LECTURE_LOCK_NAMESPACE, lecture_id)
            current = self._current_for_update(connection, lecture_id)
            return self._write(connection, result, current, lecture_scheduled_start)

    def upsert_coverage(self, connection, rows) -> dict:
        counts = {"inserted": 0, "updated": 0}
        with connection.transaction():
            for row in rows:
                o = row.occurrence
                inserted = connection.execute(UPSERT_COVERAGE, (
                    o.occurrence_id, o.live_session_id, o.module_catalogue_id, o.programme_name,
                    o.series_status, o.meeting_type, o.status, o.scheduled_start, o.scheduled_end,
                    o.meeting_key_hash, row.rule_version, row.scope_status, row.coverage_status,
                    row.matched_lecture_id, Jsonb(row.evidence))).fetchone()[0]
                counts["inserted" if inserted else "updated"] += 1
        return counts

    # -- internals -----------------------------------------------------------
    @staticmethod
    def _lock(connection, namespace, key) -> None:
        connection.execute("SELECT pg_advisory_xact_lock(%s, %s)", (namespace, lock_key(key)))

    def _current_for_update(self, connection, lecture_id) -> dict | None:
        row = connection.execute(
            f"SELECT {', '.join(EVALUATION_FIELDS)} FROM public.bunny_lms_match_evaluations "
            "WHERE lecture_id = %s AND is_current FOR UPDATE", (lecture_id,)).fetchone()
        return dict(zip(EVALUATION_FIELDS, row)) if row else None

    def _record(self, connection, result, scheduled_start) -> RecordOutcome:
        self._lock(connection, LECTURE_LOCK_NAMESPACE, result.lecture_id)
        current = self._current_for_update(connection, result.lecture_id)
        effective = self._effective(connection, result, current)
        if effective is None:                         # operator decision stands
            return self._touch(connection, current)
        if effective.status == VERIFIED:
            for occurrence_id in sorted(l.occurrence_id for l in effective.links):
                self._lock(connection, OCCURRENCE_LOCK_NAMESPACE, occurrence_id)
            holders = {o: lec for o, lec in self.current_occurrence_owners(
                connection, [l.occurrence_id for l in effective.links]).items() if lec != str(result.lecture_id)}
            if holders:
                effective = self._conflict(effective, holders)
        return self._write(connection, effective, current, scheduled_start)

    def _effective(self, connection, result, current) -> MatchResult | None:
        if current is None:
            return result
        status = current["match_status"]
        if status == OUT_OF_LMS_SCOPE:
            return result if result.status == VERIFIED else None
        if status == VERIFIED:
            return result if result.decision_sha256 == current["decision_sha256"] else \
                self._stale(connection, result, current)
        if status == STALE:
            anchor = self.evaluation(connection, current["evidence"].get("stale_from_evaluation_id"))
            if anchor is not None and result.decision_sha256 == anchor["decision_sha256"]:
                return result                         # the LMS came back to the verified answer
            return self._stale(connection, result, anchor or current)
        return result

    def _stale(self, connection, observed, anchor) -> MatchResult:
        previous = [l["lms_occurrence_id"] for l in self.links(connection, anchor["evaluation_id"])
                    if l["link_role"] == PRIMARY]
        return MatchResult(
            lecture_id=observed.lecture_id, status=STALE, meeting_key_hash=observed.meeting_key_hash,
            candidate_ids=observed.candidate_ids,
            evidence={"reason": "verified LMS match changed; review before re-pointing",
                      "stale_from_evaluation_id": str(anchor["evaluation_id"]),
                      "previous_primary_occurrence_id": previous[0] if previous else None,
                      "observed_status": observed.status,
                      "observed_candidates": list(observed.candidate_ids),
                      "observed_decision_sha256": observed.decision_sha256})

    @staticmethod
    def _conflict(result, holders: dict) -> MatchResult:
        return MatchResult(
            lecture_id=result.lecture_id, status=CONFLICT, meeting_key_hash=result.meeting_key_hash,
            candidate_ids=tuple(sorted(l.occurrence_id for l in result.links)) or result.candidate_ids,
            evidence={**result.evidence, "reason": "LMS occurrence already belongs to another lecture",
                      "held_by_lectures": dict(sorted(holders.items()))})

    def _touch(self, connection, current) -> RecordOutcome:
        connection.execute(
            "UPDATE public.bunny_lms_match_evaluations SET last_evaluated_at = now(), "
            "evaluation_count = evaluation_count + 1 WHERE evaluation_id = %s", (current["evaluation_id"],))
        return RecordOutcome(str(current["evaluation_id"]), current["match_status"], False, current["match_status"])

    def _write(self, connection, result, current, scheduled_start) -> RecordOutcome:
        if current is not None and current["decision_sha256"] == result.decision_sha256:
            return self._touch(connection, current)
        new_id = uuid.uuid4()
        if current is not None:
            # Flip first: the one-current indexes (lecture, occurrence) must be
            # free before the new decision and its links are inserted.
            connection.execute(
                "UPDATE public.bunny_lms_match_evaluations SET is_current = false, superseded_at = now() "
                "WHERE evaluation_id = %s AND is_current", (current["evaluation_id"],))
        connection.execute(INSERT_EVALUATION, (
            new_id, result.lecture_id, result.rule_version, result.status, result.requires_review,
            scheduled_start, result.meeting_key_hash, len(result.candidate_ids), result.decision_sha256,
            Jsonb({**result.evidence, "candidates": list(result.candidate_ids)})))
        if current is not None:
            connection.execute("UPDATE public.bunny_lms_match_evaluations SET superseded_by = %s "
                               "WHERE evaluation_id = %s", (new_id, current["evaluation_id"]))
        if result.status == VERIFIED:
            if sum(1 for l in result.links if l.role == PRIMARY) != 1:
                raise _db("a VERIFIED LMS match needs exactly one PRIMARY occurrence")
            for link in result.links:
                connection.execute(INSERT_LINK, (
                    new_id, result.lecture_id, link.occurrence_id, link.live_session_id,
                    link.module_catalogue_id, link.session_number, link.scheduled_start,
                    link.role, link.shared_source_module_id))
        return RecordOutcome(str(new_id), result.status, True,
                             current["match_status"] if current is not None else None)
