"""
The documented LMS contract (docs/api/publisher_lms_v1.openapi.json) is the real one: every body the
delivery API returns - computed by app.publisher.delivery against the isolated test database - conforms
to the document, with no missing and no undocumented property, in every state an LMS sees: not yet
discovered, processing, READY, released and withdrawn, one occurrence at a time and through the feed.
"""
from __future__ import annotations

from datetime import timedelta

import psycopg

from app.publisher import delivery
from tests.integration.test_publisher_delivery import END, START, act, answer, delivered
from tests.integration.test_publisher_runtime import _isolated, url  # noqa: F401 - fixtures
from tests.openapi_contract import load, problems, schema
from tests.unit.lms_fakes import occurrence, private_url
from tests.unit.publisher_fakes import LIBRARY_ID

SPEC = load()


def conforms(body, name="OccurrenceRecording"):
    assert problems(SPEC, body, schema(name)) == []
    return body


def feed(world, **paging):
    with psycopg.connect(world.url) as c:
        c.read_only = True
        return conforms(delivery.occurrence_feed(c, library_id=LIBRARY_ID, playback_enabled=True, **paging), "Feed")


def test_every_answer_the_lms_can_get_is_the_documented_one(url):
    world = delivered(url)
    world.part(START + timedelta(minutes=1), END - timedelta(minutes=2))
    states = set()
    for _ in range(12):
        world.run(rounds=1)
        body = answer(world)                      # None (404) until the publisher has seen the occurrence
        if body is not None:
            states.add(conforms(body)["state"])
            if body["state"] == "READY":
                break
    assert {"PROCESSING", "READY"} <= states
    assert body["recording"]["parts"][0]["bunny"]["library_id"] == LIBRARY_ID

    act(world, "release")
    released = conforms(answer(world))
    assert released["release"]["status"] == "RELEASED" and released["playback"]["available"] is True
    assert conforms(answer(world, enabled=False))["playback"]["reason"] == "PLAYBACK_DISABLED"

    orphan = occurrence(url=private_url(thread="19:meeting_OPENAPICONTRACT@thread.v2"),
                        start=START + timedelta(hours=3), meeting_type="week-additional")
    world.lms.occurrences.append(orphan)
    world.clock.advance(timedelta(hours=30))
    world.plan()
    assert conforms(answer(world, orphan.occurrence_id))["state"] == "NOT_DISCOVERED"

    first = feed(world, limit=1)
    assert first["has_more"] is True and first["next_cursor"]
    feed(world, limit=1, cursor=first["next_cursor"])

    act(world, "withdraw")
    withdrawn = conforms(answer(world))
    assert withdrawn["state"] == "WITHDRAWN" and withdrawn["release"]["withdrawn_at"]
    assert all(conforms(item) for item in feed(world, limit=50)["items"])
