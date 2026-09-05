import unittest
from unittest.mock import Mock, patch

from app import app
from crypto_service import clear_cache, get_crypto_market_data


class CryptoTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()
        clear_cache()

    def tearDown(self):
        clear_cache()

    @staticmethod
    def provider_rows():
        return [
            {
                "id": coin_id,
                "current_price": price,
                "price_change_percentage_24h": change,
                "total_volume": volume,
                "market_cap": market_cap,
                "last_updated": "2026-09-02T12:00:00.000Z",
            }
            for coin_id, price, change, volume, market_cap in [
                ("bitcoin", 100000, 1.2, 30000000000, 2000000000000),
                ("ethereum", 4000, -0.5, 15000000000, 500000000000),
                ("solana", 200, 2.4, 3000000000, 90000000000),
                ("cardano", 1, 0.1, 500000000, 35000000000),
                ("ripple", 2, -1.1, 1000000000, 100000000000),
            ]
        ]

    def mocked_response(self, rows=None):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = self.provider_rows() if rows is None else rows
        return response

    @patch("crypto_service.requests.get")
    def test_api_returns_normalized_multiple_assets(self, mock_get):
        mock_get.return_value = self.mocked_response()

        response = self.client.get("/api/crypto")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["source"], "coingecko")
        self.assertEqual(len(payload["assets"]), 5)
        self.assertEqual(
            [asset["symbol"] for asset in payload["assets"]],
            ["BTC", "ETH", "SOL", "ADA", "XRP"],
        )
        self.assertEqual(
            set(payload["assets"][0]),
            {"symbol", "price_usd", "change_24h_percent", "volume_24h_usd", "market_cap_usd", "updated_at"},
        )
        self.assertEqual(payload["assets"][0]["price_usd"], 100000.0)
        self.assertEqual(payload["assets"][0]["updated_at"], "2026-09-02T12:00:00.000Z")

    @patch("crypto_service.requests.get")
    def test_cache_avoids_repeated_provider_calls(self, mock_get):
        mock_get.return_value = self.mocked_response()

        first = get_crypto_market_data()
        second = get_crypto_market_data()

        self.assertEqual(first, second)
        mock_get.assert_called_once()

    @patch("crypto_service.requests.get")
    def test_provider_failure_returns_unavailable_without_cache(self, mock_get):
        mock_get.side_effect = RuntimeError("unexpected provider failure")

        response = self.client.get("/api/crypto")

        self.assertEqual(response.status_code, 503)
        payload = response.get_json()
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["assets"], [])
        self.assertNotIn("unexpected provider failure", payload["message"])

    @patch("crypto_service.requests.get")
    def test_provider_failure_returns_stale_cached_data(self, mock_get):
        mock_get.return_value = self.mocked_response()
        with patch("crypto_service.time.monotonic", side_effect=[100.0, 100.0, 105.0]):
            fresh_response = self.client.get("/api/crypto")
            self.assertEqual(fresh_response.status_code, 200)
            mock_get.side_effect = RuntimeError("provider offline")
            response = self.client.get("/api/crypto")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "stale")
        self.assertEqual(len(payload["assets"]), 5)
        self.assertIn("last successful update", payload["message"].lower())

    @patch("crypto_service.requests.get")
    def test_missing_market_data_is_unavailable(self, mock_get):
        mock_get.return_value = self.mocked_response(self.provider_rows()[:-1])

        response = self.client.get("/api/crypto")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
