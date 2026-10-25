"""
Lecture Publishing Monitor against the real schema: the batched FACTS query, the delivery rule's
answer, health from heartbeats, and the lecture detail - on the isolated test database.
"""
from __future__ import annotations

import json
from datetime import timedelta

import psycopg

from app.db.repositories.publisher_ops import PublisherAlertRepository
from app.publisher import monitor
from tests.integration.test_publisher_delivery import LIBRARY_ID, act, delivered, production
from tests.integration.test_publisher_runtime import END, START, World, url  # noqa: F401 - fixture


def rows_for(world, *, enabled=True, now=None):
    with psycopg.connect(world.url, prepare_threshold=None) as c:
        c.read_only = True
        facts = monitor.load_facts(c, START.date(), START.date(), LIBRARY_ID)
        facts = [f for f in facts if f["lecture_id"] == str(world.lecture_id)]
        views = {f["lecture_id"]: monitor.resolve(c, f, library_id=LIBRARY_ID)
                 for f in facts if monitor.needs_resolution(f)}
        return [monitor.classify(f, views.get(f["lecture_id"]), playback_enabled=enabled, library_id=LIBRARY_ID,
                                 now=now or world.clock()) for f in facts]


def test_a_released_video_without_a_transcript_is_playable_and_not_failed(url):
    world = delivered(url, **production())
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2), transcript=False)   # no transcript
    world.run(rounds=10)
    act(world, "release")
    [row] = rows_for(world)
    assert row["video"]["status"] == "VERIFIED" and row["bunny"]["status"] == "UPLOADED"
    assert row["recording"]["status"] == "LOCATED" and row["lms"]["status"] == "VERIFIED"
    assert row["release"]["status"] == "RELEASED" and row["playback"]["available"] is True
    assert row["transcript"]["status"] in ("WAITING", "MISSING", "NOT_STARTED")
    assert row["captions"]["status"] in ("NOT_AVAILABLE", "PENDING")
    assert row["state"] == monitor.PLAYABLE_NO_CAPTIONS and not row["needs_attention"]
    [off] = rows_for(world, enabled=False)
    assert off["state"] == monitor.RELEASED_PLAYBACK_OFF and off["playback"]["reason"] == "PLAYBACK_DISABLED"


def test_a_fully_captioned_lecture_is_playable_with_verified_captions(url):
    world = delivered(url, **production())
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))
    world.run(rounds=12)
    [before] = rows_for(world)
    assert before["state"] == monitor.READY_TO_RELEASE and before["automatic"]
    act(world, "release")
    [row] = rows_for(world)
    assert row["state"] == monitor.PLAYABLE and row["captions"]["status"] == "VERIFIED"
    assert row["transcript"]["status"] == "AVAILABLE"


def test_a_lecture_waiting_for_its_recording_is_waiting_not_failed(url):
    world = World(url)                                          # no recording at all
    world.run(rounds=4)
    [row] = rows_for(world, now=world.clock())
    assert row["state"] in (monitor.WAITING_FOR_RECORDING, monitor.RETRY_SCHEDULED, monitor.SCHEDULED)
    assert not row["needs_attention"] and row["bunny"]["status"] == "NOT_IMPORTED"
    assert row["playback"] == {"available": False, "reason": "NO_VIDEO"}


