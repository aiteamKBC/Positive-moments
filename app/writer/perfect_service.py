"""
Phase 3C2.3D orchestration: plan, and (only under a write-enabled mode)
perform, the legacy Perfect Lecture write.

This is a DERIVED output. Its only input is the already-frozen Phase 3A/3B
result: no model call, no transcript read, no attendance or LMS query, no
recalculation of a single checklist status. That is what makes a missing
legacy row repairable later at zero cost.

TRANSACTION DESIGN - Option B, deliberately.

The QA session transaction commits FIRST, and the Perfect Lecture row is a
separate, idempotent, derived-output transaction. Reasons, in order of weight:

  * legacy already behaves this way. The child upserts the QA rows, then does
    the recording lookup, then upserts the Perfect Lecture in its own
    statement with onError: continueRegularOutput. "QA present, Perfect
    absent" is a state production has been in since the beginning - two
    currently-eligible sessions have no Perfect Lecture row right now.
  * the QA session is the primary artefact and the expensive one. Coupling it
    to the derived output means a Perfect Lecture failure would roll back a
    correct, paid-for QA write. That trades a cheap, repairable gap for an
    expensive one.
  * the Perfect Lecture row acquires foreign state after creation -
    recording_url from the Master's recording branch, excel_synced_at from
    the Excel Sync workflow. Its lifetime is genuinely not the QA session's,
    and a shared transaction would imply a shared rollback that must never
    happen.
  * recovery is deterministic either way, because eligibility is a pure
    function of a frozen fingerprint. A missing row is repaired by re-running
    this planner; there is nothing to reconstruct.

Within this transaction the three writes - the coded result, the legacy row
and the ownership record - ARE atomic together, so the platform can never end
up owning a row it did not write, or writing a row it does not own.
"""
import logging
import time
import uuid

from app.attendance.coverage import SOURCE_UNKNOWN
from app.db.repositories.attendance_coverage import AttendanceCoverageRepository
from app.attendance.roster import ATTENDANCE_RESOLUTION_VERSION
from app.qa.perfect import (
    DEFAULT_PERFECT_ELIGIBILITY_VERSION,
    PERFECT_ELIGIBILITY_VERSION,
    apply_attendance_policy,
    attendance_policy_required,
    evaluate,
    legacy_lecture_key,
)
from app.writer.mapping import PayloadInvariantError
from app.writer.perfect_mapping import (
    CODED_OWNED_COLUMNS,
    PERFECT_MAPPING_VERSION,
    PERFECT_WRITER_VERSION,
    diff_perfect,
    perfect_digest,
    perfect_mapped_digest,
    perfect_row,
)
from app.writer.modes import (
    PERFECT_ACTIONABLE_DECISIONS,
    PERFECT_BLOCKED_INVALID_PAYLOAD,
    PERFECT_BLOCKED_KEY_COLLISION,
    PERFECT_BLOCKED_NOT_READY,
    PERFECT_PENDING_ATTENDANCE_DATA,
    PERFECT_SUPERSEDED_NOT_PERFECT,
    PERFECT_UPDATE_MAPPING_OUTPUT_CHANGED,
    PERFECT_UPDATE_SOURCE_CHANGED,
    PERFECT_WOULD_INSERT,
    PERFECT_WOULD_UPDATE,
    plan_perfect_decision,
    validate_mode,
    writes_enabled,
    WriterModeError,
)
from app.writer.mapping import _canonical


class PerfectWriteVerificationError(RuntimeError):
    """The Perfect Lecture row read back after a write does not match."""


