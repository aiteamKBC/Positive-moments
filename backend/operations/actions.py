"""
Phase 5B: the guarded action endpoints.

THE SHAPE, AND WHY
------------------
Three named routes, one per action. The action is in the URL, never in the
body - so there is no request this API can receive that names an action it was
not built to perform. A `POST /actions/ {"action": "..."}` endpoint would be
one validation bug away from force-reprocessing a settled lecture.

Every mutating route has a GET twin that returns the plan and changes nothing.
The UI shows the plan, the operator confirms, and the POST re-derives the plan
server-side before acting. A stale browser tab therefore cannot execute a
decision that stopped being true while it sat open.

`FORCE_REPROCESS` has no route here and must never get one.
"""
from datetime import date as _date

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from app.common.errors import PlatformError
from app.orchestration.actions import ActionRefused
from app.orchestration.factory import build_guarded_actions
from app.orchestration.locks import LectureBusy, SchedulerCycleBusy

from .platform import reading, settings, writing
from .views import _BadRequest, _date_param


def _service():
    return build_guarded_actions(settings())


def _guarded(view):
    """
    Map every expected outcome to a status code, and nothing to a 500.

    A refusal is not a server error: the platform decided, correctly, not to
    act, and the operator needs the reason rather than a stack trace.
    """
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except _BadRequest as exc:
            return Response({"code": "invalid_parameter", "detail": str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        except ActionRefused as exc:
            return Response({"code": exc.code, "detail": exc.detail,
                             "plan": exc.plan},
                            status=status.HTTP_409_CONFLICT)
        except SchedulerCycleBusy:
            # A scheduled cycle owns the platform right now. Standing down is
            # correct; the cycle will very likely do this work anyway.
            return Response(
                {"code": "SCHEDULER_CYCLE_RUNNING",
                 "detail": "a scheduler cycle is running; try again shortly"},
                status=status.HTTP_409_CONFLICT)
        except LectureBusy:
            return Response(
                {"code": "LECTURE_LOCKED",
                 "detail": "another process holds this lecture"},
                status=status.HTTP_409_CONFLICT)
        except PlatformError as exc:
            return Response({"code": exc.code, "detail": str(exc)},
                            status=status.HTTP_502_BAD_GATEWAY)
    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return wrapper


# --- RETRY ---------------------------------------------------------------------

@api_view(["GET"])
@_guarded
def retry_plan_view(request, lecture_id):
    """
    What a retry would do, what it would cost, and what it would leave alone.

    Read-only, on a read-only connection.
    """
    with reading() as connection:
        return Response(_service().plan_retry(connection, lecture_id))


@api_view(["POST"])
@_guarded
def retry_view(request, lecture_id):
    """
    Resume this lecture from its earliest incomplete stage.

    `expected_action` is optional and is the stale-tab guard: send back the
    action the plan showed, and the server refuses if it is no longer the
    plan. Retry can never touch a COMPLETE stage, whatever is sent.
    """
    expected = request.data.get("expected_action") if request.data else None
    with writing() as connection:
        outcome = _service().execute_retry(
            connection, lecture_id, expected_action=expected)
        connection.commit()
    return Response(outcome)


# --- RECOVER ATTENDANCE -----------------------------------------------------------

@api_view(["GET"])
@_guarded
def recover_attendance_plan_view(request, lecture_id):
    with reading() as connection:
        return Response(_service().plan_recover_attendance(connection, lecture_id))


@api_view(["POST"])
@_guarded
def recover_attendance_view(request, lecture_id):
    """
    Ask the attendance source whether the roster has arrived.

    Offered only while the lecture is genuinely waiting, and executed with the
    model provider switched off - clicking this must never buy a generation.
    If the source is still empty, nothing at all is written.
    """
    with writing() as connection:
        outcome = _service().execute_recover_attendance(connection, lecture_id)
        connection.commit()
    return Response(outcome)


# --- RECONCILE -----------------------------------------------------------------------

@api_view(["POST"])
@_guarded
def reconcile_view(request):
    """
    Re-evaluate a day from persisted state.

    A POST because an operator is asking for something, but it is READ-ONLY
    and it runs on a read-only connection: reconcile answers "what is true
    now?", which is a question, not an instruction to do work.
    """
    raw = (request.data or {}).get("date")
    if raw:
        try:
            session_date = _date.fromisoformat(str(raw))
        except ValueError as exc:
            raise _BadRequest(f"date must be YYYY-MM-DD, got {raw!r}") from exc
    else:
        session_date = _date_param(request)
    with reading() as connection:
        return Response(_service().reconcile(connection, session_date))