def test_health_reads_fresh_heartbeats_and_never_trusts_a_stale_one(url):
    world = delivered(url, **production())
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))
    world.run(rounds=2)                                         # the scheduler and w0 heartbeat (real now())
    with psycopg.connect(url, prepare_threshold=None) as c:
        c.read_only = True
        fresh = monitor.health(c, library_id=LIBRARY_ID, playback_enabled=True)
    assert fresh["scheduler"]["state"] == "OK" and fresh["status"] in ("HEALTHY", "DEGRADED")
    assert fresh["workers_seen"] >= 1 and fresh["tasks"]["DISCOVERY"]["last_status"] == "SUCCEEDED"
    with psycopg.connect(url, autocommit=True) as c:          # an old record: the daemon has gone away
        c.execute("UPDATE public.bunny_publisher_heartbeats SET seen_at = now() - interval '2 hours', "
                  "started_at = least(started_at, now() - interval '2 hours')")
    with psycopg.connect(url, prepare_threshold=None) as c:
        c.read_only = True
        stale = monitor.health(c, library_id=LIBRARY_ID, playback_enabled=True)
    assert stale["status"] == "STALE" and stale["scheduler"]["state"] == "STALE"
    assert all(w["state"] == "STALE" for w in stale["workers"])
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("DELETE FROM public.bunny_publisher_heartbeats")
    with psycopg.connect(url, prepare_threshold=None) as c:
        c.read_only = True
        assert monitor.health(c, library_id=LIBRARY_ID, playback_enabled=True)["status"] == "UNKNOWN"


def test_the_detail_shows_identity_parts_jobs_and_timeline_without_secrets(url):
    world = delivered(url, **production())
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2), transcript=False)
    world.run(rounds=10)
    act(world, "release")
    with psycopg.connect(url, prepare_threshold=None) as c:
        c.read_only = True
        detail = monitor.lecture_detail(c, str(world.lecture_id), library_id=LIBRARY_ID, playback_enabled=True)
        missing = monitor.lecture_detail(c, "00000000-0000-0000-0000-000000000000", library_id=LIBRARY_ID,
                                         playback_enabled=True)
    assert missing is None
    assert detail["row"]["state"] == monitor.PLAYABLE_NO_CAPTIONS
    assert detail["delivery"]["release"] == "RELEASED" and detail["delivery"]["parts"][0]["bunny"]["video_id"]
    assert detail["recording"]["discovery"]["status"] == "LOCATED" and detail["recording"]["parts"]
    assert detail["lms"]["match"]["status"] == "VERIFIED" and detail["lms"]["links"]
    assert detail["jobs"] and any(e["event"] == "PUBLICATION_RELEASED" for e in detail["timeline"])
    text = json.dumps(detail, default=str).lower()
    for secret in ("join_url", "drive_id", "item_id", "tempauth", "http://", "https://", "payload"):
        assert secret not in text, secret


def test_open_alerts_are_read_as_stored_and_resolved_ones_are_not_shown(url):
    world = World(url)
    alerts = PublisherAlertRepository()
    with psycopg.connect(url, autocommit=True) as c:
        alerts.raise_alert(c, severity="WARNING", kind="ORPHAN_BUNNY_VIDEO", dedupe_key="orphan:demo",
                           summary="Bunny video with no publisher row", detail={"bunny_video_id": "v-1", "token": None})
        alerts.raise_alert(c, severity="CRITICAL", kind="VIDEO_MISSING_IN_BUNNY", dedupe_key="missing:demo",
                           summary="part 1 video is not in Bunny", detail={"bunny_video_id": "v-2", "job_id": "j-9"},
                           lecture_id=world.lecture_id)
        alerts.raise_alert(c, severity="WARNING", kind="ORPHAN_BUNNY_VIDEO", dedupe_key="orphan:gone",
                           summary="resolved one", detail={})
        alerts.resolve_absent(c, "ORPHAN_BUNNY_VIDEO", ["orphan:demo"])
    with psycopg.connect(url, prepare_threshold=None) as c:
        c.read_only = True
        found = monitor.open_alerts(c)
    assert [a["severity"] for a in found] == ["CRITICAL", "WARNING"]           # critical first; resolved gone
    critical, warning = found
    assert critical["lecture"]["lecture_id"] == str(world.lecture_id) and critical["lecture"]["subject"]
    assert critical["job_id"] == "j-9" and critical["detail"] == {"bunny_video_id": "v-2"}
    assert warning["lecture"] is None and warning["job_id"] is None                # nothing invented
    assert warning["summary"] == "Bunny video with no publisher row" and warning["first_seen_at"]