class PerfectLecturePlanner:
    """
    Plans - and, in a write-enabled mode, performs - the Perfect Lecture
    output for one lecture at a time.
    """

    def __init__(self, *, result_repository, ownership_repository, legacy_repository,
                 mode: str = "DRY_RUN", writer_version: str = PERFECT_WRITER_VERSION,
                 eligibility_version: str = DEFAULT_PERFECT_ELIGIBILITY_VERSION,
                 mapping_version: str = PERFECT_MAPPING_VERSION,
                 allow_update_existing: bool = False,
                 coverage_repository=None,
                 attendance_resolution_version: str = ATTENDANCE_RESOLUTION_VERSION,
                 lecture_ids=None, confirmed: bool = False,
                 persist_shadow_result: bool = False):
        self.mode = validate_mode(mode)
        # The same guard the QA writer applies, enforced again here so a
        # programmatic caller cannot write unconfirmed or unscoped.
        if writes_enabled(self.mode):
            if not lecture_ids:
                raise WriterModeError(f"{self.mode} requires an explicit lecture scope")
            if self.mode == "CANARY_NEW_ONLY" and len(set(lecture_ids)) != 1:
                raise WriterModeError("CANARY_NEW_ONLY writes exactly one lecture")
            if not confirmed:
                raise WriterModeError(
                    f"{self.mode} requires an explicit write confirmation")
        self.result_repository = result_repository
        self.ownership_repository = ownership_repository
        self.legacy_repository = legacy_repository
        self.writer_version = writer_version
        self.eligibility_version = eligibility_version
        # The legacy mapping CONTRACT, versioned separately from the writer so
        # that correcting the mapping refreshes the row this platform already
        # owns instead of creating a second ownership identity for it.
        self.mapping_version = mapping_version
        # EXPLICIT_BACKFILL additionally requires this flag before it may
        # rewrite a row that already exists.
        self.allow_update_existing = allow_update_existing
        # Phase 3C3D. Only the attendance-aware policy needs it, and it is a
        # read of counts this platform already froze - never a live attendance
        # query and never a call to the external source.
        # Phase 3C3E: the attendance-aware policy is now the DEFAULT, so the
        # reader it needs is supplied by default too. Omitting it must not
        # quietly downgrade the policy - the only way to decide a Perfect
        # Lecture without attendance evidence is to name v1 explicitly.
        if coverage_repository is None and attendance_policy_required(eligibility_version):
            coverage_repository = AttendanceCoverageRepository()
        self.coverage_repository = coverage_repository
        self.attendance_resolution_version = attendance_resolution_version
        self.confirmed = confirmed
        self.lecture_ids = {str(value) for value in lecture_ids} if lecture_ids else None
        # The coded shadow result is a coded-platform write, separate from any
        # legacy write, and off unless asked for.
        self.persist_shadow_result = persist_shadow_result
        self.log = logging.getLogger(__name__)

    @property
    def writes_enabled(self) -> bool:
        return writes_enabled(self.mode)

    def plan_one(self, connection, rendered: dict, rendered_items, counters=None) -> dict:
        """Derive eligibility, then decide what the legacy row should become."""
        counters = counters if counters is not None else {}
        coverage = self._coverage(connection, rendered["lecture_id"])
        # v1 stays exactly the legacy rule. v2 layers the attendance-source
        # condition on top of it, keeping the v1 answer visible alongside.
        facts = apply_attendance_policy(
            evaluate(rendered, rendered_items),
            coverage_status=coverage["attendance_coverage_status"],
            eligibility_version=self.eligibility_version)

        try:
            proposed = perfect_row(rendered)
            payload_valid, payload_error = True, None
        except PayloadInvariantError as error:
            proposed, payload_valid, payload_error = None, False, str(error)

        lecture_key = (proposed["lecture_key"] if proposed
                       else _best_effort_key(rendered))
        existing = (self.legacy_repository.load(connection, lecture_key)
                    if lecture_key else None)
        collision = (self.legacy_repository.collisions(
            connection, lecture_key, rendered.get("session_id"))
            if lecture_key else {"key_in_use": False, "foreign_session": False,
                                 "existing_session_id": None})
        ownership = (self.ownership_repository.find(
            connection, lecture_key, self.writer_version) if lecture_key else None)

        # Gate F. An unchanged source fingerprint is NOT sufficient: the source
        # can be frozen while the MAPPING changes, which is exactly how a wrong
        # attended_count reached production and then survived a second
        # "identical" run. So the mapped payload this writer would produce
        # today is compared against what the target actually holds - over
        # coded-owned columns only, so a recording or Excel-sync enrichment is
        # never mistaken for drift.
        proposed_mapped = (
            perfect_mapped_digest(proposed, mapping_version=self.mapping_version)
            if proposed is not None else None)
        target_mapped = (
            perfect_mapped_digest(existing, mapping_version=self.mapping_version)
            if existing is not None else None)
        recorded_mapped = ownership["mapped_digest"] if ownership else None
        fingerprint_matches = bool(ownership
                                   and ownership["source_fingerprint"]
                                   == rendered["source_fingerprint"])
        # Two comparisons, and BOTH must hold:
        #
        #   proposed == target   - the row currently holds what we would write.
        #                          Catches a mapping whose OUTPUT changed, and
        #                          any out-of-band edit of a coded-owned column.
        #   proposed == recorded - the audit agrees this is what we last wrote.
        #                          Catches a mapping CONTRACT version bump whose
        #                          values happen to be identical, so provenance
        #                          is re-stamped rather than silently stale.
        #
        # A recorded digest of None means the row predates mapping versioning;
        # there is nothing to disagree with, so the target comparison decides
        # alone rather than forcing a rewrite of every historical row.
        target_agrees = proposed_mapped is not None and proposed_mapped == target_mapped
        audit_agrees = recorded_mapped is None or recorded_mapped == proposed_mapped
        mapped_output_matches = bool(target_agrees and audit_agrees)

        decision = plan_perfect_decision(
            mode=self.mode, render_status=rendered["render_status"],
            qa_status=rendered["qa_status"], payload_valid=payload_valid,
            is_perfect=facts["is_perfect"],
            target_exists=existing is not None,
            coded_owned=ownership is not None,
            foreign_session_on_key=bool(collision["foreign_session"]),
            fingerprint_matches=fingerprint_matches,
            mapped_output_matches=mapped_output_matches,
            allow_update_existing=self.allow_update_existing,
            attendance_pending=facts["attendance_pending"])

        update_reason = None
        if decision == PERFECT_WOULD_UPDATE:
            update_reason = (PERFECT_UPDATE_SOURCE_CHANGED if not fingerprint_matches
                             else PERFECT_UPDATE_MAPPING_OUTPUT_CHANGED)

        pre_digest = perfect_digest(existing)
        result = {
            "lecture_id": str(rendered["lecture_id"]),
            "legacy_lecture_key": lecture_key,
            "legacy_session_id": rendered.get("session_id"),
            "eligibility_version": self.eligibility_version,
            "perfect_writer_version": self.writer_version,
            "is_perfect": facts["is_perfect"],
            "eligibility_reason": facts["reason"],
            "base_is_perfect": facts["base_is_perfect"],
            "base_eligibility_reason": facts["base_reason"],
            "attendance_pending": facts["attendance_pending"],
            **coverage,
            "met_count": facts["met_count"], "partial_count": facts["partial_count"],
            "not_met_count": facts["not_met_count"],
            "checklist_row_count": facts["checklist_row_count"],
            "distinct_order_count": facts["distinct_order_count"],
            "cancelled_session": facts["cancelled_session"],
            "perfect_decision": decision,
            "legacy_row_exists": existing is not None,
            "coded_owned": ownership is not None,
            "lecture_key_in_use": bool(collision["key_in_use"]),
            "lecture_key_collision": bool(collision["foreign_session"]),
            "pre_write_digest": pre_digest,
            "pre_write_digest_prefix": pre_digest[:16],
            "mapping_version": self.mapping_version,
            "proposed_mapped_digest": proposed_mapped,
            "target_mapped_digest": target_mapped,
            # What the audit says this writer last wrote. None on a row written
            # before mapping versioning existed, which is itself meaningful.
            "recorded_mapped_digest": recorded_mapped,
            "recorded_mapping_version": ownership["mapping_version"] if ownership else None,
            "source_fingerprint_matches": fingerprint_matches,
            "mapped_output_matches": mapped_output_matches,
            "target_mapped_digest_agrees": target_agrees,
            "recorded_mapped_digest_agrees": audit_agrees,
            "perfect_update_reason": update_reason,
            "payload_invariant_error": payload_error,
            # The recording columns are never proposed, in any mode.
            "recording_url_written": False,
            "existing_recording_url_present": bool(
                existing and existing.get("recording_url")),
            # Reported, never diffed: these belong to other systems.
            "foreign_fields": (self._foreign_fields(connection, lecture_key)
                               if existing is not None else None),
        }
        counters[decision.lower()] = counters.get(decision.lower(), 0) + 1

        if proposed is not None:
            result["proposed_digest_prefix"] = perfect_digest(proposed)[:16]
            result["field_diff"] = sorted(diff_perfect(proposed, existing))

        if decision in PERFECT_ACTIONABLE_DECISIONS and self.writes_enabled:
            result.update(self._write_one(connection, rendered, facts, proposed,
                                          decision, pre_digest, ownership, counters,
                                          update_reason))
        elif decision == PERFECT_SUPERSEDED_NOT_PERFECT and self.writes_enabled:
            result.update(self._supersede(connection, rendered, facts, ownership,
                                          counters))
        elif self._may_persist_result(decision, payload_valid, lecture_key):
            # Phase 3C3B: the coded answer is persisted for EVERY finalized
            # rendered session, not only the perfect ones. A NOT_ELIGIBLE
            # lecture used to leave no row at all, so Operations and Recovery
            # could not tell "not eligible" from "never computed" without
            # re-deriving it. The answer is deterministic and free, but a
            # state model you have to recompute is not a state model.
            #
            # This writes ONLY coded shadow state. A non-perfect lecture still
            # gets no qa_perfect_lectures row and no Perfect legacy ownership.
            result.update(self._persist_result(connection, rendered, facts, lecture_key))
        return result

    def _coverage(self, connection, lecture_id) -> dict:
        """
        Attendance source coverage for one lecture.

        Without a repository the answer is SOURCE_UNKNOWN rather than an
        assumed pass: v1 ignores it entirely, and v2 refuses to be constructed
        without one, so an unknown can never quietly authorise a write.
        """
        if self.coverage_repository is None:
            return {"attendance_coverage_status": SOURCE_UNKNOWN,
                    "attendance_source_authoritative": False,
                    "attendance_coverage_evaluated": False}
        return {**self.coverage_repository.for_lecture(
            connection, lecture_id,
            attendance_resolution_version=self.attendance_resolution_version),
            "attendance_coverage_evaluated": True}

    def _may_persist_result(self, decision, payload_valid, lecture_key) -> bool:
        """
        When the coded eligibility answer may be recorded.

        Explicitly on request (a dry run stays a pure read unless asked), or
        whenever the run is already authorised to write - in which case the
        answer is recorded whatever it turned out to be.

        Never for a payload that is not finalized or does not map: an answer
        derived from an unusable render would be provenance for nothing.
        """
        if not (self.persist_shadow_result or self.writes_enabled):
            return False
        if not payload_valid or not lecture_key:
            return False
        return decision not in (PERFECT_BLOCKED_NOT_READY, PERFECT_BLOCKED_INVALID_PAYLOAD,
                                PERFECT_BLOCKED_KEY_COLLISION)

    # -- writes ------------------------------------------------------------

    def _persist_result(self, connection, rendered, facts, lecture_key) -> dict:
        """Record the coded answer only. Touches no legacy table."""
        outcome = self.result_repository.upsert(connection, {
            "lecture_id": rendered["lecture_id"],
            "evaluation_id": rendered["evaluation_id"],
            "rendered_session_id": rendered["rendered_session_id"],
            "eligibility_version": self.eligibility_version,
            "source_fingerprint": rendered["source_fingerprint"],
            "is_perfect": facts["is_perfect"], "reason": facts["reason"],
            "met_count": facts["met_count"], "partial_count": facts["partial_count"],
            "not_met_count": facts["not_met_count"],
            "checklist_row_count": facts["checklist_row_count"],
            "distinct_order_count": facts["distinct_order_count"],
            "cancelled_session": facts["cancelled_session"],
            "legacy_lecture_key": lecture_key,
            "legacy_session_id": rendered["session_id"],
            "metadata": {"orders": facts["orders"],
                         "met_status_count": facts["met_status_count"],
                         # Phase 3C3D: why a perfect checklist may still not be
                         # publishable, recorded with the answer so Operations
                         # never has to re-derive it.
                         "base_reason": facts.get("base_reason"),
                         "base_is_perfect": facts.get("base_is_perfect"),
                         "attendance_pending": facts.get("attendance_pending", False),
                         "attendance_coverage_status":
                             facts.get("attendance_coverage_status"),
                         "attendance_source_authoritative":
                             facts.get("attendance_source_authoritative")},
        })
        return {"result_id": str(outcome["result_id"]),
                "shadow_result_created": outcome["created"],
                "previous_is_perfect": outcome["previous_is_perfect"]}

    def _write_one(self, connection, rendered, facts, proposed, decision,
                   pre_digest, ownership, counters, update_reason=None) -> dict:
        """
        Write the coded result, the legacy row and the ownership record inside
        ONE transaction, then verify by re-reading.

        Verification covers only the columns the coded writer owns:
        recording_url deliberately is not compared, because a recording
        workflow writing it between our INSERT and our re-read is correct
        behaviour, not a verification failure.
        """
        lecture_key = proposed["lecture_key"]
        try:
            with connection.transaction():
                persisted = self._persist_result(connection, rendered, facts, lecture_key)
                self.legacy_repository.write(connection, proposed, allow_write=True)
                written = self.legacy_repository.load(connection, lecture_key)
                self._verify(proposed, written)
                post_digest = perfect_digest(written)
                owner = self.ownership_repository.record(connection, {
                    "perfect_write_id": (ownership["perfect_write_id"] if ownership
                                         else uuid.uuid4()),
                    "lecture_id": rendered["lecture_id"],
                    "result_id": persisted["result_id"],
                    "evaluation_id": rendered["evaluation_id"],
                    "legacy_lecture_key": lecture_key,
                    "legacy_session_id": rendered["session_id"],
                    "writer_version": self.writer_version,
                    "eligibility_version": self.eligibility_version,
                    "source_fingerprint": rendered["source_fingerprint"],
                    "write_mode": self.mode,
                    "write_status": ("WRITTEN" if decision == PERFECT_WOULD_INSERT
                                     else "UPDATED"),
                    "legacy_row_created": decision == PERFECT_WOULD_INSERT,
                    "legacy_row_updated": decision == PERFECT_WOULD_UPDATE,
                    "pre_write_digest": pre_digest, "post_write_digest": post_digest,
                    "metadata": self._audit_metadata(ownership, decision, facts,
                                                     written, pre_digest, post_digest,
                                                     update_reason),
                })
        except PerfectWriteVerificationError as error:
            counters["perfect_verification_failures"] = counters.get(
                "perfect_verification_failures", 0) + 1
            return {"perfect_write_status": "WRITE_VERIFICATION_FAILED",
                    "perfect_write_error": str(error), "rolled_back": True}

        counters["perfect_rows_written"] = counters.get("perfect_rows_written", 0) + 1
        counters["perfect_ownership_rows_written"] = counters.get(
            "perfect_ownership_rows_written", 0) + (1 if owner["created"] else 0)
        return {"perfect_write_status": ("WRITTEN" if decision == PERFECT_WOULD_INSERT
                                         else "UPDATED"),
                "perfect_write_id": str(owner["perfect_write_id"]),
                "result_id": persisted["result_id"],
                "post_write_digest": post_digest,
                "post_write_digest_prefix": post_digest[:16],
                "rolled_back": False}

    def _audit_metadata(self, ownership, decision, facts, written,
                        pre_digest, post_digest, update_reason=None) -> dict:
        """
        Provenance for "what exact mapped payload did this writer last write?".

        Kept in the existing metadata jsonb: a mapping correction must not add
        a column, and must not add a second ownership row. On a rewrite the
        superseded digests are appended to a bounded `repairs` list, so the
        evidence that the first write carried the OLD digest is never erased.
        """
        metadata = {
            "decision": decision,
            "eligibility_reason": facts["reason"],
            "mapping_version": self.mapping_version,
            # The coded-owned mapped payload, foreign columns excluded.
            "mapped_digest": perfect_mapped_digest(
                written, mapping_version=self.mapping_version),
        }
        previous = (ownership or {}).get("metadata") or {}
        repairs = list(previous.get("repairs", []))
        if decision == PERFECT_WOULD_UPDATE:
            repairs.append({
                "previous_mapping_version": previous.get("mapping_version"),
                "previous_mapped_digest": previous.get("mapped_digest"),
                "previous_post_write_digest": (ownership.get("post_write_digest")
                                               if ownership else None),
                "new_mapping_version": self.mapping_version,
                "new_mapped_digest": metadata["mapped_digest"],
                "new_post_write_digest": post_digest,
                "pre_write_digest": pre_digest,
                "reason": update_reason,
                "write_mode": self.mode,
            })
        if repairs:
            metadata["repairs"] = repairs[-20:]
        return metadata

    def _foreign_fields(self, connection, lecture_key) -> dict:
        """Read-only snapshot of the columns other systems own."""
        try:
            row = connection.execute(
                "SELECT id, recording_url, recap_url, detected_at, excel_synced_at "
                "  FROM public.qa_perfect_lectures WHERE lecture_key = %s",
                (lecture_key,)).fetchone()
        except Exception:
            return {"readable": False}
        if row is None:
            return {"readable": True, "present": False}
        return {"readable": True, "present": True, "id": row[0],
                "recording_url": row[1], "recap_url": row[2],
                "detected_at": str(row[3]) if row[3] is not None else None,
                "excel_synced_at": str(row[4]) if row[4] is not None else None}

    def _supersede(self, connection, rendered, facts, ownership, counters) -> dict:
        """
        PERFECT -> NOT PERFECT on a row we own.

        The legacy row is NOT deleted and NOT modified. The coded answer is
        updated and the ownership record is marked SUPERSEDED_NOT_PERFECT, so
        the divergence is visible to reconciliation. Removing the row is a
        separate, explicitly requested rollback that additionally proves no
        downstream enrichment depends on it.
        """
        previous = self.result_repository.find(connection, rendered["lecture_id"],
                                               self.eligibility_version)
        # The key stays whatever we originally wrote under: the legacy row is
        # addressed by that key and must not be re-derived here.
        lecture_key = (previous["legacy_lecture_key"] if previous
                       else _best_effort_key(rendered))
        with connection.transaction():
            persisted = self._persist_result(connection, rendered, facts, lecture_key)
            self.ownership_repository.mark_status(
                connection, ownership["perfect_write_id"], "SUPERSEDED_NOT_PERFECT",
                {"eligibility_reason": facts["reason"],
                 "legacy_row_left_in_place": True})
        counters["perfect_superseded"] = counters.get("perfect_superseded", 0) + 1
        return {"perfect_write_status": "SUPERSEDED_NOT_PERFECT",
                "legacy_row_deleted": False, "result_id": persisted["result_id"]}

    def _verify(self, proposed, written) -> None:
        if written is None:
            raise PerfectWriteVerificationError("perfect lecture row missing after write")
        for column in CODED_OWNED_COLUMNS:
            if _canonical(written.get(column)) != _canonical(proposed[column]):
                raise PerfectWriteVerificationError(
                    f"perfect lecture column mismatch: {column}")

    # -- rollback ----------------------------------------------------------

    def rollback_owned_row(self, connection, lecture_key, *,
                           allow_downstream_loss: bool = False) -> dict:
        """
        Remove a Perfect Lecture row THIS platform created.

        Two independent refusals, both fail-closed:

        1. no ownership record for (lecture_key, writer_version) means the row
           is legacy history. Refused in every mode.
        2. downstream enrichment already happened - the Master's recording
           branch filled recording_url, or the Excel Sync workflow stamped
           excel_synced_at and therefore already published the row to a shared
           workbook. Deleting then would destroy data this platform did not
           create, and an Excel row that no database row explains. Refused
           unless the caller explicitly accepts that loss.
        """
        if not self.writes_enabled:
            raise PerfectWriteVerificationError("rollback requires a write-enabled mode")
        ownership = self.ownership_repository.find(connection, lecture_key,
                                                   self.writer_version)
        if ownership is None:
            return {"status": "PROTECTED_EXISTING_LEGACY_ROW", "deleted": False,
                    "legacy_lecture_key": lecture_key,
                    "reason": "no coded ownership record"}
        existing = self.legacy_repository.load(connection, lecture_key)
        if existing is None:
            self.ownership_repository.mark_status(connection,
                                                  ownership["perfect_write_id"],
                                                  "ROLLED_BACK",
                                                  {"row_already_absent": True})
            return {"status": "ROLLED_BACK", "deleted": False,
                    "legacy_lecture_key": lecture_key,
                    "reason": "row already absent"}
        downstream = self._downstream_dependency(connection, lecture_key, existing)
        if downstream and not allow_downstream_loss:
            return {"status": "BLOCKED_DOWNSTREAM_DEPENDENCY", "deleted": False,
                    "legacy_lecture_key": lecture_key,
                    "downstream_dependencies": downstream}
        with connection.transaction():
            self.legacy_repository.delete_owned(connection, lecture_key, allow_write=True)
            self.ownership_repository.mark_status(
                connection, ownership["perfect_write_id"], "ROLLED_BACK",
                {"downstream_dependencies": downstream,
                 "downstream_loss_accepted": bool(downstream)})
        return {"status": "ROLLED_BACK", "deleted": True,
                "legacy_lecture_key": lecture_key,
                "downstream_dependencies": downstream}

    def _downstream_dependency(self, connection, lecture_key, existing) -> list[str]:
        reasons = []
        if existing.get("recording_url"):
            reasons.append("RECORDING_URL_POPULATED")
        try:
            row = connection.execute(
                "SELECT excel_synced_at IS NOT NULL "
                "  FROM public.qa_perfect_lectures WHERE lecture_key = %s",
                (lecture_key,)).fetchone()
        except Exception:
            # Fail closed: an unreadable dependency is treated as present.
            return reasons + ["EXCEL_SYNC_STATE_UNREADABLE"]
        if row and row[0]:
            reasons.append("EXCEL_SYNCED")
        return reasons


