import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import JSON, DateTime, Integer, String, delete, insert, select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Coin(Base):
    __tablename__ = "coins"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)


class CoinCache(Base):
    __tablename__ = "coin_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


CACHE_DURATION = timedelta(days=1)
CACHE_LOCK_ID = 718039251


def create_database_engine() -> AsyncEngine:
    password = os.getenv("POSTGRES_PASSWORD")
    settings = {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": os.getenv("POSTGRES_PORT", "5432"),
        "username": os.getenv("POSTGRES_USER", "postgres"),
        "database": os.getenv("POSTGRES_DB", "app"),
    }
    if not password or not password.strip() or any(not value.strip() for value in settings.values()):
        raise RuntimeError("PostgreSQL connection settings must not be empty")
    try:
        port = int(settings["port"])
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise RuntimeError("POSTGRES_PORT must be a valid port number") from exc

    return create_async_engine(
        URL.create(
            "postgresql+asyncpg",
            username=settings["username"],
            password=password,
            host=settings["host"],
            port=port,
            database=settings["database"],
        )
    )


async def lock_coin_cache(connection: AsyncConnection) -> None:
    await connection.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": CACHE_LOCK_ID}
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv(Path(__file__).with_name(".env"))
    api_key = os.getenv("COINGECKO_API_KEY")
    if not api_key or not api_key.strip():
        raise RuntimeError("COINGECKO_API_KEY must be set")

    engine = create_database_engine()
    try:
        async with engine.begin() as connection:
            await lock_coin_cache(connection)
            await connection.run_sync(Base.metadata.create_all)
        async with httpx.AsyncClient(
            base_url="https://api.coingecko.com/api/v3",
            headers={"x-cg-demo-api-key": api_key},
            timeout=10.0,
        ) as client:
            app.state.coingecko_client = client
            app.state.database_engine = engine
            yield
    finally:
        await engine.dispose()


app = FastAPI(lifespan=lifespan)
INDEX_PATH = Path(__file__).with_name("public") / "index.html"


async def coingecko_get(
    request: Request, path: str, params: dict[str, str] | None = None
):
    try:
        response = await request.app.state.coingecko_client.get(path, params=params)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="CoinGecko request timed out") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"CoinGecko returned HTTP {exc.response.status_code}",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="CoinGecko request failed") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="CoinGecko returned invalid JSON") from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(INDEX_PATH)


@app.get("/price")
async def price(request: Request, coin: str):
    return await coingecko_get(
        request, "/simple/price", {"ids": coin, "vs_currencies": "usd"}
    )


@app.get("/coin/list")
async def coin_list(request: Request):
    async with AsyncSession(request.app.state.database_engine) as session:
        async with session.begin():
            await lock_coin_cache(await session.connection())
            cache = await session.get(CoinCache, 1)
            if cache is not None and utc_now() - cache.refreshed_at < CACHE_DURATION:
                result = await session.scalars(select(Coin.data).order_by(Coin.position))
                return result.all()

            coins = await coingecko_get(request, "/coins/list")
            if not isinstance(coins, list) or not all(
                isinstance(coin, dict)
                and all(isinstance(coin.get(field), str) for field in ("id", "name", "symbol"))
                and coin["id"]
                for coin in coins
            ) or len({coin["id"] for coin in coins}) != len(coins):
                raise HTTPException(status_code=502, detail="CoinGecko returned an invalid coin list")

            await session.execute(delete(Coin))
            if coins:
                await session.execute(
                    insert(Coin),
                    [{"id": coin["id"], "position": position, "data": coin}
                     for position, coin in enumerate(coins)],
                )
            if cache is None:
                session.add(CoinCache(id=1, refreshed_at=utc_now()))
            else:
                cache.refreshed_at = utc_now()
            return coins
