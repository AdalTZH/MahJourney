# MahJourney

MahJourney is a risk-aware, auditable dispatch system for a synthetic Singapore fleet of 10 trucks and 40 stops. It combines deterministic routing, scenario replay, deterministic autonomy policy, external LTA/NEA/OneMap context, bounded agents, master-only curated memory, Telegram driver identity, and an isolated x401 0.1.0 approval demonstrator.

The repository is deliberately a modular monolith: one FastAPI API, one durable collector worker, one React operator surface, and PostgreSQL with PostGIS/pgvector. Voice, runtime BERT, and mascot code are intentionally absent.

## Run locally

1. Copy `.env.example` to `.env` and preserve your existing values.
2. Generate application secrets with `powershell -File scripts/generate-secrets.ps1`, then copy the generated values into `.env`.
3. Fill `LTA_DATAMALL_ACCOUNT_KEY` and `OPENAI_API_KEY` when available. The current app remains fixture-safe without them.
4. Run `docker compose up --build`.
5. Open `http://localhost/dispatcher`.

For a repeatable build, boot, migration, extension, UI, and API check that loads
the ignored generated secrets without modifying `.env`, run:

```powershell
powershell -File scripts/container-smoke.ps1
```

The API is also available directly at `http://localhost:8000/docs`. The dashboard uses OneMap Night raster tiles, while Search and Routing tokens remain server-side.

## Safe tests

Run the unattended test contract with:

```powershell
powershell -File scripts/overnight.ps1
```

It runs fixture suites before any optional live read checks, never sends Telegram messages, never activates plans, continues independent suites after failures, and writes redacted timestamped artifacts under `artifacts/`.

## Environment notes

- `ONEMAP_ACCESS_TOKEN` is the only OneMap credential used. The app never calls the token-generation endpoint and ignores legacy `ONEMAP_EMAIL`/`ONEMAP_PASSWORD` values.
- Generated plans query OneMap only for their selected legs, persist decoded road geometry, and
  interpolate scenario trucks along that geometry. Calls are concurrency-limited with bounded
  retry; a transient routing failure falls back without invalidating the solver result.
- A OneMap `401` or authentication error disables further live calls until the operator replaces the token and resets the health state.
- LTA collectors use the requested 120/300-second cadences and `$skip` pagination. The v4
  speed feed is normalized into one PostGIS-indexed current-link table; historical snapshots
  retain compact hashes/counts instead of duplicating roughly 144,000 links every five minutes.
- NEA rainfall and two-hour forecast collectors use the requested 300/1800-second cadences.
- `TELEGRAM_SEND_TESTS=false` and `ALLOW_PLAN_EXECUTION_IN_TESTS=false` are enforced defaults.
- Docker enables PostgreSQL persistence for immutable plan versions, approvals, curated memory,
  conversation retention, and the HMAC audit chain. Host-side tests keep it disabled explicitly.
- Curated memory uses hybrid PostgreSQL full-text and pgvector cosine retrieval. Dispatcher
  conversations are searchable for 30 days and expired messages are pruned on API startup.
- The weather-conditioned Markov model starts `EXPERIMENTAL`; it cannot become operational until the 14-day and evaluation gates pass.
- Compose health checks and resource ceilings keep the five services within the intended 2-vCPU/4-GB Lightsail envelope.

## Key routes

- `/dispatcher`: map, plans, alerts, route load, and master-agent console.
- `/scenario`: deterministic clock, seeking, speed, reset, branching, and four disruption types.
- `/operations`: integration state, agent handoffs, policy trace, audit verification, and forecast gate.

## Phase map

The current implementation covers the cross-phase vertical slice needed for a hackathon demo: foundation, validated baseline and LTA-speed-aware routing, live integration collectors, dashboard/simulation, policy/forecast gating, four-agent orchestration, master-only hybrid memory, Telegram enrollment boundaries, x401 proof binding/replay prevention, persistent audit chaining, and a 24-case golden/disruption/adversarial evaluation harness. PostgreSQL migrations are in `backend/migrations/`; fixture tests deliberately run in memory. The evaluation dashboard compares traffic-aware and traffic-free route duration under the same disruption costs and reports the measured result.
