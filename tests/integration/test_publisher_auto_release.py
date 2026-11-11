"""
Automatic release (AUTO_RELEASE scheduler task): the real daemon, planner, workers and delivery.release()
against the isolated test database, with fake Graph / LMS / Bunny. Off by default; when on, it releases
ONLY what an operator would, through the same audited release, and never twice.
"""
from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest

from app.publisher import delivery
from app.publisher.auto_release import ACTOR, AutoReleaser
from app.publisher.config import PRODUCTION_WRITE, STAGING_WRITE, PublisherConfig
from app.publisher.daemon import AUTO_RELEASE
from app.publisher.operations import OperatorActionRefused
from tests.integration.test_publisher_delivery import (
    INSECURE, SECURE, act, add_second_lecture, answer, delivered, lifecycle_events, production,
)
from tests.integration.test_publisher_runtime import (  # noqa: F401 - fixtures
    END, START, World, _confirm_organizer, _sibling, url,
)


class Security:
    """The live library check, scripted: a list of results or exceptions, the last one repeating."""

    def __init__(self, *results):
        self.results, self.calls = list(results) or [SECURE], 0

    def __call__(self):
        self.calls += 1
        result = self.results[min(self.calls, len(self.results)) - 1]
        if isinstance(result, Exception):
            raise result
        return result


def auto_world(url, *, security=None, **kwargs):
    """A PRODUCTION_WRITE world (the same library) whose daemon runs AUTO_RELEASE every tick."""
    world = delivered(url, **production(auto_release=True, **kwargs))
    world.security = security or Security()
    attach(world)
    return world


def attach(world, connect=None, **kwargs):
    connect = connect or (lambda: psycopg.connect(world.url, prepare_threshold=None))
    world.daemon.auto_releaser = AutoReleaser(config=world.config, connect=connect, security=world.security, **kwargs)


def one_part(world):
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))


def schedule(world):
    rows = world.q("SELECT last_status, last_detail FROM public.bunny_publisher_schedule WHERE task = %s", AUTO_RELEASE)
    return rows[0] if rows else (None, None)


def visibility(world):
    return [r[0] for r in world.q("SELECT visibility_state FROM public.bunny_videos WHERE lecture_id = %s "
                                  "AND retired_at IS NULL ORDER BY recording_part_index", world.lecture_id)]


# =========================================================================== configuration
def test_auto_release_is_off_by_default_needs_production_mode_and_has_no_date_restriction():
    base = dict(mode=PRODUCTION_WRITE, database_url="postgresql://x/y", graph_tenant_id="t", graph_client_id="c",
                graph_client_secret="s", start_date=START.date(), lms_database_url="l", discover_calendar=False,
                production_library_id=1, production_api_key="k", production_account_api_key="a",
                production_write_enabled=True)
    off = PublisherConfig(**base)
    assert off.auto_release is False and off.problems() == []
    assert off.describe()["auto_release"] is False and "auto_release_from" not in off.describe()
    on = PublisherConfig(**base, auto_release=True)
    assert on.problems() == [] and on.describe()["auto_release"] is True
    staging = PublisherConfig(**{**base, "mode": STAGING_WRITE, "staging_library_id": 1, "staging_api_key": "k",
                                 "staging_account_api_key": "a"}, auto_release=True)
    assert any("PRODUCTION_WRITE" in p for p in staging.problems())
    # A leftover PUBLISHER_AUTO_RELEASE_FROM is ignored: there is no date restriction any more.
    env = PublisherConfig.from_mapping({"PUBLISHER_AUTO_RELEASE": "true", "PUBLISHER_AUTO_RELEASE_FROM": "2026-10-26"})
    assert env.auto_release is True and not hasattr(env, "auto_release_from")
    assert PublisherConfig.from_mapping({}).auto_release is False


def test_without_an_auto_releaser_the_task_never_runs(url):
    world = delivered(url, **production())                    # auto_release off: build_daemon passes none
    one_part(world)
    world.run(rounds=10)
    assert world.ready() and answer(world)["release"]["status"] == "NOT_RELEASED"
    assert schedule(world) == (None, None) and lifecycle_events(world) == []


