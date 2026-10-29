"""
Operations Console routes.

Phase 5A: the read-only views. Phase 5B: three named actions, each with a
read-only plan twin. The action is always in the URL and never in the body -
there is no route here that will perform an action named by the caller.
"""
from django.urls import path

from . import (
    actions, backfill_views, media_views, publisher_backfill_views, publisher_views, publishing_monitor_views, views,
)

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
    # --- Positive Moments Media ------------------------------------------
    # Reads are read-only transactions; POSTs queue work for `media-runner`.
    path("media/dashboard/", media_views.media_dashboard_view,
         name="operations-media-dashboard"),
    path("media/runs/", media_views.media_run_create_view,
         name="operations-media-run-create"),
    path("media/runs/<uuid:run_id>/", media_views.media_run_view,
         name="operations-media-run"),
    path("media/moments/<uuid:moment_id>/render/", media_views.moment_render_view,
         name="operations-media-moment-render"),
    path("media/moments/<uuid:moment_id>/retry/", media_views.moment_retry_view,
         name="operations-media-moment-retry"),
    path("media/assets/<uuid:asset_id>/open/", media_views.asset_open_view,
         name="operations-media-asset-open"),
    path("media/webhooks/creatomate/", media_views.creatomate_webhook_view,
         name="operations-media-creatomate-webhook"),
    path("lectures/<uuid:lecture_id>/positive-moments/",
         media_views.lecture_positive_moments_view,
         name="operations-lecture-positive-moments"),
    path("lectures/<uuid:lecture_id>/recording/open/", media_views.recording_open_view,
         name="operations-lecture-recording-open"),

    # Recording Links: lecture-level outcomes for one run. Read-only.
    path("backfills/<uuid:run_id>/recording-links/",
         backfill_views.backfill_recording_links_view,
         name="operations-backfill-recording-links"),

    # Continuous Bunny Publisher (P0-D). Reads are READ ONLY; the two actions are audited.
    path("publisher/status/", publisher_views.status_view, name="operations-publisher-status"),
    path("publisher/pending/", publisher_views.pending_view, name="operations-publisher-pending"),
    path("publisher/exceptions/", publisher_views.exceptions_view, name="operations-publisher-exceptions"),
    path("publisher/lectures/<uuid:lecture_id>/", publisher_views.lecture_view,
         name="operations-publisher-lecture"),
    path("publisher/jobs/<uuid:job_id>/history/", publisher_views.job_history_view,
         name="operations-publisher-job-history"),
    path("publisher/jobs/<uuid:job_id>/retry/", publisher_views.job_retry_view,
         name="operations-publisher-job-retry"),
    path("publisher/lectures/<uuid:lecture_id>/withdraw/", publisher_views.lecture_withdraw_view,
         name="operations-publisher-lecture-withdraw"),
    path("publisher/lectures/<uuid:lecture_id>/parts/<int:part_index>/decision/",
         publisher_views.part_decision_view, name="operations-publisher-part-decision"),

    # Lecture Publishing Monitor: every publisher query on a READ ONLY connection; the only writes are
    # the signed-in user's own alert review state (positive_mentions.PublisherAlertReview).
    path("publishing-monitor/health/", publishing_monitor_views.health_view,
         name="operations-publishing-monitor-health"),
    path("publishing-monitor/alerts/", publishing_monitor_views.alerts_view,
         name="operations-publishing-monitor-alerts"),
    # The signed-in user's own read / dismiss state for an alert (never the publisher's alert itself).
    path("publishing-monitor/alerts/<uuid:alert_id>/read/", publishing_monitor_views.alert_read_view,
         name="operations-publishing-monitor-alert-read"),
    path("publishing-monitor/alerts/<uuid:alert_id>/dismiss/", publishing_monitor_views.alert_dismiss_view,
         name="operations-publishing-monitor-alert-dismiss"),
    path("publishing-monitor/alerts/<uuid:alert_id>/restore/", publishing_monitor_views.alert_restore_view,
         name="operations-publishing-monitor-alert-restore"),
    path("publishing-monitor/summary/", publishing_monitor_views.summary_view,
         name="operations-publishing-monitor-summary"),
    path("publishing-monitor/lectures/", publishing_monitor_views.lectures_view,
         name="operations-publishing-monitor-lectures"),
    path("publishing-monitor/lectures/<uuid:lecture_id>/", publishing_monitor_views.lecture_view,
         name="operations-publishing-monitor-lecture"),

    # Historical Backfill: preview is READ ONLY; start / cancel / retry need the operator group and only
    # write the run record - the publisher service does the publishing (publisher_backfill_views.py).
    path("publishing-monitor/backfill/runs/", publisher_backfill_views.runs_view,
         name="operations-publisher-backfill-runs"),
    path("publishing-monitor/backfill/preview/", publisher_backfill_views.preview_view,
         name="operations-publisher-backfill-preview"),
    path("publishing-monitor/backfill/runs/start/", publisher_backfill_views.start_view,
         name="operations-publisher-backfill-start"),
    path("publishing-monitor/backfill/runs/<uuid:run_id>/", publisher_backfill_views.run_view,
         name="operations-publisher-backfill-run"),
    path("publishing-monitor/backfill/runs/<uuid:run_id>/cancel/", publisher_backfill_views.cancel_view,
         name="operations-publisher-backfill-cancel"),
    path("publishing-monitor/backfill/runs/<uuid:run_id>/retry/", publisher_backfill_views.retry_view,
         name="operations-publisher-backfill-retry"),
]
