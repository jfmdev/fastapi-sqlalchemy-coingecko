import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi import HTTPException, Request


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv(Path(__file__).with_name(".env"))
    api_key = os.getenv("COINGECKO_API_KEY")
    if not api_key or not api_key.strip():
        raise RuntimeError("COINGECKO_API_KEY must be set")

    async with httpx.AsyncClient(
        base_url="https://api.coingecko.com/api/v3",
        headers={"x-cg-demo-api-key": api_key},
        timeout=10.0,
    ) as client:
        app.state.coingecko_client = client
        yield


app = FastAPI(lifespan=lifespan)


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


@app.get("/price")
async def price(request: Request, coin: str):
    return await coingecko_get(
        request, "/simple/price", {"ids": coin, "vs_currencies": "usd"}
    )


@app.get("/coin/list")
async def coin_list(request: Request):
    return await coingecko_get(request, "/coins/list")