# =========================================================================== the happy path
def test_a_verified_matched_lecture_is_released_once_and_becomes_playable(url):
    world = auto_world(url)
    one_part(world)
    world.run(rounds=10)
    body = answer(world)
    assert body["state"] == "READY" and body["release"]["status"] == "RELEASED"
    assert body["playback"]["available"] is True and visibility(world) == ["READY_FOR_LMS"]
    [(actor, detail)] = world.q("SELECT actor, detail FROM public.bunny_publisher_events WHERE lecture_id = %s "
                                "AND event = 'PUBLICATION_RELEASED'", world.lecture_id)
    assert actor == ACTOR and detail["reason"].startswith("automatic release") and detail["parts"] == [1]
    status, last = schedule(world)
    assert status == "SUCCEEDED"
    # Idempotent: more ticks, the security check is not even consulted again, no second event.
    calls = world.security.calls
    world.run(rounds=6)
    assert lifecycle_events(world) == ["PUBLICATION_RELEASED"] and world.security.calls == calls
    assert schedule(world)[1]["candidates"] == 0
    assert world.events().count("LECTURE_FULLY_READY") == 1 and len(world.bunny.fetch_calls) == 1  # no re-import


def test_released_only_after_every_check_holds_not_before(url):
    world = auto_world(url)
    one_part(world)
    released_at_tick, ready_at_tick = None, None
    for tick in range(14):
        world.run(rounds=1)
        if ready_at_tick is None and answer(world)["state"] == "READY":
            ready_at_tick = tick
        if released_at_tick is None and lifecycle_events(world):
            released_at_tick = tick
            assert answer(world)["state"] == "READY"
    assert released_at_tick is not None and released_at_tick >= (ready_at_tick or 0)


# =========================================================================== never released
def test_an_unmatched_recording_is_never_released(url):
    world = auto_world(url, lms=False)                        # no LMS occurrence: no current link
    one_part(world)
    world.run(rounds=12)
    assert lifecycle_events(world) == [] and set(visibility(world)) <= {"HIDDEN"}
    assert world.security.calls == 0


def released_without_captions(world, body):
    assert body["state"] == "READY" and body["release"]["status"] == "RELEASED" and body["blocking"] == []
    assert body["playback"]["available"] is True and body["recording"]["caption_status"] == "NOT_AVAILABLE"
    assert lifecycle_events(world) == ["PUBLICATION_RELEASED"] and visibility(world) == ["READY_FOR_LMS"]


def test_a_missing_transcript_never_blocks_the_verified_video(url):
    world = auto_world(url)
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2), transcript=False)
    world.run(rounds=12)
    released_without_captions(world, answer(world))
    assert all(c["content_verified"] is not True for p in answer(world)["recording"]["parts"] for c in p["captions"])


def test_a_late_transcript_attaches_captions_to_the_released_video_without_re_release(url):
    world = auto_world(url)
    start, end = START + timedelta(minutes=1), END - timedelta(minutes=2)
    recording = world.part(start, end, transcript=False)
    world.run(rounds=10)
    before = answer(world)
    released_without_captions(world, before)
    video_ids = [p["bunny"]["video_id"] for p in before["recording"]["parts"]]
    fetches, released_at = len(world.bunny.fetch_calls), before["release"]["released_at"]
    # The transcript turns up hours later (well inside the 72 h caption window).
    world.transcript(start, end, recording["contentCorrelationId"])
    world.run(rounds=12, step=timedelta(minutes=50))
    after = answer(world)
    assert after["recording"]["caption_status"] == "VERIFIED"
    assert after["recording"]["parts"][0]["captions"][0]["content_verified"] is True
    assert [p["bunny"]["video_id"] for p in after["recording"]["parts"]] == video_ids          # same video
    assert len(world.bunny.fetch_calls) == fetches and world.bunny.delete_calls == []          # no re-upload
    assert after["release"] == {**before["release"], "released_at": released_at}               # untouched
    assert lifecycle_events(world) == ["PUBLICATION_RELEASED"] and after["playback"]["available"] is True
    assert world.events().count("LECTURE_FULLY_READY") == 1                    # fully ready once captions land


