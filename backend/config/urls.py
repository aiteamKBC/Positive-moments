from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("positive_mentions.auth_urls")),
    path("api/positive-mentions/", include("positive_mentions.urls")),
    path("api/social-media/", include("positive_mentions.social_urls")),
    path("api/operations/", include("operations.urls")),
]
