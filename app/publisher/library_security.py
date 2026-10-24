"""
Learner-delivery security of the ONE Bunny library the publisher delivers from.

Read-only. Two Bunny reads with the account key (Get Video Library, Get Pull
Zone); nothing is ever changed on Bunny. The report carries flags and counts
only, never a key, a referrer hostname list or a URL.

What learner delivery needs (Bunny documentation, verified 2026-10-24):

  library   PlayerTokenAuthenticationEnabled = true   every embed carries a signed, expiring token
            AllowDirectPlay                  = false  no tokenless player.mediadelivery.net/play page
            BlockNoneReferrer                = true   no request without a referrer
            AllowedReferrers                 non-empty only the LMS domains may embed
            EnableMP4Fallback                = false  no whole-file MP4 URLs to share
  pull zone ZoneSecurityEnabled              = true   HLS playlists, segments, thumbnails and
                                                      caption files need a CDN token

The check gates RELEASE (app.publisher.delivery): a lecture cannot be released
to learners from a library that fails it. It does NOT stop the publisher daemon
- a library setting changed by hand must never take discovery, imports and
captions down with it.
"""
from __future__ import annotations

REQUIRED_LIBRARY = (
    ("PlayerTokenAuthenticationEnabled", True, "LIBRARY_EMBED_TOKEN_AUTHENTICATION", "embed token authentication is off"),
    ("AllowDirectPlay", False, "LIBRARY_DIRECT_PLAY", "direct play is allowed"),
    ("BlockNoneReferrer", True, "LIBRARY_BLOCK_NONE_REFERRER", "requests without a referrer are not blocked"),
    ("EnableMP4Fallback", False, "LIBRARY_MP4_FALLBACK", "MP4 fallback is enabled"),
)


def check_delivery_security(library: dict, pull_zone: dict | None) -> dict:
    """{"secure": bool, "problems": [...], "observed": {...flags only...}}."""
    problems, observed = [], {}
    for field, wanted, code, problem in REQUIRED_LIBRARY:
        value = library.get(field)
        observed[field] = value
        if value is not wanted:
            problems.append(f"{code}: {problem} ({field}={value!r}, needs {wanted!r})")
    referrers = library.get("AllowedReferrers") or []
    observed["AllowedReferrers_count"] = len(referrers)
    if not referrers:
        problems.append("LIBRARY_ALLOWED_REFERRERS: no allowed referrer domain is configured")
    observed["EnableDRM"] = library.get("EnableDRM")
    observed["KeepOriginalFiles"] = library.get("KeepOriginalFiles")
    observed["PullZoneId"] = library.get("PullZoneId")
    if pull_zone is None:
        problems.append("PULL_ZONE_UNREADABLE: the library's pull zone could not be read")
    else:
        observed["ZoneSecurityEnabled"] = pull_zone.get("ZoneSecurityEnabled")
        observed["ZoneSecurityIncludeHashRemoteIP"] = pull_zone.get("ZoneSecurityIncludeHashRemoteIP")
        if pull_zone.get("ZoneSecurityEnabled") is not True:
            problems.append("PULL_ZONE_TOKEN_AUTHENTICATION: CDN token authentication is off "
                            "(playlists, segments and caption files are served without a token)")
    return {"secure": not problems, "problems": problems, "observed": observed}


def read_delivery_security(core, library_id: int) -> dict:
    """Read the library and its pull zone (read-only) and check them."""
    library = core.get_video_library(int(library_id))
    pull_zone = None
    zone_id = library.get("PullZoneId")
    if zone_id:
        try:
            pull_zone = core.get_pull_zone(int(zone_id))
        except Exception:  # noqa: BLE001 - an unreadable zone is reported as a problem, never ignored
            pull_zone = None
    report = check_delivery_security(library, pull_zone)
    report["library_id"] = int(library_id)
    return report