def test_a_failed_transcript_never_blocks_the_video(url):
    world = auto_world(url)
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2), transcript=False)
    world.run(rounds=10)
    released_without_captions(world, answer(world))
    world.run(rounds=5, step=timedelta(hours=20))                  # past the 72 h window: caption work gives up
    released_without_captions(world, answer(world))
    captions = world.jobs("PUBLISH_CAPTION")
    assert captions and all(j[2] != "SUCCEEDED" for j in captions)


def test_incomplete_encoding_is_never_released(url):
    world = auto_world(url)
    world.bunny.polls_to_finish = 10 ** 6                     # Bunny never finishes encoding
    one_part(world)
    world.run(rounds=12)
    assert answer(world)["state"] != "READY" and lifecycle_events(world) == []
    assert world.security.calls == 0


def test_a_caption_never_content_checked_is_released_and_reported_honestly(url):
    world = World(url, **production(auto_release=True))   # no CDN read-back
    world.security = Security()
    attach(world)
    one_part(world)
    world.run(rounds=12)
    body = answer(world)
    released_without_captions(world, body)
    [caption] = body["recording"]["parts"][0]["captions"]
    assert caption["status"] == "VERIFIED" and caption["content_verified"] is False         # never claimed


def test_a_lecture_that_became_a_duplicate_is_never_released(url):
    world = delivered(url, **production(auto_release=True))
    one_part(world)
    world.run(rounds=10)
    assert answer(world)["state"] == "READY"
    _confirm_organizer(world)
    _sibling(world, with_meeting=True)                        # a second confident event: ambiguous duplicate
    world.security = Security()
    attach(world)
    world.run(rounds=4)
    assert answer(world)["state"] == "BLOCKED" and lifecycle_events(world) == []
    assert schedule(world)[1]["skipped"] == {"STATE_BLOCKED": 1} and world.security.calls == 0


def test_a_historical_lecture_is_released_like_a_new_one(url):
    """No date restriction: a lecture verified long after its date (a backfill) is released by the same task."""
    world = delivered(url, **production(auto_release=True))
    one_part(world)
    world.run(rounds=10)                                      # imported and verified; nothing releases it yet
    assert answer(world)["state"] == "READY" and lifecycle_events(world) == []
    world.clock.advance(timedelta(days=400))                  # the lecture is now more than a year old
    world.security = Security()
    attach(world)
    world.run(rounds=2)
    assert answer(world)["release"]["status"] == "RELEASED" and answer(world)["playback"]["available"] is True
    assert lifecycle_events(world) == ["PUBLICATION_RELEASED"] and len(world.bunny.fetch_calls) == 1  # no re-import


def test_a_new_part_of_a_released_lecture_is_never_released_automatically(url):
    world = delivered(url, **production(auto_release=True))
    middle = START + (END - START) / 2
    world.part(START + timedelta(minutes=1), middle + timedelta(minutes=1))
    world.run(rounds=10)
    act(world, "release")                                     # released (by an operator) with one part
    world.security = Security()
    attach(world)
    world.part(middle, END - timedelta(minutes=2))            # Teams makes the second recording available later
    world.clock.advance(timedelta(hours=25))                  # the publisher's re-check of a located lecture finds it
    world.run(rounds=14)
    # A changed recording set goes to an operator (RECORDING_SET_CHANGED): nothing new is imported, the
    # released part stays as it is, and the automatic task neither re-releases nor touches it.
    recording_jobs = [j for j in world.jobs() if j[0] == "RESOLVE_RECORDING"]
    assert any("REVIEW_REQUIRED" in j for j in recording_jobs), recording_jobs
    assert visibility(world) == ["READY_FOR_LMS"] and len(world.bunny.fetch_calls) == 1
    assert lifecycle_events(world) == ["PUBLICATION_RELEASED"] and world.security.calls == 0


