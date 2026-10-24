"""
Bunny signed-URL helpers, written from Bunny's documentation (verified 2026-10-24).

EMBED VIEW TOKEN (player token authentication, library Security page)
    token   = SHA256_HEX(token_security_key + video_id + expires)
    expires = UNIX timestamp in SECONDS (milliseconds are not supported)
    URL     = https://player.mediadelivery.net/embed/{library_id}/{video_id}?token={token}&expires={expires}
    https://bunny.net/docs/stream-embed-token-authentication

    The LMS backend signs embeds, never the publisher: it is the only party that
    knows the learner is entitled. `signed_embed_url` exists for the LMS
    integration example and the tests; the publisher never calls it with a
    production key.

CDN TOKEN (pull zone "advanced" token authentication)
    token = "HS256-" + flags + Base64URL(HMAC-SHA256(security_key,
                                  signature_path + expires + user_ip + signing_data))
    flags = "1-" when an IP was signed, else ""; signing_data = the other query
    parameters sorted by name as key=value joined by "&" (token and expires
    excluded); Base64URL = "+"->"-", "/"->"_", "=" padding removed.
    https://bunny.net/docs/cdn/security/token-authentication/advanced

    The publisher uses it for one thing only: reading back its own caption file
    from the CDN once CDN token authentication is on. Bunny states that "CDN
    token authentication uses the same key" as the embed token.

Nothing here logs, stores or returns a key.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse

EMBED_HOST = "https://player.mediadelivery.net"
MAX_EMBED_TTL_SECONDS = 6 * 3600


def embed_token(token_key: str, video_id: str, expires: int) -> str:
    """SHA256 hex of key + video id + expiry (seconds), exactly as Bunny documents it."""
    if not token_key or not video_id:
        raise ValueError("token key and video id are required")
    if not isinstance(expires, int) or expires <= 0 or expires > 10 ** 11:
        raise ValueError("expires must be a UNIX timestamp in seconds")
    return hashlib.sha256(f"{token_key}{video_id}{expires}".encode("utf-8")).hexdigest()


def signed_embed_url(library_id: int, video_id: str, token_key: str, *, ttl_seconds: int = 600,
                     now: float | None = None) -> str:
    """A short-lived player URL. The LMS backend calls this AFTER it has checked enrolment."""
    if not 1 <= ttl_seconds <= MAX_EMBED_TTL_SECONDS:
        raise ValueError(f"ttl_seconds must be 1..{MAX_EMBED_TTL_SECONDS}")
    expires = int(now if now is not None else time.time()) + int(ttl_seconds)
    token = embed_token(token_key, str(video_id), expires)
    return f"{EMBED_HOST}/embed/{int(library_id)}/{video_id}?token={token}&expires={expires}"


def _b64url(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").replace("+", "-").replace("/", "_").replace("=", "")


def cdn_token(security_key: str, signature_path: str, expires: int, *, params: dict | None = None,
              user_ip: str = "") -> str:
    """Bunny's advanced (HMAC-SHA256) CDN token for one path or path prefix."""
    if not security_key or not signature_path.startswith("/"):
        raise ValueError("security key and an absolute signature path are required")
    signing_data = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items())
                            if k not in ("token", "expires"))
    message = f"{signature_path}{int(expires)}{user_ip}{signing_data}"
    digest = hmac.new(security_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return "HS256-" + ("1-" if user_ip else "") + _b64url(digest)


def signed_cdn_url(hostname: str, path: str, security_key: str, *, params: dict | None = None,
                   ttl_seconds: int = 300, now: float | None = None) -> str:
    """https://{hostname}{path}?{params}&token=...&expires=... for a single file (query-string token)."""
    expires = int(now if now is not None else time.time()) + int(ttl_seconds)
    params = dict(params or {})
    token = cdn_token(security_key, path, expires, params=params)
    query = urllib.parse.urlencode({**dict(sorted(params.items())), "token": token, "expires": expires})
    return f"https://{hostname}{path}?{query}"
