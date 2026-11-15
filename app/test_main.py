import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.main import app


class CoinGeckoTests(unittest.TestCase):
    def test_routes_use_demo_key_and_return_upstream_json(self):
        requests = []

        def handle(request):
            requests.append(request)
            if request.url.path == "/api/v3/simple/price":
                return httpx.Response(200, json={"bitcoin": {"usd": 42}})
            return httpx.Response(200, json=[{"id": "bitcoin"}])

        client_class = httpx.AsyncClient
        transport = httpx.MockTransport(handle)
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text("COINGECKO_API_KEY=test-key\n")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("app.main.__file__", str(Path(directory, "main.py"))),
                patch(
                    "app.main.httpx.AsyncClient",
                    side_effect=lambda **kwargs: client_class(
                        transport=transport, **kwargs
                    ),
                ),
                TestClient(app) as client,
            ):
                self.assertEqual(client.get("/health").json(), {"status": "ok"})
                index = client.get("/")
                self.assertEqual(index.status_code, 200)
                self.assertIn("text/html", index.headers["content-type"])
                self.assertIn("Coin prices", index.text)
                response = client.get("/price", params={"coin": "bitcoin"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"bitcoin": {"usd": 42}})
                self.assertEqual(client.get("/coin/list").json(), [{"id": "bitcoin"}])

        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].url.host, "api.coingecko.com")
        self.assertEqual(
            dict(requests[0].url.params), {"ids": "bitcoin", "vs_currencies": "usd"}
        )
        self.assertEqual(requests[1].url.path, "/api/v3/coins/list")
        self.assertTrue(all(r.headers["x-cg-demo-api-key"] == "test-key" for r in requests))

    def test_upstream_errors_are_reported(self):
        def handle(request):
            if request.url.path.endswith("/simple/price"):
                return httpx.Response(429)
            return httpx.Response(200, text="invalid JSON")

        client_class = httpx.AsyncClient
        transport = httpx.MockTransport(handle)
        with (
            patch.dict(os.environ, {"COINGECKO_API_KEY": "test-key"}),
            patch("app.main.load_dotenv"),
            patch(
                "app.main.httpx.AsyncClient",
                side_effect=lambda **kwargs: client_class(transport=transport, **kwargs),
            ),
            TestClient(app) as client,
        ):
            response = client.get("/price", params={"coin": "bitcoin"})
            self.assertEqual(response.status_code, 502)
            self.assertIn("429", response.json()["detail"])
            self.assertEqual(client.get("/coin/list").status_code, 502)
            self.assertEqual(client.get("/price").status_code, 422)

    def test_startup_requires_key(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("app.main.load_dotenv"),
            self.assertRaisesRegex(RuntimeError, "COINGECKO_API_KEY"),
            TestClient(app),
        ):
            pass

    def test_upstream_timeout_is_reported(self):
        def handle(request):
            raise httpx.ReadTimeout("timeout", request=request)

        client_class = httpx.AsyncClient
        transport = httpx.MockTransport(handle)
        with (
            patch.dict(os.environ, {"COINGECKO_API_KEY": "test-key"}),
            patch("app.main.load_dotenv"),
            patch(
                "app.main.httpx.AsyncClient",
                side_effect=lambda **kwargs: client_class(transport=transport, **kwargs),
            ),
            TestClient(app) as client,
        ):
            self.assertEqual(client.get("/coin/list").status_code, 504)


if __name__ == "__main__":
    unittest.main()
