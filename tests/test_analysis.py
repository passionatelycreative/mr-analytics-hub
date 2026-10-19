import io
import json
import os
import unittest
from unittest.mock import Mock, patch

import requests
from werkzeug.datastructures import FileStorage

from ai_service import AIUnavailableError, MAX_CONTEXT_CHARACTERS, answer_question, build_ai_context
from analytics import CSVValidationError, analyze_csv


def analyze(text):
    return analyze_csv(FileStorage(
        stream=io.BytesIO(text.encode("utf-8")), filename="dataset.csv", content_type="text/csv"
    ))


class AnalysisEngineTests(unittest.TestCase):
    def test_aggregates_and_histogram_use_all_rows_beyond_preview(self):
        result = analyze("value,group\n" + "\n".join(f"{index},repeated" for index in range(250)))
        self.assertEqual(len(result["preview"]), 100)
        self.assertEqual(result["rows"], 250)
        self.assertEqual(result["numeric_statistics"]["value"]["sum"], sum(range(250)))
        self.assertEqual(result["numeric_statistics"]["value"]["max"], 249)
        self.assertEqual(sum(result["numeric_histograms"]["value"]["counts"]), 250)
        self.assertEqual(result["categorical_statistics"]["group"]["top_values"][0]["count"], 250)

    def test_missing_duplicates_correlations_and_outliers_are_grounded(self):
        result = analyze("x,y,label\n1,2,a\n2,4,a\n2,4,a\n3,6,b\n4,8,b\n100,200,c\n,,c\n")
        self.assertEqual(result["duplicate_rows"], 1)
        self.assertEqual(result["total_missing_values"], 2)
        self.assertEqual(result["outliers"]["x"]["count"], 1)
        self.assertEqual(result["correlations"][0]["coefficient"], 1)
        self.assertEqual(result["correlations"][0]["pairs"], 6)
        self.assertEqual(result["categorical_statistics"]["label"]["unique_count"], 3)
        self.assertTrue(any("causation" in insight for insight in result["insights"]))

    def test_nonfinite_and_extreme_values_produce_strict_json(self):
        result = analyze("value,extreme\n1,1e308\ninf,1e308\n-inf,-1e308\n,1e308\n")
        json.dumps(result, allow_nan=False)
        self.assertEqual(result["invalid_numeric_values"], {"value": 2})
        self.assertEqual(result["missing_values"]["value"], 3)
        self.assertEqual(sum(result["numeric_histograms"]["extreme"]["counts"]), 4)
        self.assertAlmostEqual(result["numeric_statistics"]["extreme"]["mean"] / 1e307, 5)
        self.assertEqual(result["duplicate_rows"], 0)

    def test_rejects_ambiguous_headers_empty_data_and_binary_content(self):
        invalid = [
            ("a,a\n1,2\n", "duplicate"),
            (" a ,a\n1,2\n", "duplicate"),
            ("a,\n1,2\n", "non-empty"),
            ("a,b\n", "data row"),
            ("1,-2.5\n3,4\n", "header"),
            ("a,b\n1,2,3\n", "inconsistent"),
            ("a,b\n1,\x00\n", "null"),
        ]
        for content, message in invalid:
            with self.subTest(content=content):
                with self.assertRaisesRegex(CSVValidationError, message):
                    analyze(content)

    def test_quoted_fields_bom_and_unicode_preserve_data(self):
        result = analyze('\ufeffcity,note,value\nLagos,"quoted, comma",3\nAbuja,"two\nlines",5\n')
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["preview"][0]["note"], "quoted, comma")
        self.assertEqual(result["preview"][1]["note"], "two\nlines")

    def test_large_integer_digits_are_preserved_as_text_before_parsing(self):
        result = analyze("id,value\n9007199254740992,1\n9007199254740993,2\n,3\n")
        self.assertEqual(result["column_types"]["id"], "text")
        self.assertEqual(result["precision_preserved_columns"], ["id"])
        self.assertEqual(result["preview"][0]["id"], "9007199254740992")
        self.assertEqual(result["preview"][1]["id"], "9007199254740993")
        self.assertEqual(result["missing_values"]["id"], 1)
        self.assertEqual(result["categorical_statistics"]["id"]["unique_count"], 2)
        self.assertNotIn("id", result["numeric_histograms"])
        self.assertTrue(any("preserved as text" in insight for insight in result["insights"]))
        # Values with thousands of digits never pass through Python's int parser.
        enormous = "9" * 5000
        self.assertEqual(analyze("id\n" + enormous + "\n")["preview"][0]["id"], enormous)
        json.dumps(result, allow_nan=False)


