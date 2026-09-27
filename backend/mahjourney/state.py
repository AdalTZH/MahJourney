from __future__ import annotations

import hashlib
import logging
from collections import defaultdict

from .agents import AgentSystem
from .audit import AuditChain
from .auth import LoginThrottle
from .config import Settings
from .domain import Depot, MasterAgentState, Order, PlanVersion, Vehicle
from .integrations import GraphHopperClient, LtaClient, NeaClient, OneMapClient, TelegramClient
from .memory import MasterMemory
from .notifications import NotificationHub
from .openai_gateway import OpenAIGateway
from .repository import PostgresRepository
from .route_geometry import enrich_plan_geometry
from .security import ApprovalService, EnrollmentService
from .simulation import SimulationService
from .voice import VoiceGateway

logger = logging.getLogger(__name__)


class OperationalDataMissingError(RuntimeError):
    """Raised at startup when PostgreSQL has no usable fleet/orders.

    Operational data (fleet, orders, depots) is sourced exclusively from the
    database — there is no synthetic/in-memory fallback. Run
    ``python -m mahjourney.import_operational`` against the configured
    database to load real operational data before starting the app.
    """


class AppState:
    # Bumped when the planning algorithm changes so persisted plans built by an
    # older algorithm are treated as stale and rebuilt (the fingerprint already
    # covers data + config; this covers code). v2: duration excludes pre-window
    # idle wait at the depot. v3: delivery-window enforcement is configurable.
    data_source_version = "operational-v3"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Fleet/orders/depots and the initial plan are populated from the
        # database in initialize() — there is no synthetic/in-memory fallback,
        # so AppState is not fully usable until initialize() has run.
        self.fleet: tuple[Vehicle, ...] = ()
        self.orders: tuple[Order, ...] = ()
        self.depots: tuple = ()
        self._operational_source_version = self.data_source_version
        self.plans: dict[str, list[PlanVersion]] = defaultdict(list)
        self.active_plan_id: str | None = None
        # A single-driver route send that the agent proposed and is awaiting the
        # dispatcher's confirmation on, keyed by conversation_id. The agent never
        # sends on the same turn it proposes: it stashes the intent here, asks the
        # dispatcher to confirm, and only sends on a later turn once the
        # dispatcher approves. At most one pending send per conversation; a new
        # proposal supersedes the previous one.
        self.pending_driver_sends: dict[str, dict] = {}
        # A plan activation the agent proposed and is awaiting the dispatcher's
        # "yes" on. Same shape/lifecycle as pending_driver_sends: at most one per
        # conversation, superseded by a newer proposal, resolved on the next turn.
        self.pending_plan_approvals: dict[str, dict] = {}
        self.simulation = SimulationService()
        self.memory = MasterMemory()
        self.openai = OpenAIGateway(settings)
        # Speech legs (STT + streaming TTS) for the hands-free voice console.
        self.voice = VoiceGateway(settings)
        # Cross-turn state snapshot for the master agent — updated at phase
        # transitions so operators and the UI always know what the agent is doing.
        self.agent_state = MasterAgentState()
        # The agent graph's supervisor uses the OpenAI gateway to refine routing
        # when a key is configured; without one it falls back to keyword routing.
        self.agents = AgentSystem(
            self.memory,
            gateway=self.openai,
            master_state=self.agent_state,
            incident_lesson_recall_limit=settings.incident_lesson_recall_limit,
        )
        self.audit = AuditChain(settings.audit_chain_hmac_key)
        self.enrollment = EnrollmentService(settings.enrollment_token_pepper)
        self.login_throttle = LoginThrottle()
        self.approvals = ApprovalService(
            settings.approval_action_hmac_key,
            settings.x401_trusted_issuer,
            settings.x401_verifier_audience,
        )
        self.onemap = OneMapClient(settings)
        self.graphhopper = GraphHopperClient(settings)
        self.lta = LtaClient(settings)
        self.nea = NeaClient(settings)
        self.telegram = TelegramClient(settings)
        # In-app live notification hub — pushes transient UI alerts (e.g. driver
        # breakdown reports) to connected dispatcher UIs over the events socket.
        self.notifications = NotificationHub()
        # Operational data is DB-only, so a repository is always constructed.
        self.repository: PostgresRepository | None = PostgresRepository(settings.database_url)
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
            # database_required is always True, so this should be unreachable
            # in practice — repository construction failing to happen at all
            # (rather than failing to connect) indicates a config bug.
            raise OperationalDataMissingError(
                "No database repository was configured. Operational data "
                "(fleet, orders, depots) can only be sourced from PostgreSQL."
            )
        await self.repository.check()
        await self._load_operational_data()
        if not self.persist:
            await self._enrich_initial_geometry()
            return
        all_persisted = await self.repository.load_plans()
        # Restoration policy:
        #   ACTIVE plans  — always restore, regardless of fingerprint.  The
        #     dispatcher already made a deliberate decision to run this plan;
        #     discarding it on restart just because the code version or config
        #     changed is worse than surfacing a potentially stale plan.  We
        #     re-save it under the current fingerprint so subsequent restarts
        #     match without needing this bypass again.
        #   CANDIDATE / VALIDATED / SUPERSEDED — fingerprint-gated as before.
        #     A candidate built from a previous data snapshot should not be
        #     offered to the dispatcher as if it were fresh.
        current_fp = self._operational_source_version
        active_plans = tuple(p for p in all_persisted if p.status == "ACTIVE")
        candidate_plans = tuple(
            p for p in all_persisted
            if p.status != "ACTIVE"
            and (
                p.source_data_version == current_fp
                or p.source_data_version.startswith(current_fp)
            )
        )
        plans_to_restore = active_plans + candidate_plans
        if plans_to_restore:
            self.plans.clear()
            for plan in plans_to_restore:
                self.plans[plan.plan_id].append(plan)
            # Most-recently activated plan wins if there are somehow multiple.
            latest_active = sorted(active_plans, key=lambda p: p.created_at)
            self.active_plan_id = latest_active[-1].plan_id if latest_active else None
            logger.info(
                "startup: restored %d plan version(s) from DB "
                "(active_plan_id=%s, current_fingerprint=%s)",
                len(plans_to_restore),
                self.active_plan_id,
                current_fp,
            )
            # Re-save every restored ACTIVE plan stamped with the current
            # fingerprint so the next restart matches without this bypass.
            for plan in active_plans:
                if not plan.source_data_version.startswith(current_fp):
                    retagged = plan.model_copy(
                        update={"source_data_version": current_fp}
                    )
                    versions = self.plans[plan.plan_id]
                    for i, v in enumerate(versions):
                        if v.version == plan.version:
                            versions[i] = retagged
                            break
                    await self.repository.save_plan(retagged)
                    logger.info(
                        "startup: re-tagged plan %s v%d source_data_version "
                        "from %r to %r",
                        plan.plan_id,
                        plan.version,
                        plan.source_data_version,
                        current_fp,
                    )
        else:
            logger.info(
                "startup: no matching plans in DB "
                "(current_fingerprint=%s) — waiting for dispatcher to generate one",
                current_fp,
            )
        # Only enrich geometry when an active plan actually exists.
        if self.active_plan is not None:
            await self._enrich_initial_geometry()
        # Prune stale memory in Postgres BEFORE loading, so boot rehydration
        # reflects the already-pruned set rather than loading rows we're about
        # to discard in-memory anyway. Bounds MasterMemory's otherwise
        # unconditional growth (see MasterMemory.prune_stale for exactly what
        # is/isn't touched).
        pruned_row_count = await self.repository.prune_stale_memory(
            self.settings.proposed_incident_lesson_retention_days,
            self.settings.superseded_memory_retention_days,
        )
        if pruned_row_count:
            logger.info(
                "startup: pruned %d stale memory_items row(s) "
                "(proposed INCIDENT_LESSON > %dd or SUPERSEDED > %dd)",
                pruned_row_count,
                self.settings.proposed_incident_lesson_retention_days,
                self.settings.superseded_memory_retention_days,
            )
        self.memory.restore(await self.repository.load_memory())
        approvals, used_nonces = await self.repository.load_approvals()
        self.approvals.restore(approvals, used_nonces)
        self.audit.restore(await self.repository.load_audit_events())
        self.enrollment.restore(await self.repository.load_telegram_drivers())
        await self.repository.prune_expired_conversations()

    async def _load_operational_data(self) -> None:
        """Load orders/vehicles/depots from the database and build the initial plan.

        Raises OperationalDataMissingError when the database has no usable
        fleet or orders — there is no synthetic/in-memory fallback. Operators
        must run ``python -m mahjourney.import_operational`` against the
        configured database before starting the app.
        """
        assert self.repository is not None
        depots = await self.repository.load_depots()
        fleet = await self.repository.load_fleet(available_only=True)
        orders = await self.repository.load_orders(pending_only=True)
        if not fleet or not orders:
            raise OperationalDataMissingError(
                "The database has no usable fleet/orders. Run "
                "'python -m mahjourney.import_operational --workbook <path>' "
                "against the configured database to load operational data "
                "before starting the app."
            )
        self.depots = depots
        self.fleet = fleet
        self.orders = orders
        self._operational_source_version = self._fingerprint(fleet, orders, depots)
        # No plan is built automatically at startup. The system starts with an
        # empty plan history and no active plan, so the dispatcher drives the
        # full flow from scratch: generate a candidate (POST /plans/generate),
        # review it, then activate it (POST /plans/activate). Until a plan is
        # activated, the live map has no active plan to show. Persisted plans
        # for the current data snapshot are still restored in initialize().
        self.plans = defaultdict(list)
        self.active_plan_id = None

    async def _enrich_initial_geometry(self) -> None:
        """Attach OneMap road geometry to the current live plan at startup.

        Without this, the startup plan carries no geometry and the map shows no
        route lines until an operator generates a plan. Runs only when a OneMap
        token is configured and the live plan lacks geometry (a plan restored
        from the DB may already have it). Failures are swallowed — the plan is
        still usable, just without drawn road lines — so startup never breaks on
        a slow or rejected OneMap call.
        """
        if not (
            self.settings.onemap_access_token
            or (self.settings.onemap_api_email and self.settings.onemap_api_password)
        ) or not self.fleet:
            return
        plan = self.active_plan
        if plan is None or any(route.geometry for route in plan.routes):
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
        await self.graphhopper.client.aclose()
        await self.lta.client.aclose()
        await self.nea.client.aclose()
        await self.telegram.client.aclose()
        if self.repository:
            await self.repository.close()

    @property
    def has_plan(self) -> bool:
        """Whether any plan exists yet (false right after a from-scratch start)."""
        return any(versions for versions in self.plans.values())

    @property
    def latest_plan(self) -> PlanVersion | None:
        """The most recently created plan, or None if no plan exists yet."""
        if not self.plans:
            return None
        return max(
            (versions[-1] for versions in self.plans.values() if versions),
            key=lambda plan: plan.created_at,
            default=None,
        )

    @property
    def active_plan(self) -> PlanVersion | None:
        """The plan the dispatcher has activated, or ``None`` if none is active.

        This is what the fleet is actually executing. Unlike ``latest_plan``
        (the most recently *created* plan, which flips to a freshly generated
        candidate the moment one is built), the active plan only changes when a
        dispatcher explicitly activates a candidate via ``/plans/activate``. The
        live map should track this so an unapproved candidate never replaces the
        running plan on screen.
        """
        if self.active_plan_id is None:
            return None
        versions = self.plans.get(self.active_plan_id)
        return versions[-1] if versions else None
