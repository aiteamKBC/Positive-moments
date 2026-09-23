"""
Phase 3C1 orchestration: plan, and (only under a write-enabled mode) perform
the legacy QA write.

Per lecture:

  1. load the Phase 3B rendered payload and check the checklist invariant;
  2. read the current legacy target rows and digest them;
  3. decide, through one policy function whose default is to refuse;
  4. in DRY_RUN, report a field-level diff and write nothing;
  5. in a write-enabled mode, write session + 11 checklist rows + the
     ownership record inside ONE transaction, verify by re-reading, and roll
     back the whole lecture if anything disagrees.

The writer never calls a model, never re-renders evidence, and never queries
LMS, attendance, Graph or transcripts. It persists an already-validated
payload, or it does nothing.
"""
import logging
import time
import uuid

from app.writer.mapping import (
    SESSION_COLUMNS,
    _canonical,
    WRITER_VERSION,
    PayloadInvariantError,
    checklist_rows,
    diff_checklist,
    diff_session,
    digest,
    payload_is_valid,
    session_row,
)
from app.rendering.compatibility import session_id_match
from app.writer.legacy_identity import (
    AMBIGUOUS,
    FOREIGN_SAME_OCCURRENCE,
    OWNED_SAME_OCCURRENCE,
    LegacyOccurrenceGuard,
)
from app.writer.modes import (
    ACTIONABLE_DECISIONS,
    BLOCKED_AMBIGUOUS_LEGACY_IDENTITY,
    BLOCKED_INVALID_PAYLOAD,
    CANARY_NEW_ONLY,
    BLOCKED_NOT_READY,
    DRY_RUN,
    INSERTING_DECISIONS,
    PROTECTED_EXISTING_LEGACY_ROW,
    WOULD_INSERT,
    WOULD_SKIP_IDENTICAL,
    WOULD_UPDATE,
    plan_decision,
    WriterModeError,
    validate_mode,
    writes_enabled,
)


# Field diff classifications for the historical comparison.
IDENTICAL = "IDENTICAL"
DIFFERENT_EXPECTED_AI = "DIFFERENT_EXPECTED_AI"
DIFFERENT_LMS_DRIFT = "DIFFERENT_LMS_DRIFT"
DIFFERENT_KNOWN_LEGACY_ITEM2_DEFECT = "DIFFERENT_KNOWN_LEGACY_ITEM2_DEFECT"
DIFFERENT_OTHER = "DIFFERENT_OTHER"

AI_FIELDS = frozenset({"met_count", "partial_count", "not_met_count", "ksb_coverage",
                       "strengths", "areas_for_development", "overall_judgement",
                       "teaching_quality_rating", "teaching_quality_comments"})
LMS_FIELDS = frozenset({"lms_students", "lms_students_count", "lms_module"})

WRITE_VERIFICATION_FAILED = "WRITE_VERIFICATION_FAILED"


class WriteVerificationError(RuntimeError):
    """The rows read back after a write do not match what was written."""


