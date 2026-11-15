# FastAPI SQLAlchemy CoinGecko

> An example application using FastAPI and the CoinGecko API, with PostgreSQL caching planned.

A lightweight application for looking up cryptocurrency prices from CoinGecko.
The FastAPI endpoints return prices in USD and a list of supported coins and
their CoinGecko IDs.

The project is intended to use PostgreSQL to cache CoinGecko responses and
reduce repeated external requests. PostgreSQL is included in the Docker
Compose setup, but the application does not yet connect to it or cache
responses. SQLAlchemy integration is not implemented yet.

## Requirements

* A [CoinGecko Demo API key](https://www.coingecko.com/en/api/pricing).
* Python 3.10 or later for a local installation, or Docker and Docker Compose.

Create `app/.env` with your key:

```env
COINGECKO_API_KEY=your_coingecko_demo_api_key
```

The application reads this file at startup. An existing `COINGECKO_API_KEY`
environment variable takes precedence. Keep the key private.

For Docker Compose, also set `POSTGRES_PASSWORD` in your shell or a separate
`.env` file in the repository root. You can optionally set `POSTGRES_USER`
and `POSTGRES_DB` there; they default to `postgres` and `app`. Compose sets the
PostgreSQL connection variables for the application automatically.

## Running locally

Run commands from the repository root.

### Using a virtual environment

```bash
python3 -m venv ./my-env
source ./my-env/bin/activate
pip install -r ./app/requirements.txt
cd app
uvicorn main:app --reload
```

The browser interface and API are available at http://localhost:8000. The
interface loads the available coins and displays their current USD prices;
it requires internet access to load Alpine.js and Bootstrap from CDNs.
PostgreSQL is not currently
required when running the application locally.

### Using Docker

#### Docker Compose

After creating `app/.env` and setting `POSTGRES_PASSWORD` as described above:

```bash
docker compose up --build
```

This starts the API on port 8000 and PostgreSQL on port 5432. PostgreSQL data
is stored in a Docker volume; its initialization settings apply only when the
volume is first created.

#### Manual Docker setup

To run the application container without Compose:

```bash
docker build -t fastapi-coingecko ./app
docker run --rm -p 8000:8000 --env-file ./app/.env fastapi-coingecko
```

This starts only the API; the application does not yet use PostgreSQL.

## API endpoints

* `GET /health` returns the application's health status.
* `GET /price?coin=bitcoin` returns the price in USD for a CoinGecko coin ID.
* `GET /coin/list` returns the supported coins and their IDs.

Interactive API documentation is available at http://localhost:8000/docs.

## License

This application is free software; you can redistribute it and/or modify it under the terms of the Mozilla Public License v2.0. You should have received a copy of the MPL 2.0 along with this library, otherwise you can obtain one at http://mozilla.org/MPL/2.0/.