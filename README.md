# MahJourney

MahJourney is a risk-aware, auditable delivery-dispatch demonstrator for a synthetic Singapore fleet of 10 trucks and 40 stops. It maintains a living route plan that combines capacity and time-window optimization with LTA traffic, NEA weather, OneMap road geometry, deterministic autonomy policy, disruption simulation, bounded agents, and human approvals.

The project is built as a modular monolith for a 2-vCPU/4-GB AWS Lightsail target:

- React, TypeScript, Vinext/Vite, and MapLibre GL JS operator dashboard.
- Python 3.12, FastAPI, LangGraph, OR-Tools, and NumPy backend.
- PostgreSQL with PostGIS and pgvector.
- A separate durable collector worker backed by PostgreSQL jobs.
- Caddy as the public reverse proxy.

This is a hackathon demonstrator, not a production fleet-control system. The included fleet, orders, drivers, and truck movement are synthetic. Voice, runtime BERT, live vehicle telematics, and the 2D mascot are intentionally excluded.

## What is implemented

- A validated 10-truck/40-stop OR-Tools plan with immutable versions and deltas.
- Selected-leg driving geometry from OneMap, displayed on OneMap Night tiles.
- LTA incidents, VMS, Speed Bands v4, and estimated-travel-time collectors.
- NEA five-minute rainfall and two-hour forecast collectors.
- Live and deterministic scenario modes with seeking, replay, and branching.
- Road closure, urgent order, truck breakdown, and heavy-rain disruptions.
- Deterministic three-tier autonomy policy with fail-closed behavior.
- Four bounded LangGraph roles and six versioned skills.
- Master-only curated memory, pgvector/full-text retrieval, and a 30-day conversation archive.
- Telegram driver enrollment and driver-scoped access boundaries.
- x401 0.1.0 demo approvals with proof binding and replay protection.
- HMAC-linked audit events and an audit verifier.
- A 24-case golden, disruption, and adversarial evaluation harness.

## Route computation

MahJourney separates stop sequencing from road-path calculation:

1. The planning cost matrix uses distance, road/traffic factors, and available LTA speed context.
2. OR-Tools assigns stops to vehicles and determines their visit order while enforcing capacity and time-window constraints.
3. The backend asks OneMap Routing for the driving geometry of each consecutive selected leg.
4. MapLibre renders only geometry confirmed by OneMap.
5. Scenario truck positions are interpolated along the persisted road geometry using scenario time.

MahJourney does not implement Dijkstra directly. OneMap’s routing service performs the road-graph shortest-path work; OR-Tools solves the higher-level vehicle-routing problem.

OneMap calls are concurrency-limited, cached, and retried twice after transient request failures. A leg that still cannot be routed is not replaced by a straight coordinate line and is not presented as a valid road route. Authentication failures are not repeatedly retried.

The map’s coloured route overlay is distinct from the roads, expressways, and MRT lines baked into the OneMap base tiles. Truck markers are simulated positions, not live GPS reports.

## Agents and authority boundaries

| Role | Responsibility | Explicit boundary |
|---|---|---|
| Master Dispatcher | Dispatcher contact, delegation, explanations, and curated memory | Cannot authorize actions, alter solver output, activate plans, or message drivers directly |
| Route Planning | Planning, traffic/weather context, validation, and candidate evidence | Cannot approve or activate a plan |
| Disruption Analyst | Determines affected legs and proposes bounded replanning | Cannot edit or approve a plan directly |
| Driver Communications | Authenticated driver enquiries and approved notification drafts | Cannot reassign work or access another driver’s assignments |

Authentication, policy decisions, optimization, simulation, audit verification, and x401 proof checks remain deterministic tools rather than LLM decisions. Worker agents never receive the master-memory object.

## Run locally

Requirements:

- Docker Desktop with Docker Compose.
- PowerShell for the supplied scripts.
- `uv` and Node.js 22+ only when running tests directly on the host.

1. Create the local environment file without overwriting existing credentials:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Generate application secrets:

   ```powershell
   powershell -File scripts/generate-secrets.ps1
   ```

   The command writes ignored values to `.env.generated-secrets`. Copy those values into `.env`; the script never overwrites `.env`.

3. Add the external credentials you want to exercise:

   ```dotenv
   OPENAI_API_KEY=
   LTA_DATAMALL_ACCOUNT_KEY=
   ONEMAP_ACCESS_TOKEN=
   DATA_GOV_SG_API_KEY=
   TELEGRAM_BOT_TOKEN=
   TELEGRAM_BOT_USERNAME=
   ```

4. Build and start the stack:

   ```powershell
   docker compose up -d --build
   ```

5. Open the application:

   - Dispatcher: `http://localhost/dispatcher`
   - Scenario lab: `http://localhost/scenario`
   - Operations: `http://localhost/operations`
   - FastAPI documentation: `http://localhost:8000/docs`

6. Stop the stack without deleting PostgreSQL data:

   ```powershell
   docker compose down
   ```

PostgreSQL data is stored in the `postgres-data` Docker volume. Do not add `--volumes` unless you intentionally want to remove it.

