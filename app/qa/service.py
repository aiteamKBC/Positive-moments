"""
Phase 3A orchestration: persisted evidence -> shadow QA evaluation.

Flow per lecture:

  1. assemble the deterministic input package from earlier phases;
  2. apply the legacy delivery gate — under 20 minutes never reaches the model;
  3. in preview mode, stop and report (no provider call, no write);
  4. reuse an existing successful evaluation with the same fingerprint unless
     forced, so a rerun costs nothing;
  5. call the model, validate the structured output, validate every evidence
     timestamp against the canonical cues;
  6. apply the deterministic Items 1, 2 and 7 over the model's answers;
  7. persist to the shadow tables only.

Never: Graph, live attendance, transcript rebuild, speaker rematching,
engagement recalculation, or a write to any legacy QA table.
"""
import logging
import time
import uuid
from decimal import Decimal

from app.qa.checklist import (
    CHECKLIST_ITEMS,
    DURATION_ITEM,
    ENGAGEMENT_ITEM,
    NOT_MET,
    PUNCTUALITY_ITEM,
    SOURCE_AI,
    SOURCE_DETERMINISTIC_DURATION,
    SOURCE_DETERMINISTIC_ENGAGEMENT,
    SOURCE_DETERMINISTIC_PUNCTUALITY,
)
from app.qa.deterministic import (
    DELIVERED,
    NON_DELIVERED,
    duration_score,
    duration_text,
    item1_status,
    item2_status,
    non_delivered_checklist,
    non_delivered_summary,
)
from app.qa.delivery import (
    DELIVERY_POLICY_VERSION,
    TRANSCRIPT_COVERAGE_INCOMPLETE,
    classify_delivery,
)
from app.qa.inputs import (
    KSB_FRAMEWORK,
    QA_ENGINE_VERSION,
    MODEL_INPUT_FINGERPRINT_VERSION,
    REQUIRED_ATTENDANCE_ROSTER_VERSION,
    preview,
    qa_model_input_fingerprint,
    qa_source_fingerprint,
)
from app.qa.punctuality import (
    CANONICAL_CUE_BOUNDS,
    PUNCTUALITY_SOURCE_VERSIONS,
    PunctualitySourceError,
    derive_bounds,
    difference_minutes,
    punctuality_provenance,
)
from app.qa.prompt import (
    PROMPT_VERSION,
    STRUCTURED_SCHEMA_VERSION,
    SYSTEM_MESSAGE,
    build_user_message,
    prompt_sha256,
)
from app.qa.generation_budget import generation_budget
from app.qa.provider import (
    PROVIDER_CONFIGURATION_BLOCKED,
    PROVIDER_CONFIGURATION_ERROR,
    ProviderCircuit,
    ProviderError,
)
from app.qa.structured_output import (
    DEFAULT_PROVIDER_CONTRACT,
    contract_provenance,
    validate_contract,
)
from app.qa.validation import (
    VALID,
    checklist_item_whitespace_variants,
    collect_clips,
    validate_clip,
    validate_structured_output,
)
from app.lectures.matching import normalize_group
from app.transcripts.speakers import normalize_speaker_label
from app.attendance.coverage import classify as classify_coverage
from app.attendance.coverage import is_authoritative
from app.qa.evidence_policy import (
    DEFAULT_EVIDENCE_POLICY,
    EVIDENCE_POLICY_V1,
    EVIDENCE_POLICY_VERSIONS,
    assess as assess_evidence,
)
from app.transcripts.webvtt import PARSER_VERSION


PENDING = "PENDING"
COMPLETED = "COMPLETED"
MODEL_ERROR = "MODEL_ERROR"
INVALID_STRUCTURED_OUTPUT = "INVALID_STRUCTURED_OUTPUT"
INVALID_EVIDENCE = "INVALID_EVIDENCE"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
# Not a QA outcome and never persisted: the attendance source has not answered,
# so there is nothing to evaluate yet. Named after the orchestrator's own
# WAIT_FOR_ATTENDANCE_SOURCE so the operator path and the scheduler describe
# the same situation with the same words.
WAITING_FOR_ATTENDANCE_SOURCE = "WAITING_FOR_ATTENDANCE_SOURCE"
ATTENDANCE_NOT_AUTHORITATIVE = "ATTENDANCE_NOT_AUTHORITATIVE"

# Phase 2C4 statuses that must not silently become a finished QA result.
ENGAGEMENT_REVIEW_STATUSES = frozenset(
    {"REVIEW_AMBIGUITY_MAY_CHANGE_RESULT", "TRAINER_ATTENDANCE_AMBIGUOUS"})

TRAINER_SOURCE_DETERMINISTIC = "PHASE_2C3_VTT_TOP_SPEAKER"

# Bounded model generations per QA source fingerprint (Phase 3C1 hardening).
# Production must never pay for unlimited re-generation of a response the
# validators keep rejecting. Provider-level HTTP retries inside ONE call are a
# separate, already-bounded concern.
MAX_MODEL_GENERATIONS = 3
UNSUCCESSFUL_OUTCOMES = frozenset(
    {MODEL_ERROR, INVALID_STRUCTURED_OUTPUT, INVALID_EVIDENCE})
MAX_GENERATIONS_EXHAUSTED = "MAX_GENERATIONS_EXHAUSTED"

# Phase 3C3E. A QA result recomputed from new DETERMINISTIC evidence while
# reusing the frozen model answer. Versioned because it is a genuinely
# different way of producing an evaluation and must stay legible as such.
# Phase 4B. A stored model answer RE-JUDGED under a newer evidence policy. No
# provider call, no new generation, no new attempt: the same answer, assessed
# by the rule that governs current work.
EVIDENCE_REVALIDATION_VERSION = "qa_evidence_revalidation_v1"
EVIDENCE_REVALIDATED = "EVIDENCE_REVALIDATED"
EVIDENCE_STILL_INVALID = "EVIDENCE_STILL_INVALID"
NO_STORED_OUTPUT_TO_REVALIDATE = "NO_STORED_OUTPUT_TO_REVALIDATE"
EVIDENCE_POLICY_UNCHANGED = "EVIDENCE_POLICY_UNCHANGED"

DETERMINISTIC_REFRESH_VERSION = "qa_deterministic_refresh_v1"
DETERMINISTIC_REFRESH_REASON = "ATTENDANCE_EVIDENCE_ARRIVED"

QA_REUSED = "QA_REUSED"
QA_DETERMINISTIC_REFRESHED = "QA_DETERMINISTIC_REFRESHED"
NO_REUSABLE_MODEL_OUTPUT = "NO_REUSABLE_MODEL_OUTPUT"


class QaInputError(RuntimeError):
    """The persisted evidence cannot support a QA run; never a model problem."""


