import unittest

from app import app


class PageRouteTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def test_all_feature_pages_have_routes(self):
        pages = (
            "/",
            "/login.html",
            "/platform.html",
            "/banking.html",
            "/crypto.html",
            "/crypto-portfolio.html",
            "/dashboard.html",
            "/research.html",
            "/portfolio.html",
            "/transactions.html",
            "/accounts.html",
            "/deposit.html",
            "/withdrawal.html",
            "/transfer.html",
            "/customer-care.html",
            "/support-console.html",
            "/ai-assistant.html",
            "/market-research.html",
            "/investment-research.html",
        )
        for path in pages:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_hub_links_to_real_feature_destinations(self):
        response = self.client.get("/platform.html")
        page = response.get_data(as_text=True)

        self.assertIn('href="banking.html"', page)
        self.assertIn('href="dashboard.html"', page)
        self.assertIn('href="research.html"', page)
        self.assertIn('href="ai-assistant.html"', page)
        self.assertIn('href="crypto.html"', page)
        self.assertIn('href="customer-care.html"', page)

    def test_crypto_has_a_dedicated_workspace(self):
        crypto_page = self.client.get("/crypto.html").get_data(as_text=True)
        banking_page = self.client.get("/banking.html").get_data(as_text=True)

        self.assertIn("Crypto Analytics", crypto_page)
        self.assertIn('fetch("/api/crypto"', crypto_page)
        self.assertIn("BTC, ETH, SOL, ADA, XRP", crypto_page)
        self.assertIn('href="crypto.html"', banking_page)
        self.assertNotIn("updateCryptoMarket", banking_page)

    def test_banking_actions_have_dedicated_destinations(self):
        banking_page = self.client.get("/banking.html").get_data(as_text=True)

        for destination in (
            "deposit.html",
            "withdrawal.html",
            "transfer.html",
            "transactions.html",
            "portfolio.html",
            "accounts.html",
            "crypto.html",
            "customer-care.html",
        ):
            with self.subTest(destination=destination):
                self.assertIn(f'href="{destination}"', banking_page)

    def test_empty_pages_are_explicit_about_unavailable_data(self):
        expected_content = {
            "/portfolio.html": "No portfolio data yet.",
            "/transactions.html": "No transactions yet.",
            "/accounts.html": "Account management is not available yet.",
            "/deposit.html": "No deposit activity yet.",
            "/withdrawal.html": "No withdrawal activity yet.",
            "/transfer.html": "No transfer activity yet.",
            "/crypto-portfolio.html": "No portfolio data yet.",
            "/customer-care.html": "No conversations yet.",
            "/support-console.html": "No support requests yet.",
            "/ai-assistant.html": "not available yet.",
        }
        for path, content in expected_content.items():
            with self.subTest(path=path):
                self.assertIn(content, self.client.get(path).get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