def _best_effort_key(rendered) -> str | None:
    """A key for reporting when the payload is too incomplete to map."""
    try:
        return legacy_lecture_key(rendered.get("legacy_date"), rendered.get("subject"))
    except Exception:
        return None


def plan_perfect_for_day(planner, connection, payload_repository, target_date,
                         renderer_version) -> dict:
    """Convenience wrapper: plan every rendered lecture on one day."""
    started = time.monotonic()
    rendered = payload_repository.load_sessions(connection, target_date, renderer_version)
    if planner.lecture_ids is not None:
        rendered = [row for row in rendered
                    if str(row["lecture_id"]) in planner.lecture_ids]
    counters = {}
    lectures = []
    for row in rendered:
        items = payload_repository.load_items(connection, row["rendered_session_id"])
        lectures.append(planner.plan_one(connection, row, items, counters))
    return {"target_date": target_date.isoformat(), "mode": planner.mode,
            "eligibility_version": planner.eligibility_version,
            "perfect_writer_version": planner.writer_version,
            "lectures_considered": len(rendered),
            "perfect_rows_written": counters.get("perfect_rows_written", 0),
            **counters, "lectures": lectures,
            # Boundary markers: a derived output never spends anything.
            "provider_calls": 0, "graph_calls": 0, "lms_queries": 0,
            "attendance_queries": 0, "evidence_regenerations": 0,
            "duration_ms": round((time.monotonic() - started) * 1000)}
