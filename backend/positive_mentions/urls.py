from django.urls import path

from .views import LectureDetailView, LectureListView, SummaryView, WatchMomentView

urlpatterns = [
    path("summary/", SummaryView.as_view(), name="summary"),
    path("lectures/", LectureListView.as_view(), name="lectures"),
    path("lectures/<str:session_key>/", LectureDetailView.as_view(), name="lecture-detail"),
    path(
        "lectures/<str:session_key>/clips/<int:clip_index>/watch/",
        WatchMomentView.as_view(),
        name="watch-moment",
    ),
]
