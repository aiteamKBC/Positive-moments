"""
Sign in with the college's Microsoft 365 account - the same way the
Communication Centre signs people in - so someone already signed in there
lands here without a second login.

THE FLOW (OpenID Connect authorization code + PKCE, confidential client)
------------------------------------------------------------------------
1. /api/auth/sso/start      remember state, nonce and a PKCE verifier in the
                            server-side Django session, send the browser to
                            Microsoft.
2. Microsoft                signs the user in (silently when they already are)
                            and sends a one-time code back to the registered
                            redirect URI.
3. /api/auth/sso/callback   check state (single use, ten minutes), exchange
                            the code server-to-server, check the ID token's
                            issuer, audience, tenant, expiry and nonce, map
                            the person by their immutable Entra object id,
                            then hand the browser a second one-time code in
                            the URL fragment (never sent to any server).
4. /api/auth/sso/exchange   the SPA trades that code - bound to the same
                            session, two minutes, single use - for the usual
                            API token. No token ever travels in a URL.

WHY THE ID TOKEN SIGNATURE IS NOT RE-VERIFIED
---------------------------------------------
The ID token is received directly from Microsoft's token endpoint over TLS,
in exchange for a code plus our client secret plus the PKCE verifier. OpenID
Connect Core 1.0 section 3.1.3.7 (6) allows TLS server validation in place of
the signature check in exactly this case, which keeps this module on the
standard library. Every other claim is still checked.

Only members of the configured tenant get in: the issuer and `tid` must both
be that tenant.
"""
import base64
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.http import HttpResponseRedirect
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

log = logging.getLogger(__name__)

AUTHORITY = "https://login.microsoftonline.com"
SCOPES = "openid profile email"
LOGIN_SESSION_KEY = "microsoft_sso_login"
HANDOFF_SESSION_KEY = "microsoft_sso_handoff"
LOGIN_TTL_SECONDS = 600
# Sign-ins in flight per browser: a second tab, a refresh or a double click
# must not invalidate the attempt the person is about to approve.
MAX_PENDING_LOGINS = 5
HANDOFF_TTL_SECONDS = 120
CLOCK_SKEW_SECONDS = 300
DEFAULT_RETURN_TO = "/positive-moments"
USERNAME_PREFIX = "entra-"
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class SsoError(Exception):
    """A refusal. `code` is what the login page shows; nothing secret is in it."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def configured():
    return bool(settings.MICROSOFT_SSO_TENANT_ID and settings.MICROSOFT_SSO_CLIENT_ID
                and settings.MICROSOFT_SSO_CLIENT_SECRET and settings.MICROSOFT_SSO_REDIRECT_URI)


def safe_return_path(value):
    """A same-site relative path, or the default. Never another origin."""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return DEFAULT_RETURN_TO
    if "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return DEFAULT_RETURN_TO
    parts = urllib.parse.urlsplit(value)
    if parts.scheme or parts.netloc or value.startswith("/api/"):
        return DEFAULT_RETURN_TO
    return value


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _login_page(code):
    return HttpResponseRedirect("/login?" + urllib.parse.urlencode({"sso_error": code}))


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def sso_start(request):
    if not configured():
        return _login_page("not_configured")
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    now = time.time()
    pending = {key: entry for key, entry in _pending_logins(request).items()
               if now - entry["started_at"] <= LOGIN_TTL_SECONDS}
    pending[_digest(state)] = {
        "nonce": nonce, "verifier": verifier,
        "return_to": safe_return_path(request.GET.get("return_to")),
        "started_at": now,
    }
    newest = sorted(pending.items(), key=lambda item: item[1]["started_at"])[-MAX_PENDING_LOGINS:]
    request.session[LOGIN_SESSION_KEY] = dict(newest)
    query = urllib.parse.urlencode({
        "client_id": settings.MICROSOFT_SSO_CLIENT_ID,
        "response_type": "code",
        "response_mode": "query",
        "redirect_uri": settings.MICROSOFT_SSO_REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    })
    return HttpResponseRedirect(
        f"{AUTHORITY}/{settings.MICROSOFT_SSO_TENANT_ID}/oauth2/v2.0/authorize?{query}")


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def sso_callback(request):
    # Popped before anything else: a state is good for one attempt only.
    pending = None
    state = request.GET.get("state")
    if isinstance(state, str):
        logins = _pending_logins(request)
        pending = logins.pop(_digest(state), None)
        request.session[LOGIN_SESSION_KEY] = logins
    try:
        user, return_to = _complete_login(request, pending)
    except SsoError as refusal:
        log.warning("microsoft sign-in refused: %s", refusal.code)
        return _login_page(refusal.code)
    # A fresh session id once someone is signed in (no fixation).
    request.session.cycle_key()
    handoff = secrets.token_urlsafe(32)
    request.session[HANDOFF_SESSION_KEY] = {
        "digest": _digest(handoff), "user_id": user.pk,
        "return_to": return_to, "issued_at": time.time(),
    }
    return HttpResponseRedirect("/sso/complete#" + urllib.parse.urlencode({"code": handoff}))


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def sso_exchange(request):
    pending = request.session.pop(HANDOFF_SESSION_KEY, None)
    code = request.data.get("code")
    refused = Response({"code": "sso_handoff_invalid",
                        "detail": "Sign-in link expired. Please sign in again."},
                       status=status.HTTP_400_BAD_REQUEST)
    if (not pending or not isinstance(code, str)
            or time.time() - pending["issued_at"] > HANDOFF_TTL_SECONDS
            or not secrets.compare_digest(_digest(code), pending["digest"])):
        return refused
    user = get_user_model().objects.filter(pk=pending["user_id"], is_active=True).first()
    if user is None:
        return refused
    token, _ = Token.objects.get_or_create(user=user)
    return Response({
        "token": token.key,
        "return_to": safe_return_path(pending["return_to"]),
        "user": {"username": user.get_username(), "is_staff": user.is_staff},
    })


def _pending_logins(request):
    logins = request.session.get(LOGIN_SESSION_KEY)
    # Sessions from before several sign-ins could be in flight held one
    # attempt under "state"; such an entry is simply not a dict of them.
    if not isinstance(logins, dict) or "state" in logins:
        return {}
    return dict(logins)


def _complete_login(request, pending):
    if request.GET.get("error"):
        # Microsoft said no (cancelled, consent, disabled account...). Its
        # error text is not echoed: it is not ours to show.
        raise SsoError("microsoft_refused")
    if not pending:
        raise SsoError("invalid_state")
    if time.time() - pending["started_at"] > LOGIN_TTL_SECONDS:
        raise SsoError("expired")
    code = request.GET.get("code")
    if not code:
        raise SsoError("invalid_response")
    claims = validated_claims(redeem_code(code, pending["verifier"]), pending["nonce"])
    return user_for(claims), pending["return_to"]


def redeem_code(code, verifier):
    """Server-to-server: the one-time code for an ID token."""
    body = urllib.parse.urlencode({
        "client_id": settings.MICROSOFT_SSO_CLIENT_ID,
        "client_secret": settings.MICROSOFT_SSO_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.MICROSOFT_SSO_REDIRECT_URI,
        "code_verifier": verifier,
        "scope": SCOPES,
    }).encode()
    request = urllib.request.Request(
        f"{AUTHORITY}/{settings.MICROSOFT_SSO_TENANT_ID}/oauth2/v2.0/token",
        data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # The body is dropped: it can quote the request, which carries the secret.
        log.warning("microsoft token exchange failed: HTTP %s", exc.code)
        raise SsoError("token_exchange_failed") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("microsoft token exchange failed: %s", type(exc).__name__)
        raise SsoError("token_exchange_failed") from None
    id_token = payload.get("id_token")
    if not isinstance(id_token, str):
        raise SsoError("token_exchange_failed")
    return id_token


def validated_claims(id_token, expected_nonce, now=None):
    now = time.time() if now is None else now
    try:
        segment = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (IndexError, ValueError):
        raise SsoError("invalid_token") from None
    if not isinstance(claims, dict):
        raise SsoError("invalid_token")
    tenant = settings.MICROSOFT_SSO_TENANT_ID
    if claims.get("iss") != f"{AUTHORITY}/{tenant}/v2.0" or claims.get("tid") != tenant:
        raise SsoError("wrong_tenant")
    if claims.get("aud") != settings.MICROSOFT_SSO_CLIENT_ID:
        raise SsoError("wrong_audience")
    exp, iat = claims.get("exp"), claims.get("iat")
    if not isinstance(exp, (int, float)) or exp < now - CLOCK_SKEW_SECONDS:
        raise SsoError("expired")
    if not isinstance(iat, (int, float)) or iat > now + CLOCK_SKEW_SECONDS:
        raise SsoError("invalid_token")
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not secrets.compare_digest(nonce, expected_nonce):
        raise SsoError("invalid_nonce")
    if not isinstance(claims.get("oid"), str) or not GUID.match(claims["oid"]):
        raise SsoError("invalid_token")
    return claims


def college_email(claims):
    """The sign-in name, if it is on a college domain. Guests and service
    accounts have other domains and are not linked to anyone's account."""
    email = (claims.get("preferred_username") or claims.get("email") or "").strip().lower()
    domain = email.rpartition("@")[2]
    return email if "@" in email and domain in settings.MICROSOFT_SSO_EMAIL_DOMAINS else ""


