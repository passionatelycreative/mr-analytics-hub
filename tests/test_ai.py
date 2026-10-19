import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app


class AnalysisAndAITests(unittest.TestCase):
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.storage.cleanup)
        config = patch.dict(app.config, {
            "TESTING": True,
            "SECRET_KEY": "test-session-secret",
            "ANALYSIS_DB_PATH": str(Path(self.storage.name) / "analysis.sqlite3"),
        })
        config.start()
        self.addCleanup(config.stop)
        environment = patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-provider-key",
            "OPENAI_MODEL": "test-model",
            "ANALYST_ACCESS_TOKEN": "test-analyst-token",
            "PUBLIC_ORIGIN": "",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.client = app.test_client()
        provider_patch = patch("app.answer_question", return_value="The total revenue is 30.")
        self.provider = provider_patch.start()
        self.addCleanup(provider_patch.stop)

    def upload(self, client=None):
        response = (client or self.client).post(
            "/upload",
            data={"file": (io.BytesIO(b"region,revenue\nNorth,10\nSouth,20\n"), "sales.csv", "text/csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def ask(self, analysis_id, payload=None, headers=None, client=None):
        return (client or self.client).post(
            f"/api/analysis/{analysis_id}/question",
            json={"question": "What is total revenue?", "consent": True} if payload is None else payload,
            headers={"Origin": "http://localhost", "X-Analyst-Token": "test-analyst-token"} if headers is None else headers,
        )

    def test_upload_report_can_be_reopened_only_by_its_session(self):
        uploaded = self.upload()
        analysis_id = uploaded["analysis_id"]
        self.assertIsInstance(analysis_id, str)
        self.assertGreaterEqual(len(analysis_id), 24)
        response = self.client.get(f"/api/analysis/{analysis_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["analysis"], uploaded["analysis"])
        self.assertEqual(app.test_client().get(f"/api/analysis/{analysis_id}").status_code, 404)
        self.provider.assert_not_called()

    def test_public_origin_supports_https_reverse_proxy(self):
        report_id = self.upload()["analysis_id"]
        with patch.dict(os.environ, {"PUBLIC_ORIGIN": "https://analytics.example.com"}):
            response = self.ask(report_id, headers={"Origin": "https://analytics.example.com", "X-Analyst-Token": "test-analyst-token"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.ask(report_id).status_code, 403)

    def test_malformed_origin_is_rejected_without_provider_call(self):
        report_id = self.upload()["analysis_id"]
        response = self.ask(report_id, headers={"Origin": "http://[", "X-Analyst-Token": "test-analyst-token"})
        self.assertEqual(response.status_code, 403)
        self.provider.assert_not_called()

    def test_provider_failure_has_sanitized_unavailable_response(self):
        from ai_service import AIUnavailableError
        report_id = self.upload()["analysis_id"]
        self.provider.side_effect = AIUnavailableError("secret-provider-detail")
        response = self.ask(report_id)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret-provider-detail", response.get_data(as_text=True))

    def test_report_capacity_evicts_oldest_report(self):
        with patch("report_store.MAX_REPORTS", 2):
            first = self.upload()["analysis_id"]
            second = self.upload()["analysis_id"]
            third = self.upload()["analysis_id"]
        self.assertEqual(self.client.get(f"/api/analysis/{first}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/analysis/{second}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/analysis/{third}").status_code, 200)

    def test_report_deletion_checks_ownership_and_removes_report(self):
        analysis_id = self.upload()["analysis_id"]
        route = f"/api/analysis/{analysis_id}"
        self.assertEqual(app.test_client().delete(route).status_code, 404)
        self.assertEqual(self.client.get(route).status_code, 200)
        self.assertIn(self.client.delete(route).status_code, (200, 204))
        self.assertEqual(self.client.get(route).status_code, 404)
        self.assertEqual(self.ask(analysis_id).status_code, 404)
        self.provider.assert_not_called()

    def test_expired_report_cannot_be_read_or_questioned(self):
        analysis_id = self.upload()["analysis_id"]
        later = time.time() + 3601
        with patch("report_store.time.time", return_value=later):
            self.assertEqual(self.client.get(f"/api/analysis/{analysis_id}").status_code, 404)
            self.assertEqual(self.ask(analysis_id).status_code, 404)
        self.provider.assert_not_called()

    def test_csv_analysis_remains_available_without_ai_configuration(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "OPENAI_MODEL": "", "ANALYST_ACCESS_TOKEN": ""}):
            status = self.client.get("/api/ai-status")
            self.assertEqual(status.status_code, 200)
            self.assertIs(status.get_json()["enabled"], False)
            analysis_id = self.upload()["analysis_id"]
            self.assertEqual(self.ask(analysis_id).status_code, 503)
        self.provider.assert_not_called()

    def test_ai_status_requires_key_model_and_access_token_without_exposing_them(self):
        response = self.client.get("/api/ai-status")
        self.assertIs(response.get_json()["enabled"], True)
        self.assertNotIn("test-provider-key", response.get_data(as_text=True))
        self.assertNotIn("test-analyst-token", response.get_data(as_text=True))
        for setting in ("OPENAI_API_KEY", "OPENAI_MODEL", "ANALYST_ACCESS_TOKEN"):
            with self.subTest(setting=setting), patch.dict(os.environ, {setting: ""}):
                self.assertIs(self.client.get("/api/ai-status").get_json()["enabled"], False)

    def test_authorized_question_returns_answer_and_sends_saved_report(self):
        uploaded = self.upload()
        response = self.ask(uploaded["analysis_id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["answer"], "The total revenue is 30.")
        self.provider.assert_called_once()
        arguments = str(self.provider.call_args)
        self.assertIn("revenue", arguments)
        self.assertIn("What is total revenue?", arguments)

    def test_bearer_access_token_is_supported(self):
        analysis_id = self.upload()["analysis_id"]
        response = self.ask(analysis_id, headers={
            "Origin": "http://localhost", "Authorization": "Bearer test-analyst-token",
        })
        self.assertEqual(response.status_code, 200)
        self.provider.assert_called_once()

    def test_invalid_or_missing_access_token_never_calls_provider(self):
        analysis_id = self.upload()["analysis_id"]
        for headers in (
            {"Origin": "http://localhost"},
            {"Origin": "http://localhost", "X-Analyst-Token": "wrong"},
            {"Origin": "http://localhost", "Authorization": "Bearer wrong"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.ask(analysis_id, headers=headers).status_code, 401)
        self.provider.assert_not_called()

    def test_cross_origin_and_missing_origin_never_call_provider(self):
        analysis_id = self.upload()["analysis_id"]
        for headers in (
            {"X-Analyst-Token": "test-analyst-token"},
            {"Origin": "https://other.example", "X-Analyst-Token": "test-analyst-token"},
            {"Origin": "null", "X-Analyst-Token": "test-analyst-token"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.ask(analysis_id, headers=headers).status_code, 403)
        self.provider.assert_not_called()

    def test_consent_must_be_explicit_boolean_true(self):
        analysis_id = self.upload()["analysis_id"]
        for consent in (None, False, "true", 1):
            with self.subTest(consent=consent):
                response = self.ask(analysis_id, {"question": "Summarize this report", "consent": consent})
                self.assertEqual(response.status_code, 400)
        self.provider.assert_not_called()

    def test_question_must_be_nonempty_text_within_limit(self):
        analysis_id = self.upload()["analysis_id"]
        for question in (None, "", "   ", 42, [], "x" * 2001):
            with self.subTest(question=question):
                response = self.ask(analysis_id, {"question": question, "consent": True})
                self.assertEqual(response.status_code, 400)
        self.provider.assert_not_called()

    def test_other_sessions_cannot_question_report_even_with_access_token(self):
        analysis_id = self.upload()["analysis_id"]
        self.assertEqual(self.ask(analysis_id, client=app.test_client()).status_code, 404)
        self.provider.assert_not_called()

    def test_session_question_limit_stops_paid_calls(self):
        analysis_id = self.upload()["analysis_id"]
        for _ in range(10):
            self.assertEqual(self.ask(analysis_id).status_code, 200)
        self.assertEqual(self.ask(analysis_id).status_code, 429)
        self.assertEqual(self.provider.call_count, 10)

    def test_global_question_limit_applies_across_sessions(self):
        for _ in range(10):
            client = app.test_client()
            analysis_id = self.upload(client)["analysis_id"]
            for _ in range(10):
                self.assertEqual(self.ask(analysis_id, client=client).status_code, 200)
        another_client = app.test_client()
        analysis_id = self.upload(another_client)["analysis_id"]
        response = self.ask(analysis_id, client=another_client)
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)
        self.assertEqual(self.provider.call_count, 100)


if __name__ == "__main__":
    unittest.main()
