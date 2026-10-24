"""
Learner-delivery security of the ONE Bunny library the publisher delivers from.

Read-only. Two Bunny reads with the account key (Get Video Library, Get Pull
Zone); nothing is ever changed on Bunny. The report carries flags, counts and
domain names only, never a key or a URL.

THE APPROVED POLICY (Kent decision, 2026-10-24) - each item a release blocker:

  library   PlayerTokenAuthenticationEnabled = true    every embed carries a signed, expiring token
            AllowDirectPlay                  = false   no tokenless player.mediadelivery.net/play page
            AllowedReferrers  = exactly the approved LMS domain(s)   (lms.kentbusinesscollege.org)
            BlockNoneReferrer                = true    subject to browser testing (policy switch)
            EnableMP4Fallback                = false   no whole-file MP4 to share

  pull zone CDN token authentication (ZoneSecurityEnabled):
            DEFERRED (this rollout)  reported as DEFERRED with its residual risk; NOT a blocker
            REQUIRED (future)        must be on, and the publisher signs its caption read-back

What the DEFERRED mode leaves open (verified 2026-10-24 against video 069df826…, status codes only):
anyone who knows a video GUID and the pull zone host - both visible to every learner who opens an
embed - can fetch the HLS playlists and segments, the thumbnails and, while KeepOriginalFiles is on,
the ORIGINAL uploaded file by sending an allowed Referer header; caption files are served with no
referrer check at all. The embed token protects the player page, not the files behind it.

The check gates RELEASE (app.publisher.delivery). It never stops the publisher
daemon: a library setting changed by hand must not take imports and captions down.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFERRED, REQUIRED = "DEFERRED", "REQUIRED"


@dataclass(frozen=True)
class DeliveryPolicy:
    cdn_token_auth: str = DEFERRED
    approved_referrers: tuple = ("lms.kentbusinesscollege.org",)
    require_block_none_referrer: bool = True

    @classmethod
    def from_config(cls, config) -> "DeliveryPolicy":
        return cls(cdn_token_auth=config.cdn_token_auth_policy, approved_referrers=tuple(config.approved_referrers),
                   require_block_none_referrer=config.require_block_none_referrer)

    def describe(self) -> dict:
        return {"cdn_token_authentication": self.cdn_token_auth, "approved_referrers": list(self.approved_referrers),
                "require_block_none_referrer": self.require_block_none_referrer}


def check_delivery_security(library: dict, pull_zone: dict | None, policy: DeliveryPolicy = DeliveryPolicy()) -> dict:
    """{"secure", "problems", "warnings", "observed", "cdn_token_authentication", "policy"}; secure = no problem."""
    problems, warnings, observed = [], [], {}

    def need(field, wanted, code, text):
        value = library.get(field)
        observed[field] = value
        if value is not wanted:
            problems.append(f"{code}: {text} ({field}={value!r}, needs {wanted!r})")

    need("PlayerTokenAuthenticationEnabled", True, "LIBRARY_EMBED_TOKEN_AUTHENTICATION", "embed token authentication is off")
    need("AllowDirectPlay", False, "LIBRARY_DIRECT_PLAY", "the tokenless direct play page is allowed")
    need("EnableMP4Fallback", False, "LIBRARY_MP4_FALLBACK", "MP4 fallback is enabled")
    if policy.require_block_none_referrer:
        need("BlockNoneReferrer", True, "LIBRARY_BLOCK_NONE_REFERRER", "requests without a referrer are not blocked")
    else:
        observed["BlockNoneReferrer"] = library.get("BlockNoneReferrer")
        warnings.append("BLOCK_NONE_REFERRER_NOT_REQUIRED: the policy allows requests without a referrer")

    allowed = sorted({str(d).strip().lower() for d in library.get("AllowedReferrers") or [] if str(d).strip()})
    approved = sorted({d.strip().lower() for d in policy.approved_referrers})
    observed["AllowedReferrers"] = allowed
    missing, unexpected = sorted(set(approved) - set(allowed)), sorted(set(allowed) - set(approved))
    if missing:
        problems.append(f"LIBRARY_ALLOWED_REFERRERS_MISSING: approved domain(s) not allowed: {', '.join(missing)}")
    if unexpected:
        problems.append(f"LIBRARY_ALLOWED_REFERRERS_UNEXPECTED: domain(s) allowed but not approved: "
                        f"{', '.join(unexpected)}")

    observed["EnableDRM"] = library.get("EnableDRM")
    if library.get("EnableDRM"):
        warnings.append("DRM_ENABLED: DRM is on, but the approved rollout has it off (playback behaviour differs)")
    observed["KeepOriginalFiles"] = library.get("KeepOriginalFiles")
    observed["PullZoneId"] = library.get("PullZoneId")

    zone_on = None if pull_zone is None else pull_zone.get("ZoneSecurityEnabled") is True
    observed["ZoneSecurityEnabled"] = zone_on
    if policy.cdn_token_auth == REQUIRED:
        status = "ENABLED" if zone_on else ("UNREADABLE" if zone_on is None else "MISSING")
        if zone_on is None:
            problems.append("PULL_ZONE_UNREADABLE: CDN token authentication is REQUIRED but the pull zone "
                            "could not be read")
        elif not zone_on:
            problems.append("PULL_ZONE_TOKEN_AUTHENTICATION: CDN token authentication is REQUIRED and is off")
    else:
        status = "DEFERRED"
        warnings.append(
            "CDN_TOKEN_AUTHENTICATION_DEFERRED: HLS playlists, segments and thumbnails"
            + (" and the ORIGINAL uploaded file" if library.get("KeepOriginalFiles") else "")
            + " are reachable without a token by anyone who knows a video id and sends an allowed Referer; "
              "caption files are reachable with no referrer at all")
        if zone_on:
            status = "DEFERRED_BUT_ENABLED"
            warnings.append("CDN_TOKEN_AUTHENTICATION_ENABLED_WHILE_DEFERRED: the pull zone enforces tokens but the "
                            "publisher's caption read-back is unsigned; new captions will read back 403 and cannot "
                            "be released until PUBLISHER_CDN_TOKEN_AUTH=REQUIRED with BUNNY_CDN_TOKEN_KEY")
        if zone_on is None:
            warnings.append("PULL_ZONE_UNREADABLE: CDN token authentication state unknown (not required)")
    if library.get("KeepOriginalFiles"):
        warnings.append("ORIGINAL_FILES_KEPT: the uploaded source file of every video is stored and served at "
                        "/{video_id}/original")
    return {"secure": not problems, "problems": problems, "warnings": warnings, "observed": observed,
            "cdn_token_authentication": {"policy": policy.cdn_token_auth, "status": status, "enabled": zone_on},
            "policy": policy.describe()}


def read_delivery_security(core, library_id: int, policy: DeliveryPolicy = DeliveryPolicy()) -> dict:
    """Read the library and its pull zone (read-only) and check them against the policy."""
    library = core.get_video_library(int(library_id))
    pull_zone = None
    zone_id = library.get("PullZoneId")
    if zone_id:
        try:
            pull_zone = core.get_pull_zone(int(zone_id))
        except Exception:  # noqa: BLE001 - an unreadable zone is reported, never ignored
            pull_zone = None
    report = check_delivery_security(library, pull_zone, policy)
    report["library_id"] = int(library_id)
    return report
