"""
Phase 2C4 orchestration: persisted Phase 2C3 evidence -> engagement.

Entirely database-backed and entirely downstream of Phase 2C3. No Microsoft
Graph, no WebVTT, no calendar or Aptem lookup, no live attendance read, no
speaker rematching, no AI, and no QA write.
"""
import logging
import time
import uuid
from decimal import Decimal

from app.attendance.coverage import (
    ATTENDANCE_SOURCE_MISSING,
    classify as classify_coverage,
    confirms_zero_attendance,
    is_authoritative,
    provenance as coverage_provenance,
)
from app.attendance.resolver import RESOLVER_VERSION
from app.attendance.roles import ROLE_ALGORITHM_VERSION
from app.attendance.roster import ATTENDANCE_RESOLUTION_VERSION
from app.common.hashing import engagement_identity, engagement_participant_identity
from app.engagement.calculator import (
    ENGAGEMENT_ALGORITHM_VERSION,
    NO_ATTENDED_LEARNERS,
    REVIEW_AMBIGUITY_MAY_CHANGE_RESULT,
    TRAINER_ATTENDANCE_AMBIGUOUS,
    TRAINER_EXCLUSION_VERSION,
    calculate,
    engagement_fingerprint,
    engagement_percentage,
    engagement_score,
    learner_engagement_status,
)
from app.lectures.matching import normalize_group


REVIEW_STATUSES = frozenset({REVIEW_AMBIGUITY_MAY_CHANGE_RESULT, TRAINER_ATTENDANCE_AMBIGUOUS})


class EngagementScopeError(RuntimeError):
    """The requested engagement scope cannot be satisfied."""


