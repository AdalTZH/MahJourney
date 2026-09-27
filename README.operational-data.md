# MahJourney operational data guide

MahJourney loads its depots, drivers, vehicles, and orders from PostgreSQL. The repository includes a Singapore logistics workbook at `database/Singapore_Logistics_Delivery_Planning_Dataset.xlsx` and an idempotent importer that maps the workbook into the application schema.

## Data flow

```text
Singapore_Logistics_Delivery_Planning_Dataset.xlsx
  │
  ▼
mahjourney.import_operational
  │
  ▼
PostgreSQL: depots · drivers · vehicles · orders
  │
  ▼
domain models and planning constraints
  │
  ▼
versioned candidate plan · validation · road geometry · operator review
```

The `data-init` Compose service runs before the API and upserts the workbook records. This makes a fresh deployment ready from a single `docker compose up -d --build` command while keeping repeated starts safe.

## Planning model

The planner uses the following operational constraints:

1. Total route weight must remain within the vehicle's weight capacity.
2. Total route volume must remain within the vehicle's volume capacity.
3. Stops must satisfy configured delivery windows.
4. Routes must fit the paired driver's working hours and vehicle availability.
5. Stops per route must stay within `MAX_STOPS_PER_VEHICLE`.
6. Vehicles and drivers must be available and assigned to the same depot.

Orders are assigned to an operating depot by delivery area and proximity. Planning then runs per depot, pairs drivers and vehicles by compatible working windows, groups orders into serviceable time bands, and sequences each vehicle's stops with OR-Tools. The resulting plan is validated again before it is offered for review.

When road optimization is enabled, OneMap distances inform sequencing. Selected route legs are enriched with road geometry for the map, and GraphHopper supplies closure-aware alternatives during disruption workflows.

## Workbook refresh

The workbook is mounted read-only into the importer container. After editing it on the host, run:

```powershell
# Insert new records and update existing records by primary id.
powershell -File scripts/reimport.ps1 -AdminPassword 'your-admin-password'

# Reconcile deletions by clearing operational tables before importing.
powershell -File scripts/reimport.ps1 -Replace -AdminPassword 'your-admin-password'
```

The helper starts the required services, imports the workbook, restarts the API, waits for readiness, and prints the resulting fleet and plan summary.

The importer can also be run directly:

```powershell
docker compose run --rm data-init

docker compose run --rm data-init python -m mahjourney.import_operational `
  --workbook /data/Singapore_Logistics_Delivery_Planning_Dataset.xlsx `
  --replace
```

## Upsert and replace behavior

| Mode | Behavior | Recommended use |
|---|---|---|
| Upsert | Inserts new rows and updates matching primary ids | Routine additions and edits |
| Replace | Clears operational records, then imports the complete workbook | Deletions or full dataset reconciliation |

Replace mode also removes plan versions and approval records derived from the previous dataset. The dispatcher then generates and reviews a new candidate against the refreshed data.

## Operational API

These authenticated endpoints expose the imported records:

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/fleet` | Combined depot, vehicle, and order view |
| `GET /api/v1/orders` | Order manifest |
| `GET /api/v1/vehicles` | Fleet inventory and capacities |
| `GET /api/v1/drivers` | Driver assignments and availability |
| `GET /api/v1/depots` | Depot locations and operating windows |
| `GET /api/v1/map/state` | Current active plan and vehicle progress |
| `GET /api/v1/ready` | Readiness and operational data source status |

Use the generated API documentation at `/docs` after signing in for complete request and response schemas.

## Data-change checklist

1. Keep record ids stable for entities that should be updated in place.
2. Validate coordinates, capacities, working windows, and order statuses in the workbook.
3. Use replace mode when rows were removed.
4. Review the import counts printed by the helper.
5. Generate and validate a new candidate plan.
6. Inspect unassigned orders and hard-constraint findings before activation.
