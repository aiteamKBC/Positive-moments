"""Routes of the LMS delivery API, mounted at /api/publisher/v1/ (operations/lms_api.py)."""
from django.urls import path

from . import lms_api

urlpatterns = [
    path("occurrences/<str:occurrence_id>/recording", lms_api.occurrence_recording,
         name="publisher-lms-occurrence-recording"),
    path("occurrences", lms_api.occurrence_feed, name="publisher-lms-occurrence-feed"),
    path("health", lms_api.health, name="publisher-lms-health"),
]