class EngagementService:
    def __init__(self, *, input_repository, engagement_repository, run_repository,
                 legacy_parity_repository=None,
                 engagement_algorithm_version: str = ENGAGEMENT_ALGORITHM_VERSION,
                 attendance_resolution_version: str = ATTENDANCE_RESOLUTION_VERSION,
                 resolver_version: str = RESOLVER_VERSION,
                 role_algorithm_version: str = ROLE_ALGORITHM_VERSION):
        self.input_repository = input_repository
        self.engagement_repository = engagement_repository
        self.run_repository = run_repository
        self.legacy_parity_repository = legacy_parity_repository
        self.engagement_algorithm_version = engagement_algorithm_version
        self.attendance_resolution_version = attendance_resolution_version
        self.resolver_version = resolver_version
        self.role_algorithm_version = role_algorithm_version
        self.log = logging.getLogger(__name__)

    @property
    def versions(self) -> dict:
        return {
            "engagement_algorithm_version": self.engagement_algorithm_version,
            "trainer_exclusion_version": TRAINER_EXCLUSION_VERSION,
            "attendance_resolution_version": self.attendance_resolution_version,
            "resolver_version": self.resolver_version,
            "role_algorithm_version": self.role_algorithm_version,
        }

    def calculate_lecture(self, connection, lecture_id, *, persist: bool = True) -> dict:
        """
        Calculate engagement for EXACTLY one lecture.

        The scope is the lecture id, enforced by a separate SQL statement that
        has no session_date predicate at all - so this path cannot widen to the
        date by leaving an argument unset. The controlled-day pilot needed this:
        recalculating one lecture used to re-upsert every sibling on the same
        date with identical values, which is harmless in content but wrong for
        a scheduler, for recovery, and for single-lecture repair, because it
        makes "what did this run touch?" unanswerable.
        """
        lecture = self.input_repository.load_lecture(connection, lecture_id)
        if lecture is None:
            raise EngagementScopeError(f"unknown lecture {lecture_id}")

        snapshots = self.input_repository.load_snapshots_for_lecture(
            connection, lecture_id,
            attendance_resolution_version=self.attendance_resolution_version,
            resolver_version=self.resolver_version,
            role_algorithm_version=self.role_algorithm_version)
        if not snapshots:
            # Refusing beats silently producing nothing: a lecture with no
            # usable attendance evidence is a recovery signal, not a no-op.
            raise EngagementScopeError(
                f"no attendance snapshot with complete role evidence for lecture "
                f"{lecture_id} at {self.attendance_resolution_version}")
        foreign = {str(item["lecture_id"]) for item in snapshots} - {str(lecture_id)}
        if foreign:
            raise EngagementScopeError(
                f"lecture scope leaked to {len(foreign)} other lectures")

        return self._run(connection, snapshots, persist=persist,
                         target_date=lecture["session_date"], scope="LECTURE",
                         scope_value=str(lecture_id), legacy_parity=False)

    def calculate_day(self, connection, target_date, *, persist: bool = True) -> dict:
        """Batch mode: every lecture on one date. Still explicit, never implied."""
        snapshots = self.input_repository.load_snapshots(
            connection, target_date,
            attendance_resolution_version=self.attendance_resolution_version,
            resolver_version=self.resolver_version,
            role_algorithm_version=self.role_algorithm_version)
        return self._run(connection, snapshots, persist=persist,
                         target_date=target_date, scope="DATE",
                         scope_value=target_date.isoformat(), legacy_parity=True)

    def _run(self, connection, snapshots, *, persist, target_date, scope, scope_value,
             legacy_parity) -> dict:
        started = time.monotonic()
        run_id = (self.run_repository.start(connection, target_date, self.versions)
                  if persist else uuid.uuid4())
        snapshot_ids = sorted({item["snapshot_id"] for item in snapshots}, key=str)
        members_by_snapshot = self.input_repository.load_members(connection, snapshot_ids)
        speakers_by_key = self.input_repository.load_speakers(
            connection, snapshot_ids, resolver_version=self.resolver_version,
            role_algorithm_version=self.role_algorithm_version)

        # The current snapshot per lecture is the newest; ties break on the id
        # so the choice never depends on row order. Older snapshots are still
        # calculated - that is history, and reruns are idempotent.
        current = {}
        for item in snapshots:
            key = (item["snapshot_created_at"], str(item["snapshot_id"]))
            if item["lecture_id"] not in current or key > current[item["lecture_id"]]:
                current[item["lecture_id"]] = key

        counters = {name: 0 for name in (
            "snapshots_considered", "engagements_created", "engagements_updated",
            "participants_written", "review_required_count", "error_count")}
        counters["snapshots_considered"] = len(snapshots)
        results = []

        for item in snapshots:
            members = members_by_snapshot.get(item["snapshot_id"], [])
            speakers = speakers_by_key.get((item["snapshot_id"], item["document_id"]), [])
            outcome = calculate(members=members, speakers=speakers)
            # Phase 3C3D. Whether the external attendance source answered the
            # question at all, kept strictly separate from what it answered.
            coverage_status = classify_coverage(
                source_row_count=item.get("source_row_count"),
                present_row_count=item.get("present_row_count"),
                effective_member_count=item.get("effective_member_count"))
            coverage = coverage_provenance(
                status=coverage_status,
                source_row_count=item.get("source_row_count"),
                present_row_count=item.get("present_row_count"),
                effective_member_count=item.get("effective_member_count"),
                snapshot_id=item["snapshot_id"])
            # The stored calculation_status keeps its versioned meaning and its
            # CHECK constraint; the detail says whether a 0/0 is a finding or a
            # gap. Downstream must read the detail, never the bare zero.
            status_detail = (
                ATTENDANCE_SOURCE_MISSING
                if (outcome["calculation_status"] == NO_ATTENDED_LEARNERS
                    and not is_authoritative(coverage_status))
                else outcome["calculation_status"])
            fingerprint = engagement_fingerprint(
                algorithm_version=self.engagement_algorithm_version,
                snapshot_id=item["snapshot_id"],
                snapshot_fingerprint=item["snapshot_fingerprint"],
                resolver_version=self.resolver_version,
                role_algorithm_version=self.role_algorithm_version,
                document_id=item["document_id"], members=members, speakers=speakers)
            engagement_id = engagement_identity(
                document_id=item["document_id"],
                attendance_snapshot_id=item["snapshot_id"],
                resolver_version=self.resolver_version,
                role_algorithm_version=self.role_algorithm_version,
                engagement_algorithm_version=self.engagement_algorithm_version,
                source_fingerprint=fingerprint)
            participants = [{
                **participant,
                "participant_id": engagement_participant_identity(
                    engagement_id=engagement_id, member_id=participant["member_id"]),
            } for participant in outcome["participants"]]

            if outcome["calculation_status"] in REVIEW_STATUSES:
                counters["review_required_count"] += 1

            row = {
                "engagement_id": engagement_id, "lecture_id": item["lecture_id"],
                "document_id": item["document_id"],
                "attendance_snapshot_id": item["snapshot_id"],
                **{key: value for key, value in self.versions.items()
                   if key != "attendance_resolution_version"},
                "source_fingerprint": fingerprint,
                **{key: outcome[key] for key in (
                    "calculation_status", "trainer_speaker_id", "trainer_exclusion_status",
                    "trainer_attendance_match_count", "attendance_before_trainer_exclusion",
                    "trainer_excluded_count", "attended_count",
                    "resolved_learner_speaker_count", "spoke_count", "silent_count",
                    "ambiguous_speaker_count", "unresolved_speaker_count",
                    "engagement_percentage", "engagement_score",
                    "learner_engagement_status", "item7_override_applied")},
                "metadata": {
                    **self.versions,
                    "source": "PHASE_2C3_PERSISTED_EVIDENCE",
                    "live_attendance_read": False,
                    "speaker_rematching_performed": False,
                    "learner_speakers_on_excluded_member":
                        outcome["learner_speakers_on_excluded_member"],
                    "duplicate_speaker_aliases": outcome["duplicate_speaker_aliases"],
                    "ambiguity_upper_bound": outcome["ambiguity_upper_bound"],
                    "legacy_compatible_attended_count":
                        outcome.get("legacy_compatible_attended_count"),
                    **coverage,
                    "engagement_status_detail": status_detail,
                    "attendance_zero_confirmed": confirms_zero_attendance(
                        coverage_status, outcome["attended_count"]),
                    "qa_written": False, "ai_calls": 0,
                },
            }

            if persist:
                written = self.engagement_repository.upsert(connection, row, participants)
                counters["engagements_created"] += written["created"]
                counters["engagements_updated"] += written["updated"]
                counters["participants_written"] += written["participants_written"]

            results.append({
                "lecture_id": str(item["lecture_id"]), "subject": item["subject"],
                "document_id": str(item["document_id"]),
                "attendance_snapshot_id": str(item["snapshot_id"]),
                "is_current_snapshot": current[item["lecture_id"]] == (
                    item["snapshot_created_at"], str(item["snapshot_id"])),
                "engagement_id": str(engagement_id),
                "source_fingerprint_prefix": fingerprint[:16],
                **{key: outcome[key] for key in (
                    "calculation_status", "trainer_exclusion_status",
                    "trainer_attendance_match_count", "attendance_before_trainer_exclusion",
                    "trainer_excluded_count", "attended_count",
                    "resolved_learner_speaker_count", "spoke_count", "silent_count",
                    "ambiguous_speaker_count", "unresolved_speaker_count",
                    "engagement_score", "learner_engagement_status",
                    "item7_override_applied", "ambiguity_upper_bound")},
                "engagement_percentage": (str(outcome["engagement_percentage"])
                                          if outcome["engagement_percentage"] is not None
                                          else None),
                "participant_status_counts": _status_counts(outcome["participants"]),
                **coverage,
                "engagement_status_detail": status_detail,
                "attendance_zero_confirmed": confirms_zero_attendance(
                    coverage_status, outcome["attended_count"]),
            })

        parity = None
        if legacy_parity and self.legacy_parity_repository is not None:
            parity = self._legacy_parity(connection, target_date,
                                         [row for row in results if row["is_current_snapshot"]])

        summary = {
            "run_id": str(run_id), "target_date": target_date.isoformat(),
            "scope": scope, "scope_value": scope_value,
            "lectures_in_scope": len({str(item["lecture_id"]) for item in snapshots}),
            "mode": "SHADOW" if persist else "DRY_RUN", "status": "COMPLETED",
            **self.versions, **counters,
            # Explicit zero markers for this phase's boundaries.
            "graph_calls": 0, "webvtt_parsed": False, "calendar_lookups": 0,
            "aptem_queries": 0, "live_attendance_queries": 0,
            "speaker_rematching_performed": False, "ai_calls": 0,
            "qa_tables_written": False, "ai_trainer_override_applied": False,
            "lectures": results,
            "legacy_parity": parity,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "metadata": {**self.versions, "counters": counters,
                         "live_attendance_queries": 0, "qa_tables_written": False},
        }
        if persist:
            self.run_repository.complete(connection, run_id, summary)
        self.log.info("engagement completed", extra={"fields": {
            "service": "engagement", "operation": "calculate_" + scope.lower(),
            "run_id": str(run_id), "status": summary["status"],
            "snapshots": len(results),
            "review_required": counters["review_required_count"],
            "duration_ms": summary["duration_ms"],
        }})
        return summary

    def _legacy_parity(self, connection, target_date, results) -> dict:
        """
        READ-ONLY comparison with the stored legacy values.

        Three separate questions, so a mismatch can be attributed:
          - output parity: do our stored numbers equal the legacy numbers?
          - input parity: do our spoke / attended counts equal the counts
            legacy wrote into its own Item 7 evidence?
          - algorithm parity: fed legacy's own counts, does our arithmetic
            reproduce legacy's percentage, score and Item 7 exactly?
        """
        legacy_rows = self.legacy_parity_repository.load(connection, target_date)
        by_subject = {normalize_group(item["subject"]): item for item in results}
        rows = []
        totals = {key: 0 for key in (
            "engagement", "engagement_score", "item7", "item7_compared",
            "spoke_count", "attended_count", "algorithm")}
        for legacy in legacy_rows:
            ours = by_subject.get(normalize_group(legacy["subject"] or ""))
            if ours is None:
                rows.append({"subject": legacy["subject"], "status": "NO_NEW_RESULT"})
                continue
            our_pct = (Decimal(ours["engagement_percentage"])
                       if ours["engagement_percentage"] is not None else None)
            legacy_pct = Decimal(legacy["engagement"]) if legacy["engagement"] is not None else None
            engagement_match = our_pct is not None and legacy_pct is not None and our_pct == legacy_pct
            score_match = ours["engagement_score"] == legacy["engagement_score"]
            # Item 7 is only comparable when legacy actually applied the
            # deterministic override (its evidence text proves it did).
            item7_comparable = legacy["legacy_attended_count"] is not None
            item7_match = item7_comparable and ours["learner_engagement_status"] == legacy["item7_status"]

            algorithm_match = None
            if item7_comparable and legacy["legacy_attended_count"] > 0:
                replay = engagement_percentage(legacy["legacy_spoke_count"],
                                               legacy["legacy_attended_count"])
                algorithm_match = (replay == legacy_pct
                                   and engagement_score(replay) == legacy["engagement_score"]
                                   and learner_engagement_status(replay) == legacy["item7_status"])

            spoke_match = ours["spoke_count"] == legacy["legacy_spoke_count"]
            attended_match = ours["attended_count"] == legacy["legacy_attended_count"]
            totals["engagement"] += engagement_match
            totals["engagement_score"] += score_match
            totals["item7"] += item7_match
            totals["item7_compared"] += item7_comparable
            totals["spoke_count"] += spoke_match
            totals["attended_count"] += attended_match
            totals["algorithm"] += bool(algorithm_match)
            rows.append({
                "subject": legacy["subject"],
                "engagement_new": ours["engagement_percentage"],
                "engagement_legacy": str(legacy_pct) if legacy_pct is not None else None,
                "engagement_match": "YES" if engagement_match else "NO",
                "engagement_score_new": ours["engagement_score"],
                "engagement_score_legacy": legacy["engagement_score"],
                "engagement_score_match": "YES" if score_match else "NO",
                "item7_new": ours["learner_engagement_status"],
                "item7_legacy": legacy["item7_status"],
                "item7_match": ("YES" if item7_match else "NO") if item7_comparable
                               else "NOT_COMPARABLE",
                "spoke_new": ours["spoke_count"],
                "spoke_legacy": legacy["legacy_spoke_count"],
                "attended_new": ours["attended_count"],
                "attended_legacy": legacy["legacy_attended_count"],
                "silent_new": ours["silent_count"],
                "silent_legacy": legacy["legacy_silent_count"],
                "algorithm_replay_on_legacy_counts": (
                    "YES" if algorithm_match else "NO") if algorithm_match is not None
                    else "NOT_APPLICABLE",
                "mismatch_cause": _mismatch_cause(engagement_match, spoke_match, attended_match),
            })
        count = len(legacy_rows)
        return {
            "qa_rows": count,
            "engagement_parity": f"{totals['engagement']} / {count}",
            "engagement_score_parity": f"{totals['engagement_score']} / {count}",
            "item7_parity": f"{totals['item7']} / {totals['item7_compared']}",
            "spoke_count_parity": f"{totals['spoke_count']} / {count}",
            "attended_count_parity": f"{totals['attended_count']} / {count}",
            "algorithm_replay_parity": f"{totals['algorithm']} / {totals['item7_compared']}",
            "note": "Read-only. Nothing is adjusted to improve parity and no QA row is written.",
            "rows": rows,
        }


def _status_counts(participants) -> dict:
    counts: dict = {}
    for participant in participants:
        counts[participant["participation_status"]] = (
            counts.get(participant["participation_status"], 0) + 1)
    return dict(sorted(counts.items()))


def _mismatch_cause(engagement_match, spoke_match, attended_match):
    if engagement_match:
        return None
    if spoke_match and not attended_match:
        return "ATTENDED_DENOMINATOR_DIFFERS"
    if attended_match and not spoke_match:
        return "SPOKE_NUMERATOR_DIFFERS"
    if not spoke_match and not attended_match:
        return "NUMERATOR_AND_DENOMINATOR_DIFFER"
    return "ARITHMETIC_DIFFERS"
