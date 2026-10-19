"""Supabase Auth transport contract; no remote accounts or credentials used."""

import unittest
from unittest.mock import Mock

import requests

from auth_service import AuthError, SupabaseAuthService, public_user


USER = {"id": "13ef7224-fae9-4630-b6d3-0897b1546321", "email": "alice@example.com",
        "user_metadata": {"display_name": "Alice"}}


def response(payload=None, status=200, content=b"json"):
    result = Mock(status_code=status, content=content)
    result.json.return_value = {} if payload is None else payload
    return result


def tokens(**changes):
    return {"access_token": "private-access-token", "refresh_token": "private-refresh-token",
            "user": dict(USER), **changes}


class AuthServiceTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.service = SupabaseAuthService("https://project.supabase.co/", "publishable-key", self.http)

    def test_login_uses_password_grant_and_never_returns_password_as_session_data(self):
        self.http.request.return_value = response(tokens())
        result = self.service.login("alice@example.com", "private-password")
        arguments, keywords = self.http.request.call_args
        self.assertEqual(arguments, ("POST", "https://project.supabase.co/auth/v1/token"))
        self.assertEqual(keywords["params"], {"grant_type": "password"})
        self.assertEqual(keywords["json"], {"email": "alice@example.com", "password": "private-password"})
        self.assertEqual(keywords["headers"]["apikey"], "publishable-key")
        self.assertNotIn("Authorization", keywords["headers"])
        self.assertFalse(keywords["allow_redirects"])
        self.assertEqual(result["user"]["id"], USER["id"])
        self.assertNotIn("password", result)

    def test_incomplete_login_session_does_not_authenticate(self):
        for changes in [{"access_token": ""}, {"access_token": None}, {"access_token": 123},
                        {"refresh_token": ""}, {"refresh_token": None}, {"refresh_token": 123},
                        {"user": None}, {"user": {}}, {"user": {"id": "not-an-account-id"}}]:
            with self.subTest(changes=changes):
                self.http.request.return_value = response(tokens(**changes))
                with self.assertRaises(AuthError) as caught:
                    self.service.login("alice@example.com", "private-password")
                self.assertEqual(caught.exception.status, 401)

    def test_get_user_validates_identity_with_provider_using_access_token(self):
        self.http.request.return_value = response(USER)
        self.assertEqual(self.service.get_user("private-access-token"), USER)
        arguments, keywords = self.http.request.call_args
        self.assertEqual(arguments, ("GET", "https://project.supabase.co/auth/v1/user"))
        self.assertEqual(keywords["headers"]["Authorization"], "Bearer private-access-token")
        self.assertIsNone(keywords["json"])

    def test_invalid_or_expired_user_is_unauthenticated(self):
        for result in [response({"id": "forged-id"}), response({}),
                       response({"message": "private JWT details"}, 401),
                       response({"message": "forbidden"}, 403)]:
            with self.subTest(status=result.status_code):
                self.http.request.return_value = result
                self.assertIsNone(self.service.get_user("expired-token"))

    def test_provider_outage_is_not_misreported_as_a_signed_out_customer(self):
        self.http.request.return_value = response({"message": "private provider internals"}, 503)
        with self.assertRaises(AuthError) as caught:
            self.service.get_user("private-access-token")
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn("provider internals", str(caught.exception))

    def test_refresh_uses_refresh_grant_and_validates_replacement_tokens(self):
        self.http.request.return_value = response(tokens(access_token="new-access", refresh_token="new-refresh"))
        result = self.service.refresh("old-refresh")
        keywords = self.http.request.call_args.kwargs
        self.assertEqual(keywords["params"], {"grant_type": "refresh_token"})
        self.assertEqual(keywords["json"], {"refresh_token": "old-refresh"})
        self.assertNotIn("Authorization", keywords["headers"])
        self.assertEqual(result["access_token"], "new-access")
        self.assertEqual(result["refresh_token"], "new-refresh")

    def test_refresh_cannot_create_a_session_without_an_account(self):
        self.http.request.return_value = response(tokens(user=None))
        with self.assertRaises(AuthError) as caught:
            self.service.refresh("private-refresh-token")
        self.assertEqual(caught.exception.status, 401)

    def test_signup_confirmation_does_not_fabricate_a_signed_in_session(self):
        self.http.request.return_value = response({"user": USER})
        result = self.service.signup("alice@example.com", "private-password", "Alice",
                                     redirect_to="https://analytics.example.com/login.html")
        self.assertEqual(result, {"confirmation_required": True})
        keywords = self.http.request.call_args.kwargs
        self.assertEqual(keywords["json"], {"email": "alice@example.com", "password": "private-password",
                                           "data": {"display_name": "Alice"}})
        self.assertEqual(keywords["params"], {"redirect_to": "https://analytics.example.com/login.html"})

    def test_signup_with_confirmations_disabled_returns_valid_session(self):
        self.http.request.return_value = response(tokens())
        self.assertEqual(self.service.signup("alice@example.com", "private-password")["access_token"],
                         "private-access-token")

    def test_signup_error_is_bad_input_without_exposing_account_existence(self):
        self.http.request.return_value = response({"message": "private-password / account already exists"}, 422)
        with self.assertRaises(AuthError) as caught:
            self.service.signup("alice@example.com", "private-password")
        self.assertEqual(caught.exception.status, 400)
        self.assertNotIn("already exists", str(caught.exception))
        self.assertNotIn("private-password", str(caught.exception))

    def test_signout_revokes_only_current_session_and_handles_empty_success(self):
        self.http.request.return_value = response(status=204, content=b"")
        self.service.logout("private-access-token")
        arguments, keywords = self.http.request.call_args
        self.assertEqual(arguments, ("POST", "https://project.supabase.co/auth/v1/logout"))
        self.assertEqual(keywords["params"], {"scope": "local"})
        self.assertEqual(keywords["headers"]["Authorization"], "Bearer private-access-token")

    def test_json_array_or_unreadable_response_cannot_establish_a_session(self):
        unreadable = response()
        unreadable.json.side_effect = ValueError("private-password in provider body")
        for result in [response([]), response("not-an-object"), unreadable]:
            self.http.request.return_value = result
            with self.subTest(result=result):
                with self.assertRaises(AuthError) as caught:
                    self.service.login("alice@example.com", "private-password")
                self.assertEqual(caught.exception.status, 503)
                self.assertNotIn("private-password", str(caught.exception))

    def test_rate_limit_and_network_failures_have_safe_messages(self):
        self.http.request.return_value = response({"message": "internal rate bucket"}, 429)
        with self.assertRaises(AuthError) as caught:
            self.service.login("alice@example.com", "private-password")
        self.assertEqual(caught.exception.status, 429)
        self.assertNotIn("internal", str(caught.exception))
        for failure in [requests.Timeout("private-access-token"), requests.ConnectionError("private-password")]:
            self.http.request.side_effect = failure
            with self.subTest(failure=failure):
                with self.assertRaises(AuthError) as caught:
                    self.service.login("alice@example.com", "private-password")
                self.assertEqual(caught.exception.status, 503)
                self.assertNotIn("private-", str(caught.exception))

    def test_no_configuration_cannot_start_a_session_or_contact_provider(self):
        service = SupabaseAuthService("", "", self.http)
        self.assertFalse(service.configured)
        with self.assertRaises(AuthError) as caught:
            service.login("alice@example.com", "private-password")
        self.assertEqual(caught.exception.status, 503)
        self.http.request.assert_not_called()

    def test_redirect_response_is_rejected_even_if_its_body_looks_like_a_user(self):
        self.http.request.return_value = response(USER, status=302)
        with self.assertRaises(AuthError) as caught:
            self.service.get_user("private-access-token")
        self.assertEqual(caught.exception.status, 503)

    def test_public_user_excludes_tokens_and_tolerates_malformed_display_metadata(self):
        result = public_user({**USER, "access_token": "secret", "refresh_token": "secret", "user_metadata": ["bad"]})
        self.assertEqual(result, {"id": USER["id"], "email": USER["email"], "display_name": ""})
        self.assertEqual(public_user({**USER, "user_metadata": {"full_name": "A" * 200}})["display_name"], "A" * 120)


if __name__ == "__main__":
    unittest.main()
