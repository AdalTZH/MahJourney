# MahJourney — v2

A risk-aware, auditable delivery-dispatch system for Singapore logistics.

Combines OR-Tools route optimisation, LTA/NEA live data, a bounded multi-agent LangGraph system, Telegram driver dispatch, and a live map dashboard.

---

## What's new in v2

- **Real operational data** — 50 orders, 12 depots, 12 vehicles and 111 drivers imported from an Excel workbook instead of synthetic fixtures.
- **Multi-depot, multi-dimensional planning** — per-depot routing, real weight/volume capacity, shift-aware driver–vehicle pairing.
- **Delivery window control** — `ENFORCE_DELIVERY_WINDOWS` flag lets you toggle per-order time-window enforcement on/off without touching code.
- **Telegram driver dispatch** — dispatcher can activate a plan and send each driver their route directly over Telegram from the UI.
- **Configurable max stops** — `MAX_STOPS_PER_VEHICLE` safety cap exposed as an env var.
- **Road-optimised routing** — `ROAD_OPTIMIZED_ROUTING=true` uses real OneMap road distances for stop sequencing (requires `ONEMAP_ACCESS_TOKEN`).
- **Login UI** — `/login` page with session-based admin auth.
- **Dispatcher "Send to drivers" button** — activates the plan and dispatches it, or resends if already active.

---

## Stack

| Layer | Technology |
|---|---|
| Frontend | React, TypeScript, Vinext/Vite, MapLibre GL JS |
| Backend | Python 3.12, FastAPI, LangGraph, OR-Tools |
| Database | PostgreSQL 16 + PostGIS + pgvector |
| Proxy | Caddy |
| Containers | Docker Compose |

---

## How to start the app

### Prerequisites

- **Docker Desktop** (with Docker Compose v2) — [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop)
- **PowerShell** (Windows) — already available on Windows 10/11
- Optional: `uv` + Node 22+ only if running tests directly on the host

---

### Step 1 — Copy the environment file

```powershell
Copy-Item .env.example .env
```

---

### Step 2 — Generate application secrets

```powershell
powershell -File scripts/generate-secrets.ps1
```

This writes `POSTGRES_PASSWORD`, `APP_SESSION_SECRET`, `TELEGRAM_WEBHOOK_SECRET`, `ENROLLMENT_TOKEN_PEPPER`, `AUDIT_CHAIN_HMAC_KEY`, and `APPROVAL_ACTION_HMAC_KEY` to `.env.generated-secrets`.

Open `.env.generated-secrets`, copy every line, and paste it into `.env` under the matching section.

---

### Step 3 — Set an admin password

Generate the hash for your chosen password:

```powershell
docker compose run --rm api python -c "from mahjourney.auth import hash_password; print(hash_password('your-password'))"
```

Paste the output into `.env`:

```dotenv
ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=<paste-hash-here>
```

---

### Step 4 — Add external API credentials

Edit `.env` and fill in any credentials you have:

```dotenv
OPENAI_API_KEY=           # Agent chat, memory embeddings
LTA_DATAMALL_ACCOUNT_KEY= # Live traffic incidents, speed bands
ONEMAP_ACCESS_TOKEN=      # Road geometry on the map
DATA_GOV_SG_API_KEY=      # NEA rainfall and forecast
TELEGRAM_BOT_TOKEN=       # Driver Telegram dispatch
TELEGRAM_BOT_USERNAME=    # Enrollment deep-link
```

---

### Step 5 — Build and start

```powershell
docker compose up -d --build
```

First build takes 2–3 minutes. Subsequent starts are fast.

---

### Step 6 — Open the app

| Page | URL |
|---|---|
| Login | http://localhost/login |
| Dispatcher dashboard | http://localhost/dispatcher |
| Scenario lab | http://localhost/scenario |
| Operations & audit | http://localhost/operations |
| API docs | http://localhost/docs |

Log in with the `ADMIN_USERNAME` and password you set in Step 3.

---

### Step 7 — Import the Singapore Logistics data

The app defaults to `DATA_SOURCE=database`, so it will load the real fleet and orders once they are imported. Run the importer:

```powershell
powershell -File scripts/reimport.ps1 -Replace -AdminPassword 'your-password'
```

Then restart the API to pick up the new data:

```powershell
docker compose restart api
```

> **No database?** If you want to skip the import and use the built-in synthetic fixtures instead, set `DATA_SOURCE=synthetic` in `.env` and run `docker compose up -d api`.

To apply any other `.env` change without a full restart:
```powershell
docker compose up -d api
```

---


| Variable | Default | What it does |
|---|---|---|
| `DATA_SOURCE` | `database` | `database` loads the Excel-imported Singapore Logistics data; `synthetic` uses in-code fixtures (no DB required) |
| `ENFORCE_DELIVERY_WINDOWS` | `true` | When `false`, orders can be delivered any time within driver working hours; per-order windows are ignored |
| `MAX_STOPS_PER_VEHICLE` | `25` | Safety cap on stops per vehicle per route |
| `ROAD_OPTIMIZED_ROUTING` | `false` | When `true` + OneMap token set, route sequencing uses real road distances |
| `PERSISTENCE_ENABLED` | `false` | Enables PostgreSQL-backed plan/audit persistence (required when `DATA_SOURCE=database`) |

---

## Telegram driver dispatch

Drivers receive their route as a Telegram message when a plan is activated. There are two ways to link a driver ID to a Telegram account:

### Option A — Enrollment link (normal flow)
1. Log in as dispatcher
2. Call `POST /api/v1/telegram/enrollment-tokens` with `{"driver_id": "DRV-01"}` (valid 10 min)
3. Open the returned link: `https://t.me/<TELEGRAM_BOT_USERNAME>?start=enroll_<token>`
4. The driver taps Start — they are now linked

### Option B — Manual database insert (dev/testing)
First get the driver's Telegram numeric user ID (have them message `@userinfobot` on Telegram):

```sql
INSERT INTO telegram_drivers (driver_id, telegram_user_id, enrollment_used_at)
VALUES ('DRV-01', 123456789, now())
ON CONFLICT (driver_id)
DO UPDATE SET telegram_user_id = EXCLUDED.telegram_user_id,
              enrollment_used_at = now();
```

Run this via:
```powershell
docker compose exec db psql -U mahjourney -d mahjourney -c "<paste SQL here>"
```

---

## Stopping the app

```powershell
docker compose down
```

To also wipe the database volume:

```powershell
docker compose down -v
```

---

## Running tests

```powershell
# Inside the backend container
docker run --rm -v "${PWD}/backend:/app" -w /app ghcr.io/astral-sh/uv:python3.12-bookworm-slim sh -c "uv sync --group dev && uv run pytest tests/ -q"
```

---

## Project structure

```
MahJourney/
├── backend/
│   ├── mahjourney/         # FastAPI app, planning, agents, integrations
│   ├── migrations/         # PostgreSQL schema (001–004)
│   ├── skills/             # Versioned LangGraph skill definitions
│   └── tests/
├── database/
│   └── Singapore_Logistics_Delivery_Planning_Dataset.xlsx
├── frontend/
│   ├── app/                # Next-style pages: dispatcher, scenario, operations, login
│   ├── components/         # AppShell, DispatchMap, UI library
│   ├── hooks/              # use-speech, use-webmcp
│   └── lib/api.ts          # Typed API client
├── scripts/
│   ├── generate-secrets.ps1
│   ├── reimport.ps1        # Re-import Excel workbook and rebuild plan
│   ├── container-smoke.ps1
│   └── overnight.ps1
├── compose.yaml
├── Caddyfile
├── .env.example
└── README.v2.md            # This file
```
