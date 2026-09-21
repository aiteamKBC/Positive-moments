"""
Phase 4A: the stage runner.

This is the ONLY place in the orchestration package that calls a pipeline
service, and it does nothing except call them. There is no checklist logic
here, no roster rule, no eligibility rule, no fingerprint arithmetic and no
SQL against a pipeline table. Every method is a translation: an action name in,
a validated service call out, a normalised outcome back.

That constraint is the point of the file existing. The scheduler's failure mode
is not "it forgot to run something"; it is "it grew its own slightly different
copy of a rule, and the copy drifted". Keeping the coordination and the rules in
separate files is how that stays visible in review.

SCOPE
-----
Some validated services are day-scoped (transcript acquisition, selection,
canonical parsing, speaker inventory) and some are lecture-scoped (attendance,
engagement, QA, rendering, Perfect). That is a property of the services, not a
choice made here, and pretending otherwise would mean rewriting them.

So each action declares its scope. A day-scoped action runs at most ONCE per
day per pass and its result is shared; a lecture-scoped action runs under that
lecture's advisory lock and can fail without touching any sibling. The stages
that can fail for lecture-specific reasons - model validation, attendance,
rendering - are all lecture-scoped, which is what makes per-lecture isolation
real rather than nominal.

COST
----
`allow_graph` and `allow_provider` are hard gates, not preferences. When
`allow_provider` is false the QA service is constructed with `provider=None`,
so the run is structurally incapable of buying a generation rather than merely
instructed not to.
"""
import logging

from app.attendance.coverage import classify, is_authoritative
from app.attendance.recovery import AttendanceRecoveryService
from app.attendance.resolver import RESOLVER_VERSION
from app.attendance.roles import ROLE_ALGORITHM_VERSION
from app.attendance.roster import ATTENDANCE_RESOLUTION_VERSION
from app.attendance.service import SpeakerResolutionService
from app.db.repositories.attendance_coverage import AttendanceCoverageRepository
from app.db.repositories.attendance_resolution import (
    AttendanceSnapshotRepository,
    AttendanceSourceRepository,
    SpeakerIdentityRepository,
    SpeakerInventoryReadRepository,
    SpeakerResolutionRunRepository,
    SpeakerRoleRepository,
)
from app.db.repositories.engagement import (
    EngagementInputRepository,
    EngagementRepository,
    EngagementRunRepository,
)
from app.db.repositories.perfect_lectures import (
    LegacyPerfectLectureRepository,
    PerfectLectureOwnershipRepository,
    PerfectLectureResultRepository,
)
from app.db.repositories.qa_rendering import (
    LmsSnapshotRepository,
    LmsSourceRepository,
    RenderInputRepository,
    RenderRunRepository,
    RenderedOutputRepository,
)
from app.db.repositories.qa_shadow import (
    QaEvaluationRepository,
    QaInputRepository,
    QaRunRepository,
)
from app.db.repositories.qa_writer import (
    GenerationAttemptRepository,
    LegacyQaTargetRepository,
    RenderedPayloadRepository,
    WriterOwnershipRepository,
)
from app.db.repositories.ready_lectures import ReadyLectureRepository
from app.db.repositories.transcript_artifacts import TranscriptArtifactRepository
from app.db.repositories.transcript_documents import (
    TranscriptDocumentRepository,
    TranscriptParseRunRepository,
)
from app.db.repositories.transcript_runs import TranscriptAcquisitionRunRepository
from app.db.repositories.transcript_selections import (
    TranscriptSelectionRepository,
    TranscriptSelectionRunRepository,
)
from app.db.repositories.transcript_speakers import (
    SpeakerInventoryRunRepository,
    TranscriptSpeakerRepository,
)
from app.engagement.calculator import ENGAGEMENT_ALGORITHM_VERSION
from app.engagement.service import EngagementService
from app.lectures.duplicate_service import (
    ALREADY_SUPPRESSED,
    REFUSED_RACED,
    SUPPRESSED,
    DuplicateResolutionService,
)
from app.orchestration.stages import (
    ACQUIRE_TRANSCRIPT,
    BUILD_CANONICAL_CUES,
    CALCULATE_ENGAGEMENT,
    EVALUATE_PERFECT,
    RECOVER_ATTENDANCE,
    REFRESH_DETERMINISTIC_QA,
    RENDER_QA,
    RESOLVE_ATTENDANCE,
    RESOLVE_SPEAKERS,
    REVALIDATE_EVIDENCE,
    RUN_QA,
    SELECT_TRANSCRIPT,
    SUPPRESS_DUPLICATE_EVENT,
    SYNC_LEGACY_QA,
    SYNC_PERFECT,
)
from app.qa.perfect import DEFAULT_PERFECT_ELIGIBILITY_VERSION
from app.qa.provider import OpenAIChatProvider
from app.qa.service import ShadowQaService
from app.rendering.evidence import RENDERER_VERSION
from app.rendering.service import QaRenderingService
from app.transcripts.document_service import CanonicalTranscriptService
from app.transcripts.selection_service import TranscriptSelectionService
from app.transcripts.service import TranscriptAcquisitionService
from app.transcripts.speaker_service import SpeakerInventoryService
from app.transcripts.webvtt import PARSER_VERSION
from app.orchestration.sync_safety import classify_plan
from app.writer.modes import DRY_RUN, PRODUCTION_NEW_ONLY
from app.writer.mapping import WRITER_VERSION
from app.writer.perfect_service import PerfectLecturePlanner, plan_perfect_for_day
from app.writer.service import LegacyQaWriter


