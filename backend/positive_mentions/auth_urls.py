from django.urls import path

from .sso import sso_callback, sso_exchange, sso_start
from .views import login_view, logout_view, me_view

urlpatterns = [
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("me/", me_view, name="me"),
    path("sso/start", sso_start, name="sso-start"),
    path("sso/callback", sso_callback, name="sso-callback"),
    path("sso/exchange/", sso_exchange, name="sso-exchange"),
]