class ShadowQaService:
    def __init__(self, *, input_repository, evaluation_repository, run_repository,
                 legacy_repository=None, provider=None, attempt_repository=None,
                 max_model_generations: int = MAX_MODEL_GENERATIONS,
                 engine_version: str = QA_ENGINE_VERSION,
                 parser_version: str = PARSER_VERSION,
                 attendance_roster_version: str = REQUIRED_ATTENDANCE_ROSTER_VERSION,
                 resolver_version: str, role_algorithm_version: str,
                 engagement_algorithm_version: str, model_name: str,
                 punctuality_source_version: str = CANONICAL_CUE_BOUNDS,
                 provider_contract_version: str = DEFAULT_PROVIDER_CONTRACT,
                 evidence_policy_version: str = DEFAULT_EVIDENCE_POLICY,
                 lecture_ids=None):
        if attendance_roster_version != REQUIRED_ATTENDANCE_ROSTER_VERSION:
            # The makeup-inclusive v1 evidence is still on record on purpose;
            # consuming it for QA would reintroduce the resolved drift.
            raise QaInputError(
                f"Phase 3A requires {REQUIRED_ATTENDANCE_ROSTER_VERSION}, "
                f"refusing {attendance_roster_version}")
        self.input_repository = input_repository
        self.evaluation_repository = evaluation_repository
        self.run_repository = run_repository
        self.legacy_repository = legacy_repository
        self.provider = provider
        # Optional so existing callers keep working; without it the cap simply
        # cannot be enforced, and that is reported rather than assumed.
        self.attempt_repository = attempt_repository
        self.max_model_generations = max_model_generations
        self.engine_version = engine_version
        self.parser_version = parser_version
        if punctuality_source_version not in PUNCTUALITY_SOURCE_VERSIONS:
            raise QaInputError(
                f"unknown punctuality source {punctuality_source_version}")
        # Phase 3C3B default. The legacy call-bounds source is still selectable
        # by name, so an old evaluation can be reproduced deliberately rather
        # than by accident.
        self.punctuality_source_version = punctuality_source_version
        # Phase 3C3D. Which contract the provider is asked to enforce. It is
        # part of the QA source fingerprint, so switching it opens a fresh
        # bounded generation budget instead of resetting the old one.
        self.provider_contract_version = validate_contract(provider_contract_version)
        if (self.provider is not None
                and getattr(self.provider, "response_contract", None) is not None
                and self.provider.response_contract != self.provider_contract_version):
            # Two names for one contract is how provenance starts lying.
            raise QaInputError(
                f"provider is configured for {self.provider.response_contract} "
                f"but the service declares {self.provider_contract_version}")
        # Phase 4B. What an invalid evidence clip means for the evaluation.
        # Deliberately NOT part of the QA source fingerprint: it changes how a
        # returned answer is JUDGED, never what the provider was asked, so the
        # same generation stays reusable and no existing evaluation is orphaned
        # into buying itself again.
        if evidence_policy_version not in EVIDENCE_POLICY_VERSIONS:
            raise QaInputError(
                f"unknown evidence policy {evidence_policy_version}")
        self.evidence_policy_version = evidence_policy_version
        self.attendance_roster_version = attendance_roster_version
        self.resolver_version = resolver_version
        self.role_algorithm_version = role_algorithm_version
        self.engagement_algorithm_version = engagement_algorithm_version
        self.model_name = model_name
        # Optional scope so a controlled single-lecture run cannot spend a
        # provider call on any other lecture of the day.
        self.lecture_ids = {str(value) for value in lecture_ids} if lecture_ids else None
        self.log = logging.getLogger(__name__)
        # Replaced at the start of every run_day; closed until a credential
        # rejection opens it.
        self._circuit = ProviderCircuit()

    @property
    def versions(self) -> dict:
        return {
            "qa_engine_version": self.engine_version,
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": prompt_sha256(model=self.model_name),
            "structured_schema_version": STRUCTURED_SCHEMA_VERSION,
            "provider_contract_version": self.provider_contract_version,
            "evidence_policy_version": self.evidence_policy_version,
            "model_name": self.model_name,
            "attendance_roster_version": self.attendance_roster_version,
            "parser_version": self.parser_version,
            "punctuality_source_version": self.punctuality_source_version,
            "resolver_version": self.resolver_version,
            "role_algorithm_version": self.role_algorithm_version,
            "engagement_algorithm_version": self.engagement_algorithm_version,
        }

    def run_day(self, connection, target_date, *, execute: bool = False,
                force: bool = False) -> dict:
        started = time.monotonic()
        mode = "SHADOW" if execute else "PREVIEW"
        run_id = self.run_repository.start(
            connection, target_date, mode=mode, engine_version=self.engine_version,
            prompt_version=PROMPT_VERSION, model_name=self.model_name)

        packages = self._load_packages(connection, target_date)
        counters = {name: 0 for name in (
            "lectures_considered", "delivered_count", "non_delivered_count", "provider_calls",
            "reused_evaluations", "evaluations_created", "evaluations_updated",
            "review_required_count", "error_count", "attendance_waiting_count",
            "coverage_incomplete_count", "provider_configuration_failures",
            "provider_configuration_blocked_count")}
        counters["lectures_considered"] = len(packages)
        results = []
        # The same circuit the orchestrator holds per cycle, held here per
        # run: after the first credential rejection, no other lecture of this
        # run is sent to the provider.
        self._circuit = ProviderCircuit()

        for package in packages:
            if package["delivery_status"] == DELIVERED:
                counters["delivered_count"] += 1
            elif package["delivery_status"] == NON_DELIVERED:
                counters["non_delivered_count"] += 1
            else:
                counters["coverage_incomplete_count"] += 1
            results.append(self._evaluate(connection, package, counters,
                                          execute=execute, force=force))

        comparison = None
        if self.legacy_repository is not None:
            comparison = self._legacy_comparison(connection, target_date, results)

        summary = {
            "run_id": str(run_id), "target_date": target_date.isoformat(),
            "mode": mode, "status": "COMPLETED", **self.versions, **counters,
            # Explicit boundary markers for this phase.
            "graph_calls": 0, "live_attendance_queries": 0, "lms_queries": 0,
            "transcript_rebuilds": 0, "speaker_rematches": 0,
            "engagement_recalculations": 0, "legacy_qa_writes": 0,
            "lectures": results, "legacy_comparison": comparison,
            "provider_circuit": self._circuit.report(blocked=[
                row["lecture_id"] for row in results
                if row.get("qa_status") == PROVIDER_CONFIGURATION_BLOCKED]),
            "duration_ms": round((time.monotonic() - started) * 1000),
            "metadata": {**self.versions, "counters": counters, "mode": mode,
                         "legacy_qa_writes": 0, "ksb_framework_supplied": bool(KSB_FRAMEWORK)},
        }
        self.run_repository.complete(connection, run_id, summary)
        # Counts and ids only: no transcript text, no prompt, no names, no key.
        self.log.info("shadow qa completed", extra={"fields": {
            "service": "shadow_qa", "operation": "run_day", "run_id": str(run_id),
            "mode": mode, "lectures": len(results),
            "provider_calls": counters["provider_calls"],
            "duration_ms": summary["duration_ms"],
        }})
        return summary

    def _load_packages(self, connection, target_date) -> list[dict]:
        rows = self.input_repository.load_inputs(
            connection, target_date, parser_version=self.parser_version,
            attendance_roster_version=self.attendance_roster_version,
            resolver_version=self.resolver_version,
            role_algorithm_version=self.role_algorithm_version,
            engagement_algorithm_version=self.engagement_algorithm_version)
        if not rows:
            raise QaInputError(
                f"no {self.attendance_roster_version} engagement evidence for {target_date}")
        seen: dict = {}
        for row in rows:
            if row["lecture_id"] in seen:
                # Two "current" results for one lecture under one version
                # contract means the provenance is inconsistent; guessing
                # between them would be worse than stopping.
                raise QaInputError(
                    f"more than one current QA input for lecture {row['lecture_id']}")
            if row["attendance_roster_version"] != REQUIRED_ATTENDANCE_ROSTER_VERSION:
                raise QaInputError(
                    f"unexpected roster version {row['attendance_roster_version']}")
            # Punctuality first: it keeps the provider call bounds as
            # call_actual_start / call_actual_end, which the delivery policy
            # needs as its independent evidence.
            self._apply_punctuality_source(row)
            self._classify_delivery(row)
            seen[row["lecture_id"]] = row
        packages = list(seen.values())
        if self.lecture_ids is not None:
            packages = [row for row in packages
                        if str(row["lecture_id"]) in self.lecture_ids]
            if not packages:
                raise QaInputError(
                    f"no QA input for the requested lecture scope on {target_date}")
        return packages

    def _classify_delivery(self, row) -> None:
        """
        The versioned delivery policy, over the provider CALL bounds and the
        scheduled window. See app/qa/delivery.py for why a short transcript is
        no longer, on its own, proof that a lecture was not delivered.
        """
        decision = classify_delivery(
            duration_minutes=row["duration_minutes"],
            scheduled_start=row["scheduled_start"], scheduled_end=row["scheduled_end"],
            call_start=row["call_actual_start"], call_end=row["call_actual_end"],
            transcript_span_seconds=(float(row["duration_seconds"])
                                     if row.get("duration_seconds") is not None else None))
        row["delivery"] = decision
        row["delivery_status"] = decision.classification

    def _apply_punctuality_source(self, row) -> None:
        """
        Phase 3C3B: bind Item 2 to an explicit, versioned punctuality source.

        Phase 2B persisted `actual_start` / `actual_end` as the TEAMS CALL
        bounds. Under `canonical_cue_bounds_v1` the lecture's own first and
        last surviving cue are used instead, converted to wall clock through
        the call-start instant. Thresholds are untouched; only the definition
        of "started" and "ended" moves.

        The call bounds stay in the package as
        `call_actual_start` / `call_actual_end`, so nothing is lost and the
        two sources can be compared after the fact.
        """
        row["call_actual_start"] = row["actual_start"]
        row["call_actual_end"] = row["actual_end"]
        try:
            actual_start, actual_end = derive_bounds(
                call_start=row["actual_start"], call_end=row["actual_end"],
                first_cue_start_ms=row.get("first_cue_start_ms"),
                last_cue_end_ms=row.get("last_cue_end_ms"),
                source_version=self.punctuality_source_version)
            source = self.punctuality_source_version
        except PunctualitySourceError as error:
            # Refuse rather than silently fall back to a different source: an
            # Item 2 whose provenance is ambiguous is worse than a stopped run.
            raise QaInputError(
                f"punctuality source {self.punctuality_source_version} unusable for "
                f"lecture {row['lecture_id']}: {error}") from error

        row["actual_start"] = actual_start
        row["actual_end"] = actual_end
        row["start_difference_minutes"] = difference_minutes(
            actual_start, row["scheduled_start"])
        row["end_difference_minutes"] = difference_minutes(
            actual_end, row["scheduled_end"])
        row["punctuality"] = punctuality_provenance(
            source_version=source,
            scheduled_start=row["scheduled_start"], scheduled_end=row["scheduled_end"],
            actual_start=actual_start, actual_end=actual_end)
        row["punctuality"]["call_actual_start"] = _iso(row["call_actual_start"])
        row["punctuality"]["call_actual_end"] = _iso(row["call_actual_end"])
        row["punctuality_source_version"] = source

    def _evaluate(self, connection, package, counters, *, execute, force) -> dict:
        package["provider_contract_version"] = self.provider_contract_version
        fingerprint = qa_source_fingerprint(
            package=package, model=self.model_name,
            engine_version=self.engine_version,
            provider_contract_version=self.provider_contract_version)
        result = {
            **preview(package),
            "source_fingerprint": fingerprint,
            "source_fingerprint_prefix": fingerprint[:16],
            "prompt_version": PROMPT_VERSION,
            "model_name": self.model_name,
            "provider_contract_version": self.provider_contract_version,
            "ai_called": False, "provider_calls": 0,
        }

        coverage = package_attendance_coverage(package)
        result["attendance_coverage_status"] = coverage
        result["attendance_source_authoritative"] = is_authoritative(coverage)

        if not execute:
            result["qa_status"] = PENDING
            result["mode"] = "PREVIEW"
            return result

        existing = self.evaluation_repository.find_by_fingerprint(connection, fingerprint)

        # F-03. The attendance gate used to live ONLY in the orchestrator's
        # resolver, so an operator running this service directly could finalize
        # a lecture whose attendance source had never answered - which is how
        # two 11/11 evaluations came to rest on empty snapshots. The service now
        # asks the same question itself, with the same predicate, of the exact
        # snapshot it is about to consume. It checks BEFORE reuse as well: an
        # evaluation finalized on non-authoritative attendance must not be
        # re-reported as a current answer either.
        if not is_authoritative(coverage):
            counters["attendance_waiting_count"] = (
                counters.get("attendance_waiting_count", 0) + 1)
            result.update({
                "qa_status": WAITING_FOR_ATTENDANCE_SOURCE,
                "review_reason": ATTENDANCE_NOT_AUTHORITATIVE,
                "persisted": False, "ai_called": False, "provider_calls": 0,
                # Reported, never reused: the caller should see that a stale
                # answer exists without that answer being passed off as current.
                "existing_evaluation_id": (str(existing["evaluation_id"])
                                           if existing else None),
                "existing_qa_status": existing["qa_status"] if existing else None,
            })
            return result

        if existing and existing["qa_status"] in (COMPLETED, "NON_DELIVERED") and not force:
            # Same inputs, same prompt, same model, same engine: nothing to buy.
            counters["reused_evaluations"] += 1
            result.update({"qa_status": existing["qa_status"], "reused": True,
                           "evaluation_id": str(existing["evaluation_id"])})
            return result

        if package["delivery_status"] == TRANSCRIPT_COVERAGE_INCOMPLETE:
            # Never a model call: thirteen minutes of a two-hour lecture cannot
            # support a full checklist, and the answer to that is a human.
            if existing:
                counters["reused_evaluations"] += 1
                result.update({"qa_status": existing["qa_status"], "reused": True,
                               "review_reason": TRANSCRIPT_COVERAGE_INCOMPLETE,
                               "evaluation_id": str(existing["evaluation_id"])})
                return result
            return self._persist_coverage_incomplete(connection, package, fingerprint,
                                                     counters, result)

        if package["delivery_status"] == NON_DELIVERED:
            return self._persist_non_delivered(connection, package, fingerprint,
                                               counters, result)

        # Bounded re-generation: after MAX unsuccessful generations for the
        # same source fingerprint, scheduled processing stops calling the
        # model. Only an explicit force may try again. A call the provider
        # refused on the credential is on record but is not a generation.
        budget = self._budget(connection, fingerprint)
        used = budget["generation_budget_used"]
        if used >= self.max_model_generations and not force:
            counters["review_required_count"] += 1
            result.update({"qa_status": REVIEW_REQUIRED, "ai_called": False,
                           "provider_calls": 0, "generation_attempts": used,
                           "attempts_remaining": 0,
                           "max_model_generations": self.max_model_generations,
                           "review_reason": MAX_GENERATIONS_EXHAUSTED})
            evaluation_id = self._mark_review_required(
                connection, package, fingerprint, used)
            if evaluation_id:
                result["evaluation_id"] = str(evaluation_id)
            return result

        if self.provider is not None and self._circuit.is_open:
            # The credential was refused earlier in this run. Calling again
            # would be the same refusal; nothing is written and nothing is
            # spent, and the lecture stays exactly as retryable as it was.
            counters["provider_configuration_blocked_count"] += 1
            result.update({"qa_status": PROVIDER_CONFIGURATION_BLOCKED,
                           "review_reason": PROVIDER_CONFIGURATION_BLOCKED,
                           "persisted": False, "ai_called": False,
                           "provider_calls": 0, "generation_attempts": used,
                           "attempts_remaining": budget["attempts_remaining"],
                           "existing_evaluation_id": (str(existing["evaluation_id"])
                                                      if existing else None)})
            return result

        # Phase 3C3D. The generation number is known BEFORE the call, so the
        # evaluation can carry the authoritative count instead of the transport
        # retry count it used to store. It numbers the RECORD; what the call
        # costs the budget is decided by its outcome.
        records = budget["attempt_records"]
        outcome = self._persist_delivered(connection, package, fingerprint, counters,
                                          result, force,
                                          generation_number=records + 1,
                                          budget_used=used)
        self._record_attempt(connection, package, fingerprint, outcome, records,
                             force, used=used)
        return outcome

    def _budget(self, connection, fingerprint) -> dict:
        """The fingerprint's generation budget, under the shared policy."""
        if self.attempt_repository is None:
            return generation_budget([], max_generations=self.max_model_generations)
        reader = getattr(self.attempt_repository, "budget", None)
        if reader is None:
            # A ledger that can only count rows: every row is a generation,
            # which is exactly the rule it was written under.
            records = self.attempt_repository.count(connection, fingerprint)
            return generation_budget(
                [{"consumes_generation_budget": True}] * records,
                max_generations=self.max_model_generations)
        return reader(connection, fingerprint,
                      max_generations=self.max_model_generations)

    # -- Phase 3C3E: deterministic refresh -----------------------------------

    def refresh_deterministic(self, connection, lecture_id, *,
                              persist: bool = True) -> dict:
        """
        Recompute ONE lecture's QA from new deterministic evidence, reusing the
        frozen model answer.

        This is the path attendance recovery uses. Attendance never reached the
        provider - `build_user_message` sends the transcript, its ids, the
        schedule, the timing and the duration, and nothing at all about
        learners - so when attendance arrives, the model's answer to the
        question it was actually asked has not changed. Re-buying it would be
        paying twice for the same generation.

        What IS recomputed: Item 7's deterministic override, and therefore the
        Met / Partially Met / Not Met counts. Items 1 and 2 are recomputed from
        their own unchanged inputs and must come out identical; items 3-6 and
        8-11 come straight out of the frozen output.

        The reuse is licensed by the model-input fingerprint, never assumed. If
        no stored evaluation was asked exactly this question the refresh
        refuses and says so - it never quietly calls the model instead.
        """
        package = self._load_lecture_package(connection, lecture_id)
        fingerprint = qa_source_fingerprint(
            package=package, model=self.model_name,
            engine_version=self.engine_version,
            provider_contract_version=self.provider_contract_version)
        model_input = qa_model_input_fingerprint(
            package=package, model=self.model_name,
            engine_version=self.engine_version,
            provider_contract_version=self.provider_contract_version)

        result = {
            **preview(package),
            "source_fingerprint": fingerprint,
            "source_fingerprint_prefix": fingerprint[:16],
            "model_input_fingerprint": model_input,
            "model_input_fingerprint_prefix": model_input[:16],
            "deterministic_refresh_version": DETERMINISTIC_REFRESH_VERSION,
            "provider_contract_version": self.provider_contract_version,
            # The boundary this whole path exists to hold.
            "ai_called": False, "provider_calls": 0, "openai_reused": True,
            "graph_calls": 0, "transcript_reads": 0,
        }

        existing = self.evaluation_repository.find_by_fingerprint(connection, fingerprint)
        if existing and existing["qa_status"] == COMPLETED:
            # These deterministic inputs already produced this exact evaluation.
            result.update({"refresh_status": QA_REUSED, "qa_status": COMPLETED,
                           "evaluation_id": str(existing["evaluation_id"]),
                           "created": False, "checklist_changed": False})
            return result

        source = self.evaluation_repository.find_reusable_output(
            connection, package["lecture_id"], model_input)
        if source is None:
            result.update({"refresh_status": NO_REUSABLE_MODEL_OUTPUT,
                           "qa_status": None, "created": False,
                           "reason": "no stored evaluation was asked this exact question"})
            return result

        output = source["ai_raw_output"]
        errors = validate_structured_output(output)
        whitespace_variants = checklist_item_whitespace_variants(output)
        clips, invalid = self._validate_clips(connection, package, output)
        checklist, ai_item7, final_item7, counts, review = self._build_checklist(
            package, output, errors)

        previous = _previous_checklist_summary(connection, source["evaluation_id"])
        changed = (previous.get("final_item7_status") != final_item7
                   or previous.get("met_count") != counts["Met"]
                   or previous.get("partial_count") != counts["Partially Met"]
                   or previous.get("not_met_count") != counts["Not Met"])

        evidence = self._assess_evidence(clips)
        status = COMPLETED
        if errors:
            status = INVALID_STRUCTURED_OUTPUT
        elif not evidence["evaluation_usable"]:
            status = INVALID_EVIDENCE
        elif review:
            status = REVIEW_REQUIRED

        refresh_provenance = {
            "deterministic_refresh_version": DETERMINISTIC_REFRESH_VERSION,
            "reason": DETERMINISTIC_REFRESH_REASON,
            "origin_evaluation_id": str(source["evaluation_id"]),
            "origin_source_fingerprint": source["source_fingerprint"],
            "origin_model_input_fingerprint": source["metadata"].get(
                "model_input_fingerprint"),
            "origin_provider_contract_version": source["metadata"].get(
                "provider_contract_version"),
            "previous_final_item7_status": previous.get("final_item7_status"),
            "new_final_item7_status": final_item7,
            "previous_item7_status_source": previous.get("item7_status_source"),
            "new_item7_status_source": (SOURCE_DETERMINISTIC_ENGAGEMENT
                                        if package["item7_override_applied"]
                                        else SOURCE_AI),
            "previous_counts": {"met": previous.get("met_count"),
                                "partial": previous.get("partial_count"),
                                "not_met": previous.get("not_met_count")},
            "new_counts": {"met": counts["Met"], "partial": counts["Partially Met"],
                           "not_met": counts["Not Met"]},
            "checklist_changed": changed,
            "attendance_snapshot_id": str(package["attendance_snapshot_id"]),
            "engagement_id": str(package["engagement_id"]),
            "engagement_source_fingerprint": package["engagement_source_fingerprint"],
            "openai_reused": True,
            "provider_calls": 0,
        }

        evaluation = {
            **self._common_evaluation(package, fingerprint),
            "delivery_status": DELIVERED, "cancelled_session": False,
            "duration_score": duration_score(package["duration_minutes"]),
            "duration_text": duration_text(package["duration_minutes"]),
            "canonical_trainer": package["canonical_trainer"],
            "canonical_trainer_speaker_id": package["canonical_trainer_speaker_id"],
            "trainer_source": TRAINER_SOURCE_DETERMINISTIC,
            "attended_count": package["attended_count"],
            "spoke_count": package["spoke_count"],
            "engagement_percentage": package["engagement_percentage"],
            "engagement_score": package["engagement_score"],
            "item7_override_applied": package["item7_override_applied"],
            "qa_status": status,
            # The model WAS called - once, for the evaluation this reuses. The
            # answer is that same artefact, so the flag stays true and the
            # provenance above records exactly where it came from.
            "ai_called": True,
            "provider_attempts": source.get("provider_attempts") or 1,
            "error_code": None,
            "model_reported": source.get("model_reported"),
            "ai_suggested_trainer": source.get("ai_suggested_trainer"),
            "ai_item7_status": ai_item7, "final_item7_status": final_item7,
            "met_count": counts["Met"], "partial_count": counts["Partially Met"],
            "not_met_count": counts["Not Met"],
            "teaching_quality_rating": _quality(output, "rating_1_5"),
            "teaching_quality_comments": _quality(output, "comments"),
            "overall_judgement": _summary_field(output, "overall_judgement"),
            "evidence_clip_count": len(clips),
            "invalid_evidence_clip_count": invalid,
            "evidence_assessment": evidence,
            "structured_output_error_count": len(errors),
            "review_reason": review,
            "ai_raw_output": output,
            "metadata": {
                **self.versions,
                **contract_provenance(self.provider_contract_version),
                "model_input_fingerprint": model_input,
                "model_input_fingerprint_version": MODEL_INPUT_FINGERPRINT_VERSION,
                "punctuality": package.get("punctuality"),
                "structured_output_errors": errors[:40],
                "checklist_item_whitespace_variants": whitespace_variants,
                "deterministic_refresh": refresh_provenance,
            },
        }

        summary = {
            "refresh_status": QA_DETERMINISTIC_REFRESHED, "qa_status": status,
            "checklist_changed": changed,
            "previous_final_item7_status": previous.get("final_item7_status"),
            "final_item7_status": final_item7,
            "met_count": counts["Met"], "partial_count": counts["Partially Met"],
            "not_met_count": counts["Not Met"],
            "origin_evaluation_id": str(source["evaluation_id"]),
        }
        if not persist:
            result.update({**summary, "created": False, "mode": "DRY_RUN"})
            return result

        written = self.evaluation_repository.upsert(connection, evaluation, checklist, clips)
        result.update({**summary, "evaluation_id": str(written["evaluation_id"]),
                       "created": bool(written["created"])})
        return result

    # -- Phase 4B: re-judge a stored answer under the current policy ---------

    def revalidate_evidence(self, connection, lecture_id, *,
                            persist: bool = True) -> dict:
        """
        Re-assess ONE lecture's stored model answer under the current evidence
        policy. Buys nothing.

        On 2026-09-18 two lectures were rejected for two and one degenerate
        evidence clips out of fifty-six and forty-eight, discarding complete,
        correct checklists. Under `evidence_policy_v2_exclude_degenerate_clips`
        those same answers are usable. Re-generating to discover that would pay
        the provider for an answer already sitting in the database.

        WHAT THIS IS NOT:

          * it is not a retry. No provider call is made, no generation attempt
            is recorded, and the bounded budget is neither consumed nor reset.
          * it is not a repair. No timestamp is rewritten; every clip keeps the
            status `validate_clip` gave it, and a fabricated coordinate still
            fails the evaluation exactly as before.
          * it is not a rerun. Discovery, transcripts, selection, cues,
            speakers, attendance and engagement are all untouched, and the
            source fingerprint does not move - the question asked of the
            provider has not changed, only the rule that judges its answer.

        The generation history in `lecture_qa_generation_attempts` is immutable
        and stays exactly as it was: generation 1 returned INVALID_EVIDENCE,
        and it always will have.
        """
        package = self._load_lecture_package(connection, lecture_id)
        fingerprint = qa_source_fingerprint(
            package=package, model=self.model_name,
            engine_version=self.engine_version,
            provider_contract_version=self.provider_contract_version)
        result = {
            "lecture_id": str(lecture_id), "scope": "LECTURE",
            "lectures_in_scope": 1,
            "mode": "REVALIDATE" if persist else "DRY_RUN",
            "source_fingerprint": fingerprint,
            "source_fingerprint_prefix": fingerprint[:16],
            "evidence_policy_version": self.evidence_policy_version,
            "revalidation_version": EVIDENCE_REVALIDATION_VERSION,
            # The boundaries, asserted as data on every path out of here.
            "provider_calls": 0, "graph_calls": 0, "generation_attempts_recorded": 0,
        }

        stored = self.evaluation_repository.find_stored_output(connection, fingerprint)
        if stored is None or stored.get("ai_raw_output") is None:
            result["revalidation_status"] = NO_STORED_OUTPUT_TO_REVALIDATE
            return result

        previous_policy = (stored["metadata"] or {}).get(
            "evidence_policy_version", EVIDENCE_POLICY_V1)
        result["previous_evidence_policy_version"] = previous_policy
        result["previous_qa_status"] = stored["qa_status"]
        if (previous_policy == self.evidence_policy_version
                and stored["qa_status"] != INVALID_EVIDENCE):
            # Already judged by this rule and not rejected by it: re-judging
            # cannot reach a different answer.
            result["revalidation_status"] = EVIDENCE_POLICY_UNCHANGED
            result["evaluation_id"] = str(stored["evaluation_id"])
            return result

        output = stored["ai_raw_output"]
        errors = validate_structured_output(output)
        whitespace_variants = checklist_item_whitespace_variants(output)
        clips, invalid = self._validate_clips(connection, package, output)
        checklist, ai_item7, final_item7, counts, review = self._build_checklist(
            package, output, errors)
        evidence = self._assess_evidence(clips)

        status = COMPLETED
        if errors:
            status = INVALID_STRUCTURED_OUTPUT
        elif not evidence["evaluation_usable"]:
            status = INVALID_EVIDENCE
        elif review:
            status = REVIEW_REQUIRED

        result.update({
            "revalidation_status": (EVIDENCE_REVALIDATED if status == COMPLETED
                                    else EVIDENCE_STILL_INVALID),
            "qa_status": status, "evidence_assessment": evidence,
            "evidence_clip_count": len(clips),
            "invalid_evidence_clip_count": invalid,
            "excluded_clip_count": evidence["excluded_clip_count"],
            "fatal_clip_count": evidence["fatal_clip_count"],
            "structured_output_error_count": len(errors),
            "met_count": counts["Met"], "partial_count": counts["Partially Met"],
            "not_met_count": counts["Not Met"],
            "final_item7_status": final_item7,
        })
        if not persist:
            result["created"] = False
            return result

        quality = output.get("teaching_quality") if isinstance(output, dict) else None
        summary = output.get("overall_summary") if isinstance(output, dict) else None
        generation_number = stored.get("provider_attempts") or 1
        metadata = dict(stored["metadata"] or {})
        metadata.update({
            **self.versions,
            "evidence_revalidation": {
                "revalidation_version": EVIDENCE_REVALIDATION_VERSION,
                "previous_qa_status": stored["qa_status"],
                "previous_evidence_policy_version": previous_policy,
                "evidence_policy_version": self.evidence_policy_version,
                "evidence_assessment": evidence,
                "provider_calls": 0,
                "generation_attempts_recorded": 0,
                # The generation this answer came from. Unchanged: re-judging
                # does not create a generation.
                "origin_generation_number": generation_number,
            },
        })

        evaluation = {
            **self._common_evaluation(package, fingerprint),
            "delivery_status": DELIVERED, "cancelled_session": False,
            "duration_score": duration_score(package["duration_minutes"]),
            "duration_text": duration_text(package["duration_minutes"]),
            "canonical_trainer": package["canonical_trainer"],
            "canonical_trainer_speaker_id": package["canonical_trainer_speaker_id"],
            "trainer_source": TRAINER_SOURCE_DETERMINISTIC,
            "attended_count": package["attended_count"],
            "spoke_count": package["spoke_count"],
            "engagement_percentage": package["engagement_percentage"],
            "engagement_score": package["engagement_score"],
            "item7_override_applied": package["item7_override_applied"],
            "qa_status": status, "ai_called": bool(stored["ai_called"]),
            "provider_attempts": generation_number, "error_code": None,
            "model_reported": stored.get("model_reported"),
            "ai_suggested_trainer": stored.get("ai_suggested_trainer"),
            "ai_item7_status": ai_item7, "final_item7_status": final_item7,
            "met_count": counts["Met"], "partial_count": counts["Partially Met"],
            "not_met_count": counts["Not Met"],
            "teaching_quality_rating": (quality or {}).get("rating_1_5")
            if isinstance(quality, dict) else None,
            "teaching_quality_comments": (quality or {}).get("comments")
            if isinstance(quality, dict) else None,
            "overall_judgement": (summary or {}).get("overall_judgement")
            if isinstance(summary, dict) else None,
            "evidence_clip_count": len(clips),
            "invalid_evidence_clip_count": invalid,
            "evidence_assessment": evidence,
            "structured_output_error_count": len(errors),
            "review_reason": review,
            # Byte-identical to what the provider returned. Re-judging never
            # edits the answer.
            "ai_raw_output": output,
            "metadata": metadata,
        }
        written = self.evaluation_repository.upsert(connection, evaluation, checklist,
                                                    clips)
        result.update({"evaluation_id": str(written["evaluation_id"]),
                       "created": bool(written["created"]),
                       "checklist_item_whitespace_variants": whitespace_variants})
        return result

    def _load_lecture_package(self, connection, lecture_id) -> dict:
        """The QA input package for EXACTLY one lecture. Never widens."""
        rows = self.input_repository.load_inputs_for_lecture(
            connection, lecture_id, parser_version=self.parser_version,
            attendance_roster_version=self.attendance_roster_version,
            resolver_version=self.resolver_version,
            role_algorithm_version=self.role_algorithm_version,
            engagement_algorithm_version=self.engagement_algorithm_version)
        if not rows:
            raise QaInputError(
                f"no {self.attendance_roster_version} QA input for lecture {lecture_id}")
        if len(rows) > 1:
            # The same refusal the date-wide loader makes, for the same reason.
            raise QaInputError(
                f"more than one current QA input for lecture {lecture_id}")
        row = rows[0]
        if row["attendance_roster_version"] != REQUIRED_ATTENDANCE_ROSTER_VERSION:
            raise QaInputError(
                f"unexpected roster version {row['attendance_roster_version']}")
        self._apply_punctuality_source(row)
        self._classify_delivery(row)
        row["provider_contract_version"] = self.provider_contract_version
        return row

    def _record_attempt(self, connection, package, fingerprint, outcome, records,
                        forced, *, used=None) -> None:
        """Append this call, and close the fingerprint if the budget is spent."""
        if self.attempt_repository is None or not outcome.get("ai_called"):
            return
        if used is None:
            used = records      # every earlier record was a generation
        status = outcome.get("qa_status")
        failure = outcome.get("provider_failure") or {}
        # Recorded either way - the refusal is audit history - but only a call
        # that could have produced an answer spends the budget.
        consumes = failure.get("failure_class") != PROVIDER_CONFIGURATION_ERROR
        used_after = used + (1 if consumes else 0)
        self.attempt_repository.record(connection, {
            "lecture_id": package["lecture_id"], "source_fingerprint": fingerprint,
            "qa_engine_version": self.engine_version, "prompt_version": PROMPT_VERSION,
            "model_name": self.model_name, "outcome": status,
            "error_code": outcome.get("error_code"),
            "structured_output_error_count": outcome.get("structured_output_error_count", 0),
            "invalid_evidence_clip_count": outcome.get("invalid_evidence_clip_count", 0),
            "forced": bool(forced),
            "metadata": {"generation_number": records + 1,
                         "consumes_generation_budget": consumes,
                         "generation_budget_used": used_after,
                         **failure},
        })
        outcome["generation_budget_used"] = used_after
        outcome["attempts_remaining"] = max(self.max_model_generations - used_after, 0)
        if (consumes and status in UNSUCCESSFUL_OUTCOMES
                and used_after >= self.max_model_generations):
            # Terminal for automated processing; the attempts stay on record.
            self._mark_review_required(connection, package, fingerprint, used_after)
            outcome["qa_status"] = REVIEW_REQUIRED
            outcome["review_reason"] = MAX_GENERATIONS_EXHAUSTED
            outcome["generation_attempts"] = used_after

    def _mark_review_required(self, connection, package, fingerprint, attempts):
        """Flip the stored evaluation to the terminal review state."""
        existing = self.evaluation_repository.find_by_fingerprint(connection, fingerprint)
        if existing is None:
            return None
        self.evaluation_repository.mark_review_required(
            connection, existing["evaluation_id"],
            reason=MAX_GENERATIONS_EXHAUSTED, attempts=attempts)
        return existing["evaluation_id"]

    def _persist_non_delivered(self, connection, package, fingerprint, counters, result) -> dict:
        summary = non_delivered_summary()
        checklist = non_delivered_checklist()
        evaluation = {
            **self._common_evaluation(package, fingerprint),
            "qa_status": "NON_DELIVERED", "delivery_status": NON_DELIVERED,
            "ai_called": False, "provider_attempts": 0, "error_code": None,
            "cancelled_session": True,
            "duration_score": summary["duration_score"],
            "duration_text": summary["duration_text"],
            # Legacy writes the literal "Session not delivered" as the trainer.
            "canonical_trainer": summary["trainer_display"],
            "canonical_trainer_speaker_id": None,
            "ai_suggested_trainer": None, "trainer_source": "NON_DELIVERED",
            "attended_count": None, "spoke_count": None,
            "engagement_percentage": summary["engagement_percentage"],
            "engagement_score": summary["engagement_score"],
            "ai_item7_status": None, "final_item7_status": NOT_MET,
            "item7_override_applied": False,
            "met_count": summary["met_count"], "partial_count": summary["partial_count"],
            "not_met_count": summary["not_met_count"],
            "teaching_quality_rating": summary["teaching_quality_rating"],
            "teaching_quality_comments": summary["teaching_quality_comments"],
            "overall_judgement": summary["overall_judgement"],
            "evidence_clip_count": 0, "invalid_evidence_clip_count": 0,
            "structured_output_error_count": 0, "review_reason": None,
            "ai_raw_output": None,
            "metadata": {**self.versions, "reason": "DURATION_BELOW_DELIVERY_MINIMUM",
                         "duration_minutes": package["duration_minutes"],
                         **_delivery_metadata(package)},
        }
        written = self.evaluation_repository.upsert(connection, evaluation, checklist, [])
        counters["evaluations_created"] += written["created"]
        counters["evaluations_updated"] += written["updated"]
        result.update({
            "qa_status": "NON_DELIVERED", "ai_called": False, "provider_calls": 0,
            "evaluation_id": str(written["evaluation_id"]),
            "cancelled_session": True, "met_count": 0, "partial_count": 0, "not_met_count": 11,
            "final_item7_status": NOT_MET, "checklist_rows": len(checklist),
        })
        return result

    def _persist_coverage_incomplete(self, connection, package, fingerprint,
                                     counters, result) -> dict:
        """
        The lecture appears to have run; the transcript cannot support QA.

        REVIEW_REQUIRED with reason TRANSCRIPT_COVERAGE_INCOMPLETE. The
        delivery status is DELIVERED because the independent call evidence says
        the session took place, but cancelled_session stays false, no checklist
        is written and no model answer exists: an empty verdict a human
        resolves, never a score invented from the fragment that was captured.
        """
        evaluation = {
            **self._common_evaluation(package, fingerprint),
            "qa_status": REVIEW_REQUIRED, "delivery_status": DELIVERED,
            "ai_called": False, "provider_attempts": 0, "error_code": None,
            "cancelled_session": False,
            "duration_score": None,
            "duration_text": duration_text(package["duration_minutes"]),
            "canonical_trainer": package["canonical_trainer"],
            "canonical_trainer_speaker_id": package["canonical_trainer_speaker_id"],
            "ai_suggested_trainer": None, "trainer_source": TRAINER_SOURCE_DETERMINISTIC,
            "attended_count": package["attended_count"],
            "spoke_count": package["spoke_count"],
            "engagement_percentage": package["engagement_percentage"],
            "engagement_score": package["engagement_score"],
            "ai_item7_status": None, "final_item7_status": None,
            "item7_override_applied": package["item7_override_applied"],
            "met_count": None, "partial_count": None, "not_met_count": None,
            "teaching_quality_rating": None, "teaching_quality_comments": None,
            "overall_judgement": None,
            "evidence_clip_count": 0, "invalid_evidence_clip_count": 0,
            "structured_output_error_count": 0,
            "review_reason": TRANSCRIPT_COVERAGE_INCOMPLETE,
            "ai_raw_output": None,
            "metadata": {**self.versions, "reason": TRANSCRIPT_COVERAGE_INCOMPLETE,
                         "punctuality": package.get("punctuality"),
                         **_delivery_metadata(package)},
        }
        written = self.evaluation_repository.upsert(connection, evaluation, [], [])
        counters["evaluations_created"] += written["created"]
        counters["evaluations_updated"] += written["updated"]
        counters["review_required_count"] += 1
        result.update({
            "qa_status": REVIEW_REQUIRED, "review_reason": TRANSCRIPT_COVERAGE_INCOMPLETE,
            "ai_called": False, "provider_calls": 0,
            "evaluation_id": str(written["evaluation_id"]),
            "cancelled_session": False, "checklist_rows": 0,
            "delivery": package["delivery"].diagnostics,
        })
        return result

    def _persist_delivered(self, connection, package, fingerprint, counters,
                           result, force, *, generation_number: int = 1,
                           budget_used: int | None = None) -> dict:
        if self.provider is None:
            # No model configured: record the deterministic layer and leave the
            # AI verdict PENDING. Inventing statuses, or writing MODEL_ERROR for
            # a call that was never attempted, would both be dishonest.
            return self._persist_pending(connection, package, fingerprint, counters, result)

        user_message = build_user_message(
            transcript_text=package["combined_content"],
            transcript_id=package["primary_provider_transcript_id"],
            meeting_id=package["meeting_id"], subject=package["subject"],
            scheduled_start=_iso(package["scheduled_start"]),
            created_datetime=_iso(package["actual_start"]),
            start_difference_minutes=package["start_difference_minutes"],
            start_status=_start_status(package["start_difference_minutes"]),
            scheduled_end=_iso(package["scheduled_end"]),
            end_datetime=_iso(package["actual_end"]),
            end_difference_minutes=package["end_difference_minutes"],
            end_status=_end_status(package["end_difference_minutes"]),
            duration_minutes=package["duration_minutes"], ksb_framework=KSB_FRAMEWORK)

        evaluation = {**self._common_evaluation(package, fingerprint),
                      "delivery_status": DELIVERED, "cancelled_session": False,
                      "duration_score": duration_score(package["duration_minutes"]),
                      "duration_text": duration_text(package["duration_minutes"]),
                      "canonical_trainer": package["canonical_trainer"],
                      "canonical_trainer_speaker_id": package["canonical_trainer_speaker_id"],
                      "trainer_source": TRAINER_SOURCE_DETERMINISTIC,
                      "attended_count": package["attended_count"],
                      "spoke_count": package["spoke_count"],
                      "engagement_percentage": package["engagement_percentage"],
                      "engagement_score": package["engagement_score"],
                      "item7_override_applied": package["item7_override_applied"]}

        try:
            counters["provider_calls"] += 1
            result["provider_calls"] = 1
            response = self.provider.complete_json(
                system_message=SYSTEM_MESSAGE, user_message=user_message)
        except ProviderError as error:
            counters["error_count"] += 1
            failure = error.diagnostics()
            if error.is_configuration_failure:
                # The credential, not the lecture. Recorded, never charged,
                # and every other lecture of this run is spared the same call.
                counters["provider_configuration_failures"] += 1
                self._circuit.open(lecture_id=package["lecture_id"], **failure)
            evaluation.update({
                "qa_status": MODEL_ERROR, "ai_called": True,
                # Generations for this fingerprint, NOT HTTP retries inside one
                # call. The two were conflated, which is why an evaluation with
                # three exhausted generations reported provider_attempts = 1.
                "provider_attempts": generation_number,
                "error_code": error.code, "ai_raw_output": None,
                "ai_suggested_trainer": None, "ai_item7_status": None,
                "final_item7_status": None, "met_count": None, "partial_count": None,
                "not_met_count": None, "teaching_quality_rating": None,
                "teaching_quality_comments": None, "overall_judgement": None,
                "evidence_clip_count": 0, "invalid_evidence_clip_count": 0,
                "structured_output_error_count": 0,
                # A provider failure is never turned into Not Met verdicts.
                "review_reason": (PROVIDER_CONFIGURATION_ERROR
                                  if error.is_configuration_failure
                                  else "PROVIDER_ERROR"),
                "metadata": {**self.versions,
                             **contract_provenance(self.provider_contract_version),
                             "generation_number": generation_number,
                             "provider_transport_attempts": getattr(error, "attempts", 1),
                             "max_model_generations": self.max_model_generations,
                             "provider_error_code": error.code,
                             "http_status": error.http_status,
                             "provider_failure": failure},
            })
            written = self.evaluation_repository.upsert(connection, evaluation, [], [])
            counters["evaluations_created"] += written["created"]
            counters["evaluations_updated"] += written["updated"]
            result.update({"qa_status": MODEL_ERROR, "ai_called": True,
                           "error_code": error.code,
                           "review_reason": evaluation["review_reason"],
                           "http_status": error.http_status,
                           "provider_failure": failure,
                           "evaluation_id": str(written["evaluation_id"])})
            return result

        output = response["output"]
        errors = validate_structured_output(output)
        # Reported, never an error: the legacy prompt states items 1, 2 and 10
        # with trailing spaces in one list and without in the other.
        whitespace_variants = checklist_item_whitespace_variants(output)
        clips, invalid = self._validate_clips(connection, package, output)
        checklist, ai_item7, final_item7, counts, review = self._build_checklist(
            package, output, errors)

        evidence = self._assess_evidence(clips)
        status = COMPLETED
        if errors:
            status = INVALID_STRUCTURED_OUTPUT
        elif not evidence["evaluation_usable"]:
            # Phase 4B: a FABRICATED coordinate still fails the whole answer. A
            # degenerate window - zero length, or under two seconds - is empty
            # evidence rather than false evidence, so it is excluded from the
            # render and the verdict stands. The clip keeps its real status.
            status = INVALID_EVIDENCE
        elif review:
            status = REVIEW_REQUIRED
        if status != COMPLETED:
            counters["error_count"] += 1 if status in (
                INVALID_STRUCTURED_OUTPUT, INVALID_EVIDENCE) else 0
        if status == REVIEW_REQUIRED or review:
            counters["review_required_count"] += 1

        quality = output.get("teaching_quality") if isinstance(output, dict) else None
        summary = output.get("overall_summary") if isinstance(output, dict) else None
        evaluation.update({
            "qa_status": status, "ai_called": True,
            "provider_attempts": generation_number, "error_code": None,
            "model_reported": response.get("model_reported"),
            # Diagnostic only: it never replaces the deterministic trainer.
            "ai_suggested_trainer": ((output.get("session_info") or {}).get("trainer")
                                     if isinstance(output, dict) else None),
            "ai_item7_status": ai_item7, "final_item7_status": final_item7,
            "met_count": counts["Met"], "partial_count": counts["Partially Met"],
            "not_met_count": counts["Not Met"],
            "teaching_quality_rating": (quality or {}).get("rating_1_5")
            if isinstance(quality, dict) else None,
            "teaching_quality_comments": (quality or {}).get("comments")
            if isinstance(quality, dict) else None,
            "overall_judgement": (summary or {}).get("overall_judgement")
            if isinstance(summary, dict) else None,
            "evidence_clip_count": len(clips),
            "invalid_evidence_clip_count": invalid,
            "evidence_assessment": evidence,
            "structured_output_error_count": len(errors),
            "review_reason": review,
            "ai_raw_output": output,
            "metadata": {**self.versions,
                         # Phase 3C3E: what the PROVIDER was actually asked.
                         # Stamped on every evaluation so a later deterministic
                         # refresh can PROVE this answer is still valid instead
                         # of re-buying it.
                         "model_input_fingerprint": qa_model_input_fingerprint(
                             package=package, model=self.model_name,
                             engine_version=self.engine_version,
                             provider_contract_version=self.provider_contract_version),
                         "model_input_fingerprint_version": MODEL_INPUT_FINGERPRINT_VERSION,
                         # Phase 3C3B: the full Item 2 derivation, so the final
                         # Met/Partial/Not Met is never the only evidence.
                         "punctuality": package.get("punctuality"),
                         "structured_output_errors": errors[:40],
                         "checklist_item_whitespace_variants": whitespace_variants,
                         "provider": response.get("provider"),
                         **contract_provenance(self.provider_contract_version),
                         "generation_number": generation_number,
                         "max_model_generations": self.max_model_generations,
                         # HTTP retries inside this single call. Bounded
                         # separately and never the generation count.
                         "provider_transport_attempts": response.get("attempts", 1),
                         "null_optionals_dropped": response.get("null_optionals_dropped", 0),
                         "response_id": response.get("response_id"),
                         "usage": response.get("usage"),
                         "forced_re_evaluation": bool(force)},
        })
        written = self.evaluation_repository.upsert(connection, evaluation, checklist, clips)
        counters["evaluations_created"] += written["created"]
        counters["evaluations_updated"] += written["updated"]
        result.update({
            "qa_status": status, "ai_called": True, "provider_calls": 1,
            "generation_number": generation_number,
            "generation_attempts": generation_number,
            "attempts_remaining": max(self.max_model_generations - (
                (generation_number - 1 if budget_used is None else budget_used) + 1), 0),
            "evaluation_id": str(written["evaluation_id"]),
            "structured_output_error_count": len(errors),
            "structured_output_errors": errors[:10],
            "checklist_item_whitespace_variants": whitespace_variants,
            "evidence_clip_count": len(clips), "invalid_evidence_clip_count": invalid,
            "ai_item7_status": ai_item7, "final_item7_status": final_item7,
            "met_count": counts["Met"], "partial_count": counts["Partially Met"],
            "not_met_count": counts["Not Met"],
            "teaching_quality_rating": evaluation["teaching_quality_rating"],
            "ai_trainer_matches_canonical": _trainer_agreement(
                evaluation["ai_suggested_trainer"], package["canonical_trainer"]),
            "review_reason": review,
            "checklist_rows": len(checklist),
        })
        return result

    def _persist_pending(self, connection, package, fingerprint, counters, result) -> dict:
        evaluation = {
            **self._common_evaluation(package, fingerprint),
            "qa_status": PENDING, "delivery_status": DELIVERED, "cancelled_session": False,
            "ai_called": False, "provider_attempts": 0,
            "error_code": "provider_not_configured",
            "duration_score": duration_score(package["duration_minutes"]),
            "duration_text": duration_text(package["duration_minutes"]),
            "canonical_trainer": package["canonical_trainer"],
            "canonical_trainer_speaker_id": package["canonical_trainer_speaker_id"],
            "ai_suggested_trainer": None, "trainer_source": TRAINER_SOURCE_DETERMINISTIC,
            "attended_count": package["attended_count"], "spoke_count": package["spoke_count"],
            "engagement_percentage": package["engagement_percentage"],
            "engagement_score": package["engagement_score"],
            "ai_item7_status": None,
            # The deterministic override stands on its own; only items 3-6 and
            # 8-11 are waiting on the model.
            "final_item7_status": (package["learner_engagement_status"]
                                   if package["item7_override_applied"] else None),
            "item7_override_applied": package["item7_override_applied"],
            "met_count": None, "partial_count": None, "not_met_count": None,
            "teaching_quality_rating": None, "teaching_quality_comments": None,
            "overall_judgement": None, "evidence_clip_count": 0,
            "invalid_evidence_clip_count": 0, "structured_output_error_count": 0,
            "review_reason": "QA_MODEL_NOT_CONFIGURED", "ai_raw_output": None,
            "metadata": {**self.versions, "reason": "QA_MODEL_NOT_CONFIGURED",
                         "deterministic_item1": item1_status(package["duration_minutes"]),
                         "deterministic_item2": item2_status(
                             package["start_difference_minutes"],
                             package["end_difference_minutes"])},
        }
        # The items code owns are already decided, so they are persisted even
        # though the model has not run. Items 3-6 and 8-11 stay absent rather
        # than being invented.
        deterministic_rows = self._deterministic_only_checklist(package)
        written = self.evaluation_repository.upsert(
            connection, evaluation, deterministic_rows, [])
        counters["evaluations_created"] += written["created"]
        counters["evaluations_updated"] += written["updated"]
        counters["review_required_count"] += 1
        result.update({
            "qa_status": PENDING, "checklist_rows": len(deterministic_rows), "ai_called": False, "provider_calls": 0,
            "evaluation_id": str(written["evaluation_id"]),
            "error_code": "provider_not_configured",
            "deterministic_item1": item1_status(package["duration_minutes"]),
            "deterministic_item2": item2_status(package["start_difference_minutes"],
                                                package["end_difference_minutes"]),
            "final_item7_status": evaluation["final_item7_status"],
            "duration_score": evaluation["duration_score"],
            "duration_text": evaluation["duration_text"],
            "review_reason": "QA_MODEL_NOT_CONFIGURED",
        })
        return result

    def _deterministic_only_checklist(self, package) -> list[dict]:
        """Items 1, 2 and (when the override applies) 7 - no AI needed."""
        rows = [
            {"checklist_order": DURATION_ITEM,
             "checklist_item": CHECKLIST_ITEMS[DURATION_ITEM - 1],
             "status": item1_status(package["duration_minutes"]),
             "ai_status": None, "status_source": SOURCE_DETERMINISTIC_DURATION,
             "reasoning": None, "evidence_text": None,
             "evidence_clip_count": 0, "invalid_evidence_clip_count": 0},
            {"checklist_order": PUNCTUALITY_ITEM,
             "checklist_item": CHECKLIST_ITEMS[PUNCTUALITY_ITEM - 1],
             "status": item2_status(package["start_difference_minutes"],
                                    package["end_difference_minutes"]),
             "ai_status": None, "status_source": SOURCE_DETERMINISTIC_PUNCTUALITY,
             "reasoning": None, "evidence_text": None,
             "evidence_clip_count": 0, "invalid_evidence_clip_count": 0},
        ]
        if package["item7_override_applied"] and package["learner_engagement_status"]:
            rows.append({
                "checklist_order": ENGAGEMENT_ITEM,
                "checklist_item": CHECKLIST_ITEMS[ENGAGEMENT_ITEM - 1],
                "status": package["learner_engagement_status"], "ai_status": None,
                "status_source": SOURCE_DETERMINISTIC_ENGAGEMENT, "reasoning": None,
                "evidence_text": None, "evidence_clip_count": 0,
                "invalid_evidence_clip_count": 0})
        return rows

    def _common_evaluation(self, package, fingerprint) -> dict:
        return {
            "evaluation_id": uuid.uuid4(),
            "lecture_id": package["lecture_id"], "document_id": package["document_id"],
            "selection_id": package["selection_id"],
            "attendance_snapshot_id": package["attendance_snapshot_id"],
            "engagement_id": package["engagement_id"],
            "qa_engine_version": self.engine_version, "prompt_version": PROMPT_VERSION,
            "prompt_sha256": prompt_sha256(model=self.model_name),
            "structured_schema_version": STRUCTURED_SCHEMA_VERSION,
            "model_provider": "openai", "model_name": self.model_name, "model_reported": None,
            "attendance_roster_version": self.attendance_roster_version,
            "primary_provider_transcript_id": package["primary_provider_transcript_id"],
            "meeting_id": package["meeting_id"],
            "duration_minutes": package["duration_minutes"],
            "start_difference_minutes": package["start_difference_minutes"],
            "end_difference_minutes": package["end_difference_minutes"],
            "source_fingerprint": fingerprint,
        }

    def _assess_evidence(self, clips) -> dict:
        """What this run's clip statuses mean under the active policy."""
        return assess_evidence([clip["validation_status"] for clip in clips],
                               policy=self.evidence_policy_version)

    def _validate_clips(self, connection, package, output):
        if not isinstance(output, dict):
            return [], 0
        cues = self.input_repository.load_cues(connection, package["document_id"])
        start_ms = package["first_cue_start_ms"] or 0
        end_ms = package["last_cue_end_ms"] or 0
        rows, invalid = [], 0
        for found in collect_clips(output):
            clip = found["clip"]
            verdict = validate_clip(clip, document_start_ms=start_ms,
                                    document_end_ms=end_ms, cues=cues)
            if verdict["status"] != VALID:
                invalid += 1
            rows.append({
                "clip_source": found["source"], "source_position": found["source_position"],
                "clip_index": found["clip_index"],
                "start_text": str(clip.get("start") or "")[:32],
                "end_text": str(clip.get("end") or "")[:32],
                "start_ms": verdict["start_ms"], "end_ms": verdict["end_ms"],
                "speaker_label": (str(clip.get("speaker"))[:200]
                                  if clip.get("speaker") else None),
                "validation_status": verdict["status"],
                "overlapping_cue_count": verdict["overlapping_cue_count"],
            })
        return rows, invalid

    def _build_checklist(self, package, output, structural_errors):
        """
        Merge the model's checklist with the deterministic verdicts.

        Items 1 and 2 are metadata facts; Item 7 comes from Phase 2C4 whenever
        the override applied, and falls back to the model only when legacy
        would also have fallen back (no attended learners).
        """
        ai_rows = output.get("checklist_evaluation") if isinstance(output, dict) else None
        ai_rows = ai_rows if isinstance(ai_rows, list) else []
        review = None
        if package["engagement_calculation_status"] in ENGAGEMENT_REVIEW_STATUSES:
            review = f"ENGAGEMENT_{package['engagement_calculation_status']}"

        deterministic = {
            DURATION_ITEM: (item1_status(package["duration_minutes"]),
                            SOURCE_DETERMINISTIC_DURATION),
            PUNCTUALITY_ITEM: (item2_status(package["start_difference_minutes"],
                                            package["end_difference_minutes"]),
                               SOURCE_DETERMINISTIC_PUNCTUALITY),
        }
        if package["item7_override_applied"] and package["learner_engagement_status"]:
            deterministic[ENGAGEMENT_ITEM] = (package["learner_engagement_status"],
                                              SOURCE_DETERMINISTIC_ENGAGEMENT)

        rows, counts = [], {"Met": 0, "Partially Met": 0, "Not Met": 0}
        ai_item7 = final_item7 = None
        for order, item in enumerate(CHECKLIST_ITEMS, start=1):
            ai_row = ai_rows[order - 1] if order <= len(ai_rows) else None
            ai_row = ai_row if isinstance(ai_row, dict) else {}
            ai_status = ai_row.get("status")
            ai_status = ai_status if ai_status in counts else None
            clips = ai_row.get("evidence_clips")
            clips = clips if isinstance(clips, list) else []
            if order in deterministic:
                status, source = deterministic[order]
            else:
                status, source = ai_status, SOURCE_AI
            if order == ENGAGEMENT_ITEM:
                ai_item7 = ai_status
                # Legacy kept the AI verdict only when no override applied.
                final_item7 = status
            if status is None:
                # The model gave nothing usable; the structural errors already
                # record why, and Not Met is not invented here.
                status = NOT_MET if structural_errors else NOT_MET
                source = "MISSING_AI_STATUS"
            counts[status] += 1
            rows.append({
                "checklist_order": order, "checklist_item": item, "status": status,
                "ai_status": ai_status, "status_source": source,
                "reasoning": (str(ai_row.get("reasoning"))[:8000]
                              if ai_row.get("reasoning") else None),
                "evidence_text": None,          # Phase 3B renders evidence text.
                "evidence_clip_count": len(clips),
                "invalid_evidence_clip_count": 0,
            })
        return rows, ai_item7, final_item7, counts, review

    def _legacy_comparison(self, connection, target_date, results) -> dict:
        """READ-ONLY deterministic parity plus an AI-only agreement count."""
        sessions = self.legacy_repository.load_sessions(connection, target_date)
        checklists = self.legacy_repository.load_checklist(connection, target_date)
        legacy_counts = self.legacy_repository.load_engagement_counts(connection, target_date)
        legacy_timing = self.legacy_repository.load_timing(connection, target_date)
        by_subject = {normalize_group(row["subject"]): row for row in results}
        deterministic_keys = ("session_id", "meeting_id", "trainer", "duration",
                              "attended_count", "engagement", "engagement_score",
                              "item1", "item2", "item7")
        totals = {key: 0 for key in deterministic_keys}
        ai_agreement = {"agreed": 0, "compared": 0}
        rows = []
        for legacy in sessions:
            ours = by_subject.get(normalize_group(legacy["subject"] or ""))
            if ours is None or not ours.get("evaluation_id"):
                rows.append({"subject": legacy["subject"], "status": "NO_SHADOW_RESULT"})
                continue
            legacy_items = checklists.get(legacy["subject"], {})
            stored = self.evaluation_repository.find_by_fingerprint(
                connection, ours["source_fingerprint"])
            detail = _load_shadow_detail(connection, stored["evaluation_id"]) if stored else {}
            matches = {
                "session_id": (ours["primary_provider_transcript_id"] == legacy["session_id"]),
                "meeting_id": (ours["meeting_id"] == legacy["meeting_id"]),
                "trainer": (detail.get("canonical_trainer") == legacy["trainer"]),
                "duration": (detail.get("duration_text") == legacy["duration"]),
                "attended_count": (ours["attended_count"]
                                   == (legacy_counts.get(legacy["subject"]) or {}
                                       ).get("attended_count")),
                "engagement": _decimal_equal(ours["engagement_percentage"], legacy["engagement"]),
                "engagement_score": (ours["engagement_score"] == legacy["engagement_score"]),
                "item1": (detail.get(1) == legacy_items.get(1)),
                "item2": (detail.get(2) == legacy_items.get(2)),
                "item7": (detail.get(7) == legacy_items.get(7)),
            }
            for key, value in matches.items():
                totals[key] += bool(value)
            timing = legacy_timing.get(legacy["subject"]) or {}
            # Legacy read naive Cairo scheduled times as UTC, so its punctuality
            # inputs are shifted. The offset is measured, not assumed.
            offsets = [
                (ours_value - legacy_value)
                for ours_value, legacy_value in (
                    (ours.get("start_difference_minutes"),
                     timing.get("start_difference_minutes")),
                    (ours.get("end_difference_minutes"),
                     timing.get("end_difference_minutes")))
                if ours_value is not None and legacy_value is not None]
            agreed = sum(1 for order in (3, 4, 5, 6, 8, 9, 10, 11)
                         if detail.get(order) is not None
                         and detail.get(order) == legacy_items.get(order))
            compared = sum(1 for order in (3, 4, 5, 6, 8, 9, 10, 11)
                           if legacy_items.get(order) is not None)
            ai_agreement["agreed"] += agreed
            ai_agreement["compared"] += compared
            rows.append({
                "subject": legacy["subject"],
                **{f"{key}_match": ("YES" if value else "NO") for key, value in matches.items()},
                "ai_checklist_agreement": f"{agreed} / 8",
                "new_met_partial_not_met": [detail.get("met_count"), detail.get("partial_count"),
                                            detail.get("not_met_count")],
                "legacy_met_partial_not_met": [legacy["met_count"], legacy["partial_count"],
                                               legacy["not_met_count"]],
                "new_teaching_quality_rating": detail.get("teaching_quality_rating"),
                "legacy_teaching_quality_rating": legacy["teaching_quality_rating"],
                "ai_trainer_matches_canonical": ours.get("ai_trainer_matches_canonical"),
                "new_start_difference_minutes": ours.get("start_difference_minutes"),
                "legacy_start_difference_minutes": timing.get("start_difference_minutes"),
                "new_end_difference_minutes": ours.get("end_difference_minutes"),
                "legacy_end_difference_minutes": timing.get("end_difference_minutes"),
                "legacy_timing_offset_minutes": (offsets[0] if offsets
                                                 and len(set(offsets)) == 1 else offsets or None),
            })
        count = len(sessions)
        return {
            "qa_rows": count,
            **{f"{key}_parity": f"{totals[key]} / {count}" for key in deterministic_keys},
            "ai_checklist_agreement": f"{ai_agreement['agreed']} / {ai_agreement['compared']}",
            "note": "Deterministic fields must match. Items 3-6 and 8-11 come from a fresh "
                    "model call and are reported, never forced.",
            "rows": rows,
        }