DAY_SCOPE = "DAY"
LECTURE_SCOPE = "LECTURE"

ACTION_SCOPE = {
    ACQUIRE_TRANSCRIPT: DAY_SCOPE,
    SELECT_TRANSCRIPT: DAY_SCOPE,
    BUILD_CANONICAL_CUES: DAY_SCOPE,
    RESOLVE_SPEAKERS: DAY_SCOPE,
    RESOLVE_ATTENDANCE: LECTURE_SCOPE,
    CALCULATE_ENGAGEMENT: LECTURE_SCOPE,
    RUN_QA: LECTURE_SCOPE,
    REFRESH_DETERMINISTIC_QA: LECTURE_SCOPE,
    REVALIDATE_EVIDENCE: LECTURE_SCOPE,
    RENDER_QA: LECTURE_SCOPE,
    RECOVER_ATTENDANCE: LECTURE_SCOPE,
    EVALUATE_PERFECT: LECTURE_SCOPE,
    # Phase 4C1. Lecture-scoped and cheap: one indexed read of the duplicate
    # group and, at most, one annotation on this lecture's own registry row.
    SUPPRESS_DUPLICATE_EVENT: LECTURE_SCOPE,
    # Phase 4B. Lecture-scoped, twice-planned, and the only two
    # actions in this table that touch a legacy production row.
    SYNC_LEGACY_QA: LECTURE_SCOPE,
    SYNC_PERFECT: LECTURE_SCOPE,
}

# Actions that would cost a Graph call or a paid generation. Listed explicitly
# so the gates are auditable rather than implied by which service is built.
GRAPH_ACTIONS = frozenset({ACQUIRE_TRANSCRIPT})
PROVIDER_ACTIONS = frozenset({RUN_QA})
# Actions that can write a legacy production row. Gated separately from
# Graph and the provider because the risk is different in kind: money is
# recoverable, a corrupted production row shared with a live n8n pipeline
# is not.
LEGACY_WRITE_ACTIONS = frozenset({SYNC_LEGACY_QA, SYNC_PERFECT})


class StageNotAutomatable(RuntimeError):
    """The orchestrator asked for an action this runner deliberately will not do."""


