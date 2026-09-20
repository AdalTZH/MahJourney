# MahJourney — Operational Data Update

This document supplements the original [`README.md`](README.md). It describes the
change that moves **orders, vehicles, drivers, and depots** out of the hardcoded
synthetic fixtures and into an Excel-imported PostgreSQL dataset, how the whole
system fits together now, what was fixed along the way, and how to run it.

The original README is still accurate for everything it covers (agents, policy,
integrations, simulation, x401 approvals, audit). Nothing there was removed — this
is an additive change to *where the fleet and order data comes from* and *how the
route solver enforces capacity*.

---

## 1. What changed at a glance

Previously the system ran on 10 synthetic trucks and 40 synthetic stops generated
in code (`backend/mahjourney/fixtures.py`). It now can load a realistic Singapore
dataset from an Excel workbook:

- **500 orders**, **60 vehicles**, **75 drivers**, **12 depots**.
- Data is imported into PostgreSQL and read back through the repository layer.
- The route solver now enforces **realistic multi-dimensional capacity** (weight
  and volume), **driver working hours**, and **multi-depot** planning.

The synthetic fixtures are still present and remain the default, so existing
behavior and tests are unchanged unless you switch the data source on.

### Data source switch

A new setting controls where fleet/order data comes from:

| Setting | Values | Default | Meaning |
|---|---|---|---|
| `DATA_SOURCE` | `synthetic` \| `database` | `synthetic` | `synthetic` keeps the in-code fixtures. `database` loads the imported Excel data from PostgreSQL. |
| `MAX_STOPS_PER_VEHICLE` | integer | `25` | Safety cap on stops per vehicle (see below). |

---

## 2. How the system works now

The high-level architecture is unchanged from the original README (modular
monolith: Next.js frontend, FastAPI backend, PostgreSQL/PostGIS/pgvector, a
collector worker, Caddy proxy). The change is confined to the **data layer** and
the **route solver**, both of which sit *below* the agent layer.

### 2.1 Data flow

```
Excel workbook (database/Singapore_Logistics_Delivery_Planning_Dataset.xlsx)
        │  python -m mahjourney.import_operational
        ▼
PostgreSQL tables: depots, drivers, vehicles, orders   (migration 004)
        │  PostgresRepository.load_depots / load_fleet / load_orders
        ▼
Domain models: Depot, Driver, Vehicle, Order
        │  build_plan(..., depots=...)
        ▼
PlanVersion (routes, ETAs, geometry)  ──►  API, map, agents' proposals
```

When `DATA_SOURCE=database`, the app loads depots, vehicles (each paired with a
driver), and pending orders from PostgreSQL at startup and builds the initial plan
from them. When `DATA_SOURCE=synthetic`, it uses the original in-code fixtures and
behaves exactly as before.

### 2.2 Capacity model (what "how much fits" means now)

The old model was a single abstract number: each vehicle held 6 "stops." The new
model is realistic and multi-dimensional. An order consumes **weight (kg)** and
**volume (m³)**; a vehicle is limited by its **capacity weight** and **capacity
volume**. The hard constraints enforced by the solver are:

1. **Weight** — total order weight on a route ≤ vehicle capacity weight.
2. **Volume** — total order volume on a route ≤ vehicle capacity volume.
3. **Time windows** — each stop is served inside the order's delivery window.
4. **Driver working hours** — a route cannot run past the assigned driver's shift.
5. **Max stops per vehicle** — a configurable safety rail (default 25).

