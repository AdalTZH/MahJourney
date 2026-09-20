from __future__ import annotations

import hashlib
from collections import defaultdict

from .agents import AgentSystem
from .audit import AuditChain
from .auth import LoginThrottle
from .config import Settings
from .domain import Depot, Order, PlanVersion, Vehicle
from .fixtures import synthetic_fleet, synthetic_orders
from .integrations import LtaClient, NeaClient, OneMapClient, TelegramClient
from .memory import MasterMemory
from .openai_gateway import OpenAIGateway
from .planning import build_plan
from .repository import PostgresRepository
from .route_geometry import enrich_plan_geometry
from .security import ApprovalService, EnrollmentService
from .simulation import SimulationService


class AppState:
    # Bumped when the planning algorithm changes so persisted plans built by an
    # older algorithm are treated as stale and rebuilt (the fingerprint already
    # covers data + config; this covers code). v2: duration excludes pre-window
    # idle wait at the depot. v3: delivery-window enforcement is configurable.
    data_source_version = "operational-v3"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.fleet = synthetic_fleet()
        self.orders = synthetic_orders()
        self.depots: tuple = ()
        self._operational_source_version = self.data_source_version
        initial = build_plan(
            self.fleet,
            self.orders,
            max_stops_per_vehicle=settings.max_stops_per_vehicle,
            enforce_delivery_windows=settings.enforce_delivery_windows,
        )
        self.plans: dict[str, list[PlanVersion]] = defaultdict(list)
        self.plans[initial.plan_id].append(initial)
        self.active_plan_id: str | None = None
        self.simulation = SimulationService()
        self.memory = MasterMemory()
        self.agents = AgentSystem(self.memory)
        self.openai = OpenAIGateway(settings)
        self.audit = AuditChain(settings.audit_chain_hmac_key)
        self.enrollment = EnrollmentService(settings.enrollment_token_pepper)
        self.login_throttle = LoginThrottle()
        self.approvals = ApprovalService(
            settings.approval_action_hmac_key,
            settings.x401_trusted_issuer,
            settings.x401_verifier_audience,
        )
        self.onemap = OneMapClient(settings)
        self.lta = LtaClient(settings)
        self.nea = NeaClient(settings)
        self.telegram = TelegramClient(settings)
        self.repository = (
            PostgresRepository(settings.database_url) if settings.database_required else None
        )
        # Persistence of plans/memory/audit stays gated on persistence_enabled;
        # a repository may still exist purely to source operational data.
        self.persist = settings.persistence_enabled

    @property
    def persistence(self) -> PostgresRepository | None:
        """Repository for writes that should only happen when persistence is on.

        Reads (traffic speed context, integration snapshots) may use
        ``self.repository`` directly whenever a DB connection exists.
        """
        return self.repository if self.persist else None

    async def initialize(self) -> None:
        if self.repository is None:
            # No DB, but still enrich the synthetic startup plan's geometry so
            # the map shows road lines immediately when a OneMap token exists.
            await self._enrich_initial_geometry()
            return
        await self.repository.check()
        operational = False
        if self.settings.data_source == "database":
            operational = await self._load_operational_data()
        if not self.persist:
            await self._enrich_initial_geometry()
            return
        persisted_plans = await self.repository.load_plans()
        # When planning from operational data, only restore persisted plans that
        # were built from the SAME data snapshot as the one just loaded. The
        # current snapshot is stamped into source_data_version with a fingerprint
        # of the fleet/orders/depots, so a re-import (which changes the
        # fingerprint) makes older persisted plans stale and prevents them from
        # overwriting the freshly built plan. Plans from a different data source
        # (e.g. leftover synthetic "fixture-v1") are also excluded.
        if operational:
            persisted_plans = tuple(
                plan
                for plan in persisted_plans
                if plan.source_data_version == self._operational_source_version
            )
        if persisted_plans:
            self.plans.clear()
            for plan in persisted_plans:
                self.plans[plan.plan_id].append(plan)
            active = [plan for plan in persisted_plans if plan.status == "ACTIVE"]
            self.active_plan_id = active[-1].plan_id if active else None
        elif operational:
            # No plan persisted for the current snapshot yet: persist the fresh one.
            await self.repository.save_plan(self.latest_plan)
        else:
            await self.repository.save_plan(self.latest_plan)
        # Attach road geometry to whatever plan is now live so the map shows
        # route lines without needing a manual /plans/generate after startup.
        await self._enrich_initial_geometry()
        self.memory.restore(await self.repository.load_memory())
        approvals, used_nonces = await self.repository.load_approvals()
        self.approvals.restore(approvals, used_nonces)
        self.audit.restore(await self.repository.load_audit_events())
        self.enrollment.restore(await self.repository.load_telegram_drivers())
        await self.repository.prune_expired_conversations()

    async def _load_operational_data(self) -> bool:
        """Replace synthetic fixtures with DB-sourced orders/vehicles/depots.

        Returns True when operational data was loaded and an operational plan
        was built; False when the DB had no usable fleet/orders (in which case
        the synthetic fixtures are kept).
        """
        assert self.repository is not None
        depots = await self.repository.load_depots()
        fleet = await self.repository.load_fleet(available_only=True)
        orders = await self.repository.load_orders(pending_only=True)
        if not fleet or not orders:
            # Keep the synthetic fixtures rather than starting empty.
            return False
        self.depots = depots
        self.fleet = fleet
        self.orders = orders
        self._operational_source_version = self._fingerprint(fleet, orders, depots)
        initial = build_plan(
            self.fleet,
            self.orders,
            source_data_version=self._operational_source_version,
            depots=self.depots,
            max_stops_per_vehicle=self.settings.max_stops_per_vehicle,
            enforce_delivery_windows=self.settings.enforce_delivery_windows,
        )
        self.plans = defaultdict(list)
        self.plans[initial.plan_id].append(initial)
        self.active_plan_id = None
        return True

    async def _enrich_initial_geometry(self) -> None:
        """Attach OneMap road geometry to the current live plan at startup.

        Without this, the startup plan carries no geometry and the map shows no
        route lines until an operator generates a plan. Runs only when a OneMap
        token is configured and the live plan lacks geometry (a plan restored
        from the DB may already have it). Failures are swallowed — the plan is
        still usable, just without drawn road lines — so startup never breaks on
        a slow or rejected OneMap call.
        """
        if not self.settings.onemap_access_token or not self.fleet:
            return
        plan = self.latest_plan
        if any(route.geometry for route in plan.routes):
            return
        try:
            depot_by_vehicle = {vehicle.vehicle_id: vehicle.start for vehicle in self.fleet}
            enriched = await enrich_plan_geometry(
                plan, self.fleet[0].start, self.onemap, depot_by_vehicle
            )
        except Exception:
            return
        versions = self.plans[plan.plan_id]
        for index, existing in enumerate(versions):
            if existing.version == plan.version:
                versions[index] = enriched
                break
        if self.persist and self.repository:
            await self.repository.save_plan(enriched)

    def _fingerprint(
        self,
        fleet: tuple[Vehicle, ...],
        orders: tuple[Order, ...],
        depots: tuple[Depot, ...],
    ) -> str:
        """Stable content hash of the current operational snapshot.

        Any change to the set of vehicles, orders, or depots (ids, capacities,
        windows, coordinates) changes the hash, so a re-import invalidates plans
        persisted from an earlier snapshot.
        """
        parts = [
            # Planning configuration that changes the plan for the same data.
            f"cfg:max_stops={self.settings.max_stops_per_vehicle}",
            f"cfg:enforce_delivery_windows={self.settings.enforce_delivery_windows}",
            *(f"v:{v.vehicle_id}:{v.capacity_weight_kg}:{v.capacity_volume_m3}:"
              f"{v.working_start_minute}:{v.working_end_minute}:{v.driver_id}" for v in fleet),
            *(f"o:{o.order_id}:{o.weight_kg}:{o.volume_m3}:{o.window_start_minute}:"
              f"{o.window_end_minute}:{o.location.lat}:{o.location.lon}" for o in orders),
            *(f"d:{d.depot_id}:{d.location.lat}:{d.location.lon}" for d in depots),
        ]
        digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]
        return f"{self.data_source_version}:{digest}"

    async def save_plan(self, plan: PlanVersion) -> None:
        if self.repository and self.persist:
            await self.repository.save_plan(plan)

    async def record_audit(self, event_type: str, actor: str, payload: dict) -> None:
        event = self.audit.append(event_type, actor, payload)
        if self.repository and self.persist:
            await self.repository.save_audit_event(event)

    async def close(self) -> None:
        await self.onemap.client.aclose()
        await self.lta.client.aclose()
        await self.nea.client.aclose()
        await self.telegram.client.aclose()
        if self.repository:
            await self.repository.close()

    @property
    def latest_plan(self) -> PlanVersion:
        return max(
            (versions[-1] for versions in self.plans.values()), key=lambda plan: plan.created_at
        )