def _delivery_metadata(package) -> dict:
    """The delivery decision's safe diagnostics: numbers and instants only."""
    delivery = package.get("delivery")
    if delivery is None:
        return {"delivery_policy_version": DELIVERY_POLICY_VERSION}
    return {"delivery_policy_version": delivery.diagnostics["delivery_policy_version"],
            "delivery_classification_reason": delivery.reason,
            "delivery": delivery.diagnostics}


def _quality(output, field):
    quality = output.get("teaching_quality") if isinstance(output, dict) else None
    return quality.get(field) if isinstance(quality, dict) else None


def _summary_field(output, field):
    summary = output.get("overall_summary") if isinstance(output, dict) else None
    return summary.get(field) if isinstance(summary, dict) else None


def _previous_checklist_summary(connection, evaluation_id) -> dict:
    """What the evaluation being refreshed concluded, for the comparison."""
    try:
        row = connection.execute("""
        SELECT e.met_count, e.partial_count, e.not_met_count, e.final_item7_status,
               (SELECT c.status_source FROM public.lecture_qa_checklist_items c
                 WHERE c.evaluation_id = e.evaluation_id AND c.checklist_order = 7)
          FROM public.lecture_qa_evaluations e WHERE e.evaluation_id = %s
        """, (evaluation_id,)).fetchone()
    except Exception:
        return {}
    if row is None:
        return {}
    return {"met_count": row[0], "partial_count": row[1], "not_met_count": row[2],
            "final_item7_status": row[3], "item7_status_source": row[4]}


