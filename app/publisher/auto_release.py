"""
Automatic release: the scheduler's AUTO_RELEASE task (off by default; PUBLISHER_AUTO_RELEASE=true).

Every discovery interval it looks for lectures that the operator workflow would release anyway, and
releases them through the SAME audited delivery.release() - same verification under the per-lecture
lock, same library security check, same PUBLICATION_RELEASED event (actor "publisher:auto-release").

A lecture is released automatically only when ALL of these hold:
  * it is dated on or after PUBLISHER_AUTO_RELEASE_FROM and has a CURRENT link to an LMS occurrence;
  * its LMS match is VERIFIED and its live state is READY (verified video + verified, content-checked
    English caption for every included part; not a duplicate, not cancelled, nothing pending review);
  * every live video is still HIDDEN and the lecture has NEVER been released or withdrawn
    (re-checked inside release()'s lock: an operator's withdrawal is never undone, and a lecture that
    gained a new part after an operator release waits for the operator, as before);
  * library 786045 passes the live learner-delivery security check (read once per run, only when
    something is eligible).

Everything else is left alone and counted. A transient failure (database, Bunny security read, a busy
lecture) releases nothing for that lecture; the next run retries. A released lecture never qualifies
again, so there are no repeated releases and no duplicate events.
"""
from __future__ import annotations

import logging
from collections import Counter

import psycopg

from app.common.errors import PlatformError
from app.publisher import delivery
from app.publisher.operations import OperatorActionRefused

log = logging.getLogger(__name__)
ACTOR = "publisher:auto-release"
MAX_PER_RUN = 25

CANDIDATES = """
SELECT v.lecture_id::text
  FROM public.bunny_videos v
  JOIN public.lecture_sessions l ON l.lecture_id = v.lecture_id
 WHERE v.bunny_library_id = %(library)s AND v.retired_at IS NULL AND l.session_date >= %(since)s
 GROUP BY v.lecture_id
HAVING bool_and(v.visibility_state = 'HIDDEN') AND bool_and(v.encoding_status = 'VERIFIED')
   AND EXISTS (SELECT 1 FROM public.bunny_lms_occurrence_links k WHERE k.lecture_id = v.lecture_id AND k.is_current)
   AND NOT EXISTS (SELECT 1 FROM public.bunny_publisher_events e
                    WHERE e.lecture_id = v.lecture_id AND e.event IN ('PUBLICATION_RELEASED', 'PUBLICATION_WITHDRAWN')
                      AND e.detail ->> 'library_id' = %(library)s::text)
 ORDER BY v.lecture_id
 LIMIT %(limit)s
"""


class AutoReleaseBlocked(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


class AutoReleaser:
    def __init__(self, *, config, connect, security, limit: int = MAX_PER_RUN):
        if not config.auto_release or config.library_id is None or config.auto_release_from is None:
            raise ValueError("automatic release is not configured")
        self.config, self.connect, self.security, self.limit = config, connect, security, limit

    def run(self) -> dict:
        library, language = int(self.config.library_id), self.config.caption_language
        skipped, refused = Counter(), Counter()
        result = {"candidates": 0, "eligible": 0, "released": 0, "unchanged": 0, "deferred": 0}
        with self.connect() as connection:
            connection.read_only = True
            ids = [r[0] for r in connection.execute(CANDIDATES, {"library": library, "limit": self.limit,
                                                                 "since": self.config.auto_release_from}).fetchall()]
            eligible = []
            for lecture_id in ids:
                why = ineligible(connection, lecture_id, library_id=library, language=language)
                if why:
                    skipped[why] += 1
                else:
                    eligible.append(lecture_id)
        result.update(candidates=len(ids), eligible=len(eligible), skipped=dict(skipped))
        if not eligible:
            return {**result, "refused": {}}
        security = self.security()                     # an exception here = task FAILED, retried next run
        if not isinstance(security, dict) or security.get("secure") is not True:
            raise AutoReleaseBlocked("LIBRARY_NOT_SECURE", "; ".join((security or {}).get("problems") or ["no check"]))
        for lecture_id in eligible:
            try:
                with self.connect() as connection:
                    connection.autocommit = True
                    out = delivery.release(
                        connection, lecture_id, library_id=library, actor=ACTOR, language=language,
                        reason="automatic release: verified READY, LMS match VERIFIED, library security check passed",
                        security=security, require_unpublished=True)
                if out["changed"]:
                    result["released"] += 1
                    log.info("publisher auto-release", extra={"lecture_id": lecture_id, "parts": out["parts_changed"]})
                else:
                    result["unchanged"] += 1
            except OperatorActionRefused as refusal:
                refused[refusal.code] += 1
            except (psycopg.Error, PlatformError) as error:
                result["deferred"] += 1
                log.warning("publisher auto-release deferred", extra={"lecture_id": lecture_id,
                                                                     "error_type": type(error).__name__})
        return {**result, "refused": dict(refused)}


def ineligible(connection, lecture_id, *, library_id: int, language: str = "en") -> str | None:
    """None when the lecture may be released automatically; otherwise the first reason it may not."""
    view = delivery.lecture_delivery(connection, lecture_id, library_id=library_id, language=language,
                                     link_current=True)
    if view is None:
        return "LECTURE_NOT_FOUND"
    if view["match"]["status"] != "VERIFIED":
        return f"MATCH_{view['match']['status']}"
    if view["state"] != delivery.READY or not view["verified_ready"]:
        return f"STATE_{view['state']}"
    if view["release"] != delivery.NOT_RELEASED or not view["parts"] \
            or any(p["visibility"] != delivery.HIDDEN for p in view["parts"]):
        return "NOT_HIDDEN"
    return None
