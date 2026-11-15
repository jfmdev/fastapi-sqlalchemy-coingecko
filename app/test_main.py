import asyncio
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from app.main import app, create_database_engine


BITCOIN = {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"}


class CoinGeckoTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        database_url = URL.create("sqlite+aiosqlite", database=str(Path(directory, "test.db")))
        self.stack.enter_context(
            patch("app.main.create_database_engine", side_effect=lambda: create_async_engine(database_url))
        )
        async def lock_sqlite(connection):
            await connection.execute(text("BEGIN IMMEDIATE"))

        self.stack.enter_context(patch("app.main.lock_coin_cache", side_effect=lock_sqlite))
        self.stack.enter_context(patch.dict(os.environ, {"COINGECKO_API_KEY": "test-key"}))
        self.stack.enter_context(patch("app.main.load_dotenv"))
        self.now = datetime(2026, 1, 1, 12)
        self.stack.enter_context(patch("app.main.utc_now", side_effect=lambda: self.now))

    def mock_upstream(self, handler):
        client_class = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        self.stack.enter_context(
            patch(
                "app.main.httpx.AsyncClient",
                side_effect=lambda **kwargs: client_class(transport=transport, **kwargs),
            )
        )

    def test_routes_use_demo_key_and_return_upstream_json(self):
        requests = []

        def handle(request):
            requests.append(request)
            if request.url.path == "/api/v3/simple/price":
                return httpx.Response(200, json={"bitcoin": {"usd": 42}})
            return httpx.Response(200, json=[BITCOIN])

        self.mock_upstream(handle)
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").json(), {"status": "ok"})
            index = client.get("/")
            self.assertEqual(index.status_code, 200)
            self.assertIn("text/html", index.headers["content-type"])
            self.assertIn("Coin prices", index.text)
            response = client.get("/price", params={"coin": "bitcoin"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"bitcoin": {"usd": 42}})
            self.assertEqual(client.get("/coin/list").json(), [BITCOIN])
            self.assertEqual(client.get("/coin/list").json(), [BITCOIN])

        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].url.host, "api.coingecko.com")
        self.assertEqual(
            dict(requests[0].url.params), {"ids": "bitcoin", "vs_currencies": "usd"}
        )
        self.assertEqual(requests[1].url.path, "/api/v3/coins/list")
        self.assertTrue(all(r.headers["x-cg-demo-api-key"] == "test-key" for r in requests))

    def test_cache_survives_restart_and_refreshes_after_one_day(self):
        calls = []
        newer = {"id": "ethereum", "symbol": "eth", "name": "Ethereum", "extra": "value"}

        def handle(request):
            calls.append(request)
            return httpx.Response(200, json=[BITCOIN] if len(calls) == 1 else [newer, BITCOIN])

        self.mock_upstream(handle)
        with TestClient(app) as client:
            self.assertEqual(client.get("/coin/list").json(), [BITCOIN])
        self.now += timedelta(hours=23, minutes=59)
        with TestClient(app) as client:
            self.assertEqual(client.get("/coin/list").json(), [BITCOIN])
            self.assertEqual(len(calls), 1)
            self.now += timedelta(minutes=1)
            self.assertEqual(client.get("/coin/list").json(), [newer, BITCOIN])
            self.assertEqual(client.get("/coin/list").json(), [newer, BITCOIN])
            self.assertEqual(len(calls), 2)

    def test_empty_list_is_cached(self):
        calls = []
        self.mock_upstream(lambda request: (calls.append(request), httpx.Response(200, json=[]))[1])
        with TestClient(app) as client:
            self.assertEqual(client.get("/coin/list").json(), [])
            self.assertEqual(client.get("/coin/list").json(), [])
        self.assertEqual(len(calls), 1)

    def test_concurrent_requests_refresh_only_once(self):
        calls = []

        async def handle(request):
            calls.append(request)
            await asyncio.sleep(0.1)
            return httpx.Response(200, json=[BITCOIN])

        self.mock_upstream(handle)
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.get("/coin/list"), range(2)))
        self.assertEqual([response.json() for response in responses], [[BITCOIN], [BITCOIN]])
        self.assertEqual(len(calls), 1)

    def test_failed_or_invalid_refresh_keeps_cache_and_retries(self):
        responses = iter([
            httpx.Response(200, json=[BITCOIN]),
            httpx.Response(429),
            httpx.Response(200, json=[{"id": "broken"}]),
            httpx.Response(200, json=[BITCOIN, BITCOIN]),
            httpx.Response(200, json=[{"id": "new", "name": "New", "symbol": "new"}]),
        ])
        self.mock_upstream(lambda request: next(responses))
        with TestClient(app) as client:
            self.assertEqual(client.get("/coin/list").json(), [BITCOIN])
            self.now += timedelta(days=1)
            self.assertEqual(client.get("/coin/list").status_code, 502)
            self.assertEqual(client.get("/coin/list").status_code, 502)
            self.assertEqual(client.get("/coin/list").status_code, 502)
            self.now -= timedelta(seconds=1)
            self.assertEqual(client.get("/coin/list").json(), [BITCOIN])
            self.now += timedelta(seconds=1)
            self.assertEqual(client.get("/coin/list").json()[0]["id"], "new")

    def test_upstream_errors_are_reported(self):
        def handle(request):
            if request.url.path.endswith("/simple/price"):
                return httpx.Response(429)
            return httpx.Response(200, text="invalid JSON")

        self.mock_upstream(handle)
        with TestClient(app) as client:
            response = client.get("/price", params={"coin": "bitcoin"})
            self.assertEqual(response.status_code, 502)
            self.assertIn("429", response.json()["detail"])
            self.assertEqual(client.get("/coin/list").status_code, 502)
            self.assertEqual(client.get("/price").status_code, 422)

    def test_startup_requires_key(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(RuntimeError, "COINGECKO_API_KEY"),
            TestClient(app),
        ):
            pass

    def test_upstream_timeout_is_reported(self):
        def handle(request):
            raise httpx.ReadTimeout("timeout", request=request)

        self.mock_upstream(handle)
        with TestClient(app) as client:
            self.assertEqual(client.get("/coin/list").status_code, 504)


class DatabaseSettingsTests(unittest.TestCase):
    def test_database_url_uses_environment_without_mangling_password(self):
        with (
            patch.dict(os.environ, {
                "POSTGRES_HOST": "db.example",
                "POSTGRES_PORT": "5433",
                "POSTGRES_USER": "user",
                "POSTGRES_PASSWORD": "p@ss:/word",
                "POSTGRES_DB": "coins",
            }, clear=True),
            patch("app.main.create_async_engine") as create_engine,
        ):
            create_database_engine()
            url = create_engine.call_args.args[0]
            self.assertEqual(url.drivername, "postgresql+asyncpg")
            self.assertEqual((url.host, url.port, url.username, url.password, url.database),
                             ("db.example", 5433, "user", "p@ss:/word", "coins"))

    def test_missing_password_and_invalid_port_are_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "connection settings"):
                create_database_engine()
            with patch.dict(os.environ, {"POSTGRES_PASSWORD": "test", "POSTGRES_PORT": "invalid"}):
                with self.assertRaisesRegex(RuntimeError, "POSTGRES_PORT"):
                    create_database_engine()


if __name__ == "__main__":
    unittest.main()