def _load_shadow_detail(connection, evaluation_id) -> dict:
    row = connection.execute("""
    SELECT canonical_trainer, duration_text, met_count, partial_count, not_met_count,
           teaching_quality_rating
      FROM public.lecture_qa_evaluations WHERE evaluation_id = %s
    """, (evaluation_id,)).fetchone()
    detail = {}
    if row:
        detail.update({"canonical_trainer": row[0], "duration_text": row[1],
                       "met_count": row[2], "partial_count": row[3], "not_met_count": row[4],
                       "teaching_quality_rating": row[5]})
    for order, status in connection.execute(
            "SELECT checklist_order, status FROM public.lecture_qa_checklist_items "
            " WHERE evaluation_id = %s", (evaluation_id,)).fetchall():
        detail[order] = status
    return detail


def _decimal_equal(left, right) -> bool:
    if left is None or right is None:
        return False
    return Decimal(str(left)) == Decimal(str(right))


def _trainer_agreement(ai_trainer, canonical) -> str:
    if not ai_trainer:
        return "AI_TRAINER_ABSENT"
    if ai_trainer == canonical:
        return "EXACT"
    if normalize_speaker_label(ai_trainer) == normalize_speaker_label(canonical or ""):
        return "NORMALIZED"
    return "DIFFERENT"


def _iso(value) -> str:
    return "" if value is None else value.isoformat()


def _start_status(difference) -> str:
    if difference is None:
        return ""
    return "OnTime" if difference == 0 else ("Early" if difference < 0 else "Late")


def _end_status(difference) -> str:
    if difference is None:
        return ""
    return "OnTime" if difference == 0 else ("EarlyFinish" if difference < 0 else "Overrun")


def package_attendance_coverage(package) -> str:
    """
    The coverage status of the attendance snapshot a QA input was built on.

    Deliberately NOT a second definition of "authoritative": the status comes
    from the same `classify` the coverage repository and the orchestrator's
    resolver use, over the same frozen counts. A package that carries no counts
    classifies as SOURCE_UNKNOWN, which is not authoritative - so a caller that
    forgot to supply them is refused rather than waved through.
    """
    return classify_coverage(
        source_row_count=package.get("attendance_source_row_count"),
        present_row_count=package.get("attendance_present_row_count"),
        effective_member_count=package.get("attendance_effective_member_count"),
        source_rows_any_status=package.get("attendance_source_rows_any_status"))
