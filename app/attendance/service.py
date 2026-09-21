"""
Phase 2C3 orchestration: speaker inventory -> attendance resolution -> roles.

Entirely database-backed. No Microsoft Graph, no WebVTT parsing, no calendar
discovery, no Aptem query, no AI, and no engagement metric.

The two layers stay separate all the way through: person resolution runs for
EVERY speaker (including the trainer candidate), and role inference reads that
result without being allowed to alter it.
"""
import logging
import time
import uuid

from app.attendance.legacy_matching import LEGACY_MATCHER_VERSION
from app.attendance.resolver import (
    AMBIGUOUS,
    EXACT_MATCH,
    LEGACY_FUZZY_MATCH,
    MATCHED_STATUSES,
    NORMALIZED_EXACT_MATCH,
    NO_MATCH,
    NOT_APPLICABLE,
    RESOLVER_VERSION,
    legacy_would_match,
    resolve_speaker,
)
from app.attendance.roles import (
    DETERMINISTIC_TRAINER_SOURCE,
    LEARNER,
    OTHER_OR_UNRESOLVED,
    ROLE_ALGORITHM_VERSION,
    TRAINER_CANDIDATE,
    assign_roles,
)
from app.attendance.roster import (
    ATTENDANCE_RESOLUTION_VERSION,
    BOT_FILTER_VERSION,
    build_roster,
    roster_fingerprint,
    roster_rule,
)
from app.common.hashing import (
    attendance_member_identity,
    attendance_snapshot_identity,
    speaker_resolution_identity,
    speaker_role_identity,
)
from app.lectures.matching import normalize_group
from app.transcripts.speakers import SPEAKER_INVENTORY_VERSION, normalize_speaker_label
from app.transcripts.webvtt import PARSER_VERSION


STATUS_COUNTER = {
    EXACT_MATCH: "exact_matches",
    NORMALIZED_EXACT_MATCH: "normalized_exact_matches",
    LEGACY_FUZZY_MATCH: "legacy_fuzzy_matches",
    AMBIGUOUS: "ambiguous_matches",
    NO_MATCH: "unmatched_speakers",
    NOT_APPLICABLE: "unmatched_speakers",
}


class AttendanceScopeError(RuntimeError):
    """A lecture-scoped attendance operation cannot be performed as asked."""


