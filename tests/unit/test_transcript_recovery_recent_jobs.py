"""
A failed background job carries its lecture's CURRENT outcome, wherever it is shown: the Manual
Transcript Recovery list and the lecture page (upload status and Teams retry status).

The job rows are history and are returned untouched (status stays FAILED). Only FAILED jobs are
annotated, each lecture is resolved once, and `resolved` follows the day report's own rule.
No database: the repository and the resolver are fakes.
"""
from app.orchestration.reconciliation import BUCKET_COMPLETE, BUCKET_FAILED
from app.orchestration.stages import FAILED as STAGE_FAILED
from app.orchestration.teams_transcript_retry import TeamsTranscriptRetry
from app.orchestration.transcript_recovery import ManualTranscriptRecovery

RESOLVED = "94008498-1fa8-51a2-a5c6-c9cd4077e8ab"   # failed retry, lecture since completed
STILL_BROKEN = "4761883d-1035-5390-9fde-625ffbdd545b"
WAITING_QA = "59929792-c621-5790-8b7c-12067f27db8e"


def _state(next_action, *, failed_stage=False):
    stages = {"ATTENDANCE": {"state": "COMPLETE"},
              "TRANSCRIPT_ACQUISITION": {"state": STAGE_FAILED if failed_stage else "COMPLETE"}}
    return {"stages": stages, "next_action": next_action, "next_executable_action": next_action}


STATES = {
    RESOLVED: _state("NOTHING_TO_DO"),
    STILL_BROKEN: _state("ACQUIRE_TRANSCRIPT", failed_stage=True),
    WAITING_QA: _state("RUN_QA"),
}


class FakeJobs:
    def __init__(self, jobs):
        self.jobs = jobs

    def recent(self, connection, limit=20):
        return [dict(job) for job in self.jobs[:limit]]

    def available(self, connection):
        return True

    def latest_for_lecture(self, connection, lecture_id):
        return next((dict(job) for job in self.jobs if job["lecture_id"] == str(lecture_id)), None)


class FakeResolver:
    def __init__(self):
        self.calls = []

    def for_lecture(self, connection, lecture_id):
        self.calls.append(lecture_id)
        return STATES[lecture_id]


def _service(jobs):
    resolver = FakeResolver()
    service = ManualTranscriptRecovery(uploads=None, resolver=resolver, orchestrator_factory=None,
                                       repository=object(), jobs=FakeJobs(jobs))
    return service, resolver


def _job(job_id, lecture_id, status):
    return {"job_id": job_id, "lecture_id": lecture_id, "status": status,
            "last_error_message": "stopped at ATTENDANCE: RESOLVE_ATTENDANCE"}


def test_a_failed_job_whose_lecture_completed_is_resolved_and_keeps_its_status():
    service, _ = _service([_job("J1", RESOLVED, "FAILED")])
    [job] = service.recent_jobs(connection=None)
    assert job["status"] == "FAILED"
    assert job["last_error_message"] == "stopped at ATTENDANCE: RESOLVE_ATTENDANCE"
    assert job["lecture_outcome"] == {"bucket": BUCKET_COMPLETE, "resolved": True,
                                      "next_action": "NOTHING_TO_DO"}


def test_a_failed_job_whose_lecture_is_still_open_stays_unresolved():
    service, _ = _service([_job("J1", STILL_BROKEN, "FAILED"), _job("J2", WAITING_QA, "FAILED")])
    broken, waiting = service.recent_jobs(connection=None)
    assert broken["lecture_outcome"]["bucket"] == BUCKET_FAILED
    assert broken["lecture_outcome"]["resolved"] is False
    assert waiting["lecture_outcome"]["resolved"] is False
    assert waiting["lecture_outcome"]["next_action"] == "RUN_QA"


def test_only_failed_jobs_are_annotated_and_each_lecture_is_resolved_once():
    service, resolver = _service([
        _job("J3", RESOLVED, "COMPLETED"), _job("J2", RESOLVED, "FAILED"),
        _job("J1", RESOLVED, "FAILED"), _job("J0", WAITING_QA, "QUEUED")])
    jobs = service.recent_jobs(connection=None)
    assert ["lecture_outcome" in job for job in jobs] == [False, True, True, False]
    assert resolver.calls == [RESOLVED]


# -- the lecture page: its latest job, from either status endpoint ---------------------------------
def _latest_from_upload_status(job):
    service, _ = _service([job])
    return service._latest_job(None, job["lecture_id"])


def _latest_from_teams_status(job):
    service = TeamsTranscriptRetry(resolver=FakeResolver(), jobs=FakeJobs([job]))
    service.plan = lambda connection, lecture_id: {"available": False}
    return service.status(None, job["lecture_id"])["job"]


LECTURE_PAGE_SOURCES = (_latest_from_upload_status, _latest_from_teams_status)


def test_the_lecture_page_shows_a_failed_job_of_a_completed_lecture_as_resolved():
    for latest in LECTURE_PAGE_SOURCES:
        job = latest(_job("J1", RESOLVED, "FAILED"))
        assert job["status"] == "FAILED", latest.__name__
        assert job["last_error_message"] == "stopped at ATTENDANCE: RESOLVE_ATTENDANCE"
        assert job["lecture_outcome"]["resolved"] is True, latest.__name__


def test_the_lecture_page_keeps_a_genuine_failure_unresolved():
    for latest in LECTURE_PAGE_SOURCES:
        job = latest(_job("J1", STILL_BROKEN, "FAILED"))
        assert job["lecture_outcome"] == {"bucket": BUCKET_FAILED, "resolved": False,
                                          "next_action": "ACQUIRE_TRANSCRIPT"}, latest.__name__


def test_the_lecture_page_leaves_other_jobs_alone():
    for latest in LECTURE_PAGE_SOURCES:
        job = latest(_job("J1", RESOLVED, "COMPLETED"))
        assert "lecture_outcome" not in job, latest.__name__
