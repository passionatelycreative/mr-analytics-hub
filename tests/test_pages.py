import unittest
from html.parser import HTMLParser

from app import app


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.destinations = []

    def handle_starttag(self, tag, attributes):
        if tag == "a":
            self.destinations.extend(value for key, value in attributes if key == "href")


class PageRouteTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def test_analytics_and_research_pages_load(self):
        pages = (
            "/", "/index.html", "/platform.html", "/dashboard.html",
            "/research.html", "/ai-assistant.html", "/market-research.html",
            "/investment-research.html", "/research-planning.html", "/login.html", "/signup.html",
        )
        for path in pages:
            with self.subTest(path=path):
                with self.client.get(path, follow_redirects=True) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("text/html", response.content_type)

    def test_navigation_keeps_visitors_in_analytics_and_research(self):
        removed_destinations = {
            "banking.html", "crypto.html", "crypto-portfolio.html", "accounts.html",
            "portfolio.html", "transactions.html", "deposit.html", "withdrawal.html",
            "transfer.html", "customer-care.html", "support-console.html",
        }
        for path in ("/", "/platform.html", "/dashboard.html", "/research.html", "/ai-assistant.html"):
            with self.subTest(path=path):
                with self.client.get(path, follow_redirects=True) as response:
                    page = response.get_data(as_text=True)
                links = LinkParser()
                links.feed(page)
                for destination in links.destinations:
                    self.assertNotIn(destination.lstrip("/").split("?")[0], removed_destinations)

    def test_removed_financial_pages_and_apis_are_unavailable(self):
        paths = (
            "/banking.html", "/crypto.html", "/crypto-portfolio.html", "/accounts.html",
            "/portfolio.html", "/transactions.html", "/deposit.html", "/withdrawal.html",
            "/transfer.html", "/customer-care.html", "/support-console.html",
            "/api/account", "/api/transactions", "/api/crypto",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertIn(self.client.get(path).status_code, (404, 410))
        self.assertIn(self.client.post("/register", json={"email": "test@example.com"}).status_code, (404, 410))

    def test_health_endpoint_does_not_require_external_services(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.get_json(), dict)


if __name__ == "__main__":
    unittest.main()
