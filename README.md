# MahJourney

MahJourney is an AI-assisted delivery planning and dispatch system for Singapore logistics. It builds the daily delivery schedule, assigns customer orders to available vehicles and drivers, and keeps that schedule workable as traffic, weather, and order volume change during the day.

## The problem

Logistics coordinators prepare daily delivery schedules by assigning customer orders to available vehicles and drivers. Routes are planned manually while weighing customer locations, delivery windows, vehicle capacities, and traffic conditions. As delivery volumes increase, planning becomes increasingly difficult, producing inefficient routes and higher transportation costs.

Manual planning breaks down in three ways:

- The number of feasible order-to-vehicle assignments grows far faster than a planner can evaluate by hand.
- Constraints interact. A change that fixes one delivery window can break a capacity limit or a driver's working hours elsewhere in the schedule.
- A plan drawn in the morning goes stale. Incidents, congestion, rain, urgent orders, and breakdowns invalidate its assumptions once vehicles are on the road.

## How MahJourney addresses it

| Coordinator problem | What the system does |
|---|---|
| Assigning orders to vehicles and drivers by hand | Generates candidate day plans with an OR-Tools solver across multiple depots, assigning each order to a specific vehicle and driver |
| Holding capacity, delivery window, and shift rules in your head | Enforces weight and volume capacity, delivery windows, working hours, and stops per vehicle as explicit constraints, and surfaces any violation as evidence |
| Estimating travel time from experience | Prices legs on OneMap road distance, adjusted by live LTA speed bands and estimated travel times rather than straight-line guesses |
| No way to tell a good schedule from a bad one | Scores every plan on total route distance and compares it against a greedy baseline, so the cost improvement is measurable |
| Replanning from scratch when the day goes wrong | Road-closure, heavy-rain, urgent-order, and breakdown workflows identify affected vehicles and propose a targeted reroute instead of a full replan |
| Getting the revised schedule to drivers | Versions and activates plans, then dispatches driver-scoped routes over Telegram |
| Accountability for schedule changes | Records who approved what, chained with HMACs and verifiable in the Operations workspace |

Planning is assistive, not automatic. The system proposes; the coordinator compares versions, activates one, then dispatches it.

## Product highlights

- Multi-depot order-to-vehicle-and-driver assignment with capacity, working-hour, stop-count, and delivery-window constraints.
- Total-distance scoring for each plan with a greedy baseline comparison, so route efficiency gains are quantified.
- OneMap road distance and geometry, with MapLibre fleet visualization.
- LTA traffic incidents, VMS, speed bands, and estimated travel times applied to leg timing.
- NEA rainfall and two-hour weather forecasts.
- Interactive road-closure, heavy-rain, urgent-order, and vehicle-breakdown workflows.
- A bounded supervisor-and-worker agent system with deterministic authority checks.
- Versioned plans, plan comparison, explicit activation, and driver dispatch controls.
- Telegram enrollment and driver-scoped route messaging.
- Hands-free dispatcher voice input with transcription and streamed speech output.
- HMAC-linked audit records, signed sessions, approval proof validation, and replay protection.
- Persistent operational memory with PostgreSQL full-text and pgvector retrieval.

## Architecture

```text
Browser
  │
  ▼
Caddy ───────────────► Vinext / React operator UI
  │
  ├──────────────────► FastAPI application
  │                       ├── LangGraph agent supervisor
  │                       ├── OR-Tools route planner
  │                       ├── policy, approval, and audit services
  │                       └── OneMap / GraphHopper / Telegram clients
  │
  └──────────────────► WebSocket event and voice streams

FastAPI + collector worker ─► PostgreSQL + PostGIS + pgvector
Collector worker ───────────► LTA DataMall + data.gov.sg
```

The application is deployed as a modular monolith with separate API, frontend, collector, database, routing, and reverse-proxy containers. PostgreSQL is the source of truth for fleet, order, plan, memory, approval, audit, and integration state.

