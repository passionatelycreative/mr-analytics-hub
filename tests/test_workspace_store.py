"""Exercise the HTTP trust boundary without contacting a real Supabase project."""

import unittest
from unittest.mock import Mock

import requests

from workspace_store import (
    MAX_ANALYSIS_BYTES,
    MAX_CSV_BYTES,
    SupabaseWorkspaceStore,
    WorkspaceConfigurationError,
    WorkspaceConflict,
    WorkspaceLimitError,
    WorkspaceStoreError,
)


USER = "13ef7224-fae9-4630-b6d3-0897b1546321"
OTHER_USER = "ed991f51-a4ba-43b7-a621-1ad58e9b01ee"
WORKSPACE = "53ad1e4a-803a-4b43-9c5d-7fb56f70640e"
ANALYSIS = {"filename": "sales.csv", "rows": 2, "columns": 2, "preview": []}


def response(data=None, status=200):
    result = Mock(status_code=status)
    result.json.return_value = [] if data is None else data
    return result


class WorkspaceStoreTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.store = SupabaseWorkspaceStore("https://project.supabase.co/", "publishable-key", self.http)
        self.row = {"id": WORKSPACE, "owner_id": USER, "title": "Sales", "revision": 1,
                    "analysis": ANALYSIS, "state": {}, "notes": "", "csv_content": "a,b\n1,2\n"}

    def call(self):
        return self.http.request.call_args

    def test_saved_dataset_uses_user_token_and_includes_raw_csv(self):
        self.http.request.return_value = response([self.row], 201)
        saved = self.store.create(USER, "user-access-token", ANALYSIS, "a,b\n1,2\n", "Sales")
        self.assertEqual(saved["id"], WORKSPACE)
        args, kwargs = self.call()
        self.assertEqual(args, ("POST", "https://project.supabase.co/rest/v1/workspaces"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer user-access-token")
        self.assertEqual(kwargs["headers"]["apikey"], "publishable-key")
        self.assertEqual(kwargs["params"]["owner_id"], f"eq.{USER}")
        self.assertEqual(kwargs["json"]["owner_id"], USER)
        self.assertEqual(kwargs["json"]["csv_content"], "a,b\n1,2\n")
        self.assertFalse(kwargs["allow_redirects"])

    def test_list_metadata_does_not_download_all_reports_or_raw_datasets(self):
        self.http.request.return_value = response([{"id": WORKSPACE, "title": "Sales"}])
        self.assertEqual(len(self.store.list(USER, "token")), 1)
        parameters = self.call().kwargs["params"]
        self.assertEqual(parameters["owner_id"], f"eq.{USER}")
        self.assertNotIn("analysis", parameters["select"].split(","))
        self.assertNotIn("csv_content", parameters["select"].split(","))
        self.assertEqual(parameters["order"], "updated_at.desc,id.asc")

    def test_valid_long_filename_receives_a_shorter_default_workspace_title(self):
        self.http.request.return_value = response([self.row], 201)
        self.store.create(USER, "token", {**ANALYSIS, "filename": "a" * 200 + ".csv"})
        self.assertEqual(len(self.call().kwargs["json"]["title"]), 120)
        self.assertEqual(len(self.call().kwargs["json"]["filename"]), 204)

    def test_reopen_loads_saved_analysis_state_notes_and_question_history(self):
        row = {**self.row, "state": {"chart_column": "revenue"}, "notes": "Investigate May"}
        history = [{"id": "message", "question": "Total?", "answer": "30"}]
        self.http.request.side_effect = [response([row]), response(history)]
        saved = self.store.get(USER, "new-login-token", WORKSPACE)
        self.assertEqual(saved["analysis"], ANALYSIS)
        self.assertEqual(saved["state"], {"chart_column": "revenue"})
        self.assertEqual(saved["notes"], "Investigate May")
        self.assertEqual(saved["messages"], history)
        self.assertEqual(self.call().kwargs["params"]["workspace_id"], f"eq.{WORKSPACE}")
        self.assertEqual(self.call().kwargs["params"]["owner_id"], f"eq.{USER}")

    def test_nonexistent_or_other_account_workspace_does_not_load_messages(self):
        self.http.request.return_value = response([])
        self.assertIsNone(self.store.get(USER, "token", WORKSPACE))
        self.assertEqual(self.http.request.call_count, 1)
        self.assertEqual(self.call().kwargs["params"]["id"], f"eq.{WORKSPACE}")

    def test_unexpected_other_account_response_is_rejected(self):
        self.http.request.return_value = response([{**self.row, "owner_id": OTHER_USER}])
        with self.assertRaises(WorkspaceStoreError):
            self.store.get(USER, "token", WORKSPACE)
        self.assertEqual(self.http.request.call_count, 1)

    def test_two_open_tabs_cannot_silently_overwrite_each_other(self):
        self.http.request.return_value = response([])
        with self.assertRaises(WorkspaceConflict) as caught:
            self.store.update(USER, "token", WORKSPACE, {"notes": "new note"}, 2)
        self.assertEqual(caught.exception.status, 409)
        parameters = self.call().kwargs["params"]
        self.assertEqual(parameters["revision"], "eq.2")
        self.assertEqual(parameters["owner_id"], f"eq.{USER}")

    def test_update_preserves_metadata_and_uses_expected_revision(self):
        self.http.request.return_value = response([{**self.row, "revision": 2}])
        saved = self.store.update(USER, "token", WORKSPACE,
                                  {"title": "  New title  ", "state": {"view": "numeric"}}, 1)
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(self.call().kwargs["json"],
                         {"title": "New title", "state": {"view": "numeric"}})

    def test_mass_assignment_and_invalid_revisions_never_reach_database(self):
        for changes, revision in [({"owner_id": OTHER_USER}, 1), ({"analysis": {}}, 1),
                                  ({"revision": 99}, 1), ({}, 1), ({"notes": "x"}, True),
                                  ({"notes": "x"}, 0), ({"notes": "x"}, None)]:
            with self.subTest(changes=changes, revision=revision):
                with self.assertRaises(WorkspaceStoreError):
                    self.store.update(USER, "token", WORKSPACE, changes, revision)
        self.http.request.assert_not_called()

    def test_oversize_and_invalid_values_never_reach_database(self):
        cases = [
            lambda: self.store.create(USER, "token", ANALYSIS, "x" * (MAX_CSV_BYTES + 1)),
            lambda: self.store.create(USER, "token", {**ANALYSIS, "preview": "x" * MAX_ANALYSIS_BYTES}),
            lambda: self.store.create(USER, "token", {**ANALYSIS, "rows": True}),
            lambda: self.store.create(USER, "token", {**ANALYSIS, "columns": 101}),
            lambda: self.store.update(USER, "token", WORKSPACE, {"notes": "x" * 20_001}, 1),
            lambda: self.store.update(USER, "token", WORKSPACE, {"state": {"x": float("nan")}}, 1),
            lambda: self.store.update(USER, "token", WORKSPACE, {"state": []}, 1),
            lambda: self.store.update(USER, "token", WORKSPACE, {"title": "\x00"}, 1),
            lambda: self.store.update(USER, "token", WORKSPACE, {"title": "   "}, 1),
        ]
        for operation in cases:
            with self.subTest(operation=operation):
                with self.assertRaises(WorkspaceStoreError):
                    operation()
        self.http.request.assert_not_called()

    def test_uuid_validation_prevents_filter_injection(self):
        with self.assertRaises(WorkspaceStoreError):
            self.store.get(USER, "token", "x,owner_id.neq.null")
        with self.assertRaises(WorkspaceStoreError):
            self.store.list("x,owner_id.neq.null", "token")
        self.http.request.assert_not_called()

    def test_explicit_delete_is_owner_scoped_and_reports_missing_dataset(self):
        self.http.request.side_effect = [response([{"id": WORKSPACE}]), response([])]
        self.assertTrue(self.store.delete(USER, "token", WORKSPACE))
        self.assertFalse(self.store.delete(USER, "token", WORKSPACE))
        self.assertEqual(self.call().kwargs["params"]["owner_id"], f"eq.{USER}")

    def test_questions_are_append_only_and_owned_by_same_account(self):
        self.http.request.return_value = response([{"id": "message", "question": "Total?", "answer": "30"}], 201)
        saved = self.store.append_message(USER, "token", WORKSPACE, "Total?", "30")
        self.assertEqual(saved["answer"], "30")
        self.assertEqual(self.call().kwargs["json"],
                         {"owner_id": USER, "workspace_id": WORKSPACE, "question": "Total?", "answer": "30"})

    def test_quota_errors_are_actionable_without_leaking_database_details(self):
        limits = [("workspace_count_limit", "25-dataset"),
                  ("workspace_storage_limit", "50 MB"),
                  ("workspace_message_limit", "200-question")]
        for marker, expected in limits:
            with self.subTest(marker=marker):
                self.http.request.return_value = response({"code": "P0001", "message": marker}, 400)
                with self.assertRaises(WorkspaceLimitError) as caught:
                    self.store.list(USER, "token")
                self.assertIn(expected, str(caught.exception))

    def test_missing_schema_or_config_has_no_ephemeral_fallback(self):
        empty = SupabaseWorkspaceStore("", "", self.http)
        self.assertFalse(empty.configured)
        with self.assertRaises(WorkspaceConfigurationError):
            empty.list(USER, "token")
        self.http.request.assert_not_called()
        self.http.request.return_value = response({"code": "PGRST205", "message": "private schema detail"}, 404)
        with self.assertRaises(WorkspaceConfigurationError):
            self.store.list(USER, "token")

    def test_expired_session_and_transport_failure_are_sanitized(self):
        self.http.request.return_value = response({"message": "private JWT detail"}, 401)
        with self.assertRaises(WorkspaceStoreError) as caught:
            self.store.list(USER, "expired-token")
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn("private JWT", str(caught.exception))
        self.http.request.side_effect = requests.ConnectionError("https://secret-key.example")
        with self.assertRaises(WorkspaceStoreError) as caught:
            self.store.list(USER, "token")
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn("secret-key", str(caught.exception))

    def test_redirects_and_invalid_backend_payloads_are_not_treated_as_success(self):
        for result in [response([], 302), response({"error": "wrong envelope"}), response(["wrong row"])]:
            self.http.request.return_value = result
            with self.subTest(status=result.status_code):
                with self.assertRaises(WorkspaceStoreError):
                    self.store.list(USER, "token")


if __name__ == "__main__":
    unittest.main()
