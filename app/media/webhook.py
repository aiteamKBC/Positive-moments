"""
The Creatomate render webhook, platform side (Django only translates HTTP).

Creatomate webhooks carry no signature, so NOTHING in the payload is trusted:

  1. the URL carries `job` and `token`; token = HMAC-SHA256(secret, job_id).
     A request without a valid token for a known job is refused before any
     database or provider call - a spoofer cannot even learn which jobs exist;
  2. the payload's render id must equal the job's stored provider_render_id;
  3. the render status is then READ FROM CREATOMATE, server-side, with the API
     key, and only that verified status changes durable state;
  4. transitions are compare-and-set, so a duplicate or late delivery is a
     harmless no-op.

The handler never uploads: a verified success moves the job to UPLOAD_PENDING
and the runner performs the transfer. The request stays fast.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timezone

from app.media.render.base import RenderProviderError
from app.media.runner import apply_render_status


def webhook_token(secret: str, job_id) -> str:
    return hmac.new(secret.encode("utf-8"), str(job_id).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def webhook_url(base_url: str, secret: str, job_id) -> str:
    base = base_url.rstrip("/")
    return (f"{base}/api/operations/media/webhooks/creatomate/"
            f"?job={job_id}&token={webhook_token(secret, job_id)}")


class WebhookRefused(Exception):
    def __init__(self, code: str, http_status: int):
        self.code = code
        self.http_status = http_status
        super().__init__(code)


def handle_creatomate_webhook(connection, *, media_repository, provider, secret: str,
                              job_id, token, payload) -> dict:
    if not secret:
        raise WebhookRefused("WEBHOOK_NOT_CONFIGURED", 503)
    if not job_id or not token or not hmac.compare_digest(
            webhook_token(secret, job_id), str(token)):
        raise WebhookRefused("WEBHOOK_TOKEN_INVALID", 403)
    try:
        uuid.UUID(str(job_id))
    except ValueError:
        raise WebhookRefused("WEBHOOK_JOB_UNKNOWN", 404) from None
    job = media_repository.job(connection, job_id, for_update=True)
    if job is None:
        raise WebhookRefused("WEBHOOK_JOB_UNKNOWN", 404)
    render_id = str((payload or {}).get("id") or "") if isinstance(payload, dict) else ""
    if not job["provider_render_id"] or render_id != str(job["provider_render_id"]):
        raise WebhookRefused("WEBHOOK_RENDER_MISMATCH", 409)
    if job["status"] != "RENDERING":
        return {"result": "ALREADY_PROCESSED", "status": job["status"]}
    try:
        verified = provider.get_status(job["provider_render_id"])
    except RenderProviderError:
        # Could not verify now: make the runner poll immediately instead.
        media_repository.transition(connection, job["job_id"], expected="RENDERING",
                                    status="RENDERING", event_stage="WEBHOOK",
                                    event_detail={"verified": False},
                                    next_poll_at=datetime.now(timezone.utc))
        return {"result": "VERIFICATION_DEFERRED", "status": "RENDERING"}
    result = apply_render_status(media_repository, connection, job, verified)
    media_repository.event(connection, job["job_id"], "WEBHOOK", "VERIFIED",
                           {"provider_status": verified.provider_status, "result": result})
    return {"result": result, "status": result}