## Quick start

### Prerequisites

Running the stack:

- Docker Desktop or Docker Engine with Docker Compose v2
- PowerShell 7+

Only needed to run the checks in [Verification](#verification) outside containers:

- [uv](https://docs.astral.sh/uv/getting-started/installation/) for the backend test and lint commands
- Node.js 22.13+ for the frontend lint and build commands

### 1. Configure the environment

```powershell
Copy-Item .env.example .env
powershell -File scripts/generate-secrets.ps1
```

Copy the generated values from `.env.generated-secrets` into the matching entries in `.env`. Then add an admin password hash:

```powershell
docker compose build api
docker compose run --rm --no-deps api python -c "from mahjourney.auth import hash_password; print(hash_password('choose-a-strong-password'))"
```

Paste the result into `ADMIN_PASSWORD_HASH`. Docker Compose interprets `$` in `.env` files, so replace each `$` in the hash with `$$`.

Add credentials for the integrations you plan to use:

```dotenv
OPENAI_API_KEY=
LTA_DATAMALL_ACCOUNT_KEY=
ONEMAP_ACCESS_TOKEN=
DATA_GOV_SG_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_BOT_USERNAME=
```

OneMap can also manage token refresh from `ONEMAP_API_EMAIL` and `ONEMAP_API_PASSWORD`.

### 2. Start the platform

```powershell
docker compose up -d --build
```

On first startup, Compose creates the schema, imports the included Singapore logistics workbook, builds the GraphHopper road graph, and starts the application services. Graph preparation takes several minutes on its first run and is cached in a Docker volume.

Check startup progress with:

```powershell
docker compose ps
docker compose logs -f data-init graphhopper api
```

### 3. Open the application

| Surface | URL |
|---|---|
| Sign in | <http://localhost/login> |
| Dispatcher | <http://localhost/dispatcher> |
| Orders | <http://localhost/orders> |
| Scenario lab | <http://localhost/scenario> |
| Operations and audit | <http://localhost/operations> |

Caddy proxies only `/api/*` and `/ws/*` to the API; every other path goes to the frontend. The generated API documentation is therefore not exposed through port 80. Reach it on the API port directly, after signing in at `/login`, since it requires an admin session:

| Surface | URL |
|---|---|
| Swagger UI | <http://localhost:8000/docs> |
| ReDoc | <http://localhost:8000/redoc> |
| OpenAPI schema | <http://localhost:8000/openapi.json> |

The normal operator flow is: generate a candidate plan, inspect the route and constraint evidence, activate the selected version, then dispatch it to enrolled drivers.

### 4. Stop the platform

```powershell
docker compose down
```

PostgreSQL and GraphHopper state remain in named volumes. Use `docker compose down --volumes` only when intentionally resetting local state.

## Operational data

The included workbook is mounted read-only and imported idempotently by the `data-init` service. To apply workbook changes later:

```powershell
powershell -File scripts/reimport.ps1 -Replace -AdminPassword 'your-admin-password'
```

See [Operational data guide](README.operational-data.md) for the workbook mapping and refresh workflow.

## Telegram dispatch

Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, and `TELEGRAM_WEBHOOK_SECRET`, then expose the API through your public HTTPS domain. For a temporary tunnel:

```powershell
docker compose --profile tunnel up -d ngrok
powershell -File scripts/set-telegram-webhook.ps1
```

The dispatcher can issue an enrollment link for a driver. After enrollment, route messages remain scoped to that driver's assignment and are sent only through explicit dispatch actions.

## Configuration

[`.env.example`](.env.example) is the configuration contract. The most important deployment values are:

| Variable | Purpose |
|---|---|
| `APP_ENV` | Runtime mode; use `production` for a public deployment |
| `APP_PUBLIC_URL` | Public application URL used for webhook registration |
| `APP_PUBLIC_HOST` | Caddy site address, such as `dispatch.example.com` |
| `CORS_ALLOWED_ORIGINS` | Comma-separated browser origins allowed by the API |
| `ADMIN_PASSWORD_HASH` | PBKDF2 hash used for dispatcher sign-in |
| `DATABASE_URL` | Async PostgreSQL connection string |
| `OPENAI_API_KEY` | Agent routing, response generation, embeddings, and voice |
| `ONEMAP_ACCESS_TOKEN` | Singapore road routing and geometry |
| `LTA_DATAMALL_ACCOUNT_KEY` | Traffic incidents and traffic conditions |
| `DATA_GOV_SG_API_KEY` | Rainfall and forecast feeds |
| `TELEGRAM_BOT_TOKEN` | Driver enrollment and dispatch messaging |
| `GRAPHHOPPER_PROFILE` | Road profile used for closure-aware reroutes |

Production mode validates persistent storage, the admin password hash, and generated application secrets before accepting traffic.

## Security and control model

- Every operator API route and generated API document requires a signed admin session.
- Session cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` in production.
- Sign-in is throttled per source IP, rejecting further attempts with `429` after five failures in a minute, and every failure is audited.
- Worker-agent actions pass versioned capability contracts before reaching the supervisor.
- Agents propose plan and communication actions; deterministic policy and explicit operator actions control execution.
- Telegram webhooks use Telegram's secret-token header.
- Sensitive values are server-side and redacted from application logs.
- Audit entries are chained with HMACs and can be verified from the Operations screen.
- Direct service ports bind to loopback; Caddy is the public entry point.
- Backend and frontend containers run as non-root users.

## Verification

Backend tests and lint:

```powershell
uv run --directory backend --group dev pytest -q
uv run --directory backend --group dev ruff check mahjourney tests
```

Frontend validation:

```powershell
Set-Location frontend
npm ci
npm run lint
npm run build
```

Container smoke test:

```powershell
powershell -File scripts/container-smoke.ps1
```

The script prompts for the admin password. For unattended CI, provide it only in the process environment as `MAHJOURNEY_ADMIN_PASSWORD`. The smoke workflow checks readiness, authenticated operator routes, the active map contract, evaluation endpoints, and required PostgreSQL extensions, then writes redacted evidence under `artifacts/`.

## Production deployment checklist

1. Set `APP_ENV=production`.
2. Set `APP_PUBLIC_URL`, `APP_PUBLIC_HOST`, and `CORS_ALLOWED_ORIGINS` to the public HTTPS origin.
3. Replace every generated-secret placeholder and set `ADMIN_PASSWORD_HASH`.
4. Configure the required external integration credentials.
5. Point DNS at the host and allow inbound TCP 80/443 for Caddy certificate provisioning.
6. Keep PostgreSQL and GraphHopper volumes on durable storage and back up `postgres-data`.
7. Run the backend, frontend, and container smoke checks before rollout.
8. Verify `/api/v1/ready`, sign-in, plan generation, activation, and driver enrollment after deployment.

## Repository layout

```text
backend/
  mahjourney/       API, agents, planning, integrations, policy, and simulation
  contracts/        Versioned agent capability contracts
  migrations/       PostgreSQL, PostGIS, and pgvector schema
  tests/            Unit, integration, resilience, and security tests
database/            Database image and operational workbook
frontend/
  app/               Operator routes
  components/        Shared UI and map components
  hooks/             Voice, responsive, and browser integration hooks
  lib/               API client and application context
graphhopper/          Self-hosted road-routing configuration
scripts/              Secrets, data refresh, smoke, and webhook utilities
compose.yaml          Application stack
Caddyfile             Public reverse proxy
```

## Technology

React 19 · TypeScript · Vinext · Vite · MapLibre GL JS · FastAPI · Python 3.12 · LangGraph · OpenAI · OR-Tools · PostgreSQL 16 · PostGIS · pgvector · GraphHopper · Caddy · Docker Compose
