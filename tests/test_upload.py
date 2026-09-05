import io
import unittest

from werkzeug.datastructures import FileStorage

from app import MAX_CSV_COLUMNS, MAX_CSV_ROWS, MAX_UPLOAD_BYTES, analyze_csv, app


class UploadTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def upload(self, content, filename="data.csv", content_type="text/csv"):
        return self.client.post(
            "/upload",
            data={"file": (io.BytesIO(content), filename, content_type)},
            content_type="multipart/form-data",
        )

    def test_analyze_csv_returns_quality_schema_statistics_and_preview(self):
        content = b"name,age,salary\nAlice,25,50000\nAlice,25,50000\nBob,,62000\n"
        file_storage = FileStorage(
            stream=io.BytesIO(content), filename="people.csv", content_type="text/csv"
        )

        result = analyze_csv(file_storage)

        self.assertEqual(result["rows"], 3)
        self.assertEqual(result["columns"], 3)
        self.assertEqual(result["column_names"], ["name", "age", "salary"])
        self.assertEqual(result["column_types"]["name"], "text")
        self.assertEqual(result["column_types"]["age"], "number")
        self.assertEqual(result["column_types"]["salary"], "integer")
        self.assertEqual(result["missing_values"], {"name": 0, "age": 1, "salary": 0})
        self.assertEqual(result["duplicate_rows"], 1)
        self.assertEqual(result["numeric_statistics"]["salary"]["mean"], 54000)
        self.assertEqual(len(result["preview"]), 3)

    def test_upload_endpoint_returns_analysis(self):
        response = self.upload(b"name,value\nalpha,1\nbeta,2\n")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["analysis"]["rows"], 2)
        self.assertEqual(payload["analysis"]["column_names"], ["name", "value"])

    def test_upload_endpoint_rejects_invalid_csv(self):
        invalid_inputs = [
            (b"", "empty.csv", "empty"),
            (b"1,2\n3,4\n", "missing.csv", "header"),
            (b"name,value\nalpha,\xff\n", "encoding.csv", "utf-8"),
            (b"name,value\nalpha,1,unexpected\n", "malformed.csv", "malformed"),
        ]
        for content, filename, expected in invalid_inputs:
            with self.subTest(filename=filename):
                response = self.upload(content, filename)
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected, response.get_json()["message"].lower())

    def test_upload_endpoint_rejects_non_csv_and_missing_file(self):
        response = self.upload(b"name\nvalue\n", "data.xlsx")
        self.assertEqual(response.status_code, 400)
        self.assertIn("only csv", response.get_json()["message"].lower())

        response = self.client.post("/upload")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "No file uploaded")

    def test_analyze_csv_caps_preview_at_100_rows(self):
        content = ("value\n" + "\n".join(str(value) for value in range(101))).encode()
        file_storage = FileStorage(
            stream=io.BytesIO(content), filename="many.csv", content_type="text/csv"
        )

        result = analyze_csv(file_storage)

        self.assertEqual(result["rows"], 101)
        self.assertEqual(len(result["preview"]), 100)

    def test_analyze_csv_rejects_size_and_shape_limits(self):
        large_file = FileStorage(
            stream=io.BytesIO(b"a\n" + b"x" * MAX_UPLOAD_BYTES),
            filename="large.csv",
            content_type="text/csv",
        )
        with self.assertRaisesRegex(ValueError, "too large"):
            analyze_csv(large_file)

        headers = ",".join(f"column{index}" for index in range(MAX_CSV_COLUMNS + 1))
        wide_file = FileStorage(
            stream=io.BytesIO((headers + "\n").encode()),
            filename="wide.csv",
            content_type="text/csv",
        )
        with self.assertRaisesRegex(ValueError, "too many columns"):
            analyze_csv(wide_file)

        rows = "value\n" + "\n".join("1" for _ in range(MAX_CSV_ROWS + 1))
        row_file = FileStorage(
            stream=io.BytesIO(rows.encode()), filename="rows.csv", content_type="text/csv"
        )
        with self.assertRaisesRegex(ValueError, "too many rows"):
            analyze_csv(row_file)


if __name__ == "__main__":
    unittest.main()
