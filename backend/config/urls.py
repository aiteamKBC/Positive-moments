from django.contrib import admin
from django.urls import include, path

from operations import api_docs

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("positive_mentions.auth_urls")),
    path("api/positive-mentions/", include("positive_mentions.urls")),
    path("api/social-media/", include("positive_mentions.social_urls")),
    path("api/operations/", include("operations.urls")),
    path("api/publisher/v1/", include("operations.lms_urls")),
    # Documentation of the LMS API above, for Dashboard users, off by default (operations/api_docs.py).
    path("api/docs/", api_docs.docs_view, name="api-docs"),
    path("api/docs/session/", api_docs.session_view, name="api-docs-session"),
    path("api/schema/", api_docs.schema_view, name="api-schema"),
]
