"""
Several Teams recordings of one lecture: the LONGEST is imported and delivered (2026-10-28).

The real daemon, recording worker, Bunny workers, delivery, reconciler and operator re-selection against
the isolated test database, with fake Graph / LMS / Bunny. Covers the 2026-10-27 shapes (a ~3 min false
start next to the ~2 h lecture), a lecture imported before the rule (the short video already in Bunny,
deleted by hand afterwards), and idempotent re-runs: nothing re-uploaded, nothing deleted by the publisher.
"""
from __future__ import annotations

from datetime import timedelta

import psycopg

from app.db.repositories.publisher_ops import PartDecisionRepository
from app.publisher import delivery, reselection
from app.publisher.operations import OperatorActionRefused
from app.publisher.recordings import RecordingPolicy, RecordingWorker
from tests.integration.test_publisher_delivery import act
from tests.integration.test_publisher_runtime import (  # noqa: F401 - fixtures
    END, START, World, _isolated, url,
)
from tests.unit.publisher_fakes import LIBRARY_ID

ACTOR, REASON = "ops-test", "2026-10-27 false start imported instead of the lecture"


def _connect(world):
    return lambda: psycopg.connect(world.url, prepare_threshold=None)


def _worker(world):
    return RecordingWorker(connect=_connect(world), graph=world.rgraph, discovery_factory=world.rgraph.discovery,
                           evidence_reader=world.rgraph.reader(), clock=world.clock,
                           policy=RecordingPolicy(lock_timeout_ms=500))


def _marketing(world):
    """~3 min false start, then the ~2 h lecture (a new Teams call)."""
    world.part(START, START + timedelta(minutes=3), call_id="call-a")
    world.part(START + timedelta(minutes=5), END + timedelta(minutes=3), call_id="call-b")


def _decide(world, **decisions):
    with psycopg.connect(world.url, autocommit=True) as c:
        for part, decision in decisions.items():
            PartDecisionRepository().decide(c, world.lecture_id, int(part[1:]), decision=decision,
                                            reason="seed", actor=ACTOR)


def _before_the_rule(world):
    """The shape of a lecture located before the rule: no selection on record, every part counted."""
    with psycopg.connect(world.url, autocommit=True) as c:
        c.execute("DELETE FROM public.bunny_publisher_part_decisions WHERE lecture_id = %s", (world.lecture_id,))
        c.execute("""UPDATE public.bunny_recording_discoveries d
                        SET metadata = d.metadata - 'recording_selection',
                            expected_part_count = s.n, located_part_count = s.n, missing_part_count = 0
                       FROM (SELECT count(*)::int AS n FROM public.bunny_recording_sources
                              WHERE lecture_id = %s AND locate_status = 'LOCATED') s
                      WHERE d.lecture_id = %s""", (world.lecture_id, world.lecture_id))


def _view(world):
    with psycopg.connect(world.url) as c:
        c.read_only = True
        return delivery.lecture_delivery(c, world.lecture_id, library_id=LIBRARY_ID)


def _plan(world):
    return reselection.plan_lecture(_connect(world), _worker(world), world.lecture_id, library_id=LIBRARY_ID)


def _apply(world):
    return reselection.apply(_connect(world), _worker(world), world.lecture_id, actor=ACTOR, reason=REASON,
                             library_id=LIBRARY_ID)


def _video_id(world, part):
    return next(str(v[3]) for v in world.videos(live=True) if v[0] == part)


# =========================================================================== new lectures
def test_the_2_hour_recording_is_imported_never_the_3_minute_false_start(url):
    world = World(url)
    _marketing(world)
    world.run(rounds=12)
    assert world.ready()
    [video] = world.videos()
    assert video[0] == 2 and video[2] == "VERIFIED" and len(world.bunny.fetch_calls) == 1
    view = _view(world)
    assert [p["source_part_index"] for p in view["parts"]] == [2]
    assert view["excluded_parts"] == [{"source_part_index": 1, "reason": "OPERATOR_EXCLUDED"}]
    world.run(rounds=4)                                        # re-running the same lecture changes nothing
    assert len(world.bunny.fetch_calls) == 1 and len(world.videos()) == 1


def test_reselecting_a_correct_lecture_changes_nothing_and_uploads_nothing(url):
    world = World(url)
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))
    world.run(rounds=10)
    assert world.ready()
    plan = _plan(world)
    assert plan["action"] == reselection.NO_CHANGE and plan["stop_delivering"] == []
    assert _apply(world)["applied"] is False
    world.run(rounds=3)
    assert len(world.bunny.fetch_calls) == 1 and "OPERATOR_RECORDING_RESELECTED" not in world.events()


