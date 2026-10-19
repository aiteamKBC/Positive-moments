"""
Social Media management API: prompts, reanalysis, runs and cost.

Dashboard routes use the project's normal token login and permissions, the same
as every other Social Media route. The internal routes are for n8n only: no user login, the SOCIAL_MEDIA_SERVICE_TOKEN as a
bearer token, refused outright while that secret is not configured.
"""
import hmac

from rest_framework import status
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from . import social_operations as ops
from .models import PROMPT_KEYS, SocialMediaAnalysisRun
from .utils import decode_session_id


class ServiceTokenOnly(BasePermission):
    """n8n's bearer secret, compared in constant time. No secret configured = nobody."""
    message = "Invalid service token."

    def has_permission(self, request, view):
        expected = ops.service_token()
        scheme, _, sent = request.headers.get("Authorization", "").partition(" ")
        return bool(expected) and scheme == "Bearer" and hmac.compare_digest(sent.strip(), expected)


def _refused(exc: ops.Refused):
    return Response(exc.body(), status=exc.status)


def _session_id(session_key):
    try:
        return decode_session_id(session_key)
    except ValueError:
        raise ops.Refused("lecture_not_found", "This lecture does not exist.", 404) from None


def _when(value):
    return value.isoformat() if value else None


def version_payload(version, with_content=True) -> dict:
    body = {
        "prompt_key": version.prompt_key, "version": version.version,
        "content_sha256": version.content_sha256,
        "restored_from_version": version.restored_from_version,
        "created_by": version.created_by_name, "created_at": _when(version.created_at),
    }
    if with_content:
        body["content"] = version.content
    return body


def run_payload(run, with_cost=True) -> dict:
    body = {
        "run_id": str(run.run_id), "session_id": run.session_id, "trigger": run.trigger,
        "status": run.status, "requested_by": run.requested_by_name or None,
        "n8n_execution_id": run.n8n_execution_id,
        "opportunities_reported": run.opportunities_reported,
        "opportunities_verified": run.opportunities_verified,
        "error_code": run.error_code, "error_message": run.error_message,
        "created_at": _when(run.created_at), "dispatched_at": _when(run.dispatched_at),
        "started_at": _when(run.started_at), "finished_at": _when(run.finished_at),
    }
    if with_cost:
        body["cost"] = ops.cost_summary({name: getattr(run, name) for name in ops.COST_AGGREGATES})
    return body


def event_payload(event) -> dict:
    def money(value):
        return None if value is None else str(value)
    return {
        "event_key": event.event_key, "stage": event.stage, "model": event.model,
        "openai_response_id": event.openai_response_id, "usage_source": event.usage_source,
        "input_tokens": event.input_tokens, "cached_input_tokens": event.cached_input_tokens,
        "output_tokens": event.output_tokens,
        "pricing": {
            "input_per_million": money(event.input_price_per_million),
            "cached_input_per_million": money(event.cached_input_price_per_million),
            "output_per_million": money(event.output_price_per_million),
            "source": event.pricing_source,
        },
        "cost_usd": money(event.cost_usd), "cost_status": event.cost_status,
        "occurred_at": _when(event.occurred_at), "recorded_at": _when(event.recorded_at),
    }


class DashboardView(APIView):
    """The project's default login and permissions; only `Refused` is mapped here."""

    def handle_exception(self, exc):
        if isinstance(exc, ops.Refused):
            return _refused(exc)
        return super().handle_exception(exc)


class InternalView(DashboardView):
    authentication_classes = []
    permission_classes = [ServiceTokenOnly]


# --- prompts --------------------------------------------------------------------------

class PromptListView(DashboardView):
    def get(self, request):
        prompts = []
        for key in PROMPT_KEYS:
            active = ops.active_prompt(key)
            prompts.append({"prompt_key": key,
                            "active": version_payload(active, with_content=False) if active else None})
        return Response({"prompts": prompts})


class PromptDetailView(DashboardView):
    def get(self, request, prompt_key):
        active = ops.active_prompt(prompt_key)
        return Response({
            "prompt_key": prompt_key,
            "active": version_payload(active) if active else None,
            "versions": [version_payload(version, with_content=False)
                         for version in ops.prompt_versions(prompt_key)],
        })

    def put(self, request, prompt_key):
        saved = ops.replace_prompt(prompt_key, request.data, request.user)
        return Response({"active": version_payload(saved)})


class PromptVersionView(DashboardView):
    def get(self, request, prompt_key, version):
        return Response(version_payload(ops.prompt_version(prompt_key, version)))


class PromptRollbackView(DashboardView):
    def post(self, request, prompt_key):
        saved = ops.rollback_prompt(prompt_key, request.data, request.user)
        return Response({"active": version_payload(saved)})


# --- reanalysis, runs and cost ----------------------------------------------------------

class ReanalysisView(DashboardView):
    """
    Ask for one lecture to be analysed again. 202 means a run was queued, not
    that anything was analysed: the run completes only on a verified result.
    """

    def post(self, request, session_key):
        run = ops.create_run(_session_id(session_key), "on_demand", user=request.user)
        return Response({"run": run_payload(run, with_cost=False)}, status=status.HTTP_202_ACCEPTED)


class LectureRunsView(DashboardView):
    def get(self, request, session_key):
        session_id = _session_id(session_key)
        if not ops.lecture_exists(session_id):
            raise ops.Refused("lecture_not_found", "This lecture does not exist.", 404)
        return Response({"session_id": session_id,
                         "runs": [run_payload(run) for run in ops.runs_for(session_id)]})


class LectureCostsView(DashboardView):
    def get(self, request, session_key):
        session_id = _session_id(session_key)
        if not ops.lecture_exists(session_id):
            raise ops.Refused("lecture_not_found", "This lecture does not exist.", 404)
        return Response(ops.lecture_costs(session_id))


class RunDetailView(DashboardView):
    def get(self, request, run_id):
        run = (SocialMediaAnalysisRun.objects.annotate(**ops.COST_AGGREGATES)
               .filter(run_id=run_id).first())
        if run is None:
            raise ops.Refused("run_not_found", "This run does not exist.", 404)
        return Response({**run_payload(run),
                         "usage_events": [event_payload(event) for event in run.usage_events.all()]})


# --- n8n only ----------------------------------------------------------------------------

class InternalPromptView(InternalView):
    """The active System Message n8n should send, with its version for the run report."""

    def get(self, request, prompt_key):
        active = ops.active_prompt(prompt_key)
        if active is None:
            raise ops.Refused("prompt_not_configured", "No version of this prompt has been saved.", 404)
        return Response({"prompt_key": active.prompt_key, "version": active.version,
                         "content_sha256": active.content_sha256, "content": active.content})


class InternalRunCreateView(InternalView):
    """The daily scheduled workflow opens a run for each lecture it analyses."""

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        session_id = ops.text_field(data, "session_id", required=True)
        run = ops.create_run(session_id, "scheduled",
                             n8n_execution_id=ops.text_field(data, "n8n_execution_id"))
        return Response({"run": run_payload(run, with_cost=False)}, status=status.HTTP_201_CREATED)


class InternalRunStatusView(InternalView):
    def post(self, request, run_id):
        data = request.data if isinstance(request.data, dict) else {}
        run, changed = ops.report_status(run_id, data)
        return Response({"run": run_payload(run, with_cost=False), "changed": changed})


class InternalUsageView(InternalView):
    def post(self, request, run_id):
        data = request.data if isinstance(request.data, dict) else {}
        event, created = ops.record_usage(run_id, data)
        return Response({"event": event_payload(event), "created": created},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