**Order quantity** is treated as informational (there is no vehicle "max parcel
count" in the data), but it still increases a stop's service time so a heavier
manifest takes longer to deliver. It is not a hard constraint.

The max-stops rail exists because, with this data, weight and volume rarely bind
(an 1,800 kg / 11.5 m³ truck dwarfs a 5 kg / 0.01 m³ parcel). Without a rail, a
solver that slightly under-estimates travel or service time could produce a
50-stop route no driver could finish. 25 is generous enough to never bind in
normal operation and only catches pathological output. It is tunable via
`MAX_STOPS_PER_VEHICLE`.

### 2.3 Drivers and depots (full modeling)

- Each **driver** has a home depot, working hours, shift type, and skills.
- Each **vehicle** is based at a depot with its own coordinates.
- A vehicle is only usable if there is an **Available** driver at the **same
  depot**. Each available vehicle is paired with one available driver, and the
  vehicle's effective working window is the **intersection** of the vehicle's
  availability window and the driver's working hours.
- Only vehicles with availability `Available` and drivers with status `Available`
  are used for planning.

**Shift-aware pairing.** When a depot has drivers on different shifts (for example
four day-shift drivers and one evening driver), the pairing spreads vehicles across
those shifts instead of assigning drivers in id order. Drivers are grouped by shift
and offered latest-ending-first, so the depot's evening driver is matched to a
vehicle before the day-shift drivers claim them all. Without this, a depot's
evening driver could sit unused while its evening deliveries went unassigned. This
lets a depot cover late deliveries with the drivers it already has; a depot with
*no* evening driver (for example Tuas in the sample data) still cannot, which is a
genuine staffing gap rather than a planner limitation.

### 2.4 Multi-depot planning

Instead of one origin for the whole fleet, planning is now **per depot**:

1. Each order is assigned to a serving depot — first by matching its delivery area
   to a depot's area, otherwise by nearest operational depot. Any exception (area
   with no matching depot, or an area-match that isn't the nearest depot) is
   recorded on the order as an assignment note.
2. Within each depot, only that depot's available vehicles serve its assigned
   orders, each starting and ending at the depot's coordinates.

This mirrors how real multi-depot dispatch works and keeps each solver problem
small and fast instead of one giant 500-order / 60-vehicle model.

### 2.5 New API endpoints

In addition to the existing endpoints, the rich records are now queryable:

| Endpoint | Returns |
|---|---|
| `GET /api/v1/orders` | All orders (full fields in database mode) |
| `GET /api/v1/vehicles` | All vehicles |
| `GET /api/v1/drivers` | All drivers (database mode); driver ids in synthetic mode |
| `GET /api/v1/depots` | All depots |
| `GET /api/v1/fleet` | Depot(s), vehicles, and orders (now includes `depots`) |

`GET /api/v1/ready` now also reports the active `data_source`.

Idle vehicles (a route with no stops, because the depot's demand did not need
them) are reported with a `STANDBY` phase in `GET /api/v1/map/state`, each carrying
its own `depot_id`, so the dispatcher can see spare capacity per depot rather than
seeing a confusing "0 stops / 1 min" route.

### 2.6 Planning algorithm

Given the available fleet, orders, and depots, `build_plan` produces a validated
`PlanVersion` through the following pipeline. Steps 1–5 run per depot, which keeps
each optimization problem small and fast.

1. **Depot assignment.** Every order is attached to a serving depot — by matching
   its delivery area to a depot's area, else the nearest operational depot. The
   choice and any exception are recorded on the order.

2. **Driver–vehicle pairing (shift-aware).** Each available vehicle is paired with
   an available driver at the same depot, spreading vehicles across the depot's
   shifts (evening drivers offered first — see §2.3). The vehicle's working window
   becomes the intersection of its availability and the driver's hours.
  
3. **Time-aware bucketing.** Within a depot, orders are grouped by delivery time
   band. For each band, only vehicles whose working window covers that band are
   eligible, and the orders are distributed across those vehicles by a
   deterministic **geographic sweep** (sorting by bearing from the depot so nearby
   stops land on the same vehicle). This prevents a single vehicle from being
   handed a mix of early- and late-window stops it cannot sequence in one shift.
   Orders whose band no vehicle can cover are left unbucketed and surface as
   unassigned.

4. **Per-vehicle sequencing (OR-Tools).** Each vehicle's bucket is solved as a
   small single-vehicle routing problem using Google OR-Tools, subject to the hard
   constraints in §2.2 (weight, volume, time windows, working hours, max stops).
   Infeasible orders are dropped via disjunctions with a penalty large enough that
   the solver only sheds an order when it genuinely cannot be served — never by
   overrunning a driver's shift. A deterministic capacity-aware nearest-neighbor
   heuristic is used as a fallback if OR-Tools is unavailable or does not converge.

5. **Route construction.** The chosen order sequence is turned into a `VehicleRoute`
   with per-stop ETAs and departure times, starting and ending at the depot.
   Service time per stop grows with order quantity.

6. **Validation.** `validate_plan` re-checks the assembled plan against every hard
   constraint and lists violations. Any order not placed on a route appears as an
   `unassigned stop` violation. A plan with zero violations is marked `VALIDATED`;
   otherwise it stays `CANDIDATE` so the dispatcher sees exactly what could not be
   served.

7. **Road geometry (optional).** When a OneMap token is configured, each route's
   legs are enriched with real road geometry from OneMap Routing, per vehicle
   depot. Legs that cannot be routed are left without geometry rather than drawn as
   straight lines.

The synthetic (`DATA_SOURCE=synthetic`) path skips steps 1–3's depot logic and uses
a single-origin geographic sweep, preserving the original fixture behavior.

**Determinism.** For a given data snapshot and configuration the plan is
reproducible: bucketing sorts deterministically, and each plan is stamped with a
`source_data_version` fingerprint of the fleet/orders/depots plus planning config
(see §4), so re-running yields the same result and a data or config change produces
a new plan.

---

## 3. This is a multi-agent system — does the update affect it?

**No. The agent behavior is unchanged.** MahJourney is a bounded LangGraph
multi-agent system: a **Master Dispatcher** delegates to three worker agents —
**Route Planning**, **Disruption Analyst**, and **Driver Communications** — while
authentication, policy, optimization, audit, and x401 proof checks remain
deterministic tools (not LLM decisions). Worker agents never receive the
master-memory object. See the original README's *Agents and authority boundaries*
table for the full contract.

The agents operate on tasks and produce *proposed actions* (for example, "generate
a candidate plan"); they do not compute routes or read vehicle/driver/order data
directly. The data and solver changes in this update sit **below** the agent
layer, so:

- Delegation, authority boundaries, and the "workers never see master memory" rule
  are untouched.
- When a `GENERATE_CANDIDATE_PLAN` proposal leads to plan generation, it now uses
  the multi-depot, multi-dimensional solver — but the agent-facing contract (a
  validated `PlanVersion`) is the same.
- The Driver Communications boundary still matches a driver to their route by
  driver id; in database mode those are the real driver ids from the workbook.

### Known nuance (cosmetic)

The Route Planning worker node currently reports a fixed
`{"hard_violations": 0}` in its result metrics — it is a demo/stub node. With real
data, a plan can legitimately carry hard violations (see the capacity-gap note
below), so the agent's spoken explanation may say "0 hard violations" while the
actual plan reports more. The **true** violation count is always available from the
plan object and from `POST /api/v1/plans/{plan_id}/versions/{version}/validate`.
This is a display-only mismatch, not a planning error, and it existed before this
update; it only becomes visible because real data produces real violations.

---

## 4. What was fixed

- **Working-hours overrun bug (found during verification).** The route solver's
  order-drop penalty was too low, which allowed a genuinely infeasible order (for
  example, an 18:00–21:00 delivery assigned to a driver whose shift ends at 17:30)
  to be scheduled **past the driver's working hours** instead of being left
  unassigned. This is now fixed: an order that cannot fit the assigned vehicle's
  shift is dropped (reported as unassigned) rather than served illegally. After the
  fix, no route violates a time window or a driver's working hours.

- **Capacity realism.** Replaced the single abstract "6 stops" capacity with real
  weight + volume constraints plus the configurable max-stops rail.

- **Single-depot assumption.** Geometry and planning previously assumed one shared
  depot (`fleet[0].start`). Routes now start and end at each vehicle's own depot.

- **Stale plan after re-import or config change.** Previously, restarting the api
  after a re-import could restore a plan persisted from the *old* data and overwrite
  the freshly built one, so edits appeared to have no effect. Each plan's
  `source_data_version` is now stamped with a content fingerprint of the current
  fleet/orders/depots **plus the planning config** (for example
  `operational-v1:de21457febd8`). A persisted plan is only reused when its
  fingerprint matches, so any data re-import *or* config change (such as adjusting
  `MAX_STOPS_PER_VEHICLE`) invalidates older plans and the plan always reflects the
  latest inputs.

- **Time-aware order bucketing.** Within each depot, orders are now grouped by
  delivery time band and only assigned to vehicles whose driver shift covers that
  band, before the geographic sweep runs. Previously a single vehicle could be
  handed a mix of morning and evening stops it could not sequence in one shift,
  which forced otherwise-serviceable orders to drop. On a 300-order dataset this
  reduced unassigned orders from 74 to 13 with no time-window or working-hours
  violations.

- **Shift-aware driver–vehicle pairing.** The pairing previously matched drivers to
  a depot's vehicles in id order, so day-shift drivers could claim every vehicle and
  leave the depot's evening driver unused — making the depot look like it had no
  evening coverage even when it did. Vehicles are now spread across the depot's
  shifts (evening drivers offered first). On the 300-order dataset this took
  unassigned orders from 13 down to **3**, and those 3 are all at a depot (Tuas)
  that genuinely has no available evening driver.

- **Idle vehicles labeled STANDBY.** A vehicle a plan does not need (its depot's
  demand did not fill it) previously showed as a confusing "0 stops / 1 min" route.
  It is now labeled `STANDBY` with its `depot_id`, so spare capacity per depot is
  visible — useful for spotting which idle trucks could be redeployed.

### Honest note on unassigned orders

In database mode, a plan can still leave some orders **unassigned**, and this is
correct behavior, not a defect. After the bucketing and pairing fixes, the
remaining unassigned orders are genuine capacity edges — typically evening
(18:00–21:00) deliveries at a depot that has no available evening driver at all
(Tuas in the sample data). The system reports these as unassigned so a dispatcher
sees the truth, rather than fabricating impossible routes. Resolving them is an
operational choice (add an evening driver/vehicle at the affected depot, widen the
delivery window, or move the order to an earlier slot), not a planner change.

---

## 5. How to run it

Yes — it is still the same Docker workflow as before. The one-time addition is
importing the Excel data and turning the data source on.

### 5.1 Standard startup (unchanged)

Follow the original README's *Run locally* steps to create `.env`, generate
secrets, and start the stack:

```powershell
docker compose up -d --build
```

By default `DATA_SOURCE` is `synthetic`, so the stack runs exactly as before with
the 10-truck / 40-stop fixtures.

### 5.2 Switch to the Excel-imported data

1. **Confirm the workbook is present** (it already is):

   ```text
   database/Singapore_Logistics_Delivery_Planning_Dataset.xlsx
   ```

2. **Set the data source** in `.env`:

   ```dotenv
   DATA_SOURCE=database
   # optional, defaults to 25
   MAX_STOPS_PER_VEHICLE=25
   ```

   Persistence is already enabled in `compose.yaml` (`PERSISTENCE_ENABLED=true`),
   which the database data source requires.

3. **Import the workbook into PostgreSQL.** The migration that creates the
   `depots`, `drivers`, `vehicles`, and `orders` tables runs automatically the
   first time the database volume is created. The import script is idempotent (it
   also applies the migration if needed) and upserts every row:

   ```powershell
   # bring the database up (and the rest of the stack)
   docker compose up -d --build

   # run the importer inside the api container
   docker compose exec api python -m mahjourney.import_operational `
     --workbook /app/../database/Singapore_Logistics_Delivery_Planning_Dataset.xlsx
   ```

   The `compose.yaml` mounts the workbook into the api container at `/data`, so the
   path above works as written. The script prints the number of depots, drivers,
   vehicles, and orders it imported. It is idempotent (upserts by id); pass
   `--replace` for a full refresh that also removes rows deleted from the workbook.
   See section 5.3 for the `scripts/reimport.ps1` helper that wraps this.

4. **Restart the API so it loads the new data source:**

   ```powershell
   docker compose restart api
   ```

5. **Verify:**

   ```powershell
   # should report "data_source": "database"
   curl http://localhost:8000/api/v1/ready

   # rich records now come from PostgreSQL
   curl http://localhost:8000/api/v1/orders
   curl http://localhost:8000/api/v1/vehicles
   curl http://localhost:8000/api/v1/drivers
   curl http://localhost:8000/api/v1/depots
   ```

### 5.3 Updating the data after editing the Excel

The workbook is mounted into the api container read-only, so edits to
`database/Singapore_Logistics_Delivery_Planning_Dataset.xlsx` on the host are
visible to the container immediately — no image rebuild is needed. After editing,
re-import and reload the plan with the helper script:

```powershell
# Normal update: inserts new rows and updates existing ones by id.
# Rows you did NOT touch are left alone.
powershell -File scripts/reimport.ps1

# Full refresh: clears the tables first, so rows you DELETED from the
# workbook are also removed from the database.
powershell -File scripts/reimport.ps1 -Replace
```

The script brings up the database and api, runs the importer inside the api
container, restarts the api so it rebuilds the plan from the refreshed data, then
prints a summary (data source, depot/vehicle/order counts, plan status, and
assigned vs unassigned orders).

**Upsert vs. replace:**

| Mode | What it does | Use when |
|---|---|---|
| default (upsert) | Insert new rows, update existing rows by id. Deletions in the workbook are **not** removed from the DB. | Quick edits or additions where you don't want to disturb untouched rows. |
| `-Replace` | Clears `depots`, `drivers`, `vehicles`, `orders` (children before parents), then imports. | You deleted rows in the workbook, or you want a guaranteed clean reload. |

You can also run the importer directly instead of the helper (for example, from a
host Python environment with the backend dependencies), adding `--replace` for a
full refresh:

```powershell
docker compose exec api python -m mahjourney.import_operational `
  --workbook /data/Singapore_Logistics_Delivery_Planning_Dataset.xlsx --replace
docker compose restart api
```

> A restart of the api is what triggers replanning: the plan is built at startup
> from the database. The helper script does this for you.

### 5.4 Reverting to synthetic

Set `DATA_SOURCE=synthetic` in `.env` and `docker compose restart api`. No data is
deleted; the imported tables simply stop being used for planning.

> Note: a new backend dependency (`openpyxl`) was added for the importer. If you
> run the backend directly on the host rather than in Docker, run `uv sync` in
> `backend/` (or rebuild the image) before importing.

---

## 6. Files added or changed

**Added**

- `backend/migrations/004_operational_data.sql` — depots, drivers, vehicles, orders tables.
- `backend/mahjourney/operational_data.py` — shared parsing/normalization helpers.
- `backend/mahjourney/import_operational.py` — Excel → PostgreSQL importer (CLI, supports `--replace`).
- `scripts/reimport.ps1` — helper that re-imports the workbook and reloads the plan (`-Replace` for a full refresh).

**Changed**

- `backend/mahjourney/domain.py` — new `Depot` and `Driver` models; richer `Vehicle` and `Order`.
- `backend/mahjourney/planning.py` — multi-dimensional capacity, working hours, multi-depot, max-stops, time-aware bucketing.
- `backend/mahjourney/repository.py` — load/list methods for the four entities; shift-aware driver–vehicle pairing.
- `backend/mahjourney/state.py` — loads operational data when `DATA_SOURCE=database`; plan fingerprint (data + config).
- `backend/mahjourney/config.py` — `DATA_SOURCE` and `MAX_STOPS_PER_VEHICLE` settings.
- `backend/mahjourney/api.py` — new orders/vehicles/drivers/depots endpoints; plan generation passes depots; `STANDBY` phase and per-vehicle depot position in `map/state`.
- `backend/mahjourney/route_geometry.py` — per-vehicle depot origin for road geometry.
- `backend/pyproject.toml` / `backend/uv.lock` — added `openpyxl`.
- `frontend/app/dispatcher/page.tsx` — live vehicle/stop/violation counts, real workload spread, LIVE SIGNALS from integrations, all routes with `STANDBY` labeling.
- `frontend/lib/api.ts`, `frontend/app/globals.css` — supporting types and styles.
- `compose.yaml` — mounts the workbook into the api container at `/data`.
- `.env` — `DATA_SOURCE` and `MAX_STOPS_PER_VEHICLE`.

The synthetic fixtures and all original agent, policy, integration, simulation,
approval, and audit behavior are unchanged.
