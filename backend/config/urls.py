from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("positive_mentions.auth_urls")),
    path("api/positive-mentions/", include("positive_mentions.urls")),
]
