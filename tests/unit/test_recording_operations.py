"""
Recording Links as a first-class Operations feature, offline.

The claims under test:

  * the nightly scheduler and the Historical Backfill reach LINK_RECORDING
    through the SAME PipelineOrchestrator and the SAME resolver - there is no
    recording pipeline of their own - and only when RECORDING_LINK_MODE=write;
  * the backfill runner's recording report is read-only, is stored beside the
    day, and never fails the day;
  * every coverage number is a count of one backend partition, so the totals
    always reconcile with the rows beneath them.

The resolver here is the real PipelineStateResolver over the fixture rows of a
fully finished lecture (tests/unit/test_pipeline_state.py); only the stage
action itself is scripted, because what is under test is who reaches it.
The SQL behind all of this runs against the isolated test database in
tests/integration/test_recording_operations_persistence.py.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.db.repositories.backfill import COMPLETED, MODE_EXECUTE, MODE_PREVIEW
from app.orchestration.backfill import BackfillRunner
from app.orchestration.orchestrator import PipelineOrchestrator
from app.orchestration.runner import StageRunner
from app.orchestration.scheduler import SchedulerConfig, SchedulerService
from app.orchestration.stages import (
    AUTOMATABLE_ACTIONS,
    LINK_RECORDING,
    RUN_TYPE_BACKFILL,
    RUN_TYPE_SCHEDULED,
)
from app.orchestration.state import PipelineStateResolver
from app.recordings import coverage as c
from app.recordings import models as m
from test_backfill import FakeRepository, a_run
from test_orchestration import StubPreflight
from test_pipeline_state import (
    LECTURE_ID,
    SESSION_DATE,
    StubConnection,
    StubObservations,
    complete_rows,
)

NOW = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# a finished lecture whose only gap is its recording
# ---------------------------------------------------------------------------

class Observations(StubObservations):
    def __init__(self):
        super().__init__(recording_url=None, excel_synced_at=NOW)

    def recording_link(self, connection, legacy_session_id):
        row = self.legacy_qa_session(connection, legacy_session_id)
        return None if row is None else {**row, "cancelled_session": "false"}


class NoAttempts:
    def attempt(self, connection, lecture_id):
        return None


class Connection(StubConnection):
    """The fixture connection, usable where the runner opens one per day."""

    def __init__(self):
        super().__init__(complete_rows())
        self.commits = 0

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class LinkingRunner:
    """
    Stands in for the ONE stage action, and nothing else.

    It performs LINK_RECORDING the way the real stage ends - the legacy row
    gains a URL - so the real resolver then settles the stage. Everything
    upstream of it (dispatch, scope, gating) is the real orchestrator's.
    """

    def __init__(self, observations, *, armed=True):
        self.observations = observations
        self.armed = armed
        self.calls = []

    def scope_of(self, action):
        return "LECTURE"

    def can_run(self, action):
        return action in AUTOMATABLE_ACTIONS and (action != LINK_RECORDING or self.armed)

    def refusal_reason(self, action):
        return "RECORDING_LINK_MODE_IS_NOT_WRITE"

    def execute(self, connection, action, *, session_date, lecture_id=None):
        self.calls.append((action, lecture_id))
        if action == LINK_RECORDING:
            self.observations.recording_url = "https://kbc.sharepoint.com/written"
        return {"summary": {"status": "WRITTEN"}, "graph_calls": 3, "provider_calls": 0,
                "legacy_rows_written": 1}


def orchestrator(mode):
    observations = Observations()
    resolver = PipelineStateResolver(legacy_observations=observations,
                                     recording_link_mode=mode,
                                     recording_links=NoAttempts(), clock=lambda: NOW)
    runner = LinkingRunner(observations, armed=mode == "write")
    return PipelineOrchestrator(resolver=resolver, runner=runner,
                                preflight=StubPreflight(), max_passes=4), runner, observations


def backfill(orchestrator_):
    repository = FakeRepository(a_run(requested_from=SESSION_DATE,
                                      requested_to=SESSION_DATE, mode=MODE_EXECUTE))
    return BackfillRunner(orchestrator=orchestrator_, connection_factory=Connection,
                          readonly_connection_factory=Connection,
                          repository=repository), repository


# ---------------------------------------------------------------------------
# 1-3. one stage, reached the same way by both automatic paths
# ---------------------------------------------------------------------------

def test_historical_backfill_in_write_mode_reaches_link_recording_through_the_orchestrator():
    orchestrator_, runner, observations = orchestrator("write")
    calls = []
    real = orchestrator_.run_window

    def spy(connection, target, **kwargs):
        calls.append(kwargs["run_type"])
        return real(connection, target, **kwargs)

    orchestrator_.run_window = spy
    service, repository = backfill(orchestrator_)
    outcome = service.run_claimed("bf-1")

    assert outcome["outcome"] == COMPLETED
    assert calls == [RUN_TYPE_BACKFILL]
    assert runner.calls == [(LINK_RECORDING, LECTURE_ID)]
    assert observations.recording_url                    # the stage ended the gap
    assert repository.days[0]["provider_calls"] == 0


def test_the_nightly_scheduler_in_write_mode_reaches_the_same_stage():
    orchestrator_, runner, _ = orchestrator("write")
    config = SchedulerConfig(enabled=True, lookback_days=0, discover=False)
    summary = SchedulerService(orchestrator=orchestrator_, config=config).run_cycle(
        Connection(), now=datetime(2026, 9, 17, 21, 0, tzinfo=timezone.utc))

    assert summary["run_type"] == RUN_TYPE_SCHEDULED
    assert runner.calls == [(LINK_RECORDING, LECTURE_ID)]
    assert summary["provider_calls"] == 0
    assert summary["lectures"][0]["actions"] == [LINK_RECORDING]


@pytest.mark.parametrize("path", ["backfill", "scheduler"])
def test_observe_mode_never_offers_or_runs_the_write(path):
    orchestrator_, runner, observations = orchestrator("observe")
    if path == "backfill":
        backfill(orchestrator_)[0].run_claimed("bf-1")
    else:
        SchedulerService(orchestrator=orchestrator_, config=SchedulerConfig(
            enabled=True, lookback_days=0, discover=False)).run_cycle(
            Connection(), now=datetime(2026, 9, 17, 21, 0, tzinfo=timezone.utc))
    assert runner.calls == []
    assert observations.recording_url is None


def test_the_real_runner_refuses_link_recording_in_observe_mode_whatever_it_is_asked():
    class Settings:
        recording_link_mode = "observe"
        recording_links_writable = False

    runner = StageRunner(settings=Settings(), persist=True)
    assert runner.can_run(LINK_RECORDING) is False
    assert runner.refusal_reason(LINK_RECORDING) == "RECORDING_LINK_MODE_IS_NOT_WRITE"


# ---------------------------------------------------------------------------
# the backfill runner's recording hook
# ---------------------------------------------------------------------------

class Report:
    """Answers the runner's two questions and records how it was asked."""

    mode = "write"

    def __init__(self, items=None, raises=None):
        self.items = items if items is not None else [
            {"outcome": c.EXACT_MATCH, "item_key": "a"}]
        self.raises = raises
        self.previewed, self.executed = [], []

    def preview_day(self, connection, day):
        self.previewed.append(day)
        if self.raises:
            raise self.raises
        return self.items, 7

    def execute_day(self, connection, day, *, written_since):
        self.executed.append((day, written_since))
        return self.items


