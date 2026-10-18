from django.urls import path

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
]