def user_for(claims):
    """
    The person's account, found by their Entra object id.

    `auth_user` is shared with other college systems, which already keep the
    object id in `auth_user.azure_oid` (unique) and allow one account per
    email (unique). So:

      1. the account whose azure_oid is this person        -> that account
      2. otherwise the one account with their college email -> link it
      3. otherwise                                          -> a new account

    An account linked to a DIFFERENT object id is never taken over. Existing
    accounts keep their name, email, password and flags; only the link and
    last_login are written.
    """
    oid = claims["oid"].lower()
    User = get_user_model()
    try:
        with transaction.atomic():
            user = _user_with_oid(oid)
            if user is None:
                email = college_email(claims)
                if not email:
                    raise SsoError("not_a_college_account")
                matches = list(User.objects.filter(email__iexact=email)[:2])
                if len(matches) > 1:
                    raise SsoError("account_conflict")
                if matches:
                    user = matches[0]
                else:
                    taken = User.objects.filter(username__iexact=email).exists()
                    user = User(username=f"{USERNAME_PREFIX}{oid}" if taken else email,
                                email=email, first_name=(claims.get("name") or "").strip()[:150],
                                is_active=True, is_staff=False)
                    user.set_unusable_password()        # Microsoft only, never a password
                    user.save()
                _link(user, oid)
            if not user.is_active:
                raise SsoError("account_disabled")
            user.last_login = timezone.now()
            user.save(update_fields=["last_login"])
            return user
    except IntegrityError:
        raise SsoError("account_conflict") from None


def _user_with_oid(oid):
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM auth_user WHERE lower(azure_oid) = %s", [oid])
        row = cursor.fetchone()
    return get_user_model().objects.get(pk=row[0]) if row else None


def _link(user, oid):
    """Fill the link once. A row already linked to someone else is refused."""
    with connection.cursor() as cursor:
        cursor.execute("UPDATE auth_user SET azure_oid = %s WHERE id = %s AND azure_oid IS NULL",
                       [oid, user.pk])
        if cursor.rowcount != 1:
            raise SsoError("account_conflict")