class SpeakerResolutionService:
    def __init__(self, *, inventory_repository, attendance_repository,
                 snapshot_repository, identity_repository, role_repository,
                 run_repository, legacy_diagnostic_repository=None,
                 attendance_resolution_version: str = ATTENDANCE_RESOLUTION_VERSION,
                 resolver_version: str = RESOLVER_VERSION,
                 role_algorithm_version: str = ROLE_ALGORITHM_VERSION,
                 inventory_version: str = SPEAKER_INVENTORY_VERSION,
                 parser_version: str = PARSER_VERSION):
        self.inventory_repository = inventory_repository
        self.attendance_repository = attendance_repository
        self.snapshot_repository = snapshot_repository
        self.identity_repository = identity_repository
        self.role_repository = role_repository
        self.run_repository = run_repository
        self.legacy_diagnostic_repository = legacy_diagnostic_repository
        roster_rule(attendance_resolution_version)  # refuse unknown roster versions early
        self.attendance_resolution_version = attendance_resolution_version
        self.resolver_version = resolver_version
        self.role_algorithm_version = role_algorithm_version
        self.inventory_version = inventory_version
        self.parser_version = parser_version
        self.log = logging.getLogger(__name__)

    @property
    def versions(self) -> dict:
        return {
            "attendance_resolution_version": self.attendance_resolution_version,
            "resolver_version": self.resolver_version,
            "role_algorithm_version": self.role_algorithm_version,
            "speaker_inventory_version": self.inventory_version,
            "parser_version": self.parser_version,
            "legacy_matcher_version": LEGACY_MATCHER_VERSION,
            "bot_filter_version": BOT_FILTER_VERSION,
        }

    def resolve_lecture(self, connection, lecture_id, *, persist: bool = True) -> dict:
        """
        Re-resolve attendance for EXACTLY one lecture.

        This is the primitive attendance recovery is built on. It reads the
        external source again through the SAME validated contract
        (`AttendanceSourceRepository`, exact date + exact normalized module +
        `Attendance = 1`) and nothing else: no Graph call, no calendar lookup,
        no transcript work.

        Snapshots are content-addressed on the roster fingerprint, so an
        unchanged source produces the SAME snapshot id and the write is a
        reuse; a source that has gained rows produces a NEW snapshot and the
        old one - including its SOURCE_MISSING evidence - stays exactly where
        it is. Nothing is ever rewritten in place.
        """
        lecture = self.inventory_repository.load_lecture(
            connection, lecture_id, self.inventory_version, self.parser_version)
        if lecture is None:
            raise AttendanceScopeError(
                f"no speaker inventory for lecture {lecture_id} at "
                f"{self.inventory_version} / {self.parser_version}")
        if str(lecture["lecture_id"]) != str(lecture_id):
            raise AttendanceScopeError("lecture scope leaked")

        started = time.monotonic()
        run_id = (self.run_repository.start(connection, lecture["session_date"],
                                            self.versions)
                  if persist else uuid.uuid4())
        counters = {name: 0 for name in (
            "lectures_considered", "snapshots_created", "snapshots_reused",
            "snapshot_members_written", "speakers_considered", "resolutions_created",
            "resolutions_updated", "roles_created", "roles_updated", "exact_matches",
            "normalized_exact_matches", "legacy_fuzzy_matches", "ambiguous_matches",
            "unmatched_speakers", "error_count")}
        counters["lectures_considered"] = 1
        result = self._resolve_lecture(connection, lecture, counters, persist)
        result.pop("_trainer_label", None)

        summary = {
            "run_id": str(run_id),
            "target_date": lecture["session_date"].isoformat(),
            "scope": "LECTURE", "scope_value": str(lecture_id),
            "lectures_in_scope": 1,
            "mode": "SHADOW" if persist else "DRY_RUN", "status": "COMPLETED",
            **self.versions, **counters,
            "deterministic_trainer_source": DETERMINISTIC_TRAINER_SOURCE,
            "graph_calls": 0, "webvtt_parsed": False, "calendar_lookups": 0,
            "aptem_queries": 0, "ai_calls": 0,
            "engagement_calculated": False, "ai_trainer_override_applied": False,
            "global_person_master_created": False,
            "lectures": [result], "legacy_trainer_parity": None,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "metadata": {**self.versions, "scope": "LECTURE",
                         "scope_value": str(lecture_id),
                         "source": "SPEAKER_INVENTORY_AND_ATTENDANCE_SNAPSHOT",
                         "counters": counters},
        }
        if persist:
            self.run_repository.complete(connection, run_id, summary)
        return summary

    def resolve_day(self, connection, target_date, *, persist: bool = True) -> dict:
        started = time.monotonic()
        run_id = (self.run_repository.start(connection, target_date, self.versions)
                  if persist else uuid.uuid4())

        lectures = self.inventory_repository.load_day(
            connection, target_date, self.inventory_version, self.parser_version)

        counters = {name: 0 for name in (
            "lectures_considered", "snapshots_created", "snapshots_reused",
            "snapshot_members_written", "speakers_considered", "resolutions_created",
            "resolutions_updated", "roles_created", "roles_updated", "exact_matches",
            "normalized_exact_matches", "legacy_fuzzy_matches", "ambiguous_matches",
            "unmatched_speakers", "error_count")}
        counters["lectures_considered"] = len(lectures)
        results = []

        for lecture in lectures:
            results.append(self._resolve_lecture(connection, lecture, counters, persist))

        legacy_trainer = None
        if self.legacy_diagnostic_repository is not None:
            legacy_trainer = self._legacy_trainer_parity(connection, target_date, results)
        # The trainer's raw label is needed for the parity comparison above and
        # for nothing else. It never reaches the returned summary or any log.
        for result in results:
            result.pop("_trainer_label", None)

        summary = {
            "run_id": str(run_id), "target_date": target_date.isoformat(),
            "mode": "SHADOW" if persist else "DRY_RUN", "status": "COMPLETED",
            **self.versions, **counters,
            "deterministic_trainer_source": DETERMINISTIC_TRAINER_SOURCE,
            # Explicit zero markers for the boundaries this phase must respect.
            "graph_calls": 0, "webvtt_parsed": False, "calendar_lookups": 0,
            "aptem_queries": 0, "ai_calls": 0,
            "engagement_calculated": False, "ai_trainer_override_applied": False,
            "global_person_master_created": False,
            "lectures": results,
            "legacy_trainer_parity": legacy_trainer,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "metadata": {
                **self.versions,
                "source": "SPEAKER_INVENTORY_AND_ATTENDANCE_SNAPSHOT",
                "engagement_calculated": False,
                "ai_trainer_override_applied": False,
                "counters": counters,
            },
        }
        if persist:
            self.run_repository.complete(connection, run_id, summary)
        # Counts only: no names, emails, roster contents or transcript text.
        self.log.info("speaker resolution completed", extra={"fields": {
            "service": "speaker_resolution", "operation": "resolve_day",
            "run_id": str(run_id), "status": summary["status"],
            "lectures": len(results),
            "speakers_considered": counters["speakers_considered"],
            "duration_ms": summary["duration_ms"],
        }})
        return summary

    def _resolve_lecture(self, connection, lecture, counters, persist) -> dict:
        module = lecture["module"] or lecture["subject"] or ""
        module_normalized = normalize_group(module)
        source_rows = self.attendance_repository.load_present_rows(
            connection, lecture["session_date"], module_normalized)
        roster = build_roster(source_rows, version=self.attendance_resolution_version)
        # Phase 3C3E. The roster query filters `Attendance = 1`, so an empty
        # roster cannot say whether the source was silent or only recorded
        # absences. This unfiltered count answers that, and nothing else: it
        # contributes no member and is deliberately NOT in the fingerprint, so
        # every existing snapshot id stays exactly what it was.
        unfiltered = self.attendance_repository.count_rows_any_status(
            connection, lecture["session_date"], module_normalized)
        members = roster["members"]

        fingerprint = roster_fingerprint(
            session_date=lecture["session_date"], module_normalized=module_normalized,
            members=members, resolution_version=self.attendance_resolution_version)
        snapshot_id = attendance_snapshot_identity(
            lecture_id=lecture["lecture_id"],
            attendance_resolution_version=self.attendance_resolution_version,
            source_fingerprint=fingerprint)
        for member in members:
            member["member_id"] = attendance_member_identity(
                snapshot_id=snapshot_id, dedup_key=member["dedup_key"])

        snapshot = {
            "snapshot_id": snapshot_id, "lecture_id": lecture["lecture_id"],
            "attendance_resolution_version": self.attendance_resolution_version,
            "session_date": lecture["session_date"], "module": module,
            "module_normalized": module_normalized,
            "source_fingerprint": fingerprint, "bot_filter_version": BOT_FILTER_VERSION,
            **{key: roster[key] for key in (
                "source_row_count", "present_row_count", "excluded_bot_count",
                "excluded_no_name_count", "deduplicated_count", "effective_member_count")},
            "metadata": {"attendance_source": "public.kbc_attendance",
                         "query_contract": "exact session_date + exact normalized module "
                                           "+ Attendance = 1",
                         "bot_filter_version": BOT_FILTER_VERSION,
                         "roster_rule": roster_rule(self.attendance_resolution_version),
                         "excluded_makeup_count": roster["excluded_makeup_count"],
                         "duplicate_learner_id_count": roster["duplicate_learner_id_count"],
                         "source_rows_any_status": unfiltered["source_rows_any_status"],
                         "source_rows_present": unfiltered["source_rows_present"]},
        }

        speakers = lecture["speakers"]
        counters["speakers_considered"] += len(speakers)

        resolutions = []
        resolutions_by_speaker = {}
        legacy_differences = []
        for speaker in speakers:
            outcome = resolve_speaker(speaker["speaker_label_raw"], members)
            resolutions_by_speaker[speaker["speaker_id"]] = outcome
            matched = outcome["matched_member"]
            resolutions.append({
                "resolution_id": speaker_resolution_identity(
                    speaker_id=speaker["speaker_id"], attendance_snapshot_id=snapshot_id,
                    resolver_version=self.resolver_version),
                "speaker_id": speaker["speaker_id"],
                "resolution_status": outcome["resolution_status"],
                "matched_source": "KBC_ATTENDANCE_SNAPSHOT" if matched else None,
                "matched_member_id": matched["member_id"] if matched else None,
                # The stable external identifier from the source row, never a
                # value inferred from a name.
                "matched_person_id": matched["external_person_id"] if matched else None,
                "match_method": outcome["match_method"],
                "match_score": outcome["match_score"],
                "candidate_count": outcome["candidate_count"],
                "metadata": {
                    "stage": outcome["stage"],
                    "resolver_version": self.resolver_version,
                    "candidate_person_ids": outcome.get("candidate_person_ids"),
                },
            })
            counters[STATUS_COUNTER[outcome["resolution_status"]]] += 1

            # Read-only parity probe against the unsafe legacy boolean.
            legacy_match = legacy_would_match(speaker["speaker_label_raw"], members)
            safe_match = outcome["resolution_status"] in MATCHED_STATUSES
            if legacy_match != safe_match:
                legacy_differences.append({
                    "speaker_id": str(speaker["speaker_id"]),
                    "legacy_would_match": legacy_match,
                    "safe_resolver_status": outcome["resolution_status"],
                    "candidate_count": outcome["candidate_count"],
                })

        roles = assign_roles(speakers, resolutions_by_speaker)
        role_rows = [{
            "role_id": speaker_role_identity(
                speaker_id=role["speaker_id"],
                role_algorithm_version=self.role_algorithm_version,
                resolver_version=self.resolver_version,
                attendance_snapshot_id=snapshot_id),
            "speaker_id": role["speaker_id"], "role": role["role"],
            "role_source": role["role_source"], "role_rank": role["role_rank"],
            "metadata": {
                "role_algorithm_version": self.role_algorithm_version,
                "deterministic_trainer_source": (
                    DETERMINISTIC_TRAINER_SOURCE if role["role"] == TRAINER_CANDIDATE else None),
                "ai_trainer_override_applied": False,
                "gross_spoken_ms": role["gross_spoken_ms"],
                "person_resolution_status": role["person_resolution_status"],
                "trainer_also_matched_attendance": role["trainer_also_matched_attendance"],
            },
        } for role in roles]

        by_speaker_label = {speaker["speaker_id"]: speaker["speaker_label_raw"]
                            for speaker in speakers}
        trainer = next((role for role in roles if role["role"] == TRAINER_CANDIDATE), None)
        trainer_label = by_speaker_label.get(trainer["speaker_id"]) if trainer else None

        if persist:
            snapshot_outcome = self.snapshot_repository.upsert(connection, snapshot, members)
            counters["snapshots_created"] += snapshot_outcome["created"]
            counters["snapshots_reused"] += snapshot_outcome["reused"]
            counters["snapshot_members_written"] += snapshot_outcome["members_written"]
            identity_outcome = self.identity_repository.upsert(
                connection, snapshot_id, self.resolver_version, resolutions)
            counters["resolutions_created"] += identity_outcome["created"]
            counters["resolutions_updated"] += identity_outcome["updated"]
            role_outcome = self.role_repository.upsert(
                connection, snapshot_id, self.role_algorithm_version,
                self.resolver_version, role_rows)
            counters["roles_created"] += role_outcome["created"]
            counters["roles_updated"] += role_outcome["updated"]

        status_counts = {status: 0 for status in STATUS_COUNTER}
        for outcome in resolutions_by_speaker.values():
            status_counts[outcome["resolution_status"]] += 1

        return {
            "lecture_id": str(lecture["lecture_id"]), "subject": lecture["subject"],
            "document_id": str(lecture["document_id"]),
            "module_normalized": module_normalized,
            "attendance_snapshot_id": str(snapshot_id),
            # Prefix only: enough to prove stability, not enough to leak a roster.
            "source_fingerprint_prefix": fingerprint[:16],
            "attendance_source_row_count": roster["source_row_count"],
            "attendance_present_row_count": roster["present_row_count"],
            "attendance_source_rows_any_status": unfiltered["source_rows_any_status"],
            "attendance_resolution_version": self.attendance_resolution_version,
            "excluded_makeup_count": roster["excluded_makeup_count"],
            "excluded_bot_count": roster["excluded_bot_count"],
            "excluded_no_name_count": roster["excluded_no_name_count"],
            "duplicate_learner_id_count": roster["duplicate_learner_id_count"],
            "deduplicated_count": roster["deduplicated_count"],
            "effective_attendance_count": roster["effective_member_count"],
            "speaker_count": len(speakers),
            "exact_matches": status_counts[EXACT_MATCH],
            "normalized_exact_matches": status_counts[NORMALIZED_EXACT_MATCH],
            "legacy_fuzzy_matches": status_counts[LEGACY_FUZZY_MATCH],
            "ambiguous_matches": status_counts[AMBIGUOUS],
            "unmatched_speakers": status_counts[NO_MATCH] + status_counts[NOT_APPLICABLE],
            "resolved_learner_count": sum(1 for role in roles if role["role"] == LEARNER),
            "other_or_unresolved_count": sum(
                1 for role in roles if role["role"] == OTHER_OR_UNRESOLVED),
            "trainer_candidate_speaker_id": str(trainer["speaker_id"]) if trainer else None,
            "trainer_candidate_label_length": len(trainer_label or "") if trainer else None,
            "trainer_candidate_gross_spoken_ms": trainer["gross_spoken_ms"] if trainer else None,
            "trainer_also_matched_attendance": bool(
                trainer and trainer["trainer_also_matched_attendance"]),
            "trainer_person_resolution_status": (
                trainer["person_resolution_status"] if trainer else None),
            "legacy_vs_safe_differences": legacy_differences,
            # Kept out of the report body; the label itself is never logged.
            "_trainer_label": trainer_label,
        }

    def _legacy_trainer_parity(self, connection, target_date, results) -> dict:
        """
        READ-ONLY: does the deterministic top-speaker rule reproduce the stored
        legacy trainer?

        Exact comparisons only. Nothing is forced to agree, and no role is
        changed by the outcome.
        """
        legacy_rows = self.legacy_diagnostic_repository.load_trainers(connection, target_date)
        by_subject = {normalize_group(item["subject"]): item for item in results}
        rows = []
        exact_raw = exact_normalized = 0
        for legacy in legacy_rows:
            result = by_subject.get(normalize_group(legacy["subject"] or ""))
            legacy_trainer = legacy["trainer"] or ""
            candidate = (result or {}).get("_trainer_label")
            raw_hit = candidate is not None and candidate == legacy_trainer
            normalized_hit = candidate is not None and (
                normalize_speaker_label(candidate) == normalize_speaker_label(legacy_trainer))
            exact_raw += raw_hit
            exact_normalized += normalized_hit
            rows.append({
                "subject": legacy["subject"],
                "deterministic_trainer_matches_legacy_raw": "YES" if raw_hit else "NO",
                "deterministic_trainer_matches_legacy_normalized":
                    "YES" if normalized_hit else "NO",
                "legacy_trainer_label_length": len(legacy_trainer),
                "deterministic_trainer_label_length": len(candidate or "") if candidate else None,
            })
        return {
            "qa_rows": len(legacy_rows),
            "exact_raw_matches": exact_raw,
            "exact_normalized_matches": exact_normalized,
            "summary_raw": f"{exact_raw} / {len(legacy_rows)}",
            "summary_normalized": f"{exact_normalized} / {len(legacy_rows)}",
            "note": "Diagnostic only. The VTT top-speaker rule is never adjusted to "
                    "improve this number, and no AI override is applied.",
            "rows": rows,
        }
