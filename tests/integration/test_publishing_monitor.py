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


def _alert_rows(url, dedupe_key):
    with psycopg.connect(url, prepare_threshold=None) as c:
        return c.execute("SELECT alert_id::text, status, occurrences FROM public.bunny_publisher_alerts "
                         "WHERE dedupe_key = %s ORDER BY first_seen_at", (dedupe_key,)).fetchall()


def test_a_video_deleted_from_bunny_resolves_its_orphan_alert_at_the_next_full_reconciliation(url):
    world = World(url)
    guid = world.bunny.add_video("manual test upload rec-0a1b2c3d")       # no publisher row: an orphan
    world.reconciler.run("FULL")
    world.reconciler.run("FULL")
    [(alert_id, status, seen)] = _alert_rows(url, f"orphan:{guid}")
    assert (status, seen) == ("OPEN", 2)                                     # one alert, refreshed, same id
    del world.bunny.videos[guid]                                            # deleted in the Bunny dashboard
    world.reconciler.run("LIGHT")
    assert _alert_rows(url, f"orphan:{guid}") == [(alert_id, "OPEN", 2)]   # LIGHT never scans Bunny
    world.reconciler.run("FULL")
    assert _alert_rows(url, f"orphan:{guid}") == [(alert_id, "RESOLVED", 2)]
    with psycopg.connect(url, prepare_threshold=None) as c:
        c.read_only = True
        assert alert_id not in [a["alert_id"] for a in monitor.open_alerts(c)]
        [resolved] = [a for a in monitor.resolved_alerts(c) if a["alert_id"] == alert_id]
    assert resolved["status"] == "RESOLVED" and resolved["resolved_at"]


def test_an_alert_keeps_its_id_while_open_and_a_recurrence_is_a_new_alert(url):
    """The identity the per-user read / dismiss state relies on (operations.publishing_monitor_views)."""
    World(url)
    alerts = PublisherAlertRepository()
    with psycopg.connect(url, autocommit=True) as c:
        first, created = alerts.raise_alert(c, severity="WARNING", kind="LMS_ONLY_SESSION", dedupe_key="lms_only:OCC-9",
                                            summary="LMS session has no lecture", detail={"lms_occurrence_id": "OCC-9"})
        again, created_again = alerts.raise_alert(c, severity="WARNING", kind="LMS_ONLY_SESSION",
                                                  dedupe_key="lms_only:OCC-9", summary="LMS session has no lecture",
                                                  detail={"lms_occurrence_id": "OCC-9"})
        assert (created, created_again, again["alert_id"], again["occurrences"]) == (True, False, first["alert_id"], 2)
        alerts.resolve_absent(c, "LMS_ONLY_SESSION", [])
        recurred, created = alerts.raise_alert(c, severity="WARNING", kind="LMS_ONLY_SESSION",
                                               dedupe_key="lms_only:OCC-9", summary="LMS session has no lecture",
                                               detail={"lms_occurrence_id": "OCC-9"})
    assert created and recurred["alert_id"] != first["alert_id"]
    with psycopg.connect(url, prepare_threshold=None) as c:
        c.read_only = True
        assert [a["alert_id"] for a in monitor.open_alerts(c) if a["kind"] == "LMS_ONLY_SESSION"] == \
            [str(recurred["alert_id"])]
        assert str(first["alert_id"]) in [a["alert_id"] for a in monitor.resolved_alerts(c)]


def test_an_lms_only_alert_carries_the_lms_coverage_the_publisher_recorded(url):
    World(url)
    start = START - timedelta(days=3)
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("""INSERT INTO public.bunny_lms_occurrence_coverage (lms_occurrence_id, lms_live_session_id,
                       lms_module_catalogue_id, lms_programme_name, lms_series_status, lms_meeting_type,
                       lms_occurrence_status, lms_scheduled_start, lms_scheduled_end, meeting_key_hash, rule_version,
                       scope_status, coverage_status, evidence)
                     VALUES ('OCC-77', 'LIVE-77', 'MOD-77', 'Marketing Executive Level 4', 'active', 'live-session',
                             'scheduled', %s, %s, '0123456789abcdef', 'meeting_key_start_v2', 'IN_SCOPE',
                             'LMS_SESSION_NOT_DISCOVERED', '{"hours_since_end": 70.2}')""",
                  (start, start + timedelta(hours=2)))
        PublisherAlertRepository().raise_alert(c, severity="WARNING", kind="LMS_ONLY_SESSION", dedupe_key="lms_only:OCC-77",
                                               summary="LMS live-session session has no Lecture Intelligence lecture",
                                               detail={"lms_occurrence_id": "OCC-77", "meeting_type": "live-session"})
    with psycopg.connect(url, prepare_threshold=None) as c:
        c.read_only = True
        [alert] = [a for a in monitor.open_alerts(c) if a["kind"] == "LMS_ONLY_SESSION"]
    lms = alert["lms"]
    assert (lms["occurrence_id"], lms["series_id"], lms["programme"]) == ("OCC-77", "LIVE-77",
                                                                         "Marketing Executive Level 4")
    assert lms["coverage_status"] == "LMS_SESSION_NOT_DISCOVERED" and lms["coverage_meaning"]
    assert lms["has_teams_meeting"] is True and lms["evidence"] == {"hours_since_end": 70.2}
    assert alert["lecture"] is None and alert["match"] is None                  # no lecture is invented
    assert "0123456789abcdef" not in json.dumps(alert)                          # the meeting key hash stays inside


def test_secrets_stored_in_an_alert_never_reach_the_response_and_the_record_is_untouched(url):
    World(url)
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLXBhcnQ"
    detail = {"token": "rec-0a1b2c3d", "bunny_video_id": "v-3",
              "nested": {"password": "hunter2", "headers": [{"Authorization": "Bearer abc123"}],
                         "graph": {"access_token": jwt}}}
    with psycopg.connect(url, autocommit=True) as c:
        PublisherAlertRepository().raise_alert(c, severity="WARNING", kind="ORPHAN_BUNNY_VIDEO",
                                               dedupe_key="orphan:secret", summary=f"failed with Bearer {jwt}",
                                               detail=detail)
    with psycopg.connect(url, prepare_threshold=None) as c:
        c.read_only = True
        [alert] = [a for a in monitor.open_alerts(c) if a["kind"] == "ORPHAN_BUNNY_VIDEO"]
        [(stored_summary, stored_detail)] = c.execute(
            "SELECT summary, detail FROM public.bunny_publisher_alerts WHERE dedupe_key = 'orphan:secret'").fetchall()
    text = json.dumps(alert)
    for secret in ("hunter2", "abc123", jwt):
        assert secret not in text, secret
    assert alert["detail"]["token"] == "rec-0a1b2c3d" and alert["detail"]["bunny_video_id"] == "v-3"
    assert stored_detail == detail and jwt in stored_summary           # the database record is not changed