class RecordingRepository(FakeRepository):
    def __init__(self, run):
        super().__init__(run)
        self.stored = []

    def replace_recording_items(self, _connection, _run_id, **kwargs):
        self.stored.append(kwargs)


class PreviewService:
    def preview(self, connection, start, end, **kwargs):
        from app.orchestration.backfill import BackfillPreview, DayPreview
        return BackfillPreview(requested_from=start, requested_to=end,
                               days=[DayPreview(business_date=start)])


def preview_runner(report, repository):
    from test_backfill import FakeConnection, FakeOrchestrator
    orchestrator_ = FakeOrchestrator()
    return BackfillRunner(orchestrator=orchestrator_, connection_factory=FakeConnection,
                          readonly_connection_factory=FakeConnection,
                          repository=repository, preview_service=PreviewService(),
                          recording_report=report), orchestrator_


def test_a_preview_day_asks_for_live_recording_verdicts_and_never_runs_the_pipeline():
    repository = RecordingRepository(a_run(mode=MODE_PREVIEW))
    report = Report()
    service, orchestrator_ = preview_runner(report, repository)
    service.run_claimed("bf-1")

    assert orchestrator_.calls == []
    assert report.previewed == [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
    assert report.executed == []
    assert [entry["items"] for entry in repository.stored] == [report.items] * 3
    assert all(entry["error_code"] is None for entry in repository.stored)
    # The Graph reads are accounted for on the day, and nothing else is.
    assert repository.days[0]["graph_calls"] == 1 + 7
    assert repository.days[0]["provider_calls"] == 0


def test_an_execute_day_snapshots_what_the_orchestrator_did_after_it_ran():
    repository = RecordingRepository(a_run(mode=MODE_EXECUTE))
    report = Report()
    service, orchestrator_ = preview_runner(report, repository)
    service.run_claimed("bf-1")

    assert len(orchestrator_.calls) == 3
    assert [day for day, _ in report.executed] == [date(2026, 9, 1), date(2026, 9, 2),
                                                   date(2026, 9, 3)]
    assert all(since.tzinfo is not None for _, since in report.executed)
    assert report.previewed == []


def test_a_recording_failure_is_recorded_on_the_day_and_never_fails_it():
    from app.common.errors import PlatformError
    repository = RecordingRepository(a_run(mode=MODE_PREVIEW))
    report = Report(raises=PlatformError("GRAPH_AUTHENTICATION_FAILED", "refused"))
    service, _ = preview_runner(report, repository)
    outcome = service.run_claimed("bf-1")

    assert outcome["outcome"] == COMPLETED
    assert {entry["error_code"] for entry in repository.stored} == {
        "GRAPH_AUTHENTICATION_FAILED"}
    assert all(entry["items"] == [] for entry in repository.stored)
    assert all(day["status"] == "COMPLETED" for day in repository.days)


def test_a_runner_without_a_report_behaves_exactly_as_before():
    repository = RecordingRepository(a_run(mode=MODE_EXECUTE))
    service, _ = preview_runner(None, repository)
    service.run_claimed("bf-1")
    assert repository.stored == []


def test_the_backfill_runner_is_built_with_the_recording_report_in_the_configured_mode():
    from app.config.settings import Settings
    from app.orchestration import factory
    offline = dict(database_url="", aptem_database_url="", graph_tenant_id="t",
                   graph_client_id="c", graph_client_secret="s",
                   graph_scope="https://graph.microsoft.com/.default",
                   graph_base_url="https://graph.microsoft.com/v1.0",
                   calendar_user_upn="calendar@example.invalid")
    for mode in ("observe", "write"):
        runner = factory.build_backfill_runner(
            Settings(**offline, recording_link_mode=mode), connection_factory=lambda: None)
        assert runner.recording_report.mode == mode
        assert runner.recording_report.resolver_factory().recording_link_mode == mode


def test_the_backfill_preview_reuses_the_cli_preview_and_can_never_publish():
    """The worker and `recording-links-preview` are built by one function."""
    import inspect

    from app.cli import main
    from app.config.settings import Settings
    from app.orchestration import factory
    from app.recordings.preview import build_preview
    from tests.unit.recording_graph_fakes import FakeGraph

    assert "build_preview(" in inspect.getsource(main._recording_links_preview)
    assert "build_preview(" in inspect.getsource(factory.build_backfill_recording_report)
    settings = Settings(database_url="", aptem_database_url="", graph_tenant_id="t",
                        graph_client_id="c", graph_client_secret="s", graph_scope="s",
                        graph_base_url="https://graph.microsoft.com/v1.0",
                        calendar_user_upn="x", recording_link_mode="write",
                        recording_link_create_org_links=True)
    preview = build_preview(settings, resolver=None, graph=FakeGraph())
    assert preview.service.publisher is None
    assert preview.metadata_evidence == "LIVE"


# ---------------------------------------------------------------------------
# outcomes: one partition, decided in Python
# ---------------------------------------------------------------------------

def item(**overrides):
    values = {"population": "CODED_LECTURE", "recording_status": None,
              "recording_stage_state": "MISSING", "written": False, "would_write": False,
              "earlier_stage": None, "legacy_cancelled": False, "stage_reason": None}
    values.update(overrides)
    values["outcome"] = c.outcome_for(values)
    return values


@pytest.mark.parametrize("overrides, expected", [
    ({"written": True, "recording_status": m.WRITTEN}, c.WRITTEN),
    ({"recording_stage_state": "COMPLETE"}, c.ALREADY_LINKED),
    ({"recording_status": m.RECORDING_ALREADY_LINKED}, c.ALREADY_LINKED),
    ({"recording_status": m.WRITE_REFUSED_ALREADY_LINKED}, c.ALREADY_LINKED),
    ({"recording_status": m.NO_RECORDING_EXPECTED_CANCELLED}, c.NOT_APPLICABLE_OUTCOME),
    ({"recording_status": m.EXACT_RECORDING_FILE_MATCHED, "would_write": True},
     c.EXACT_MATCH),
    ({"recording_status": m.EXACT_RECORDING_FILE_MATCHED, "would_write": True,
      "earlier_stage": "QA_EVALUATION"}, c.BLOCKED_BY_EARLIER_STAGE),
    ({"recording_status": m.NO_LEGACY_QA_ROW, "recording_stage_state": "NOT_APPLICABLE",
      "earlier_stage": "LEGACY_QA_SYNC"}, c.BLOCKED_BY_EARLIER_STAGE),
    ({"recording_status": m.NO_LEGACY_QA_ROW, "recording_stage_state": "NOT_APPLICABLE"},
     c.NO_LEGACY_TARGET),
    ({"recording_status": m.AMBIGUOUS_RECORDING_FILES}, c.AMBIGUOUS),
    ({"recording_status": m.GRAPH_RECORDING_AMBIGUOUS}, c.AMBIGUOUS),
    ({"recording_status": m.TIMESTAMP_MISMATCH}, c.REVIEW),
    ({"recording_status": m.GRAPH_LOOKUP_FAILED, "recording_stage_state": "REVIEW_REQUIRED"},
     c.REVIEW),
    ({"recording_status": m.GRAPH_LOOKUP_FAILED}, c.GRAPH_LOOKUP_FAILED),
    ({"recording_status": m.DRIVE_ITEM_DISCOVERY_FAILED}, c.DISCOVERY_FAILED),
    ({"recording_status": m.RECORDING_FILE_NOT_FOUND}, c.NOT_FOUND),
    ({"recording_status": m.SUBJECT_MISMATCH}, c.NOT_FOUND),
    ({"recording_status": m.GRAPH_RECORDING_NOT_FOUND}, c.NOT_FOUND),
    ({"recording_status": m.LINK_URL_UNAVAILABLE}, c.WAITING_OUTCOME),
    ({}, c.NOT_EVALUATED),
    ({"population": "LEGACY_ROW_ONLY", "recording_status": "NO_CODED_LECTURE"},
     c.NO_CODED_LECTURE),
    ({"population": "LEGACY_ROW_ONLY", "recording_status": "NO_CODED_LECTURE",
      "legacy_cancelled": True}, c.NOT_APPLICABLE_OUTCOME),
    ({"population": "LEGACY_ROW_ONLY", "recording_status": m.RECORDING_ALREADY_LINKED},
     c.ALREADY_LINKED),
])
def test_every_stage_verdict_lands_in_exactly_one_outcome(overrides, expected):
    assert item(**overrides)["outcome"] == expected


def test_an_exact_match_that_is_not_live_is_never_reported_as_writable():
    assert item(recording_status=m.EXACT_RECORDING_FILE_MATCHED,
                would_write=False)["outcome"] == c.OTHER


def test_coverage_counters_reconcile_with_the_rows():
    rows = [
        item(recording_stage_state="COMPLETE"),
        item(recording_stage_state="COMPLETE"),
        item(written=True, recording_status=m.WRITTEN, perfect_row_updated=True),
        item(recording_status=m.EXACT_RECORDING_FILE_MATCHED, would_write=True,
             perfect_row_would_update=True),
        item(recording_status=m.EXACT_RECORDING_FILE_MATCHED, would_write=True,
             earlier_stage="QA_EVALUATION"),
        item(recording_status=m.AMBIGUOUS_RECORDING_FILES),
        item(recording_status=m.NO_RECORDING_EXPECTED_CANCELLED),
        item(recording_status=m.GRAPH_LOOKUP_FAILED),
        item(recording_status=m.DRIVE_ITEM_DISCOVERY_FAILED),
        item(recording_status=m.RECORDING_FILE_NOT_FOUND),
        item(population="LEGACY_ROW_ONLY", recording_status="NO_CODED_LECTURE"),
    ]
    summary = c.summarize(rows)

    assert summary["reconciles"] is True
    assert sum(summary["by_outcome"].values()) == summary["total"] == 11
    assert (summary["not_applicable"], summary["eligible"]) == (1, 10)
    assert (summary["already_linked"], summary["missing_before"]) == (2, 8)
    assert (summary["written"], summary["would_write"]) == (1, 1)
    assert summary["exact_matched"] == 3          # written + writable + blocked-but-exact
    assert summary["blocked_by_earlier_stage"] == 1
    assert (summary["ambiguous"], summary["graph_lookup_failed"],
            summary["discovery_failed"], summary["not_found"],
            summary["no_coded_lecture"]) == (1, 1, 1, 1, 1)
    assert summary["still_missing_after"] == 7
    assert summary["projected_missing_after_write"] == 6
    assert summary["perfect_rows_updated"] == 1
    assert summary["perfect_rows_would_update"] == 1
    assert summary["coverage_percent_before"] == 20.0
    assert summary["coverage_percent_after"] == 30.0
    assert summary["projected_coverage_percent"] == 40.0


def test_an_empty_range_has_no_coverage_rather_than_zero_percent():
    summary = c.summarize([])
    assert summary["total"] == 0 and summary["coverage_percent_before"] is None
    assert summary["reconciles"] is True


def test_a_wait_is_not_an_earlier_blocker_but_an_earlier_action_is():
    assert c.earlier_stage_of({"executable_stage": "ATTENDANCE",
                               "next_executable_action": "WAIT_FOR_ATTENDANCE_SOURCE"}) == (
        None, None)
    assert c.earlier_stage_of({"executable_stage": "QA_EVALUATION",
                               "next_executable_action": "MANUAL_REVIEW_REQUIRED"}) == (
        "QA_EVALUATION", "MANUAL_REVIEW_REQUIRED")
    assert c.earlier_stage_of({"executable_stage": "RECORDING_LINK",
                               "next_executable_action": LINK_RECORDING}) == (None, None)
    assert c.earlier_stage_of({"executable_stage": "EXCEL_SYNC",
                               "next_executable_action": "WAIT_FOR_EXCEL_SYNC"}) == (
        None, None)


def test_a_preview_row_keeps_no_url_file_name_or_graph_id():
    row = {"lecture_id": LECTURE_ID, "date": "2026-09-09", "subject": "S",
           "population": "CODED_LECTURE", "resolver_stage_state": "MISSING",
           "recording_match_status": m.EXACT_RECORDING_FILE_MATCHED, "would_write": True,
           "reason": "lead 4.446s via channel_recordings",
           "recording_source": "channel_recordings", "timestamp_difference_seconds": 4.446,
           "executable_stage": "RECORDING_LINK", "next_executable_action": LINK_RECORDING,
           "graph_created_at": "2026-09-09T07:37:15Z", "legacy_session_ref": "abc",
           "web_url": "https://leak", "recording_filename": "leak.mp4"}
    out = c.item_from_preview_row(row, mode="observe")
    assert out["outcome"] == c.EXACT_MATCH and out["source"] == "channel_recordings"
    assert out["evaluation"] == c.EVALUATION_LIVE_PREVIEW
    flat = repr(out)
    assert "https://" not in flat and ".mp4" not in flat
