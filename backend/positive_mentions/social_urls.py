from django.urls import path

from .social_operations_views import (
    InternalPromptView,
    InternalRunCreateView,
    InternalRunStatusView,
    InternalUsageView,
    LectureCostsView,
    LectureRunsView,
    PromptDetailView,
    PromptListView,
    PromptRollbackView,
    PromptVersionView,
    ReanalysisView,
    RunDetailView,
)
from .social_views import (
    SocialClipRequestView,
    SocialClipStatusView,
    SocialLectureDetailView,
    SocialLectureListView,
    SocialSummaryView,
)

urlpatterns = [
    path("summary/", SocialSummaryView.as_view(), name="social-summary"),
    path("lectures/", SocialLectureListView.as_view(), name="social-lectures"),
    path("lectures/<str:session_key>/", SocialLectureDetailView.as_view(), name="social-lecture-detail"),
    path("lectures/<str:session_key>/clips/", SocialClipStatusView.as_view(), name="social-clip-status"),
    path(
        "lectures/<str:session_key>/opportunities/<int:opportunity_id>/clip-requests/",
        SocialClipRequestView.as_view(), name="social-clip-request",
    ),
    path("lectures/<str:session_key>/reanalysis/", ReanalysisView.as_view(), name="social-reanalysis"),
    path("lectures/<str:session_key>/analysis-runs/", LectureRunsView.as_view(), name="social-lecture-runs"),
    path("lectures/<str:session_key>/costs/", LectureCostsView.as_view(), name="social-lecture-costs"),
    path("analysis-runs/<uuid:run_id>/", RunDetailView.as_view(), name="social-run-detail"),
    path("prompts/", PromptListView.as_view(), name="social-prompts"),
    path("prompts/<str:prompt_key>/", PromptDetailView.as_view(), name="social-prompt"),
    path("prompts/<str:prompt_key>/versions/<int:version>/", PromptVersionView.as_view(),
         name="social-prompt-version"),
    path("prompts/<str:prompt_key>/rollback/", PromptRollbackView.as_view(), name="social-prompt-rollback"),
    # n8n only (SOCIAL_MEDIA_SERVICE_TOKEN).
    path("internal/prompts/<str:prompt_key>/", InternalPromptView.as_view(), name="social-internal-prompt"),
    path("internal/analysis-runs/", InternalRunCreateView.as_view(), name="social-internal-runs"),
    path("internal/analysis-runs/<uuid:run_id>/status/", InternalRunStatusView.as_view(),
         name="social-internal-run-status"),
    path("internal/analysis-runs/<uuid:run_id>/usage/", InternalUsageView.as_view(),
         name="social-internal-run-usage"),
]
