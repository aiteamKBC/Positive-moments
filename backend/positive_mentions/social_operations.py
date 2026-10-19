"""
Social Media management: AI System Messages, reanalysis runs and AI cost.

WHO
---
Anyone who can use the Social Media Dashboard: the project's normal token
login (DEFAULT_AUTHENTICATION_CLASSES / DEFAULT_PERMISSION_CLASSES), exactly
as the other Social Media routes. n8n reports through the internal routes with
SOCIAL_MEDIA_SERVICE_TOKEN, never with a user's token.

PROMPTS
-------
Append only. Saving adds version N+1 and must name the version it replaces
(`expected_version`, 0 when none exists yet), so two people cannot silently
overwrite each other. Rollback adds a new version copying an older one.
Nothing is seeded: until someone saves one, a prompt has no active version.

RUNS
----
  queued -> running -> completed | failed       (queued -> completed/failed too)

A request creates a `queued` run and POSTs {session_id, mode: "reanalyze"} to
the n8n webhook; n8n's acknowledgement sets `dispatched_at` and nothing else.
The run becomes `running`, `completed` or `failed` only when n8n reports it
(one active run per lecture, so n8n can address it by session_id).
`completed` is accepted only when the number of
opportunity rows n8n reports equals the rows PostgreSQL holds for the lecture
with `analyzed_at` at or after the run's creation. Otherwise the run keeps its
state and the report is refused. A queued/running run older than
ACTIVE_RUN_TIMEOUT is failed (`not_dispatched` / `timed_out`) when the next
run for that lecture is requested; a timed-out run may still report.

COST
----
Each OpenAI call is one usage event. Cost = (input - cached) x input price +
cached x cached price + output x output price, per million tokens, with prices
copied from SOCIAL_MEDIA_MODEL_PRICES when recorded. `measured` = tokens the
provider reported; `estimated` = tokens n8n estimated; `unknown` = tokens or
price missing, and then there is no cost value at all.
"""
import hashlib
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from .models import (
    ACTIVE_RUN_STATUSES,
    PROMPT_KEYS,
    USAGE_SOURCES,
    USAGE_STAGES,
    DoctorSession,
    SocialMediaAnalysisRun,
    SocialMediaOpportunity,
    SocialMediaPromptVersion,
    SocialMediaUsageEvent,
)

logger = logging.getLogger(__name__)

ACTIVE_RUN_TIMEOUT = timedelta(minutes=60)
MAX_PROMPT_CHARACTERS = 200_000
MAX_TEXT = 500
MAX_ERROR_MESSAGE = 2_000
COST_PLACES = Decimal("0.00000001")
PRICE_PLACES, PRICE_LIMIT = Decimal("0.000001"), Decimal(1_000_000)
MILLION = Decimal(1_000_000)
MIN_SERVICE_TOKEN = 32
WEBHOOK_TIMEOUT_SECONDS = 20


class Refused(Exception):
    def __init__(self, code, detail, status=409, **extra):
        super().__init__(detail)
        self.code, self.detail, self.status, self.extra = code, detail, status, extra

    def body(self):
        return {"code": self.code, "detail": self.detail, **self.extra}


def _invalid(detail, code="invalid_request"):
    return Refused(code, detail, status=400)


# --- who ----------------------------------------------------------------------------

def person_name(user) -> str:
    """Who the audit names: the person's full name, else their login name."""
    return (user.get_full_name() or "").strip() or user.get_username()


def service_token() -> str:
    """The configured n8n secret, or "" when it is missing or too short to trust."""
    token = settings.SOCIAL_MEDIA_SERVICE_TOKEN
    return token if len(token) >= MIN_SERVICE_TOKEN else ""


# --- prompts -------------------------------------------------------------------------

def _prompt_key(key):
    if key not in PROMPT_KEYS:
        raise Refused("prompt_not_found", f"Unknown prompt. Use one of: {', '.join(PROMPT_KEYS)}.", 404)
    return key


