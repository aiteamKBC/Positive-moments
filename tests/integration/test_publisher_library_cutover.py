"""
Single-library cutover (2026-11-03: 786045 -> 796471): the SAME publisher, re-pointed at a new library by
configuration only. A lecture already imported, verified and released in the old library is imported,
encoded, captioned and auto-released again in the new one through the normal planner and workers; the old
library's rows, release event and Bunny video are left exactly as they were. A lecture before
PUBLISHER_START_DATE is never reprocessed.
"""
from __future__ import annotations

import dataclasses
from datetime import timedelta

import psycopg

from app.publisher import backfill, delivery, monitor
from app.publisher.auto_release import ACTOR
from tests.integration.test_publisher_auto_release import Security, attach, auto_world, one_part
from tests.integration.test_publisher_delivery import TokenCdn
from tests.integration.test_publisher_runtime import _isolated, url  # noqa: F401 - fixtures
from tests.unit.publisher_fakes import LIBRARY_ID as OLD, FakeBunnyLibrary

NEW = 796471


def released_in_old_library(url):
    world = auto_world(url)
    one_part(world)
    world.run(rounds=10)
    assert answer(world, OLD)["release"]["status"] == "RELEASED"
    return world


def cut_over(world, **config):
    """What the VPS change does: the same daemon code, the new library's id, key and Bunny client."""
    world.old_bunny = world.bunny
    world.config = dataclasses.replace(world.config, production_library_id=NEW, **config)
    world.bunny = FakeBunnyLibrary(world.media, NEW)
    world.daemon = world._daemon()
    world.cdn = TokenCdn(world.bunny)
    world.runtime.open_url = world.cdn
    world.security = Security({"secure": True, "library_id": NEW, "problems": []})
    attach(world)


def answer(world, library):
    with psycopg.connect(world.url) as c:
        c.read_only = True
        return delivery.occurrence_recording(c, world.occurrence.occurrence_id, library_id=library,
                                             playback_enabled=True)


def releases(world):
    return world.q("SELECT event, actor, detail ->> 'library_id' FROM public.bunny_publisher_events "
                   "WHERE lecture_id = %s AND event IN ('PUBLICATION_RELEASED', 'PUBLICATION_WITHDRAWN') "
                   "ORDER BY event_id", world.lecture_id)


def live_videos(world):
    return world.q("SELECT bunny_library_id, bunny_video_id::text, encoding_status, visibility_state "
                   "FROM public.bunny_videos WHERE lecture_id = %s AND retired_at IS NULL ORDER BY bunny_library_id",
                   world.lecture_id)


def backfill_category(world):
    with psycopg.connect(world.url) as c:
        c.read_only = True
        fact = monitor.load_fact(c, world.lecture_id, NEW)
        view = monitor.resolve(c, fact, library_id=NEW) if monitor.needs_resolution(fact) else None
        row = monitor.classify(fact, view, playback_enabled=True, library_id=NEW, now=world.clock())
    return backfill.categorize(fact, row, auto_release=True)[0]


def test_a_released_lecture_is_published_again_in_the_new_library_and_the_old_one_is_untouched(url):
    world = released_in_old_library(url)
    [(_, old_video, _, old_visibility)] = live_videos(world)

    cut_over(world)
    # Before the new library has anything: the old RELEASED status is no proof of publication in it.
    assert answer(world, NEW)["release"]["status"] == "NOT_RELEASED"
    assert answer(world, NEW)["playback"]["available"] is False
    assert backfill_category(world) in (backfill.RESUME_REQUIRED, backfill.ALREADY_PROCESSING)

    world.run(rounds=12)
    body = answer(world, NEW)
    assert body["state"] == "READY" and body["release"]["status"] == "RELEASED"
    assert body["playback"]["available"] is True and body["playback"]["library_id"] == NEW
    [new_video] = list(world.bunny.videos)
    assert [p["bunny"] for p in body["recording"]["parts"]] ==[{"library_id": NEW, "video_id": new_video}]
    assert backfill_category(world) == backfill.ALREADY_PUBLISHED

    # One import into the new library; nothing written to, or deleted from, the old one.
    assert len(world.bunny.fetch_calls) == 1 and world.bunny.caption_calls
    assert len(world.old_bunny.fetch_calls) == 1 and world.old_bunny.delete_calls == []
    assert live_videos(world) == [(OLD, old_video, "VERIFIED", old_visibility),
                                  (NEW, new_video, "VERIFIED", "READY_FOR_LMS")]
    assert [r[0] for r in releases(world)] == ["PUBLICATION_RELEASED", "PUBLICATION_RELEASED"]
    assert [r[2] for r in releases(world)] == [str(OLD), str(NEW)] and releases(world)[1][1] == ACTOR

    # Idempotent: more ticks import, caption and release nothing more.
    captions = len(world.bunny.caption_calls)
    world.run(rounds=8)
    assert len(world.bunny.fetch_calls) == 1 and len(world.bunny.caption_calls) == captions
    assert len(releases(world)) == 2


def test_a_lecture_past_the_daily_window_is_resumed_by_the_backfill_planning_call(url):
    world = released_in_old_library(url)
    cut_over(world)
    world.clock.advance(timedelta(days=10))           # the daily tick's lookback no longer reaches it
    world.run(rounds=6)
    assert world.bunny.fetch_calls == []
    assert backfill_category(world) == backfill.RESUME_REQUIRED
    # What a confirmed Historical Backfill run does for a RESUME_REQUIRED lecture (HistoricalBackfill._step).
    planned = world.planner.plan_lecture(world.lecture_id)
    assert planned["jobs_created"] == {"IMPORT_VIDEO": 1}
    for _ in range(10):
        world.planner.plan_lecture(world.lecture_id)  # each backfill tick re-plans the lecture's next step
        world.run(rounds=1)
    body = answer(world, NEW)
    assert body["release"]["status"] == "RELEASED" and body["playback"]["library_id"] == NEW
    assert len(world.bunny.fetch_calls) == 1 and world.old_bunny.delete_calls == []


def test_a_lecture_before_the_start_date_is_never_reprocessed_into_the_new_library(url):
    world = released_in_old_library(url)
    cut_over(world, start_date=(world.start + timedelta(days=1)).date())
    world.run(rounds=10)
    assert world.bunny.fetch_calls == [] and world.bunny.caption_calls == []
    assert [r[0] for r in live_videos(world)] == [OLD]
    assert [r[2] for r in releases(world)] == [str(OLD)]
    assert answer(world, NEW)["release"]["status"] == "NOT_RELEASED"
