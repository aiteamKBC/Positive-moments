"""
Production video delivery (2026-10-24): the LMS read model, RELEASE / WITHDRAW, and library 786045
adopted as the permanent production library - end to end against the isolated test database.

Reuses the P0-D runtime world (real planner, queue, workers; fake Graph, LMS, Bunny). A fake CDN
serves the caption files the fake Bunny holds and, when a CDN token key is configured, refuses any
request whose HS256 token does not verify - the way a token-authenticated pull zone does.
"""
from __future__ import annotations

import io
import json
import re
import threading
import urllib.error
import urllib.parse
from datetime import datetime, timedelta

import psycopg
import pytest

from app.bunny import tokens
from app.publisher import delivery, operations
from app.publisher.config import PRODUCTION_WRITE, PublisherConfig
from app.publisher.operations import OperatorActionRefused
from tests.integration.test_publisher_runtime import (  # noqa: F401 - fixtures
    END, START, World, _isolated, url,
)
from tests.unit.lms_fakes import occurrence, private_url
from tests.unit.publisher_fakes import LIBRARY_ID

CDN_HOST = "cdn.delivery-test.invalid"
CDN_KEY = "cdn-token-key-for-tests-only"
SECURE = {"secure": True, "library_id": LIBRARY_ID, "problems": []}
INSECURE = {"secure": False, "library_id": LIBRARY_ID, "problems": ["LIBRARY_EMBED_TOKEN_AUTHENTICATION: off"]}
FORBIDDEN = re.compile(r"(https?://|tempauth|[?&]sig=|@thread|MTG-|evt-|drive|/dl/|\.mp4|join_url|meeting_id)",
                       re.IGNORECASE)


class TokenCdn:
    """open_url stand-in: serves fake Bunny caption files; with a key, demands a valid HS256 CDN token."""

    def __init__(self, bunny, key=None):
        self.bunny, self.key, self.urls = bunny, key, []

    def __call__(self, request, timeout):
        url = request.full_url
        self.urls.append(url)
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        if self.key:
            params = {k: v for k, v in query.items() if k not in ("token", "expires")}
            expected = tokens.cdn_token(self.key, parts.path, int(query.get("expires", "0")), params=params)
            if query.get("token") != expected:
                raise urllib.error.HTTPError(url, 403, "token", {}, io.BytesIO(b""))
        text = self.bunny.caption_text(parts.path.split("/")[1])

        class _Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        return _Response(text.encode("utf-8"))


def production(*, signed=False, **extra):
    """PRODUCTION_WRITE on the SAME library. Approved rollout: CDN token authentication DEFERRED (unsigned
    read-back). signed=True is the future REQUIRED mode, with the pull zone key."""
    base = dict(mode=PRODUCTION_WRITE, production_library_id=LIBRARY_ID, production_api_key="k",
                production_account_api_key="a", production_write_enabled=True, production_cdn_hostname=CDN_HOST)
    if signed:
        base.update(cdn_token_auth_policy="REQUIRED", cdn_token_key=CDN_KEY)
    return {**base, **extra}


def add_second_lecture(world):
    """A new lecture (own meeting, recording and transcript) after the mode switch; returns the first id."""
    first = world.lecture_id
    [(first_meeting,)] = world.q("SELECT meeting_id FROM public.lecture_sessions WHERE lecture_id = %s", first)
    for recording in world.rgraph.recordings:
        world.rgraph.scoped[recording["id"]] = first_meeting
    second_start = END + timedelta(minutes=30)
    world.lecture_id = world.lecture(start=second_start, end=second_start + timedelta(hours=1))
    world.start, world.end = second_start, second_start + timedelta(hours=1)
    [(second_meeting,)] = world.q("SELECT meeting_id FROM public.lecture_sessions WHERE lecture_id = %s",
                                  world.lecture_id)
    world.add_occurrence()
    correlation = f"corr-after-switch-{second_meeting[-6:]}"
    recording = world.rgraph.recording(second_start + timedelta(minutes=1), world.end - timedelta(minutes=2),
                                       correlation=correlation, meeting_id=second_meeting)
    world.rgraph.recorded_file(recording, lead=35)
    world.transcript(second_start + timedelta(minutes=1), world.end - timedelta(minutes=2), correlation)
    return first