def active_prompt(key):
    return SocialMediaPromptVersion.objects.filter(prompt_key=_prompt_key(key)).order_by("-version").first()


def prompt_version(key, version):
    found = SocialMediaPromptVersion.objects.filter(prompt_key=_prompt_key(key), version=version).first()
    if found is None:
        raise Refused("version_not_found", "This prompt version does not exist.", 404)
    return found


def prompt_versions(key):
    return SocialMediaPromptVersion.objects.filter(prompt_key=_prompt_key(key)).defer("content")


def _whole_number(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _invalid(f"{name} must be a whole number of at least {minimum}.")
    return value


def _save_version(key, content, expected_version, user, restored_from=None):
    current = active_prompt(key)
    current_version = current.version if current else 0
    if expected_version != current_version:
        raise Refused("version_conflict",
                      "The prompt changed since you opened it. Reload it before saving.",
                      active_version=current_version)
    if current is not None and current.content == content:
        raise Refused("unchanged", "The text is the same as the active version.")
    try:
        with transaction.atomic():
            return SocialMediaPromptVersion.objects.create(
                prompt_key=key, version=current_version + 1, content=content,
                content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                restored_from_version=restored_from, created_by=user, created_by_name=person_name(user))
    except IntegrityError:
        # Someone else saved the same version number first.
        raise Refused("version_conflict",
                      "The prompt changed since you opened it. Reload it before saving.",
                      active_version=active_prompt(key).version) from None


def replace_prompt(key, data, user):
    _prompt_key(key)
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        raise _invalid("content must be the full, non-empty prompt text.")
    if len(content) > MAX_PROMPT_CHARACTERS:
        raise _invalid(f"content is longer than {MAX_PROMPT_CHARACTERS} characters.")
    expected = _whole_number(data.get("expected_version"), "expected_version")
    return _save_version(key, content, expected, user)


def rollback_prompt(key, data, user):
    _prompt_key(key)
    target = prompt_version(key, _whole_number(data.get("version"), "version", minimum=1))
    expected = _whole_number(data.get("expected_version"), "expected_version")
    return _save_version(key, target.content, expected, user, restored_from=target.version)


# --- runs ------------------------------------------------------------------------------

def lecture_exists(session_id) -> bool:
    return bool(session_id) and DoctorSession.objects.filter(session_id=session_id).exists()


def _expire_stale(session_id, now):
    for run in (SocialMediaAnalysisRun.objects.select_for_update()
                .filter(session_id=session_id, status__in=ACTIVE_RUN_STATUSES,
                        created_at__lt=now - ACTIVE_RUN_TIMEOUT)):
        run.status = "failed"
        run.error_code = "timed_out" if run.dispatched_at else "not_dispatched"
        run.error_message = "No result was received within the time allowed."
        run.finished_at = run.updated_at = now
        run.save(update_fields=["status", "error_code", "error_message", "finished_at", "updated_at"])


def create_run(session_id, trigger, user=None, n8n_execution_id=None):
    """A new queued run, or Refused when the lecture is missing or already being analysed."""
    if not lecture_exists(session_id):
        raise Refused("lecture_not_found", "This lecture does not exist.", 404)
    now = timezone.now()
    try:
        with transaction.atomic():
            _expire_stale(session_id, now)
            return SocialMediaAnalysisRun.objects.create(
                session_id=session_id, trigger=trigger, requested_by=user,
                requested_by_name=person_name(user) if user else "",
                n8n_execution_id=n8n_execution_id, created_at=now, updated_at=now)
    except IntegrityError:
        active = SocialMediaAnalysisRun.objects.filter(
            session_id=session_id, status__in=ACTIVE_RUN_STATUSES).first()
        raise Refused("analysis_in_progress", "This lecture is already being analysed.",
                      run_id=str(active.run_id) if active else None) from None


def is_stale(run, now=None) -> bool:
    """Still queued/running past the time allowed: no result has arrived."""
    now = now or timezone.now()
    return run.status in ACTIVE_RUN_STATUSES and run.created_at < now - ACTIVE_RUN_TIMEOUT


# --- dispatch to n8n --------------------------------------------------------------------

def _webhook():
    """The n8n webhook URL, or None when reanalysis is not configured (missing or not https)."""
    url = settings.SOCIAL_MEDIA_REANALYSIS_WEBHOOK_URL
    return url if url.startswith("https://") else None


def _post_webhook(url, payload) -> bool:
    """POST to n8n (no authentication header). True on a 2xx acknowledgement. Never logs the URL."""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        logger.warning("social media reanalysis webhook refused: HTTP %s", exc.code)
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("social media reanalysis webhook unreachable: %s", type(exc).__name__)
    return False


def request_reanalysis(session_id, user):
    """
    Queue a run and ask n8n to analyse the lecture again.

    n8n answers at once; its 2xx only means "received", so the run stays
    `queued` with `dispatched_at` set. Only n8n's own report moves it on. If
    n8n does not accept the request, the run is failed (`dispatch_failed`) so
    the lecture is free to be requested again.
    """
    webhook = _webhook()
    if webhook is None:
        raise Refused("reanalysis_not_configured", "Reanalysis is not available yet.", 503)
    run = create_run(session_id, "on_demand", user=user)
    accepted = _post_webhook(webhook, {"run_id": str(run.run_id), "session_id": session_id, "mode": "reanalyze"})
    now = timezone.now()
    queued = SocialMediaAnalysisRun.objects.filter(run_id=run.run_id, status="queued")
    if not accepted:
        queued.update(status="failed", error_code="dispatch_failed",
                      error_message="The analysis service did not accept the request.",
                      finished_at=now, updated_at=now)
        raise Refused("reanalysis_request_failed",
                      "The analysis service did not accept the request. Try again.", 502)
    queued.filter(dispatched_at__isnull=True).update(dispatched_at=now, updated_at=now)
    run.refresh_from_db()
    return run


def text_field(data, name, limit=MAX_TEXT, required=False):
    value = data.get(name)
    if value is None or value == "":
        if required:
            raise _invalid(f"{name} is required.")
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise _invalid(f"{name} must be text of at most {limit} characters.")
    return value


def _reopenable(run):
    """A run that timed out may still deliver its real outcome."""
    return run.status == "failed" and run.error_code in ("timed_out", "not_dispatched")


def report_status(run_id, data):
    """Apply n8n's report on one run. Returns (run, changed)."""
    new_status = data.get("status")
    if new_status not in ("running", "completed", "failed"):
        raise _invalid("status must be running, completed or failed.")
    execution_id = text_field(data, "n8n_execution_id")
    with transaction.atomic():
        run = SocialMediaAnalysisRun.objects.select_for_update().filter(run_id=run_id).first()
        if run is None:
            raise Refused("run_not_found", "This run does not exist.", 404)
        if execution_id and run.n8n_execution_id and execution_id != run.n8n_execution_id:
            raise Refused("execution_mismatch", "This run belongs to another n8n execution.")
        now = timezone.now()
        fields = {"n8n_execution_id": execution_id or run.n8n_execution_id, "updated_at": now}

        if new_status == "running":
            if run.status == "running":
                return run, False
            if run.status != "queued" and not _reopenable(run):
                raise Refused("run_finished", f"This run is already {run.status}.", run_status=run.status)
            fields.update(status="running", started_at=run.started_at or now,
                          dispatched_at=run.dispatched_at or now,
                          error_code=None, error_message=None, finished_at=None)

        elif new_status == "failed":
            if run.status == "failed" and not _reopenable(run):
                return run, False
            if run.status == "completed":
                raise Refused("run_finished", "This run is already completed.", run_status=run.status)
            code = text_field(data, "error_code", required=True)
            fields.update(status="failed", error_code=code,
                          error_message=text_field(data, "error_message", limit=MAX_ERROR_MESSAGE),
                          finished_at=now)

        else:
            reported = _whole_number(data.get("opportunities_reported"), "opportunities_reported")
            if run.status == "completed":
                if reported == run.opportunities_reported:
                    return run, False
                raise Refused("run_finished", "This run is already completed.", run_status=run.status)
            if run.status == "failed" and not _reopenable(run):
                raise Refused("run_finished", "This run is already failed.", run_status=run.status)
            verified = SocialMediaOpportunity.objects.filter(
                session_id=run.session_id, analyzed_at__gte=run.created_at).count()
            if verified != reported:
                raise Refused("result_not_verified",
                              "The saved opportunities do not match the reported result.",
                              opportunities_reported=reported, opportunities_found=verified)
            fields.update(status="completed", opportunities_reported=reported,
                          opportunities_verified=verified, error_code=None, error_message=None,
                          started_at=run.started_at or now, finished_at=now)

        for name, value in fields.items():
            setattr(run, name, value)
        try:
            run.save(update_fields=list(fields))
        except IntegrityError:
            # Re-opening a timed-out run while a newer one is active.
            raise Refused("analysis_in_progress", "A newer run for this lecture is active.") from None
        return run, True


# --- usage and cost ----------------------------------------------------------------------

def _price(value):
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not price.is_finite() or not 0 <= price < PRICE_LIMIT:
        return None
    # Only a price the database column holds exactly; never silently rounded.
    stored = price.quantize(PRICE_PLACES)
    return stored if stored == price else None


def model_price(model):
    """(input, cached_input, output, source) per million tokens, or None when not fully configured."""
    raw = settings.SOCIAL_MEDIA_MODEL_PRICES
    if not raw:
        return None
    try:
        entry = json.loads(raw).get(model)
    except (ValueError, AttributeError):
        logger.error("SOCIAL_MEDIA_MODEL_PRICES is not a JSON object; costs are recorded as unknown")
        return None
    if not isinstance(entry, dict):
        return None
    prices = [_price(entry.get(name)) for name in ("input", "cached_input", "output")]
    if None in prices:
        return None
    source = str(entry.get("source") or "SOCIAL_MEDIA_MODEL_PRICES")[:MAX_TEXT]
    return (*prices, source)


def calculate_cost(input_tokens, cached_tokens, output_tokens, price):
    input_price, cached_price, output_price, _ = price
    cost = ((input_tokens - cached_tokens) * input_price + cached_tokens * cached_price
            + output_tokens * output_price) / MILLION
    return cost.quantize(COST_PLACES)


def _tokens(data, name):
    value = data.get(name)
    if value is None:
        return None
    return _whole_number(value, name)


def _moment(data, name):
    value = data.get(name)
    if value in (None, ""):
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        raise _invalid(f"{name} must be an ISO 8601 date and time.") from None
    if moment.tzinfo is None:
        raise _invalid(f"{name} must include a time zone.")
    return moment


USAGE_FIELDS = ("stage", "model", "openai_response_id", "usage_source",
                "input_tokens", "cached_input_tokens", "output_tokens")


def record_usage(run_id, data):
    """Store one OpenAI call. Returns (event, created). Replays of the same event_key are no-ops."""
    values = {
        "event_key": text_field(data, "event_key", required=True),
        "stage": data.get("stage"),
        "model": text_field(data, "model", limit=100, required=True),
        "openai_response_id": text_field(data, "openai_response_id"),
        "usage_source": data.get("usage_source"),
        "input_tokens": _tokens(data, "input_tokens"),
        "cached_input_tokens": _tokens(data, "cached_input_tokens"),
        "output_tokens": _tokens(data, "output_tokens"),
        "occurred_at": _moment(data, "occurred_at"),
    }
    if values["stage"] not in USAGE_STAGES:
        raise _invalid(f"stage must be one of: {', '.join(USAGE_STAGES)}.")
    if values["usage_source"] not in USAGE_SOURCES:
        raise _invalid(f"usage_source must be one of: {', '.join(USAGE_SOURCES)}.")
    tokens = (values["input_tokens"], values["cached_input_tokens"], values["output_tokens"])
    if values["usage_source"] == "unavailable":
        if any(count is not None for count in tokens):
            raise _invalid("Token counts must be null when usage_source is unavailable.")
    else:
        if any(count is None for count in tokens):
            raise _invalid("input_tokens, cached_input_tokens and output_tokens are required "
                           "unless usage_source is unavailable.")
        if values["cached_input_tokens"] > values["input_tokens"]:
            raise _invalid("cached_input_tokens cannot exceed input_tokens.")

    run = SocialMediaAnalysisRun.objects.filter(run_id=run_id).first()
    if run is None:
        raise Refused("run_not_found", "This run does not exist.", 404)
    existing = SocialMediaUsageEvent.objects.filter(run=run, event_key=values["event_key"]).first()
    if existing is not None:
        return _replay(existing, values), False

    price = model_price(values["model"]) if values["usage_source"] != "unavailable" else None
    if price is None:
        cost, cost_status = None, "unknown"
    else:
        cost = calculate_cost(*tokens, price)
        cost_status = "measured" if values["usage_source"] == "reported" else "estimated"
    try:
        with transaction.atomic():
            event = SocialMediaUsageEvent.objects.create(
                run=run, **values, cost_usd=cost, cost_status=cost_status,
                input_price_per_million=price[0] if price else None,
                cached_input_price_per_million=price[1] if price else None,
                output_price_per_million=price[2] if price else None,
                pricing_source=price[3] if price else None)
    except IntegrityError:
        existing = SocialMediaUsageEvent.objects.get(run=run, event_key=values["event_key"])
        return _replay(existing, values), False
    return event, True


def _replay(existing, values):
    if any(getattr(existing, name) != values[name] for name in USAGE_FIELDS):
        raise Refused("event_conflict", "This event_key was already recorded with different values.")
    return existing


def _money(value):
    return None if value is None else f"{Decimal(value).quantize(COST_PLACES):f}"


COST_AGGREGATES = {
    "measured": Sum("usage_events__cost_usd", filter=Q(usage_events__cost_status="measured")),
    "estimated": Sum("usage_events__cost_usd", filter=Q(usage_events__cost_status="estimated")),
    "events": Count("usage_events"),
    "unknown": Count("usage_events", filter=Q(usage_events__cost_status="unknown")),
    "input_tokens": Sum("usage_events__input_tokens"),
    "cached_input_tokens": Sum("usage_events__cached_input_tokens"),
    "output_tokens": Sum("usage_events__output_tokens"),
}


def cost_summary(row) -> dict:
    """The cost block of a lecture or a run. Nothing unknown is counted as zero."""
    measured, estimated = row["measured"] or Decimal(0), row["estimated"] or Decimal(0)
    return {
        "currency": "USD",
        "measured_cost_usd": _money(measured),
        "estimated_cost_usd": _money(estimated),
        "known_cost_usd": _money(measured + estimated),
        "usage_events": row["events"],
        "unknown_cost_events": row["unknown"],
        # True only when every recorded call has a cost value.
        "cost_complete": row["unknown"] == 0,
        "tokens": {name: row[name] or 0 for name in
                   ("input_tokens", "cached_input_tokens", "output_tokens")},
    }


def runs_for(session_id):
    return (SocialMediaAnalysisRun.objects.filter(session_id=session_id)
            .annotate(**COST_AGGREGATES).order_by("-created_at"))


def lecture_costs(session_id) -> dict:
    runs = SocialMediaAnalysisRun.objects.filter(session_id=session_id)
    row = runs.aggregate(**COST_AGGREGATES)
    # Counted apart from the cost aggregates, which join one row per usage event.
    statuses = dict(runs.order_by().values_list("status").annotate(total=Count("run_id")))
    return {"session_id": session_id, "runs": sum(statuses.values()), "runs_by_status": statuses,
            **cost_summary(row)}
