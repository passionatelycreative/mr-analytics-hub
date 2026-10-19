"""Account and durable-workspace regressions without live provider calls."""
import copy
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import app as application
from auth_service import AuthError
from workspace_store import WorkspaceConflict, WorkspaceStoreError


ALICE_ID = "11111111-1111-4111-8111-111111111111"
BOB_ID = "22222222-2222-4222-8222-222222222222"
CSV = b"region,revenue\nNorth,10\nSouth,20\n"


class AccountProvider:
    """A provider account survives separate HTTP clients and sessions."""

    configured = True

    def __init__(self):
        self.users = {
            "alice@example.com": {"id": ALICE_ID, "email": "alice@example.com", "user_metadata": {"display_name": "Alice"}},
            "bob@example.com": {"id": BOB_ID, "email": "bob@example.com", "user_metadata": {"display_name": "Bob"}},
        }
        self.tokens = {}
        self.login_calls = []
        self.signup_calls = []
        self.logged_out = []

    def login(self, email, password):
        if not self.configured:
            raise AuthError("Account sign-in is not configured yet.", 503)
        self.login_calls.append((email, password))
        if email not in self.users or password != "correct-password":
            raise AuthError("Email or password is incorrect.", 401)
        token = "access-" + email
        self.tokens[token] = self.users[email]
        return {
            "access_token": token,
            "refresh_token": "refresh-" + email,
            "expires_in": 3600,
            "user": copy.deepcopy(self.users[email]),
        }

    def signup(self, email, password, display_name=""):
        self.signup_calls.append((email, password, display_name))
        return {"user": {"id": BOB_ID, "email": email}, "confirmation_required": True}

    def get_user(self, token):
        return copy.deepcopy(self.tokens.get(token))

    def refresh(self, refresh_token):
        return self.login(refresh_token.removeprefix("refresh-"), "correct-password")

    def logout(self, access_token):
        self.logged_out.append(access_token)
        self.tokens.pop(access_token, None)