def delivered(url, *, cdn_key=None, **config):
    """A world whose caption read-back goes through the (token-checking) fake CDN."""
    config.setdefault("staging_cdn_hostname", CDN_HOST)
    world = World(url, **config)
    world.cdn = TokenCdn(world.bunny, cdn_key)
    world.runtime.open_url = world.cdn
    return world


def answer(world, occurrence_id=None, *, enabled=True):
    with psycopg.connect(world.url) as c:
        c.read_only = True
        return delivery.occurrence_recording(c, occurrence_id or world.occurrence.occurrence_id,
                                             library_id=LIBRARY_ID, playback_enabled=enabled)


def act(world, action, *, security=SECURE, lecture_id=None, reason="checked by the delivery test"):
    with psycopg.connect(world.url, autocommit=True) as c:
        if action == "release":
            return delivery.release(c, lecture_id or world.lecture_id, library_id=LIBRARY_ID, actor="ops-test",
                                    reason=reason, security=security)
        return delivery.withdraw(c, lecture_id or world.lecture_id, library_id=LIBRARY_ID, actor="ops-test",
                                 reason=reason)


def lifecycle_events(world):
    return [r[0] for r in world.q("SELECT event FROM public.bunny_publisher_events WHERE lecture_id = %s AND event "
                                  "IN ('PUBLICATION_RELEASED', 'PUBLICATION_WITHDRAWN') ORDER BY event_id",
                                  world.lecture_id)]


def ready_world(url, **kwargs):
    world = delivered(url, **kwargs)
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))
    world.run(rounds=10)
    assert world.ready()
    return world


# =========================================================================== mapping and live readiness
def test_occurrence_maps_to_its_lecture_parts_and_verified_video(url):
    world = ready_world(url)
    body = answer(world)
    [(record_id, video_id, graph_id)] = world.q(
        "SELECT v.bunny_video_record_id, v.bunny_video_id, s.graph_recording_id FROM public.bunny_videos v "
        "JOIN public.bunny_recording_sources s ON s.lecture_id = v.lecture_id "
        "AND s.recording_part_index = v.recording_part_index WHERE v.lecture_id = %s", world.lecture_id)
    assert body["state"] == "READY" and body["blocking"] == [] and body["link_role"] == "PRIMARY"
    assert body["lms_live_session_id"] == world.occurrence.live_session_id
    assert body["lecture"]["lecture_id"] == world.lecture_id and body["lecture"]["lms_match"]["status"] == "VERIFIED"
    [part] = body["recording"]["parts"]
    assert (part["sequence"], part["source_part_index"], part["encoding"]) == (1, 1, "VERIFIED")
    assert part["bunny"] == {"library_id": LIBRARY_ID, "video_id": str(video_id)}
    assert part["teams_recording_id"] == graph_id
    assert part["captions"] == [{"language": "en", "label": "English", "status": "VERIFIED", "version": part[
        "captions"][0]["version"], "content_verified": True}]
    assert body["release"]["status"] == "NOT_RELEASED"
    assert body["playback"] == {"available": False, "reason": "NOT_RELEASED", "library_id": LIBRARY_ID,
                                "player": "BUNNY_STREAM_EMBED", "token_authentication_required": True}
    text = json.dumps(body)
    assert not FORBIDDEN.search(text), FORBIDDEN.search(text)
    assert "_record_id" not in text and str(record_id) not in text


def test_readiness_is_computed_live_not_from_the_historical_event(url):
    world = ready_world(url)
    act(world, "release")
    assert answer(world)["playback"]["available"] is True
    assert answer(world)["recording"]["caption_status"] == "VERIFIED"
    # The current caption stops being verified: captions are optional, so playback continues and the
    # caption is reported as not available (never claimed verified).
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("UPDATE public.bunny_caption_publications SET publication_status = 'REVIEW_REQUIRED', "
                  "last_error_code = 'TEST' WHERE lecture_id = %s", (world.lecture_id,))
    body = answer(world)
    assert body["state"] == "READY" and body["playback"]["available"] is True and body["blocking"] == []
    assert body["recording"]["caption_status"] == "NOT_AVAILABLE"
    # The VIDEO can never stop being verified while visible: the schema refuses it outright.
    with psycopg.connect(url, autocommit=True) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute("UPDATE public.bunny_videos SET encoding_status = 'REVIEW_REQUIRED' WHERE lecture_id = %s",
                  (world.lecture_id,))
    # A cancelled lecture is not applicable, whatever was released.
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("UPDATE public.lecture_sessions SET is_cancelled = true WHERE lecture_id = %s", (world.lecture_id,))
    assert answer(world)["state"] == "NOT_APPLICABLE"