class LegacyQaWriter:
    def __init__(self, *, payload_repository, legacy_repository, ownership_repository,
                 mode: str = DRY_RUN, writer_version: str = WRITER_VERSION,
                 renderer_version: str = "legacy_qa_v8_renderer_v1",
                 allow_update_existing: bool = False, lecture_ids=None,
                 confirmed: bool = False, perfect_planner=None,
                 occurrence_guard=None):
        # An unknown mode is refused before anything is read.
        self.mode = validate_mode(mode)
        # Phase 3C2 defence in depth: the same guard the CLI applies, enforced
        # again here so a programmatic caller cannot write unconfirmed or
        # unscoped even if it bypasses the CLI.
        if writes_enabled(self.mode):
            if not lecture_ids:
                raise WriterModeError(
                    f"{self.mode} requires an explicit lecture scope")
            if self.mode == CANARY_NEW_ONLY and len(set(lecture_ids)) != 1:
                raise WriterModeError(
                    "CANARY_NEW_ONLY writes exactly one lecture")
            if not confirmed:
                raise WriterModeError(
                    f"{self.mode} requires an explicit write confirmation")
        self.confirmed = confirmed
        self.payload_repository = payload_repository
        self.legacy_repository = legacy_repository
        self.ownership_repository = ownership_repository
        self.writer_version = writer_version
        self.renderer_version = renderer_version
        # EXPLICIT_BACKFILL additionally requires this flag AND a lecture scope.
        self.allow_update_existing = allow_update_existing
        # Normalized to text: the repository returns UUID objects, while a
        # CLI scope arrives as strings.
        self.lecture_ids = {str(value) for value in lecture_ids} if lecture_ids else None
        # Phase 3C2.3D: the derived Perfect Lecture output. Optional, so the
        # QA writer keeps working unchanged when it is not supplied, and
        # planned in the same pass so ONE canary plan reports both targets.
        self.perfect_planner = perfect_planner
        # F-02. Always on: there is no configuration of this writer that finds
        # its target by exact session_id alone. Injectable for tests only.
        self.occurrence_guard = occurrence_guard or LegacyOccurrenceGuard()
        self.log = logging.getLogger(__name__)

    @property
    def writes_enabled(self) -> bool:
        return writes_enabled(self.mode)

    def plan_day(self, connection, target_date) -> dict:
        """Plan (and, under a write-enabled mode, perform) one day's writes."""
        started = time.monotonic()
        rendered = self.payload_repository.load_sessions(
            connection, target_date, self.renderer_version)
        if self.lecture_ids is not None:
            rendered = [row for row in rendered
                        if str(row["lecture_id"]) in self.lecture_ids]

        counters = {name: 0 for name in (
            "lectures_considered", "would_insert", "would_update", "would_skip_identical",
            "protected_existing_legacy_row", "blocked_not_ready", "blocked_invalid_payload",
            "blocked_backfill_not_authorised", "review_required",
            "sessions_written", "checklist_rows_written", "ownership_rows_written",
            "verification_failures", "rolled_back",
            "perfect_rows_written", "perfect_ownership_rows_written",
            "perfect_verification_failures", "perfect_superseded")}
        counters["lectures_considered"] = len(rendered)
        results = [self._plan_one(connection, row, counters) for row in rendered]

        summary = {
            "target_date": target_date.isoformat(), "mode": self.mode,
            "writer_version": self.writer_version,
            "renderer_version": self.renderer_version,
            "writes_enabled": self.writes_enabled,
            "allow_update_existing": self.allow_update_existing,
            "perfect_lecture_planned": self.perfect_planner is not None,
            **counters,
            # Boundary markers: the writer is a persistence adapter only.
            "provider_calls": 0, "graph_calls": 0, "lms_queries": 0,
            "attendance_queries": 0, "transcript_reselections": 0,
            "evidence_regenerations": 0, "engagement_recalculations": 0,
            "legacy_rows_written": counters["sessions_written"],
            "lectures": results,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
        # Counts and decisions only: no learner names, no evidence text.
        self.log.info("legacy qa writer completed", extra={"fields": {
            "service": "legacy_qa_writer", "operation": "plan_day", "mode": self.mode,
            "lectures": len(results), "protected": counters["protected_existing_legacy_row"],
            "written": counters["sessions_written"], "duration_ms": summary["duration_ms"],
        }})
        return summary

    def _plan_one(self, connection, rendered, counters) -> dict:
        items = self.payload_repository.load_items(
            connection, rendered["rendered_session_id"])
        valid, invariant_error = payload_is_valid(rendered, items)

        proposed_session_id = rendered["session_id"]
        session_id = proposed_session_id
        existing_session = (self.legacy_repository.load_session(connection, session_id)
                            if session_id else None)

        # F-02. The exact lookup found nothing - which is precisely when the
        # writer used to insert. Before that is allowed, ask whether some OTHER
        # row already represents this lecture occurrence under a different
        # serialization of the same transcript id.
        occurrence = None
        if session_id and existing_session is None:
            occurrence = self.occurrence_guard.evaluate(
                connection, lecture_id=rendered["lecture_id"],
                session_id=session_id, writer_version=self.writer_version)
            if occurrence.verdict == OWNED_SAME_OCCURRENCE:
                # Case C. Our own row, under the id it was first published
                # with. That published id is the legacy key and it stays: the
                # payload is re-addressed to it rather than a second row
                # being created beside it.
                rendered, items = _retarget(rendered, items,
                                            occurrence.target_session_id)
                session_id = rendered["session_id"]
                valid, invariant_error = payload_is_valid(rendered, items)
                existing_session = self.legacy_repository.load_session(
                    connection, session_id)
            elif occurrence.verdict == FOREIGN_SAME_OCCURRENCE:
                # Case B. Somebody else's row for this occurrence. Treated as
                # the target exactly as an exact-id foreign row would be, so the
                # ordinary ownership policy returns PROTECTED - in every mode.
                existing_session = self.legacy_repository.load_session(
                    connection, occurrence.target_session_id)

        protected_target = (occurrence is not None
                            and occurrence.verdict == FOREIGN_SAME_OCCURRENCE)
        existing_items = (self.legacy_repository.load_checklist(connection, session_id)
                          if session_id and not protected_target else [])
        ownership = (self.ownership_repository.find(connection, session_id, self.writer_version)
                     if session_id and not protected_target else None)

        decision = plan_decision(
            mode=self.mode, render_status=rendered["render_status"],
            qa_status=rendered["qa_status"], payload_valid=valid,
            target_exists=existing_session is not None,
            coded_owned=ownership is not None,
            fingerprint_matches=bool(ownership
                                     and ownership["source_fingerprint"]
                                     == rendered["source_fingerprint"]),
            allow_update_existing=self.allow_update_existing)
        if (occurrence is not None and occurrence.verdict == AMBIGUOUS
                and decision == WOULD_INSERT):
            # Case D. The readiness and payload refusals above still take
            # precedence; this only ever replaces what would have been an
            # insert, and it never guesses which candidate is the real one.
            decision = BLOCKED_AMBIGUOUS_LEGACY_IDENTITY

        counters[decision.lower()] = counters.get(decision.lower(), 0) + 1
        pre_digest = digest(existing_session, existing_items)

        result = {
            "subject": rendered["subject"], "lecture_id": str(rendered["lecture_id"]),
            "legacy_session_id": (occurrence.target_session_id if protected_target
                                  else session_id),
            "proposed_session_id": proposed_session_id,
            **(occurrence.as_dict() if occurrence is not None
               else {"same_occurrence_verdict": None}),
            "render_status": rendered["render_status"], "qa_status": rendered["qa_status"],
            "decision": decision, "coded_owned": ownership is not None,
            "legacy_row_exists": existing_session is not None,
            "source_fingerprint_prefix": (rendered["source_fingerprint"] or "")[:16],
            "pre_write_digest": pre_digest,
            "pre_write_digest_prefix": pre_digest[:16],
            "payload_invariant_error": invariant_error,
        }

        if not valid:
            return result

        proposed_session = session_row(rendered)
        proposed_items = checklist_rows(items, session_id)
        result["proposed_checklist_rows"] = len(proposed_items)
        result["proposed_digest_prefix"] = digest(proposed_session, proposed_items)[:16]

        if existing_session is not None:
            field_diff = diff_session(proposed_session, existing_session)
            result["field_diff"] = {
                column: _classify(column) for column in sorted(field_diff)}
            result["identical_field_count"] = len(SESSION_COLUMNS) - len(field_diff)
            result["checklist_diff"] = _summarise_checklist_diff(
                diff_checklist(proposed_items, existing_items))
        else:
            result["field_diff"] = {}
            result["identical_field_count"] = 0
            result["checklist_diff"] = {"status_difference_count": None,
                                        "evidence_difference_count": None,
                                        "item2_known_defect_difference": None}

        if decision in ACTIONABLE_DECISIONS and self.writes_enabled:
            result.update(self._write_one(connection, rendered, proposed_session,
                                          proposed_items, decision, pre_digest,
                                          ownership, counters))

        if self.perfect_planner is not None:
            # Option B: the QA rows above are already committed (or, in a dry
            # run, already decided) before the derived output is considered.
            # A Perfect Lecture problem never rolls back a correct QA write.
            result["perfect_lecture"] = self.perfect_planner.plan_one(
                connection, rendered, items, counters)
        return result

    def _write_one(self, connection, rendered, proposed_session, proposed_items,
                   decision, pre_digest, ownership, counters) -> dict:
        """
        Write one lecture atomically.

        Runs inside a SAVEPOINT so a failure - including a verification
        mismatch - rolls back this lecture's session row, its eleven checklist
        rows and its ownership record together, leaving no partial state, while
        other lectures in the same run are unaffected.
        """
        session_id = rendered["session_id"]
        try:
            with connection.transaction():
                self.legacy_repository.write_session(connection, proposed_session,
                                                     allow_write=True)
                self.legacy_repository.write_checklist(connection, proposed_items,
                                                       allow_write=True)
                written_session = self.legacy_repository.load_session(connection, session_id)
                written_items = self.legacy_repository.load_checklist(connection, session_id)
                self._verify(proposed_session, proposed_items, written_session, written_items)
                post_digest = digest(written_session, written_items)
                entry = {
                    "write_id": ownership["write_id"] if ownership else uuid.uuid4(),
                    "lecture_id": rendered["lecture_id"],
                    "evaluation_id": rendered["evaluation_id"],
                    "rendered_session_id": rendered["rendered_session_id"],
                    "legacy_session_id": session_id,
                    "writer_version": self.writer_version,
                    "source_fingerprint": rendered["source_fingerprint"],
                    "write_mode": self.mode,
                    "write_status": "WRITTEN" if decision == WOULD_INSERT else "UPDATED",
                    "legacy_session_created": decision == WOULD_INSERT,
                    "legacy_session_updated": decision == WOULD_UPDATE,
                    "checklist_rows_created": len(proposed_items)
                    if decision == WOULD_INSERT else 0,
                    "checklist_rows_updated": len(proposed_items)
                    if decision == WOULD_UPDATE else 0,
                    "pre_write_digest": pre_digest, "post_write_digest": post_digest,
                    "metadata": {"renderer_version": self.renderer_version,
                                 "decision": decision},
                }
                owner = self.ownership_repository.record(connection, entry)
        except WriteVerificationError as error:
            counters["verification_failures"] += 1
            counters["rolled_back"] += 1
            # The savepoint already rolled the lecture back; nothing partial
            # remains, and automated processing must stop for this lecture.
            return {"write_status": WRITE_VERIFICATION_FAILED, "write_error": str(error),
                    "rolled_back": True}

        counters["sessions_written"] += 1
        counters["checklist_rows_written"] += len(proposed_items)
        counters["ownership_rows_written"] += 1 if owner["created"] else 0
        return {"write_status": entry["write_status"], "write_id": str(owner["write_id"]),
                "post_write_digest": post_digest,
                "post_write_digest_prefix": post_digest[:16],
                "checklist_rows_written": len(proposed_items), "rolled_back": False}

    def _verify(self, proposed_session, proposed_items, written_session, written_items) -> None:
        """Re-read verification: no mismatch may be silently accepted."""
        if written_session is None:
            raise WriteVerificationError("session row missing after write")
        for column in SESSION_COLUMNS:
            if _canonical(written_session.get(column)) != _canonical(proposed_session[column]):
                raise WriteVerificationError(f"session column mismatch: {column}")
        if len(written_items) != len(proposed_items):
            raise WriteVerificationError(
                f"expected {len(proposed_items)} checklist rows, read {len(written_items)}")
        by_key = {row["session_id_match"]: row for row in written_items}
        for row in proposed_items:
            current = by_key.get(row["session_id_match"])
            if current is None:
                raise WriteVerificationError(
                    f"checklist row missing: {row['session_id_match']}")
            for column in ("checklist_item", "status", "checklist_order", "evidence",
                           "session_id"):
                if _canonical(current.get(column)) != _canonical(row[column]):
                    raise WriteVerificationError(
                        f"checklist mismatch at order {row['checklist_order']}: {column}")

    def rollback_owned_session(self, connection, legacy_session_id) -> dict:
        """
        Remove a legacy session THIS writer created, plus its checklist rows.

        Refuses unless an ownership record proves the coded writer created it,
        so a legacy-owned row can never be deleted by this path. Requires a
        write-enabled mode as well.
        """
        if not self.writes_enabled:
            raise WriteVerificationError("rollback requires a write-enabled mode")
        ownership = self.ownership_repository.find(connection, legacy_session_id,
                                                   self.writer_version)
        if ownership is None:
            # Not ours: refuse, loudly.
            return {"status": PROTECTED_EXISTING_LEGACY_ROW, "deleted": False,
                    "legacy_session_id": legacy_session_id}
        with connection.transaction():
            self.legacy_repository.delete_owned_session(connection, legacy_session_id,
                                                        allow_write=True)
            self.ownership_repository.mark_status(connection, ownership["write_id"],
                                                  "ROLLED_BACK",
                                                  {"rolled_back_session": legacy_session_id})
        return {"status": "ROLLED_BACK", "deleted": True,
                "legacy_session_id": legacy_session_id}


def _classify(column: str) -> str:
    if column in AI_FIELDS:
        return DIFFERENT_EXPECTED_AI
    if column in LMS_FIELDS:
        return DIFFERENT_LMS_DRIFT
    return DIFFERENT_OTHER


def _summarise_checklist_diff(diff: dict) -> dict:
    """Counts only, plus the Item 2 known-defect difference reported alone."""
    item2_differs = 2 in diff["status_difference_orders"]
    return {
        "status_difference_count": diff["status_difference_count"],
        "evidence_difference_count": diff["evidence_difference_count"],
        "status_difference_orders": diff["status_difference_orders"],
        "missing_orders": diff["missing_orders"],
        "item2_known_defect_difference": item2_differs,
        "status_difference_count_excluding_item2":
            diff["status_difference_count"] - (1 if item2_differs else 0),
    }


def _retarget(rendered: dict, items, session_id: str):
    """
    Re-address a rendered payload to the legacy key this lecture already owns.

    Only the key moves. Every checklist row's session_id and its
    session_id_match ("<session_id>_<order>") move with it, because the
    checklist upsert is keyed on session_id_match - leaving the old one would
    write eleven orphan checklist rows under an id no session row carries.
    """
    moved = {**rendered, "session_id": session_id,
             "retargeted_from_session_id": rendered["session_id"]}
    moved_items = [{**item, "session_id": session_id,
                    "session_id_match": session_id_match(session_id,
                                                         item["checklist_order"])}
                   for item in items]
    return moved, moved_items
