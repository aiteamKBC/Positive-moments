"""
LMS backend example: from "learner opens a recording" to a short-lived Bunny embed URL.

Placeholders only. Every secret comes from the LMS backend's own secret store / environment:

    PUBLISHER_API_BASE        e.g. https://<positive-moments-host>/api/publisher/v1
    PUBLISHER_SERVICE_TOKEN   the service token Kent issued to the LMS backend (never sent to a browser)
    BUNNY_EMBED_TOKEN_KEY     library 786045 > Security > "Token authentication key" (NOT the library API key)

Run order inside the LMS (server side only):
    1. The LMS checks the learner is enrolled for this occurrence  <- the LMS's own data, not this example
    2. Ask the publisher for the occurrence's recording
    3. Refuse unless playback.available is true
    4. Sign one embed URL per part, valid for a few minutes, and return them to the learner's page

Standard library only (Python 3.9+).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

EMBED_HOST = "https://player.mediadelivery.net"
TOKEN_TTL_SECONDS = 600            # 5-15 minutes: long enough to start playing, short enough to be useless if shared


class NotPlayable(Exception):
    """Show the learner "Recording not available yet" - never a Teams link or a Bunny URL."""


def fetch_recording(occurrence_id: str) -> dict:
    base = os.environ["PUBLISHER_API_BASE"].rstrip("/")
    request = urllib.request.Request(
        f"{base}/occurrences/{urllib.parse.quote(occurrence_id, safe='')}/recording",
        headers={"Authorization": f"Bearer {os.environ['PUBLISHER_SERVICE_TOKEN']}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise NotPlayable("OCCURRENCE_NOT_FOUND") from None
        raise                                           # 401/429/503: log, retry with backoff, fail closed


def embed_token(token_key: str, video_id: str, expires: int) -> str:
    # Bunny: token = SHA256_HEX(token_security_key + video_id + expiration); expires in UNIX seconds.
    return hashlib.sha256(f"{token_key}{video_id}{expires}".encode("utf-8")).hexdigest()


def playable_parts(occurrence_id: str, *, learner_is_enrolled: bool) -> list[dict]:
    if not learner_is_enrolled:                         # step 1 is the LMS's responsibility
        raise NotPlayable("NOT_ENROLLED")
    answer = fetch_recording(occurrence_id)
    if not answer["playback"]["available"]:             # the ONLY switch that allows playback
        raise NotPlayable(answer["playback"]["reason"] or answer["state"])
    key = os.environ["BUNNY_EMBED_TOKEN_KEY"]
    expires = int(time.time()) + TOKEN_TTL_SECONDS
    parts = []
    for part in sorted(answer["recording"]["parts"], key=lambda p: p["sequence"]):
        library, video = part["bunny"]["library_id"], part["bunny"]["video_id"]
        token = embed_token(key, video, expires)
        parts.append({
            "sequence": part["sequence"],
            "of": answer["recording"]["published_part_count"],
            "duration_seconds": part["duration_seconds"],
            "embed_url": f"{EMBED_HOST}/embed/{library}/{video}?token={token}&expires={expires}",
            # Keep viewing analytics on the LMS's own artifact: live_session_artifacts.graph_artifact_id
            "teams_recording_id": part["teams_recording_id"],
        })
    return parts


if __name__ == "__main__":
    import sys
    print(json.dumps(playable_parts(sys.argv[1], learner_is_enrolled=True), indent=2))