class AccountFixture(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        config = patch.dict(application.app.config, {
            "TESTING": True,
            "SECRET_KEY": "account-tests-only",
            "SESSION_COOKIE_SECURE": False,
            "ANALYSIS_DB_PATH": str(Path(storage.name) / "reports.sqlite3"),
        })
        config.start()
        self.addCleanup(config.stop)
        environment = patch.dict(os.environ, {"PUBLIC_ORIGIN": "http://localhost", "COOKIE_SECURE": "false"})
        environment.start()
        self.addCleanup(environment.stop)
        self.provider = AccountProvider()
        provider_patch = patch("app.auth_service", return_value=self.provider)
        provider_patch.start()
        self.addCleanup(provider_patch.stop)
        self.client = application.app.test_client()

    def headers(self, client=None):
        response = (client or self.client).get("/api/auth/me")
        self.assertEqual(response.status_code, 200)
        return {"Origin": "http://localhost", "X-CSRF-Token": response.get_json()["csrf_token"]}

    def login(self, client=None, email="alice@example.com"):
        client = client or self.client
        response = client.post("/api/auth/login", json={"email": email, "password": "correct-password"}, headers=self.headers(client))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response

    def upload(self, client=None, **extra):
        client = client or self.client
        return client.post("/upload", data={"file": (io.BytesIO(CSV), "sales.csv", "text/csv"), **extra}, headers=self.headers(client))


class AccountTests(AccountFixture):
    def test_existing_account_signs_in_without_creating_a_new_account(self):
        before = self.client.get("/api/auth/me").get_json()
        self.assertIsNone(before["user"])
        self.assertTrue(before["configured"])
        self.assertTrue(before["csrf_token"])
        response = self.login()
        after = self.client.get("/api/auth/me").get_json()
        self.assertEqual(after["user"]["id"], ALICE_ID)
        self.assertEqual(after["user"]["email"], "alice@example.com")
        self.assertEqual(self.provider.signup_calls, [])
        self.assertNotIn("access-alice", response.get_data(as_text=True))
        self.assertNotIn("refresh-alice", response.get_data(as_text=True))
        auth_cookies = [value for value in response.headers.getlist("Set-Cookie") if value.startswith(("ma_access=", "ma_refresh="))]
        self.assertEqual(len(auth_cookies), 2)
        self.assertTrue(all("HttpOnly" in value and "SameSite=Lax" in value for value in auth_cookies))

    def test_signup_confirmation_does_not_create_a_false_authenticated_session(self):
        response = self.client.post("/api/auth/signup", json={
            "email": "new@example.com", "password": "correct-password", "display_name": "New Analyst",
        }, headers=self.headers())
        self.assertIn(response.status_code, (200, 201, 202))
        self.assertTrue(response.get_json()["confirmation_required"])
        self.assertIsNone(self.client.get("/api/auth/me").get_json()["user"])

    def test_invalid_password_leaves_customer_signed_out(self):
        response = self.client.post("/api/auth/login", json={"email": "alice@example.com", "password": "wrong-password"}, headers=self.headers())
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(self.client.get("/api/auth/me").get_json()["user"])

    def test_authentication_mutations_require_csrf_and_reject_other_origins(self):
        valid = self.headers()
        for headers in ({}, {"Origin": "http://localhost"}, {**valid, "X-CSRF-Token": "wrong"}, {**valid, "Origin": "https://attacker.example"}):
            with self.subTest(headers=headers):
                response = self.client.post("/api/auth/login", json={"email": "alice@example.com", "password": "correct-password"}, headers=headers)
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.provider.login_calls, [])

    def test_signout_clears_authentication_and_private_dashboard_requires_login(self):
        self.assertEqual(self.client.get("/platform.html").status_code, 302)
        self.login()
        with self.client.get("/platform.html") as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post("/api/auth/logout", headers=self.headers()).status_code, 200)
        self.assertIsNone(self.client.get("/api/auth/me").get_json()["user"])
        self.assertEqual(self.client.get("/platform.html").status_code, 302)
        self.assertEqual(self.provider.logged_out, ["access-alice@example.com"])

    def test_unconfigured_accounts_report_unavailability_without_fake_local_login(self):
        self.provider.configured = False
        payload = self.client.get("/api/auth/me").get_json()
        self.assertFalse(payload["configured"])
        self.assertIsNone(payload["user"])
        response = self.client.post("/api/auth/login", json={"email": "alice@example.com", "password": "correct-password"}, headers=self.headers())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.provider.login_calls, [])

    def test_expired_access_token_refreshes_the_same_customer_account(self):
        self.login()
        self.provider.tokens.clear()
        response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["user"]["id"], ALICE_ID)
        self.assertTrue(any(value.startswith("ma_refresh=") for value in response.headers.getlist("Set-Cookie")))

    def test_expired_refresh_session_can_open_the_signin_form_and_login_again(self):
        self.client.set_cookie("ma_access", "expired-access")
        self.client.set_cookie("ma_refresh", "expired-refresh")
        response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["user"])
        self.assertTrue(response.get_json()["csrf_token"])
        self.assertIsNone(self.client.get_cookie("ma_access"))
        self.assertIsNone(self.client.get_cookie("ma_refresh"))
        self.login()
        self.assertEqual(self.client.get("/api/auth/me").get_json()["user"]["id"], ALICE_ID)


class MemoryWorkspaces:
    """A shared provider substitute, independent of Flask's session and SQLite."""

    configured = True

    def __init__(self):
        self.records = {}
        self.create_owners = []

    def create(self, user_id, token, analysis, csv_content=None, title=None):
        self.create_owners.append((user_id, token))
        identifier = str(uuid4())
        record = {
            "id": identifier, "owner_id": user_id, "title": title or analysis["filename"],
            "filename": analysis["filename"], "analysis": copy.deepcopy(analysis),
            "csv_content": csv_content, "row_count": analysis["rows"], "column_count": analysis["columns"],
            "notes": "", "state": {}, "revision": 1, "messages": [],
            "created_at": "2026-10-19T12:00:00+00:00", "updated_at": "2026-10-19T12:00:00+00:00",
        }
        self.records[identifier] = record
        return copy.deepcopy(record)

    def list(self, user_id, token):
        fields = {"id", "title", "filename", "row_count", "column_count", "state", "revision", "created_at", "updated_at"}
        return [{key: copy.deepcopy(value) for key, value in record.items() if key in fields}
                for record in self.records.values() if record["owner_id"] == user_id]

    def get(self, user_id, token, workspace_id):
        record = self.records.get(workspace_id)
        return copy.deepcopy(record) if record and record["owner_id"] == user_id else None

    def update(self, user_id, token, workspace_id, changes, expected_revision):
        record = self.records.get(workspace_id)
        if not record or record["owner_id"] != user_id or record["revision"] != expected_revision:
            raise WorkspaceConflict()
        if not changes or set(changes) - {"title", "notes", "state"}:
            raise WorkspaceStoreError("Invalid update.", 400, "invalid_workspace")
        record.update(copy.deepcopy(changes))
        record["revision"] += 1
        return copy.deepcopy(record)

    def delete(self, user_id, token, workspace_id):
        if not self.get(user_id, token, workspace_id):
            return False
        del self.records[workspace_id]
        return True

    def append_message(self, user_id, token, workspace_id, question, answer):
        if not self.get(user_id, token, workspace_id):
            raise WorkspaceStoreError("Workspace unavailable.", 404, "workspace_missing")
        message = {"id": str(uuid4()), "question": question, "answer": answer, "created_at": "2026-10-19T12:00:00+00:00"}
        self.records[workspace_id]["messages"].append(message)
        return copy.deepcopy(message)