# =========================================================================== imported before the rule
def test_both_imported_and_released_before_the_rule_reselection_keeps_the_long_video(url):
    world = World(url)
    _marketing(world)
    _decide(world, p1="INCLUDE")                              # reproduce the pre-rule import of both
    world.run(rounds=14)
    assert sorted(v[0] for v in world.videos(live=True)) == [1, 2]
    _before_the_rule(world)
    assert act(world, "release")["changed"]
    short_video, fetches = _video_id(world, 1), len(world.bunny.fetch_calls)

    plan = _plan(world)                                        # read-only: nothing changed by the preview
    assert (plan["action"], plan["selection"]["part_index"]) == (reselection.RESELECT_KEEP_VIDEO, 2)
    assert [(p["part_index"], p["bunny_video_id"], p["visibility"]) for p in plan["stop_delivering"]] == [
        (1, short_video, "READY_FOR_LMS")]
    assert "OPERATOR_RECORDING_RESELECTED" not in world.events()

    result = _apply(world)
    assert result["applied"] and result["selected_part"] == 2
    view = _view(world)
    assert [p["source_part_index"] for p in view["parts"]] == [2] and view["release"] == "RELEASED"
    assert len(world.bunny.fetch_calls) == fetches and not world.bunny.delete_calls   # no upload, no delete
    assert short_video in world.bunny.videos                   # the publisher never deletes it

    del world.bunny.videos[short_video]                        # the operator deletes it in Bunny
    summary = world.reconciler.run("FULL")
    assert summary["video_missing_in_bunny"] == 0 and summary["retired_deleted_unselected_videos"] == 1
    [(reason, visibility)] = world.q("SELECT retire_reason, visibility_state FROM public.bunny_videos "
                                     "WHERE bunny_video_id = %s", short_video)
    assert (reason, visibility) == ("VIDEO_DELETED_IN_BUNNY_PART_NOT_DELIVERED", "WITHDRAWN")
    assert "VIDEO_RETIRED_DELETED_IN_BUNNY" in world.events() and "OPERATOR_RECORDING_RESELECTED" in world.events()

    world.run(rounds=4)                                        # idempotent: no re-import of either part
    assert len(world.bunny.fetch_calls) == fetches and [v[0] for v in world.videos(live=True)] == [2]
    assert _view(world)["release"] == "RELEASED" and _plan(world)["action"] == reselection.NO_CHANGE


def test_only_the_short_one_imported_before_the_rule_the_long_one_is_imported_after_reselection(url):
    world = World(url)
    _marketing(world)
    _decide(world, p1="INCLUDE", p2="EXCLUDE")                # reproduce: only the 3-minute video in Bunny
    world.run(rounds=12)
    assert [v[0] for v in world.videos(live=True)] == [1]
    _before_the_rule(world)
    short_video, fetches = _video_id(world, 1), len(world.bunny.fetch_calls)

    assert _plan(world)["action"] == reselection.RESELECT_AND_IMPORT
    _apply(world)
    world.run(rounds=12)
    assert len(world.bunny.fetch_calls) == fetches + 1         # exactly one import: the 2-hour recording
    assert sorted(v[0] for v in world.videos(live=True)) == [1, 2]
    assert [p["source_part_index"] for p in _view(world)["parts"]] == [2] and _view(world)["verified_ready"]

    del world.bunny.videos[short_video]
    assert world.reconciler.run("FULL")["retired_deleted_unselected_videos"] == 1
    world.run(rounds=4)
    assert len(world.bunny.fetch_calls) == fetches + 1 and [v[0] for v in world.videos(live=True)] == [2]


def test_an_operator_decision_against_the_selection_blocks_reselection(url):
    world = World(url)
    _marketing(world)
    _decide(world, p1="INCLUDE")
    world.run(rounds=12)
    plan = _plan(world)
    assert plan["action"] == reselection.BLOCKED and plan["blocking"] == ["OPERATOR_PART_DECISION_OVERRIDES_SELECTION"]
    try:
        _apply(world)
        raise AssertionError("re-selection must be refused")
    except OperatorActionRefused as refusal:
        assert refusal.code == "RESELECTION_BLOCKED"


def test_a_delivered_parts_missing_video_stays_a_critical_alert(url):
    world = World(url)
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))
    world.run(rounds=10)
    video = _video_id(world, 1)
    del world.bunny.videos[video]
    summary = world.reconciler.run("FULL")
    assert summary["video_missing_in_bunny"] == 1 and summary["retired_deleted_unselected_videos"] == 0
    assert world.q("SELECT retired_at IS NULL FROM public.bunny_videos WHERE bunny_video_id = %s", video) == [(True,)]