class ManualReviewRequired(RuntimeError):
    """
    Safe automation stops here and a human decides.

    Distinct from a failure: nothing went wrong, and nothing was left half
    done. The writer reached a decision this platform will not take
    unattended, and the reason travels with the exception.
    """


class StageRunner:
    """Translate one action into one validated service call."""

    def __init__(self, *, settings, allow_graph: bool = True,
                 allow_provider: bool = True, allow_legacy_writes: bool = True,
                 parser_version: str = PARSER_VERSION,
                 perfect_eligibility_version: str = DEFAULT_PERFECT_ELIGIBILITY_VERSION,
                 provider_contract_version=None, persist: bool = True):
        self.settings = settings
        self.allow_graph = allow_graph
        self.allow_provider = allow_provider
        self.allow_legacy_writes = allow_legacy_writes
        self.parser_version = parser_version
        self.perfect_eligibility_version = perfect_eligibility_version
        self.provider_contract_version = provider_contract_version
        self.persist = persist
        self.log = logging.getLogger(__name__)

    def scope_of(self, action: str) -> str:
        return ACTION_SCOPE.get(action, LECTURE_SCOPE)

    def can_run(self, action: str) -> bool:
        if action not in ACTION_SCOPE:
            return False
        if action in GRAPH_ACTIONS and not self.allow_graph:
            return False
        if action in PROVIDER_ACTIONS and not self.allow_provider:
            return False
        if action in LEGACY_WRITE_ACTIONS and not (
                self.allow_legacy_writes and self.persist):
            # A dry run can never reach a legacy table, whatever it is asked.
            return False
        return True

    def refusal_reason(self, action: str) -> str:
        if action not in ACTION_SCOPE:
            return "ACTION_NOT_AUTOMATED"
        if action in GRAPH_ACTIONS and not self.allow_graph:
            return "GRAPH_CALLS_DISABLED_FOR_THIS_RUN"
        if action in PROVIDER_ACTIONS and not self.allow_provider:
            return "PROVIDER_CALLS_DISABLED_FOR_THIS_RUN"
        if action in LEGACY_WRITE_ACTIONS and not (
                self.allow_legacy_writes and self.persist):
            return "LEGACY_WRITES_DISABLED_FOR_THIS_RUN"
        return "ACTION_NOT_AUTOMATED"

    # -- dispatch -----------------------------------------------------------

    def execute(self, connection, action, *, session_date, lecture_id=None) -> dict:
        if not self.can_run(action):
            raise StageNotAutomatable(self.refusal_reason(action))
        handler = getattr(self, f"_{action.lower()}")
        if self.scope_of(action) == DAY_SCOPE:
            return handler(connection, session_date)
        return handler(connection, lecture_id, session_date)

    # -- day-scoped stages ---------------------------------------------------

    def _acquire_transcript(self, connection, session_date) -> dict:
        from app.db.repositories.ready_lectures import LegacyQaEvidenceRepository
        from app.graph.auth import build_graph_client
        from app.transcripts.graph import TranscriptGateway
        self.settings.require_discovery()
        service = TranscriptAcquisitionService(
            gateway=TranscriptGateway(build_graph_client(self.settings)),
            lecture_repository=ReadyLectureRepository(),
            artifact_repository=TranscriptArtifactRepository(),
            run_repository=TranscriptAcquisitionRunRepository(),
            qa_evidence_repository=LegacyQaEvidenceRepository())
        outcome = service.acquire_day(connection, session_date,
                                      persist=self.persist, fetch_content=True)
        return _outcome(outcome, graph_calls=outcome.get("graph_calls", 0))

    def _select_transcript(self, connection, session_date) -> dict:
        from app.db.repositories.ready_lectures import LegacyQaEvidenceRepository
        service = TranscriptSelectionService(
            lecture_repository=ReadyLectureRepository(),
            selection_repository=TranscriptSelectionRepository(),
            run_repository=TranscriptSelectionRunRepository(),
            qa_evidence_repository=LegacyQaEvidenceRepository())
        return _outcome(service.select_day(connection, session_date,
                                           persist=self.persist))

    def _build_canonical_cues(self, connection, session_date) -> dict:
        service = CanonicalTranscriptService(
            document_repository=TranscriptDocumentRepository(),
            run_repository=TranscriptParseRunRepository())
        return _outcome(service.parse_day(connection, session_date,
                                          persist=self.persist))

    def _resolve_speakers(self, connection, session_date) -> dict:
        service = SpeakerInventoryService(
            speaker_repository=TranscriptSpeakerRepository(),
            run_repository=SpeakerInventoryRunRepository(),
            legacy_diagnostic_repository=None,
            parser_version=self.parser_version)
        return _outcome(service.build_day(connection, session_date,
                                          persist=self.persist))

    # -- lecture-scoped stages ----------------------------------------------

    def _resolve_attendance(self, connection, lecture_id, session_date) -> dict:
        outcome = self._resolution_service().resolve_lecture(
            connection, lecture_id, persist=self.persist)
        return _outcome(outcome)

    def _calculate_engagement(self, connection, lecture_id, session_date) -> dict:
        outcome = self._engagement_service().calculate_lecture(
            connection, lecture_id, persist=self.persist)
        if outcome["lectures_in_scope"] != 1:
            raise StageNotAutomatable("engagement widened beyond the lecture")
        return _outcome(outcome)

    def _run_qa(self, connection, lecture_id, session_date) -> dict:
        self.settings.require_qa_model()
        provider = OpenAIChatProvider(
            api_key=self.settings.qa_model_api_key,
            model=self.settings.qa_model_name,
            base_url=self.settings.qa_model_base_url,
            **({"response_contract": self.provider_contract_version}
               if self.provider_contract_version else {}))
        service = self._qa_service(provider=provider, lecture_ids=[str(lecture_id)])
        outcome = service.run_day(connection, session_date, execute=self.persist)
        return _outcome(outcome, provider_calls=outcome.get("provider_calls", 0))

    def _refresh_deterministic_qa(self, connection, lecture_id, session_date) -> dict:
        # No provider at all: a deterministic refresh that could buy a
        # generation is not a deterministic refresh.
        outcome = self._qa_service(provider=None).refresh_deterministic(
            connection, lecture_id, persist=self.persist)
        return _outcome(outcome)

    def _revalidate_evidence(self, connection, lecture_id, session_date) -> dict:
        # No provider, by construction. Re-judging a stored answer must be
        # structurally incapable of buying a new one.
        outcome = self._qa_service(provider=None).revalidate_evidence(
            connection, lecture_id, persist=self.persist)
        return _outcome(outcome)

    def _suppress_duplicate_event(self, connection, lecture_id, session_date) -> dict:
        """
        Retire one duplicate calendar event.

        The service re-derives the decision from the registry rather than
        trusting the state resolver's word for it, so the write is guarded by
        the same deterministic rule that proposed it - and refuses outright if
        the occurrence has any downstream footprint at all.
        """
        outcome = DuplicateResolutionService().resolve_lecture(
            connection, lecture_id, persist=self.persist)
        if outcome.get("requires_manual_review"):
            raise ManualReviewRequired(outcome["case"])
        if outcome["outcome"] not in (SUPPRESSED, ALREADY_SUPPRESSED,
                                      REFUSED_RACED):
            # The resolver offered this action and the rule now disagrees.
            # Refusing is right; inventing a suppression is not.
            raise ManualReviewRequired(outcome["outcome"])
        return _outcome(outcome)

    def _render_qa(self, connection, lecture_id, session_date) -> dict:
        outcome = self._rendering_service(lecture_id).render_day(
            connection, session_date)
        touched = {str(item["lecture_id"]) for item in outcome.get("lectures", [])}
        if touched - {str(lecture_id)}:
            raise StageNotAutomatable("render widened beyond the lecture")
        return _outcome(outcome)

    def _recover_attendance(self, connection, lecture_id, session_date) -> dict:
        service = AttendanceRecoveryService(
            resolution_service=self._resolution_service(),
            engagement_service=self._engagement_service(),
            qa_service=self._qa_service(provider=None),
            coverage_repository=AttendanceCoverageRepository(),
            perfect_planner=self._perfect_planner(),
            payload_repository=RenderedPayloadRepository(),
            renderer_version=RENDERER_VERSION,
            rendering_service_factory=self._rendering_service)
        outcome = service.recover(connection, lecture_id, persist=self.persist)
        return _outcome(outcome)

    def _evaluate_perfect(self, connection, lecture_id, session_date) -> dict:
        planner = self._perfect_planner(lecture_ids=[str(lecture_id)],
                                        persist_shadow_result=self.persist)
        outcome = plan_perfect_for_day(planner, connection,
                                       RenderedPayloadRepository(), session_date,
                                       RENDERER_VERSION)
        return _outcome(outcome)

    # -- Phase 4B: automated legacy production sync --------------------------

    def _sync_legacy_qa(self, connection, lecture_id, session_date) -> dict:
        return self._sync_legacy(connection, lecture_id, session_date,
                                 requested=SYNC_LEGACY_QA)

    def _sync_perfect(self, connection, lecture_id, session_date) -> dict:
        return self._sync_legacy(connection, lecture_id, session_date,
                                 requested=SYNC_PERFECT)

    def _sync_legacy(self, connection, lecture_id, session_date, *, requested) -> dict:
        """
        Plan, gate, then write - in that order, always.

        The first pass is a real DRY_RUN through the real writer, so the
        decision the gate reads is the writer's own, not a prediction of it.
        Only a decision on the short safe list reaches a second pass, and that
        second pass re-plans from scratch inside the write-enabled writer. If
        anything changed in between - a legacy row appeared, ownership moved -
        the second plan reaches a different decision and refuses. The writer
        always has the last word; this method can only ever decline.
        """
        plan = self._writer(lecture_id, mode=DRY_RUN).plan_day(
            connection, session_date)
        row = self._only_lecture(plan, lecture_id)
        verdict = classify_plan(row)
        outcome = {
            "requested_action": requested,
            "auto_sync": verdict,
            "qa_decision": verdict["qa"]["decision"],
            "perfect_decision": verdict["perfect"]["decision"],
            "legacy_rows_written": 0, "graph_calls": 0, "provider_calls": 0,
        }

        if verdict["requires_manual_review"] and not verdict["may_write"]:
            raise ManualReviewRequired(", ".join(verdict["reason_codes"]))
        if not verdict["may_write"]:
            # Everything the gate approved is already true. Doing nothing is
            # the entire correct action, and it must not look like a failure.
            outcome.update({"status": "NOOP", "summary": row})
            return outcome

        written = self._writer(
            lecture_id, mode=PRODUCTION_NEW_ONLY, confirmed=True,
            include_perfect=verdict["include_perfect_planner"]).plan_day(
                connection, session_date)
        result = self._only_lecture(written, lecture_id)
        if result.get("write_status") == "WRITE_VERIFICATION_FAILED":
            # The writer already rolled this lecture back inside its savepoint.
            raise ManualReviewRequired(
                f"WRITE_VERIFICATION_FAILED: {result.get('write_error')}")
        perfect = result.get("perfect_lecture") or {}
        outcome.update({
            "status": "WRITTEN",
            "summary": result,
            "legacy_sessions_written": written["sessions_written"],
            "checklist_rows_written": written["checklist_rows_written"],
            "ownership_rows_written": written["ownership_rows_written"],
            "perfect_rows_written": written["perfect_rows_written"],
            "perfect_ownership_rows_written": written["perfect_ownership_rows_written"],
            "verification_failures": written["verification_failures"],
            "post_write_digest_prefix": result.get("post_write_digest_prefix"),
            "legacy_session_id": result.get("legacy_session_id"),
            "perfect_write_status": perfect.get("write_status"),
            "perfect_mapped_digest_prefix": perfect.get("mapped_digest_prefix"),
        })
        if written["verification_failures"]:
            raise ManualReviewRequired("WRITE_VERIFICATION_FAILED")
        return _outcome(outcome)

    def _only_lecture(self, plan: dict, lecture_id) -> dict:
        rows = [row for row in plan.get("lectures", [])
                if str(row["lecture_id"]) == str(lecture_id)]
        if len(rows) != 1:
            raise StageNotAutomatable(
                f"expected exactly one writer plan for {lecture_id}, got {len(rows)}")
        return rows[0]

    def _writer(self, lecture_id, *, mode, confirmed: bool = False,
                include_perfect: bool = True):
        """
        The existing guarded writer, scoped to ONE lecture.

        No SQL is reimplemented and no guard is bypassed: the writer's own
        constructor still refuses a write-enabled mode without an explicit
        lecture scope and an explicit confirmation, and the scheduler satisfies
        both rather than avoiding them.
        """
        planner = None
        if include_perfect:
            planner = PerfectLecturePlanner(
                result_repository=PerfectLectureResultRepository(),
                ownership_repository=PerfectLectureOwnershipRepository(),
                legacy_repository=LegacyPerfectLectureRepository(),
                coverage_repository=AttendanceCoverageRepository(),
                mode=mode, lecture_ids=[str(lecture_id)], confirmed=confirmed,
                eligibility_version=self.perfect_eligibility_version,
                persist_shadow_result=self.persist)
        return LegacyQaWriter(
            payload_repository=RenderedPayloadRepository(),
            legacy_repository=LegacyQaTargetRepository(),
            ownership_repository=WriterOwnershipRepository(),
            mode=mode, writer_version=WRITER_VERSION,
            renderer_version=RENDERER_VERSION,
            allow_update_existing=False, lecture_ids=[str(lecture_id)],
            confirmed=confirmed, perfect_planner=planner)

    # -- service construction -----------------------------------------------

    def _resolution_service(self):
        return SpeakerResolutionService(
            inventory_repository=SpeakerInventoryReadRepository(),
            attendance_repository=AttendanceSourceRepository(),
            snapshot_repository=AttendanceSnapshotRepository(),
            identity_repository=SpeakerIdentityRepository(),
            role_repository=SpeakerRoleRepository(),
            run_repository=SpeakerResolutionRunRepository(),
            legacy_diagnostic_repository=None)

    def _engagement_service(self):
        return EngagementService(
            input_repository=EngagementInputRepository(),
            engagement_repository=EngagementRepository(),
            run_repository=EngagementRunRepository(),
            legacy_parity_repository=None,
            attendance_resolution_version=ATTENDANCE_RESOLUTION_VERSION,
            resolver_version=RESOLVER_VERSION,
            role_algorithm_version=ROLE_ALGORITHM_VERSION)

    def _qa_service(self, *, provider, lecture_ids=None):
        extra = {}
        if self.provider_contract_version:
            extra["provider_contract_version"] = self.provider_contract_version
        return ShadowQaService(
            input_repository=QaInputRepository(),
            evaluation_repository=QaEvaluationRepository(),
            run_repository=QaRunRepository(), legacy_repository=None,
            provider=provider, attempt_repository=GenerationAttemptRepository(),
            resolver_version=RESOLVER_VERSION,
            role_algorithm_version=ROLE_ALGORITHM_VERSION,
            engagement_algorithm_version=ENGAGEMENT_ALGORITHM_VERSION,
            model_name=self.settings.qa_model_name,
            lecture_ids=lecture_ids, **extra)

    def _rendering_service(self, lecture_id):
        # Scoped at construction, because Phase 3B's entry point takes a date.
        return QaRenderingService(
            input_repository=RenderInputRepository(),
            lms_source_repository=LmsSourceRepository(),
            lms_snapshot_repository=LmsSnapshotRepository(),
            output_repository=RenderedOutputRepository(),
            run_repository=RenderRunRepository(),
            legacy_repository=None, refresh_lms=False,
            lecture_ids=[str(lecture_id)])

    def _perfect_planner(self, *, lecture_ids=None, persist_shadow_result=None):
        # DRY_RUN always. Phase 4A computes the coded Perfect answer and never
        # writes the legacy Perfect row: that stays an explicit, confirmed,
        # ownership-aware operation through the guarded writer.
        return PerfectLecturePlanner(
            result_repository=PerfectLectureResultRepository(),
            ownership_repository=PerfectLectureOwnershipRepository(),
            legacy_repository=LegacyPerfectLectureRepository(),
            coverage_repository=AttendanceCoverageRepository(),
            mode=DRY_RUN, lecture_ids=lecture_ids,
            eligibility_version=self.perfect_eligibility_version,
            persist_shadow_result=(self.persist if persist_shadow_result is None
                                   else persist_shadow_result))


