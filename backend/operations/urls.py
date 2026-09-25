"""
Operations Console routes.

Phase 5A: the read-only views. Phase 5B: three named actions, each with a
read-only plan twin. The action is always in the URL and never in the body -
there is no route here that will perform an action named by the caller.
"""
from django.urls import path

from . import actions, backfill_views, views

urlpatterns = [
    path("day/", views.day_view, name="operations-day"),
    path("lectures/", views.lectures_view, name="operations-lectures"),
    path("calendar/", views.calendar_view, name="operations-calendar"),
    path("directory/", views.directory_view, name="operations-directory"),
    path("lectures/<uuid:lecture_id>/", views.lecture_view,
         name="operations-lecture"),
    path("lectures/<uuid:lecture_id>/history/", views.lecture_history_view,
         name="operations-lecture-history"),
    path("pending-attendance/", views.pending_attendance_view,
         name="operations-pending-attendance"),
    path("review-required/", views.review_required_view,
         name="operations-review-required"),
    path("errors/", views.errors_view, name="operations-errors"),
    path("lectures/<uuid:lecture_id>/media/", views.lecture_media_view,
         name="operations-lecture-media"),
    path("media-jobs/", views.media_jobs_view, name="operations-media-jobs"),
    path("runs/", views.runs_view, name="operations-runs"),
    path("runs/<uuid:run_id>/", views.run_view, name="operations-run"),
    path("precheck/", views.precheck_view, name="operations-precheck"),

    # --- Phase 5B: guarded actions ---------------------------------------
    path("lectures/<uuid:lecture_id>/retry/plan/", actions.retry_plan_view,
         name="operations-retry-plan"),
    path("lectures/<uuid:lecture_id>/retry/", actions.retry_view,
         name="operations-retry"),
    path("lectures/<uuid:lecture_id>/recover-attendance/plan/",
         actions.recover_attendance_plan_view,
         name="operations-recover-attendance-plan"),
    path("lectures/<uuid:lecture_id>/recover-attendance/",
         actions.recover_attendance_view, name="operations-recover-attendance"),
    path("reconcile/", actions.reconcile_view, name="operations-reconcile"),

    # --- QA Core RC2: Operations Backfill ---------------------------------
    # Both POSTs queue a durable run and return immediately; the
    # `backfill-runner` service does the work. See backfill_views.py.
    path("backfills/", backfill_views.backfill_list_view,
         name="operations-backfills"),
    path("backfills/preview/", backfill_views.backfill_preview_view,
         name="operations-backfill-preview"),
    path("backfills/start/", backfill_views.backfill_start_view,
         name="operations-backfill-start"),
    path("backfills/<uuid:run_id>/", backfill_views.backfill_detail_view,
         name="operations-backfill-detail"),
    path("backfills/<uuid:run_id>/cancel/", backfill_views.backfill_cancel_view,
         name="operations-backfill-cancel"),
    # Recording Links: lecture-level outcomes for one run. Read-only.
    path("backfills/<uuid:run_id>/recording-links/",
         backfill_views.backfill_recording_links_view,
         name="operations-backfill-recording-links"),
]