class SavedWorkspaceTests(AccountFixture):
    def setUp(self):
        super().setUp()
        self.store = MemoryWorkspaces()
        store_patch = patch("app.workspace_store", return_value=self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)

    def saved_upload(self, client=None, **extra):
        response = self.upload(client, **extra)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload["persistent"])
        self.assertTrue(payload["analysis_id"].startswith("ws_"))
        return payload

    def test_saved_report_notes_and_chart_survive_signout_and_a_new_browser_session(self):
        self.login()
        uploaded = self.saved_upload()
        identifier = uploaded["workspace"]["id"]
        route = "/api/workspaces/" + identifier
        changes = {"title": "October sales", "notes": "Check the South region next.", "state": {"numericColumn": "revenue"}, "expected_revision": uploaded["workspace"]["revision"]}
        response = self.client.patch(route, json=changes, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.client.post("/api/auth/logout", headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.get(route).status_code, 401)

        returning_browser = application.app.test_client()
        self.login(returning_browser)
        workspaces = returning_browser.get("/api/workspaces").get_json()["workspaces"]
        self.assertEqual([entry["id"] for entry in workspaces], [identifier])
        restored = returning_browser.get(route).get_json()["workspace"]
        self.assertEqual(restored["title"], changes["title"])
        self.assertEqual(restored["notes"], changes["notes"])
        self.assertEqual(restored["state"], changes["state"])
        self.assertEqual(restored["analysis"], uploaded["analysis"])
        self.assertEqual(returning_browser.get("/api/analysis/" + uploaded["analysis_id"]).get_json()["analysis"], uploaded["analysis"])
        download = returning_browser.get(route + "/download")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.data, CSV)

    def test_account_owner_comes_from_verified_identity_and_not_upload_fields(self):
        self.login()
        uploaded = self.saved_upload(owner_id=BOB_ID, user_id=BOB_ID)
        self.assertEqual(self.store.records[uploaded["workspace"]["id"]]["owner_id"], ALICE_ID)
        self.assertEqual(self.store.create_owners, [(ALICE_ID, "access-alice@example.com")])

    def test_customer_can_save_an_unexpired_guest_report_after_signing_in(self):
        guest_response = self.client.post("/upload", data={"file": (io.BytesIO(CSV), "sales.csv")})
        self.assertEqual(guest_response.status_code, 200)
        guest = guest_response.get_json()
        self.assertFalse(guest["persistent"])
        self.login()
        response = self.client.post("/api/workspaces", json={"analysis_id": guest["analysis_id"]}, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        saved = response.get_json()
        self.assertTrue(saved["persistent"])
        self.assertEqual(saved["analysis"], guest["analysis"])
        self.assertFalse(saved["workspace"]["has_original_csv"])
        self.assertEqual(self.client.get("/api/analysis/" + guest["analysis_id"]).status_code, 404)
        self.assertEqual(self.client.get("/api/workspaces/" + saved["workspace"]["id"] + "/download").status_code, 404)

    def test_expired_account_session_never_silently_uploads_as_a_guest(self):
        self.client.set_cookie("ma_access", "expired-access")
        self.client.set_cookie("ma_refresh", "expired-refresh")
        response = self.client.post("/upload", data={"file": (io.BytesIO(CSV), "sales.csv")})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("analysis_id", response.get_json())
        self.assertEqual(self.store.records, {})

    def test_other_accounts_cannot_list_read_edit_delete_or_download_the_workspace(self):
        self.login()
        uploaded = self.saved_upload()
        route = "/api/workspaces/" + uploaded["workspace"]["id"]
        other_browser = application.app.test_client()
        self.login(other_browser, "bob@example.com")
        self.assertEqual(other_browser.get("/api/workspaces").get_json()["workspaces"], [])
        self.assertEqual(other_browser.get(route).status_code, 404)
        self.assertEqual(other_browser.get(route + "/download").status_code, 404)
        self.assertEqual(other_browser.get("/api/analysis/" + uploaded["analysis_id"]).status_code, 404)
        self.assertIn(other_browser.patch(route, json={"notes": "Stolen", "expected_revision": 1}, headers=self.headers(other_browser)).status_code, (404, 409))
        self.assertEqual(other_browser.delete(route, headers=self.headers(other_browser)).status_code, 404)
        self.assertEqual(self.client.get(route).status_code, 200)
        self.assertEqual(self.store.records[uploaded["workspace"]["id"]]["notes"], "")

    def test_saved_workspace_endpoints_require_signin(self):
        self.login()
        uploaded = self.saved_upload()
        route = "/api/workspaces/" + uploaded["workspace"]["id"]
        stranger = application.app.test_client()
        headers = self.headers(stranger)
        for method, path, values in (
            ("get", "/api/workspaces", {}),
            ("get", route, {}),
            ("get", route + "/download", {}),
            ("patch", route, {"json": {"notes": "Changed", "expected_revision": 1}}),
            ("delete", route, {}),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(getattr(stranger, method)(path, headers=headers, **values).status_code, 401)

    def test_stale_tab_cannot_overwrite_more_recent_notes(self):
        self.login()
        uploaded = self.saved_upload()
        route = "/api/workspaces/" + uploaded["workspace"]["id"]
        headers = self.headers()
        first = self.client.patch(route, json={"notes": "Saved first", "expected_revision": 1}, headers=headers)
        self.assertEqual(first.status_code, 200)
        stale = self.client.patch(route, json={"notes": "Old tab", "expected_revision": 1}, headers=headers)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.client.get(route).get_json()["workspace"]["notes"], "Saved first")

    def test_signedin_upload_edit_and_delete_require_csrf(self):
        self.login()
        uploaded = self.saved_upload()
        route = "/api/workspaces/" + uploaded["workspace"]["id"]
        response = self.client.post("/upload", data={"file": (io.BytesIO(CSV), "sales.csv")})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.patch(route, json={"notes": "Changed", "expected_revision": 1}).status_code, 403)
        self.assertEqual(self.client.delete(route).status_code, 403)
        self.assertEqual(len(self.store.records), 1)

    def test_cloud_failure_returns_error_without_downgrading_to_temporary_storage(self):
        self.login()
        failure = WorkspaceStoreError("Account workspaces are not configured yet.", 503, "workspace_not_configured")
        with patch.object(self.store, "create", side_effect=failure):
            response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertNotEqual(response.get_json().get("status"), "success")
        self.assertFalse(response.get_json().get("persistent", False))
        self.assertNotIn("analysis_id", response.get_json())
        self.assertEqual(self.store.records, {})

    def test_workspace_deletion_removes_saved_report_and_download(self):
        self.login()
        uploaded = self.saved_upload()
        route = "/api/workspaces/" + uploaded["workspace"]["id"]
        response = self.client.delete(route, headers=self.headers())
        self.assertIn(response.status_code, (200, 204))
        self.assertEqual(self.client.get(route).status_code, 404)
        self.assertEqual(self.client.get(route + "/download").status_code, 404)
        self.assertEqual(self.client.get("/api/analysis/" + uploaded["analysis_id"]).status_code, 404)

    def test_paid_ai_answer_is_restored_with_its_saved_workspace(self):
        self.login()
        uploaded = self.saved_upload()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "test-model", "ANALYST_ACCESS_TOKEN": "test-code"}), patch("app.answer_question", return_value="Total revenue is 30."):
            response = self.client.post("/api/analysis/" + uploaded["analysis_id"] + "/question", json={"question": "What is total revenue?", "consent": True}, headers={**self.headers(), "X-Analyst-Token": "test-code"})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        returning_browser = application.app.test_client()
        self.login(returning_browser)
        restored = returning_browser.get("/api/workspaces/" + uploaded["workspace"]["id"]).get_json()["workspace"]
        self.assertEqual(restored["messages"][0]["question"], "What is total revenue?")
        self.assertEqual(restored["messages"][0]["answer"], "Total revenue is 30.")


if __name__ == "__main__":
    unittest.main()