def test_a_large_backlog_is_released_newest_first_a_bounded_number_per_run(url):
    world = delivered(url, **production(auto_release=True))
    one_part(world)
    world.run(rounds=10)
    first = add_second_lecture(world)                         # a later lecture, also verified
    world.run(rounds=10)
    second = world.lecture_id
    world.security = Security()
    attach(world, limit=1)
    world.run(rounds=1)
    released = [r[0] for r in world.q("SELECT lecture_id::text FROM public.bunny_publisher_events WHERE event = "
                                      "'PUBLICATION_RELEASED' AND lecture_id IN (%s, %s) ORDER BY event_id", first, second)]
    assert released == [str(second)] and schedule(world)[1]["eligible"] == 2   # both seen, one released
    world.run(rounds=1)
    released = [r[0] for r in world.q("SELECT lecture_id::text FROM public.bunny_publisher_events WHERE event = "
                                      "'PUBLICATION_RELEASED' AND lecture_id IN (%s, %s) ORDER BY event_id", first, second)]
    assert released == [str(second), str(first)]
    world.run(rounds=2)
    assert len(world.q("SELECT 1 FROM public.bunny_publisher_events WHERE event = 'PUBLICATION_RELEASED' "
                       "AND lecture_id IN (%s, %s)", first, second)) == 2             # never twice


def test_an_operator_withdrawal_is_never_undone(url):
    world = delivered(url, **production(auto_release=True))
    one_part(world)
    world.run(rounds=10)
    act(world, "release")
    act(world, "withdraw")
    world.security = Security()
    attach(world)
    world.run(rounds=6)
    assert answer(world)["state"] == "WITHDRAWN" and visibility(world) == ["WITHDRAWN"]
    assert lifecycle_events(world) == ["PUBLICATION_RELEASED", "PUBLICATION_WITHDRAWN"]


def test_the_lock_refuses_an_automatic_release_of_anything_published_before(url):
    """The race: selected while HIDDEN, withdrawn by an operator before the release takes the lock."""
    world = delivered(url, **production())
    one_part(world)
    world.run(rounds=10)
    act(world, "release")
    act(world, "withdraw")
    with psycopg.connect(url, autocommit=True) as c, pytest.raises(OperatorActionRefused) as refused:
        delivery.release(c, world.lecture_id, library_id=world.config.library_id, actor=ACTOR, reason="auto",
                         security=SECURE, require_unpublished=True)
    assert refused.value.code == "ALREADY_PUBLISHED"
    assert visibility(world) == ["WITHDRAWN"] and lifecycle_events(world)[-1] == "PUBLICATION_WITHDRAWN"
    act(world, "release")                                     # the operator path is unchanged
    assert visibility(world) == ["READY_FOR_LMS"]


# =========================================================================== security and transient failures
def test_an_insecure_library_blocks_every_automatic_release(url):
    world = auto_world(url, security=Security(INSECURE))
    one_part(world)
    world.run(rounds=10)
    assert answer(world)["state"] == "READY" and lifecycle_events(world) == []
    [(status, detail)] = world.q("SELECT last_status, last_detail FROM public.bunny_publisher_schedule WHERE task = %s",
                                 AUTO_RELEASE)
    assert status == "FAILED" and detail["code"] == "LIBRARY_NOT_SECURE"


def test_a_transient_security_read_failure_is_retried_next_run(url):
    world = auto_world(url, security=Security(RuntimeError("bunny timeout"), SECURE))
    one_part(world)
    world.run(rounds=10)
    assert lifecycle_events(world) == ["PUBLICATION_RELEASED"] and world.security.calls >= 2


def test_a_transient_database_failure_defers_then_releases(url, monkeypatch):
    world = auto_world(url)
    real_release, calls = delivery.release, {"n": 0}

    def flaky_release(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.OperationalError("synthetic connection drop")
        return real_release(*args, **kwargs)

    monkeypatch.setattr(delivery, "release", flaky_release)
    one_part(world)
    deferred_seen = False
    for _ in range(12):
        world.run(rounds=1)
        deferred_seen = deferred_seen or (schedule(world)[1] or {}).get("deferred") == 1
    assert deferred_seen and calls["n"] == 2
    assert lifecycle_events(world) == ["PUBLICATION_RELEASED"] and visibility(world) == ["READY_FOR_LMS"]


def test_concurrent_auto_and_operator_release_write_one_event(url):
    world = auto_world(url)
    one_part(world)
    world.run(rounds=10)
    # Released automatically; an operator releasing too is a no-op, and the next automatic run skips it.
    assert not act(world, "release")["changed"]
    world.run(rounds=3)
    assert lifecycle_events(world) == ["PUBLICATION_RELEASED"]