# What the validated services actually call their Graph counters. None of them
# publishes a field named `graph_calls`, so summing these is the only honest way
# to know what a stage cost. Listed explicitly rather than guessed at, so adding
# a service means adding its counter here and noticing that you had to.
GRAPH_COUNTER_KEYS = (
    "meetings_queried",            # Phase 2A: one onlineMeetings transcript list
    "artifact_contents_fetched",   # Phase 2A: one transcript content fetch
    "meeting_lookups_attempted",   # Phase 1: one onlineMeetings resolution
    "calendar_queries",            # Phase 1: the calendar sweep itself
)


def _outcome(result: dict, *, graph_calls: int = 0, provider_calls: int = 0) -> dict:
    """
    Normalise a service summary into what the orchestrator records.

    The full summary is kept out of the audit row on purpose: it can be large,
    it can contain subjects and speaker labels, and the audit table exists to
    record decisions, not payloads.
    """
    counted = result.get("graph_calls")
    if counted is None:
        counted = sum(result.get(key) or 0 for key in GRAPH_COUNTER_KEYS)
    return {
        "graph_calls": counted or graph_calls or 0,
        "provider_calls": result.get("provider_calls", provider_calls) or 0,
        "legacy_rows_written": (result.get("legacy_sessions_written", 0)
                                + result.get("perfect_rows_written", 0)),
        "summary": result,
    }


def attendance_probe_factory(resolution_service=None):
    """
    The single external question the state resolver may ask: "does the
    attendance source answer for this lecture TODAY?".

    It is answered by running the validated Phase 2C3 resolution with
    `persist=False`. That matters more than it looks. The alternative - query
    `public.kbc_attendance` directly and check for rows - would need this file
    to know how a module name is normalised, how the business date is derived
    and which rows count, and a second copy of those rules is exactly how the
    probe and the recovery end up disagreeing about whether a lecture is
    recoverable. Here there is one rule, used twice.

    It writes nothing, calls no provider and calls no Graph endpoint, so a
    lecture that is still waiting costs one read and nothing else.
    """
    service = resolution_service

    def probe(connection, lecture) -> bool:
        if service is None:
            return False
        observed = service.resolve_lecture(
            connection, lecture["lecture_id"], persist=False)["lectures"][0]
        status = classify(
            source_row_count=observed["attendance_source_row_count"],
            present_row_count=observed["attendance_present_row_count"],
            effective_member_count=observed["effective_attendance_count"],
            source_rows_any_status=observed.get("attendance_source_rows_any_status"))
        return is_authoritative(status)

    return probe