def test_a_caption_never_content_checked_is_reported_and_does_not_block_the_video(url):
    world = World(url)                                           # no CDN read-back: content check NOT_ATTEMPTED
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))
    world.run(rounds=10)
    body = answer(world)
    assert world.ready() and body["state"] == "READY" and body["blocking"] == []
    [caption] = body["recording"]["parts"][0]["captions"]
    assert caption["status"] == "VERIFIED" and caption["content_verified"] is False      # reported as it is
    assert body["recording"]["caption_status"] == "NOT_AVAILABLE"
    assert act(world, "release")["changed"] and lifecycle_events(world) == ["PUBLICATION_RELEASED"]
    assert answer(world)["playback"]["available"] is True


# =========================================================================== multipart and fragments
def test_multi_part_order_and_an_excluded_fragment(url):
    world = delivered(url)
    world.part(START + timedelta(minutes=1), START + timedelta(minutes=3), transcript=False)      # false start
    middle = START + timedelta(minutes=60)
    world.part(START + timedelta(minutes=5), middle + timedelta(minutes=1), call_id="call-1")
    world.part(middle, END - timedelta(minutes=2), call_id="call-1")
    world.run(rounds=12)
    pending = answer(world)
    assert pending["state"] == "REVIEW_REQUIRED" and "PART_1_FRAGMENT_NEEDS_DECISION" in pending["blocking"]
    with psycopg.connect(url, autocommit=True) as c:
        operations.decide_part(c, world.lecture_id, 1, decision="EXCLUDE", actor="ops-test", reason="false start")
    world.run(rounds=4)
    body = answer(world)
    assert body["state"] == "READY"
    assert [(p["sequence"], p["source_part_index"]) for p in body["recording"]["parts"]] == [(1, 2), (2, 3)]
    assert body["recording"]["excluded_parts"] == [{"source_part_index": 1, "reason": "OPERATOR_EXCLUDED"}]
    assert body["recording"]["teams_part_count"] == 3 and body["recording"]["published_part_count"] == 2
    assert len({p["bunny"]["video_id"] for p in body["recording"]["parts"]}) == 2
    result = act(world, "release")
    assert result["parts_changed"] == [2, 3] and answer(world)["release"]["status"] == "RELEASED"


# =========================================================================== release and withdraw
def test_release_and_withdraw_are_audited_idempotent_and_immediate(url):
    world = ready_world(url)
    with pytest.raises(OperatorActionRefused) as insecure:
        act(world, "release", security=INSECURE)
    assert insecure.value.code == "LIBRARY_NOT_SECURE"
    for bad in ({"actor": "", "reason": "x"}, {"actor": "ops", "reason": " "}, {"actor": "ops", "reason": "see https://x"}):
        with pytest.raises(OperatorActionRefused), psycopg.connect(url, autocommit=True) as c:
            delivery.release(c, world.lecture_id, library_id=LIBRARY_ID, security=SECURE, **bad)
    first, again = act(world, "release"), act(world, "release")
    assert first["changed"] and not again["changed"] and lifecycle_events(world) == ["PUBLICATION_RELEASED"]
    released = answer(world)
    assert released["state"] == "READY" and released["release"]["status"] == "RELEASED"
    assert released["playback"]["available"] is True and released["release"]["released_at"]
    assert answer(world, enabled=False)["playback"] == {**released["playback"], "available": False,
                                                        "reason": "PLAYBACK_DISABLED"}
    act(world, "withdraw")
    withdrawn = answer(world)
    assert withdrawn["state"] == "WITHDRAWN" and withdrawn["playback"]["available"] is False
    assert not act(world, "withdraw")["changed"]
    assert act(world, "release")["changed"] and answer(world)["playback"]["available"] is True
    assert lifecycle_events(world) == ["PUBLICATION_RELEASED", "PUBLICATION_WITHDRAWN", "PUBLICATION_RELEASED"]
    [(actor, detail)] = world.q("SELECT actor, detail FROM public.bunny_publisher_events WHERE lecture_id = %s "
                                "AND event = 'PUBLICATION_WITHDRAWN'", world.lecture_id)
    assert actor == "ops-test" and detail["reason"] == "checked by the delivery test"
    assert detail["library_id"] == str(LIBRARY_ID) and detail["parts"] == [1]