class AIServiceTests(unittest.TestCase):
    def setUp(self):
        self.analysis = analyze("value,person\n1,private-person-one\n2,private-person-one\n3,private-person-two\n")

    def test_context_excludes_raw_rows_category_labels_and_filename(self):
        context = build_ai_context(self.analysis)
        serialized = json.dumps(context, allow_nan=False)
        self.assertNotIn("private-person", serialized)
        self.assertNotIn("dataset.csv", serialized)
        self.assertNotIn("preview", context)
        self.assertEqual(context["rows"], 3)
        self.assertEqual(context["column_profiles"]["value"]["statistics"]["sum"], 6)

    def test_context_is_bounded_even_for_wide_unicode_datasets(self):
        columns = [f"列{index}" + "長" * 180 for index in range(100)]
        wide = analyze(",".join(columns) + "\n" + ",".join("1" for _ in columns) + "\n")
        context = build_ai_context(wide)
        self.assertLessEqual(len(json.dumps(context, allow_nan=False)), MAX_CONTEXT_CHARACTERS)
        self.assertGreater(context["omitted_columns"], 0)

    @patch.dict(os.environ, {}, clear=True)
    @patch("ai_service.requests.post")
    def test_missing_model_or_key_never_calls_provider(self, post):
        with self.assertRaises(AIUnavailableError):
            answer_question(self.analysis, "What stands out?")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            with self.assertRaises(AIUnavailableError):
                answer_question(self.analysis, "What stands out?")
        post.assert_not_called()

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "configured-model"}, clear=True)
    @patch("ai_service.requests.post")
    def test_provider_receives_bounded_aggregates_and_storage_disabled(self, post):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {"status": "completed", "output": [{"content": [{"type": "output_text", "text": "The mean is 2."}]}]}
        answer = answer_question(self.analysis, "What is the mean?")
        self.assertEqual(answer, "The mean is 2.")
        payload = post.call_args.kwargs["json"]
        self.assertIs(payload["store"], False)
        self.assertEqual(payload["model"], "configured-model")
        self.assertNotIn("private-person", json.dumps(payload))
        self.assertNotIn("test-key", json.dumps(payload))
        self.assertEqual(post.call_args.kwargs["timeout"], (5, 45))

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "configured-model"}, clear=True)
    @patch("ai_service.requests.post")
    def test_provider_failures_do_not_expose_response_or_credentials(self, post):
        post.return_value = Mock(status_code=401, text="secret-provider-details")
        with self.assertRaises(AIUnavailableError) as error:
            answer_question(self.analysis, "Explain the data")
        self.assertNotIn("secret-provider-details", str(error.exception))
        self.assertNotIn("test-key", str(error.exception))
        post.side_effect = requests.Timeout("secret-provider-details")
        with self.assertRaisesRegex(AIUnavailableError, "too long"):
            answer_question(self.analysis, "Explain the data")

    def test_question_validation(self):
        for question in (None, "", "   ", "a" * 2001):
            with self.subTest(question=question):
                with self.assertRaises(ValueError):
                    answer_question(self.analysis, question)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "configured-model"}, clear=True)
    @patch("ai_service.requests.post")
    def test_partial_and_malformed_provider_responses_are_rejected(self, post):
        post.return_value = Mock(status_code=200)
        invalid = [
            {"status": "incomplete", "output": [{"content": [{"type": "output_text", "text": "Partial answer"}]}]},
            {"status": "failed", "error": {"message": "secret-provider-details"}},
            {"status": "completed", "output": [{"content": None}]},
            {"status": "completed", "output": []},
            ["unexpected"],
        ]
        for result in invalid:
            with self.subTest(result=result):
                post.return_value.json.return_value = result
                with self.assertRaises(AIUnavailableError) as error:
                    answer_question(self.analysis, "Explain this dataset")
                self.assertNotIn("secret-provider-details", str(error.exception))

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "OPENAI_MODEL": "configured-model"}, clear=True)
    @patch("ai_service.requests.post")
    def test_http_and_connection_failures_have_actionable_safe_errors(self, post):
        for status, expected in ((429, "usage limit"), (500, "could not complete"), (403, "configuration")):
            with self.subTest(status=status):
                post.return_value = Mock(status_code=status, text="secret-provider-details")
                with self.assertRaisesRegex(AIUnavailableError, expected) as error:
                    answer_question(self.analysis, "Explain the data")
                self.assertNotIn("secret-provider-details", str(error.exception))
        post.side_effect = requests.ConnectionError("secret-provider-details")
        with self.assertRaisesRegex(AIUnavailableError, "could not be reached") as error:
            answer_question(self.analysis, "Explain the data")
        self.assertNotIn("secret-provider-details", str(error.exception))


if __name__ == "__main__":
    unittest.main()