def test_day_preview_reads_only_and_reports_every_lecture(url):
    world = World(url)
    _marketing(world)
    _decide(world, p1="INCLUDE")
    world.run(rounds=14)
    _before_the_rule(world)
    before = world.q("SELECT row_to_json(d)::text FROM public.bunny_recording_discoveries d WHERE lecture_id = %s",
                     world.lecture_id)
    preview = reselection.preview_day(_connect(world), _worker(world), START.date(), library_id=LIBRARY_ID)
    assert preview["lectures"] == 1 and preview["actions"] == {reselection.RESELECT_KEEP_VIDEO: 1}
    [plan] = preview["plans"]
    assert plan["selection"]["part_index"] == 2 and [p["part_index"] for p in plan["stop_delivering"]] == [1]
    assert world.q("SELECT row_to_json(d)::text FROM public.bunny_recording_discoveries d WHERE lecture_id = %s",
                   world.lecture_id) == before
    assert ".mp4" not in repr(preview) and "http" not in repr(preview)


# =========================================================================== the 2026-10-27 production sequence
def test_short_video_deleted_by_hand_before_reselection_is_seen_in_bunny_and_retired(url):
    """Production order: the operator already deleted the 3-minute video; then the fix is deployed."""
    world = World(url)
    _marketing(world)
    _decide(world, p1="INCLUDE")
    world.run(rounds=14)
    _before_the_rule(world)
    assert act(world, "release")["changed"]
    short_video, long_video, fetches = _video_id(world, 1), _video_id(world, 2), len(world.bunny.fetch_calls)
    del world.bunny.videos[short_video]                        # deleted in Bunny before the fix is deployed
    assert world.reconciler.run("FULL")["video_missing_in_bunny"] == 1      # the old rule: a CRITICAL alert

    plan = reselection.plan_lecture(_connect(world), _worker(world), world.lecture_id, library_id=LIBRARY_ID,
                                    bunny=world.bunny)
    assert plan["action"] == reselection.RESELECT_KEEP_VIDEO
    assert [(p["part_index"], p["bunny_present"]) for p in plan["stop_delivering"]] == [(1, False)]
    assert plan["selected_video"]["bunny_video_id"] == long_video and plan["selected_video"]["bunny_present"]
    _apply(world)
    summary = world.reconciler.run("FULL")
    assert summary["video_missing_in_bunny"] == 0 and summary["retired_deleted_unselected_videos"] == 1
    assert world.q("SELECT status FROM public.bunny_publisher_alerts WHERE kind = 'VIDEO_MISSING_IN_BUNNY'") == [
        ("RESOLVED",)]
    world.run(rounds=4)
    assert len(world.bunny.fetch_calls) == fetches and not world.bunny.delete_calls
    body = _view(world)
    assert [p["bunny"]["video_id"] for p in body["parts"]] == [long_video] and body["release"] == "RELEASED"


def test_an_old_import_job_waiting_for_review_is_named_and_an_operator_retry_imports(url):
    from app.publisher import operations
    from app.publisher.handlers import part_subject_key
    from app.db.repositories.publisher_queue import PublisherQueueRepository
    world = World(url)
    _marketing(world)
    _decide(world, p1="INCLUDE", p2="EXCLUDE")
    world.run(rounds=12)
    _before_the_rule(world)
    with psycopg.connect(world.url, autocommit=True) as c:     # an earlier IMPORT_VIDEO of part 2 ended in review
        job, _ = PublisherQueueRepository().enqueue(c, job_kind="IMPORT_VIDEO", lecture_id=world.lecture_id,
                                                    subject_key=part_subject_key(world.lecture_id, 2))
        c.execute("UPDATE public.bunny_publisher_jobs SET status = 'REVIEW_REQUIRED', finished_at = now(), "
                  "last_error_code = 'PART_EXCLUDED_BY_OPERATOR' WHERE job_id = %s", (job["job_id"],))
    fetches = len(world.bunny.fetch_calls)
    _apply(world)
    world.run(rounds=6)
    assert len(world.bunny.fetch_calls) == fetches             # the planner leaves it to the operator, as before
    [stalled] = _plan(world)["operator_retry_needed"]
    assert (stalled["job_id"], stalled["job_kind"]) == (str(job["job_id"]), "IMPORT_VIDEO")
    with psycopg.connect(world.url, autocommit=True) as c:
        operations.operator_retry(c, stalled["job_id"], actor=ACTOR, reason="import the selected recording")
    world.run(rounds=12)
    assert len(world.bunny.fetch_calls) == fetches + 1 and _view(world)["verified_ready"]