def test_an_unverified_lecture_cannot_be_released(url):
    world = delivered(url)
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))
    with pytest.raises(OperatorActionRefused) as refused:
        act(world, "release")
    assert refused.value.code == "NOT_VERIFIED"
    assert world.q("SELECT count(*) FROM public.bunny_videos WHERE lecture_id = %s AND visibility_state <> 'HIDDEN'",
                   world.lecture_id) == [(0,)]


def test_concurrent_releases_and_withdrawals_serialise(url):
    world = ready_world(url)
    results, errors, gate = [], [], threading.Barrier(8)

    def run(action):
        gate.wait()
        try:
            results.append(act(world, action))
        except OperatorActionRefused as exc:
            errors.append(exc.code)

    threads = [threading.Thread(target=run, args=("release",)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and sum(r["changed"] for r in results) == 1
    assert lifecycle_events(world) == ["PUBLICATION_RELEASED"]
    gate = threading.Barrier(6)
    threads = [threading.Thread(target=run, args=(a,)) for a in ("withdraw", "release") * 3]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = lifecycle_events(world)
    final = answer(world)["release"]["status"]
    assert errors == [] and all(a != b for a, b in zip(events, events[1:]))           # strictly alternating
    assert final == ("RELEASED" if events[-1] == "PUBLICATION_RELEASED" else "WITHDRAWN")


# =========================================================================== production mode adoption
@pytest.mark.parametrize("signed", [False, True], ids=["cdn-deferred-approved", "cdn-required-future"])
def test_production_mode_adopts_existing_videos_and_still_processes_new_lectures(url, signed):
    world = ready_world(url)                                    # published while STAGING_WRITE
    before = world.q("SELECT bunny_video_record_id, bunny_video_id, encoding_status, visibility_state "
                     "FROM public.bunny_videos ORDER BY 1")
    captions = world.q("SELECT publication_id, publication_status FROM public.bunny_caption_publications ORDER BY 1")
    fetches = len(world.bunny.fetch_calls)
    act(world, "release")
    released = world.q("SELECT bunny_video_record_id, visibility_state FROM public.bunny_videos ORDER BY 1")

    # The operator switches the SAME library to PRODUCTION_WRITE (no re-import, no id change).
    world.config = PublisherConfig(**{**world.config.__dict__, **production(signed=signed)})
    config = world.config.validate()
    assert config.library_id == LIBRARY_ID and config.describe()["adopts_staging_library"]
    assert config.describe()["cdn_readback_signed"] is signed
    world.daemon = world._daemon()
    world.cdn = TokenCdn(world.bunny, CDN_KEY if signed else None)
    world.runtime.open_url = world.cdn
    world.run(rounds=6)
    assert len(world.bunny.fetch_calls) == fetches and world.bunny.delete_calls == []
    assert world.q("SELECT bunny_video_record_id, bunny_video_id, encoding_status, 'HIDDEN' FROM public.bunny_videos "
                   "ORDER BY 1") == [(r[0], r[1], r[2], "HIDDEN") for r in before]
    assert world.q("SELECT bunny_video_record_id, visibility_state FROM public.bunny_videos ORDER BY 1") == released
    assert world.q("SELECT publication_id, publication_status FROM public.bunny_caption_publications "
                   "ORDER BY 1") == captions
    assert world.events().count("LECTURE_FULLY_READY") == 1
    assert answer(world)["playback"]["available"] is True

    # A NEW lecture after the switch goes all the way; its caption is read back from the production CDN host,
    # unsigned while CDN token authentication is DEFERRED, signed in the future REQUIRED mode.
    first = add_second_lecture(world)
    world.run(rounds=16)
    assert world.ready() and world.lecture_id != first
    urls = [u for u in world.cdn.urls if CDN_HOST in u]
    assert urls and all(("token=HS256-" in u and "expires=" in u) is signed for u in urls)
    body = answer(world)
    assert body["state"] == "READY" and body["recording"]["parts"][0]["captions"][0]["content_verified"]
    assert body["release"]["status"] == "NOT_RELEASED"                          # never public on its own
    with psycopg.connect(url, autocommit=True) as c:
        assert c.execute("SELECT count(*) FROM public.bunny_videos").fetchone()[0] == 2


def test_a_wrong_cdn_key_never_counts_as_a_content_check(url):
    world = delivered(url, cdn_key=CDN_KEY, **production(signed=True, cdn_token_key="not-the-pull-zone-key"))
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))
    world.run(rounds=10)
    body = answer(world)
    assert world.ready() and body["blocking"] == [] and body["recording"]["caption_status"] == "NOT_AVAILABLE"
    assert body["recording"]["parts"][0]["captions"][0]["content_verified"] is False
    [(check,)] = world.q("SELECT readback ->> 'content_check' FROM public.bunny_caption_publications "
                         "WHERE lecture_id = %s", world.lecture_id)
    assert check == "CDN_UNAVAILABLE"