## Environment contract

See [`.env.example`](.env.example) for every supported value. The important external fields are:

| Variable | Purpose | Required for fixture mode |
|---|---|---|
| `OPENAI_API_KEY` | Master-agent explanations through the Responses API | No |
| `LTA_DATAMALL_ACCOUNT_KEY` | Traffic incidents, VMS, speed bands, and travel times | No |
| `ONEMAP_ACCESS_TOKEN` | Search, driving routes, and road geometry | No |
| `DATA_GOV_SG_API_KEY` | NEA rainfall and two-hour forecast access | No |
| `TELEGRAM_BOT_TOKEN` | Telegram webhook and driver workflows | No |
| `TELEGRAM_BOT_USERNAME` | Telegram enrollment links | No |

Only `ONEMAP_ACCESS_TOKEN` is used for OneMap. The application never requests or stores a OneMap email/password and never calls `/api/auth/post/getToken`. The token remains server-side and is redacted from logs. A OneMap `401` disables subsequent live calls until the token is replaced and the integration is rechecked.

Safe defaults keep `TELEGRAM_SEND_TESTS=false`, `ALLOW_PLAN_EXECUTION_IN_TESTS=false`, `BERT_GUARD_ENABLED=false`, and `MASCOT_ENABLED=false`.

## Live-data schedules

| Source | Dataset | Poll interval | Stale after |
|---|---|---:|---:|
| LTA | Traffic incidents | 120 seconds | 360 seconds |
| LTA | VMS/EMAS | 120 seconds | 360 seconds |
| LTA | Traffic Speed Bands v4 | 300 seconds | 900 seconds |
| LTA | Estimated travel times | 300 seconds | 900 seconds |
| NEA | Five-minute rainfall | 300 seconds | 900 seconds |
| NEA | Two-hour forecast | 1,800 seconds | 3,600 seconds |

LTA collection follows `$skip` pagination and retains the last valid snapshot after a failed fetch. The current v4 speed-band links are normalized into a PostGIS-indexed table while history retains compact hashes and counts.

The weather-conditioned Markov model begins as `EXPERIMENTAL`. It remains outside operational decisions until at least 14 days of synchronized observations exist and it beats the required chronological persistence and traffic-only baselines.

## Testing

Run the backend suite:

```powershell
uv run --directory backend --group dev pytest -q
```

Run frontend validation:

```powershell
Set-Location frontend
npm ci
npm run lint
npm run build
```

Run the container smoke workflow:

```powershell
powershell -File scripts/container-smoke.ps1
```

The smoke workflow builds the stack, waits for readiness, verifies all three UI routes, checks the live map and evaluation API, verifies PostGIS/pgvector, and writes redacted artifacts under `artifacts/container-<timestamp>/`.

Run the unattended safety contract with:

```powershell
powershell -File scripts/overnight.ps1
```

It runs fixture tests before optional read-only LTA, NEA, and OneMap checks. It never sends Telegram messages or activates a plan, continues independent suites after failures, redacts credentials, and writes timestamped test, coverage, build, audit, and live-read artifacts under `artifacts/`.

Current local verification:

- 32 backend tests passing.
- 100% hard-constraint compliance across 24 evaluation scenarios.
- 100% autonomy-policy compliance and zero infeasible automatic executions.
- 31.1% median cost improvement over the greedy baseline.
- 4.7% median disruption-duration improvement over traffic-free OR-Tools.
- Frontend lint and production build passing.

Evaluation figures are fixture-based hackathon evidence, not production performance guarantees.

## Application and API entry points

| Entry point | Purpose |
|---|---|
| `/dispatcher` | Live fleet map, plan summary, alerts, assignments, approvals, and dispatcher console |
| `/scenario` | Virtual clock, playback speeds, seeking, reset, branching, and disruption injection |
| `/operations` | Data freshness, agent/policy trace, forecast gate, evaluations, and audit verification |
| `/api/v1/map/state` | Current plan and simulated truck positions |
| `/api/v1/plans/generate` | Generate and road-enrich a candidate plan |
| `/api/v1/dispatcher/messages` | Submit dispatcher requests to the bounded agent graph |
| `/api/v1/operations/integrations` | Integration health and freshness |
| `/api/v1/evaluations/run` | Run the 24-case evaluation harness |
| `/ws/events` | Live application events |

The complete REST contract is available from the generated FastAPI documentation.

## Repository layout

```text
backend/
  mahjourney/       FastAPI application, agents, planning, integrations, policy, and simulation
  migrations/       PostgreSQL/PostGIS/pgvector schema migrations
  skills/           Six versioned agent-skill contracts
  tests/            Fixture, policy, security, integration, routing, and evaluation tests
frontend/
  app/               Dispatcher, scenario, and operations routes
  components/        Operator UI and MapLibre map
database/            PostgreSQL image with PostGIS and pgvector
scripts/             Secret generation, container smoke, and unattended validation
compose.yaml         Five-service development/deployment stack
Caddyfile            Public reverse proxy configuration
```
