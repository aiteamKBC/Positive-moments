"""
Sign in with Microsoft. Microsoft itself is faked at the one place we talk to
it (the server-to-server code redemption); everything else is the real flow
through the real URLs, session and token tables.
"""
import base64
import io
import json
import time
import urllib.error
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from positive_mentions import sso

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SECRET = "client-secret-never-logged"
REDIRECT = "https://positive-moments.example/api/auth/sso/callback"
OID = "0f0f0f0f-1111-2222-3333-444444444444"


def id_token(**overrides):
    now = int(time.time())
    claims = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "tid": TENANT,
              "aud": CLIENT, "exp": now + 3600, "iat": now, "nonce": None, "oid": OID,
              "name": "Sam Tutor", "preferred_username": "Sam.Tutor@KentBusinessCollege.com"}
    claims.update(overrides)
    encode = lambda part: base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()
    return f"{encode({'alg': 'RS256'})}.{encode(claims)}.signature"


@override_settings(MICROSOFT_SSO_TENANT_ID=TENANT, MICROSOFT_SSO_CLIENT_ID=CLIENT,
                   MICROSOFT_SSO_CLIENT_SECRET=SECRET, MICROSOFT_SSO_REDIRECT_URI=REDIRECT)
class MicrosoftSignInTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Production's auth_user is shared with other college systems, which
        # added these. Rolled back with the class transaction.
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE auth_user ADD COLUMN azure_oid varchar(36) UNIQUE")
            cursor.execute("ALTER TABLE auth_user ADD CONSTRAINT auth_user_email_unique UNIQUE (email)")

    def setUp(self):
        self.client = APIClient()

    def oid_of(self, user):
        with connection.cursor() as cursor:
            cursor.execute("SELECT azure_oid FROM auth_user WHERE id = %s", [user.pk])
            return cursor.fetchone()[0]

    def link(self, user, oid):
        with connection.cursor() as cursor:
            cursor.execute("UPDATE auth_user SET azure_oid = %s WHERE id = %s", [oid, user.pk])

    # --- helpers ---------------------------------------------------------
    def start(self, return_to="/operations/backfill"):
        response = self.client.get("/api/auth/sso/start", {"return_to": return_to})
        self.assertEqual(response.status_code, 302)
        return parse_qs(urlsplit(response["Location"]).query)

    def callback(self, query, **claims):
        state = query["state"][0]
        token = id_token(**{"nonce": query["nonce"][0], **claims})
        with mock.patch.object(sso, "redeem_code", return_value=token) as redeem:
            response = self.client.get("/api/auth/sso/callback", {"code": "ms-code", "state": state})
        return response, redeem

    def sign_in(self, return_to="/operations/backfill", **claims):
        response, _ = self.callback(self.start(return_to), **claims)
        self.assertTrue(response["Location"].startswith("/sso/complete#code="), response["Location"])
        handoff = parse_qs(urlsplit(response["Location"]).fragment)["code"][0]
        return self.client.post("/api/auth/sso/exchange/", {"code": handoff}, format="json"), handoff

    def refused_with(self, response):
        self.assertEqual(response.status_code, 302)
        location = urlsplit(response["Location"])
        self.assertEqual(location.path, "/login")
        return parse_qs(location.query)["sso_error"][0]

    # --- start -----------------------------------------------------------
    def test_start_sends_the_browser_to_our_tenant_with_state_nonce_and_pkce(self):
        response = self.client.get("/api/auth/sso/start", {"return_to": "/operations"})
        location = urlsplit(response["Location"])
        query = parse_qs(location.query)
        self.assertEqual(f"{location.scheme}://{location.netloc}{location.path}",
                         f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize")
        self.assertEqual(query["client_id"], [CLIENT])
        self.assertEqual(query["redirect_uri"], [REDIRECT])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        for key in ("state", "nonce", "code_challenge"):
            self.assertGreaterEqual(len(query[key][0]), 40)
        self.assertNotIn(SECRET, response["Location"])

    @override_settings(MICROSOFT_SSO_REDIRECT_URI="")
    def test_start_without_configuration_goes_back_to_the_login_page(self):
        self.assertEqual(self.refused_with(self.client.get("/api/auth/sso/start")), "not_configured")

    def test_only_a_safe_relative_return_path_is_kept(self):
        for unsafe in ("https://evil.example/x", "//evil.example", "/\\evil.example",
                       "javascript:alert(1)", "/api/auth/logout/", "", None, "/x\nSet-Cookie:a"):
            self.assertEqual(sso.safe_return_path(unsafe), "/operations", unsafe)
        self.assertEqual(sso.safe_return_path("/lectures?day=2026-09-30"), "/lectures?day=2026-09-30")

    # --- the full round trip ----------------------------------------------
    def test_a_college_account_comes_back_signed_in_to_the_page_it_asked_for(self):
        response, _ = self.sign_in("/operations/backfill")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["return_to"], "/operations/backfill")
        user = get_user_model().objects.get(username="sam.tutor@kentbusinesscollege.com")
        self.assertEqual(Token.objects.get(user=user).key, response.data["token"])
        self.assertEqual(user.email, "sam.tutor@kentbusinesscollege.com")
        self.assertEqual(user.first_name, "Sam Tutor")
        self.assertEqual(self.oid_of(user), OID)
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff)

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {response.data['token']}")
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 200)

    def test_the_same_person_is_the_same_account_even_after_an_email_change(self):
        first, _ = self.sign_in()
        self.client = APIClient()
        second, _ = self.sign_in(preferred_username="sam.renamed@kentbusinesscollege.com")
        self.assertEqual(first.data["token"], second.data["token"])
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_an_existing_college_account_is_linked_not_duplicated(self):
        # 2026-10-04 in production: the person already had a password account
        # with this email; a second account broke the unique email and 500'd.
        existing = get_user_model().objects.create_user(
            "Sam.Tutor", email="Sam.Tutor@kentbusinesscollege.com", password="their-own",
            is_staff=True, first_name="Samantha")
        response, _ = self.sign_in()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_user_model().objects.count(), 1)
        existing.refresh_from_db()
        self.assertEqual(Token.objects.get(user=existing).key, response.data["token"])
        self.assertEqual(self.oid_of(existing), OID)
        # Their account is theirs: password, name and flags untouched.
        self.assertTrue(existing.check_password("their-own"))
        self.assertEqual((existing.first_name, existing.is_staff), ("Samantha", True))

    def test_an_account_linked_to_someone_else_is_never_taken_over(self):
        other = get_user_model().objects.create_user("sam", email="sam.tutor@kentbusinesscollege.com")
        self.link(other, "99999999-9999-9999-9999-999999999999")
        response, _ = self.callback(self.start())
        self.assertEqual(self.refused_with(response), "account_conflict")
        self.assertFalse(Token.objects.exists())

    def test_a_sign_in_name_outside_the_college_domains_is_refused(self):
        response, _ = self.callback(self.start(), preferred_username="guest@gmail.com")
        self.assertEqual(self.refused_with(response), "not_a_college_account")
        self.assertFalse(get_user_model().objects.exists())

    def test_people_without_an_existing_account_do_not_collide_with_each_other(self):
        self.sign_in()
        self.client = APIClient()
        response, _ = self.sign_in(oid="12345678-1234-1234-1234-123456789012", name="Alex",
                                   preferred_username="alex@kentbusinesscollege.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_user_model().objects.count(), 2)

    def test_the_code_is_redeemed_server_to_server_with_the_pkce_verifier(self):
        query = self.start()
        _, redeem = self.callback(query)
        code, verifier = redeem.call_args.args
        self.assertEqual(code, "ms-code")
        expected = sso._b64url(__import__("hashlib").sha256(verifier.encode()).digest())
        self.assertEqual(query["code_challenge"], [expected])

    # --- refusals ----------------------------------------------------------
    def test_an_invalid_state_is_refused(self):
        self.start()
        with mock.patch.object(sso, "redeem_code") as redeem:
            response = self.client.get("/api/auth/sso/callback", {"code": "x", "state": "forged"})
        self.assertEqual(self.refused_with(response), "invalid_state")
        redeem.assert_not_called()

    def test_a_callback_without_a_started_sign_in_is_refused(self):
        response = self.client.get("/api/auth/sso/callback", {"code": "x", "state": "y"})
        self.assertEqual(self.refused_with(response), "invalid_state")

    def test_a_replayed_callback_is_refused(self):
        query = self.start()
        first, _ = self.callback(query)
        self.assertTrue(first["Location"].startswith("/sso/complete#"))
        replay, _ = self.callback(query)
        self.assertEqual(self.refused_with(replay), "invalid_state")

    def test_a_stale_sign_in_attempt_is_refused(self):
        query = self.start()
        session = self.client.session
        for entry in session[sso.LOGIN_SESSION_KEY].values():
            entry["started_at"] -= sso.LOGIN_TTL_SECONDS + 1
        session.save()
        self.assertEqual(self.refused_with(self.callback(query)[0]), "expired")

    def test_an_older_tab_still_signs_in_after_a_newer_one_started(self):
        # 2026-10-04 in production: three starts in one browser, the person
        # approved the first on their phone, and it came back invalid_state.
        first, second = self.start("/lectures"), self.start("/operations")
        response, _ = self.callback(first)
        self.assertTrue(response["Location"].startswith("/sso/complete#"), response["Location"])
        self.client = APIClient()
        self.assertEqual(self.refused_with(self.callback(second)[0]), "invalid_state")

    def test_both_tabs_can_finish(self):
        first, second = self.start(), self.start()
        self.assertTrue(self.callback(second)[0]["Location"].startswith("/sso/complete#"))
        self.assertTrue(self.callback(first)[0]["Location"].startswith("/sso/complete#"))

    def test_only_the_newest_attempts_are_kept(self):
        oldest = self.start()
        for _ in range(sso.MAX_PENDING_LOGINS):
            self.start()
        self.assertEqual(self.refused_with(self.callback(oldest)[0]), "invalid_state")

    def test_a_sign_in_started_before_this_release_is_simply_unknown(self):
        session = self.client.session
        session[sso.LOGIN_SESSION_KEY] = {"state": "old", "nonce": "n", "verifier": "v",
                                          "return_to": "/", "started_at": time.time()}
        session.save()
        response = self.client.get("/api/auth/sso/callback", {"code": "x", "state": "old"})
        self.assertEqual(self.refused_with(response), "invalid_state")
        self.assertEqual(self.client.get("/api/auth/sso/start").status_code, 302)

    def test_logging_out_in_one_tab_does_not_cancel_a_sign_in_in_another(self):
        signed_in, _ = self.sign_in()
        pending = self.start()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {signed_in.data['token']}")
        self.client.post("/api/auth/logout/")
        self.client.credentials()
        self.assertTrue(self.callback(pending)[0]["Location"].startswith("/sso/complete#"))

    def test_an_expired_id_token_is_refused(self):
        response, _ = self.callback(self.start(), exp=int(time.time()) - 3600)
        self.assertEqual(self.refused_with(response), "expired")

    def test_a_token_for_another_application_is_refused(self):
        response, _ = self.callback(self.start(), aud="99999999-0000-0000-0000-000000000000")
        self.assertEqual(self.refused_with(response), "wrong_audience")

    def test_an_account_from_another_tenant_is_refused(self):
        other = "99999999-8888-7777-6666-555555555555"
        response, _ = self.callback(self.start(), tid=other,
                                    iss=f"https://login.microsoftonline.com/{other}/v2.0")
        self.assertEqual(self.refused_with(response), "wrong_tenant")
        self.assertFalse(get_user_model().objects.exists())

    def test_a_mismatched_nonce_is_refused(self):
        response, _ = self.callback(self.start(), nonce="another-nonce")
        self.assertEqual(self.refused_with(response), "invalid_nonce")

    def test_microsoft_refusing_is_shown_without_its_text(self):
        self.start()
        response = self.client.get("/api/auth/sso/callback",
                                   {"error": "access_denied", "error_description": "<script>"})
        self.assertEqual(self.refused_with(response), "microsoft_refused")

    def test_a_switched_off_account_cannot_sign_in(self):
        user = get_user_model().objects.create(username="sam", email="sam.tutor@kentbusinesscollege.com",
                                               is_active=False)
        self.link(user, OID)
        response, _ = self.callback(self.start())
        self.assertEqual(self.refused_with(response), "account_disabled")
        self.assertFalse(Token.objects.exists())

    # --- the handoff to the browser ------------------------------------------
    def test_the_handoff_code_works_once(self):
        response, handoff = self.sign_in()
        self.assertEqual(response.status_code, 200)
        again = self.client.post("/api/auth/sso/exchange/", {"code": handoff}, format="json")
        self.assertEqual(again.status_code, 400)

    def test_a_wrong_or_late_handoff_code_is_refused(self):
        response, _ = self.callback(self.start())
        handoff = parse_qs(urlsplit(response["Location"]).fragment)["code"][0]
        wrong = self.client.post("/api/auth/sso/exchange/", {"code": handoff + "x"}, format="json")
        self.assertEqual(wrong.status_code, 400)

        response, _ = self.callback(self.start())
        handoff = parse_qs(urlsplit(response["Location"]).fragment)["code"][0]
        session = self.client.session
        session[sso.HANDOFF_SESSION_KEY]["issued_at"] -= sso.HANDOFF_TTL_SECONDS + 1
        session.save()
        late = self.client.post("/api/auth/sso/exchange/", {"code": handoff}, format="json")
        self.assertEqual(late.status_code, 400)

    def test_a_handoff_code_is_useless_in_another_browser(self):
        response, _ = self.callback(self.start())
        handoff = parse_qs(urlsplit(response["Location"]).fragment)["code"][0]
        stranger = APIClient().post("/api/auth/sso/exchange/", {"code": handoff}, format="json")
        self.assertEqual(stranger.status_code, 400)

    def test_logout_ends_the_session(self):
        response, _ = self.sign_in()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {response.data['token']}")
        self.assertEqual(self.client.post("/api/auth/logout/").status_code, 204)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 401)

    # --- secrets stay secret ------------------------------------------------
    def test_a_failed_code_redemption_never_logs_the_secret_or_microsofts_reply(self):
        error = urllib.error.HTTPError(
            "https://login.microsoftonline.com", 400, "Bad Request", {},
            io.BytesIO(f'{{"error":"invalid_grant","echo":"{SECRET}"}}'.encode()))
        query = self.start()
        with mock.patch.object(sso.urllib.request, "urlopen", side_effect=error), \
                self.assertLogs("positive_mentions.sso", level="WARNING") as logs:
            response = self.client.get("/api/auth/sso/callback",
                                       {"code": "ms-code", "state": query["state"][0]})
        self.assertEqual(self.refused_with(response), "token_exchange_failed")
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertNotIn(SECRET, response["Location"])

    def test_the_redemption_request_carries_the_secret_only_in_its_body(self):
        query = self.start()
        reply = io.BytesIO(json.dumps({"id_token": id_token(nonce=query["nonce"][0])}).encode())
        with mock.patch.object(sso.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = reply
            self.client.get("/api/auth/sso/callback", {"code": "ms-code", "state": query["state"][0]})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url,
                         f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token")
        self.assertNotIn(SECRET, request.full_url)
        body = parse_qs(request.data.decode())
        self.assertEqual(body["client_secret"], [SECRET])
        self.assertEqual(body["redirect_uri"], [REDIRECT])
        self.assertEqual(body["grant_type"], ["authorization_code"])