def test_cdn_protection_turned_on_while_policy_is_deferred_leaves_only_the_caption_unverified(url):
    # Someone enables CDN token authentication on the pull zone without switching the policy: the unsigned
    # read-back gets 403 and the caption is registered but not content-checked. Captions are optional, so the
    # verified video stays READY and releasable; the caption is reported as not available, never as verified.
    world = delivered(url, cdn_key=CDN_KEY, **production())
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))
    world.run(rounds=10)
    body = answer(world)
    assert world.ready() and body["state"] == "READY" and body["blocking"] == []
    assert body["recording"]["caption_status"] == "NOT_AVAILABLE"
    assert act(world, "release")["changed"] and answer(world)["playback"]["available"] is True


# =========================================================================== unknown, LMS-only and the feed
def test_unknown_and_lms_only_occurrences(url):
    world = ready_world(url)
    assert answer(world, "OCC-NEVER-SEEN") is None
    with pytest.raises(OperatorActionRefused):
        answer(world, "OCC/../etc")
    orphan = occurrence(url=private_url(thread="19:meeting_DELIVERYLMSONLY@thread.v2"),
                        start=START + timedelta(hours=3), meeting_type="week-additional")
    world.lms.occurrences.append(orphan)
    world.clock.advance(timedelta(hours=30))
    world.plan()
    body = answer(world, orphan.occurrence_id)
    assert body["state"] == "NOT_DISCOVERED" and body["lecture"] is None and body["recording"] is None
    assert body["playback"]["available"] is False


def test_feed_pages_by_cursor_and_returns_changes_after_changed_since(url):
    world = ready_world(url)
    orphan = occurrence(url=private_url(thread="19:meeting_DELIVERYFEED@thread.v2"),
                        start=START + timedelta(hours=3), meeting_type="week-additional")
    world.lms.occurrences.append(orphan)
    world.clock.advance(timedelta(hours=30))
    world.plan()
    with psycopg.connect(url) as c:
        first = delivery.occurrence_feed(c, library_id=LIBRARY_ID, playback_enabled=True, limit=1)
        second = delivery.occurrence_feed(c, library_id=LIBRARY_ID, playback_enabled=True, limit=1,
                                          cursor=first["next_cursor"])
        ids = [first["items"][0]["lms_occurrence_id"]] + [i["lms_occurrence_id"] for i in second["items"]]
        assert first["has_more"] and set(ids) >= {world.occurrence.occurrence_id, orphan.occurrence_id}
        assert len(ids) == len(set(ids))
        c.rollback()
    act(world, "release")
    with psycopg.connect(url) as c:
        # changed_since is exclusive and lines up with each answer's updated_at (the runtime world stamps
        # some rows with its injected 2030 clock, so the boundary is taken from the answer itself).
        mine = answer(world)
        at = datetime.fromisoformat(mine["updated_at"].replace("Z", "+00:00"))
        before = delivery.occurrence_feed(c, library_id=LIBRARY_ID, playback_enabled=True,
                                          changed_since=at - timedelta(microseconds=1))
        assert world.occurrence.occurrence_id in [i["lms_occurrence_id"] for i in before["items"]]
        released = next(i for i in before["items"] if i["lms_occurrence_id"] == world.occurrence.occurrence_id)
        assert released["release"]["status"] == "RELEASED" and released["updated_at"] == mine["updated_at"]
        after = delivery.occurrence_feed(c, library_id=LIBRARY_ID, playback_enabled=True, changed_since=at)
        assert world.occurrence.occurrence_id not in [i["lms_occurrence_id"] for i in after["items"]]
        with pytest.raises(OperatorActionRefused):
            delivery.occurrence_feed(c, library_id=LIBRARY_ID, playback_enabled=True, cursor="not-a-cursor")
