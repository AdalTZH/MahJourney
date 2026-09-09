from __future__ import annotations

from collections import defaultdict

from .agents import AgentSystem
from .audit import AuditChain
from .config import Settings
from .domain import PlanVersion
from .fixtures import synthetic_fleet, synthetic_orders
from .integrations import LtaClient, NeaClient, OneMapClient
from .memory import MasterMemory
from .openai_gateway import OpenAIGateway
from .planning import build_plan
from .repository import PostgresRepository
from .security import ApprovalService, EnrollmentService
from .simulation import SimulationService


class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.fleet = synthetic_fleet()
        self.orders = synthetic_orders()
        initial = build_plan(self.fleet, self.orders)
        self.plans: dict[str, list[PlanVersion]] = defaultdict(list)
        self.plans[initial.plan_id].append(initial)
        self.active_plan_id: str | None = None
        self.simulation = SimulationService()
        self.memory = MasterMemory()
        self.agents = AgentSystem(self.memory)
        self.openai = OpenAIGateway(settings)
        self.audit = AuditChain(settings.audit_chain_hmac_key)
        self.enrollment = EnrollmentService(settings.enrollment_token_pepper)
        self.approvals = ApprovalService(
            settings.approval_action_hmac_key,
            settings.x401_trusted_issuer,
            settings.x401_verifier_audience,
        )
        self.onemap = OneMapClient(settings)
        self.lta = LtaClient(settings)
        self.nea = NeaClient(settings)
        self.repository = (
            PostgresRepository(settings.database_url) if settings.persistence_enabled else None
        )

    async def initialize(self) -> None:
        if self.repository is None:
            return
        await self.repository.check()
        persisted_plans = await self.repository.load_plans()
        if persisted_plans:
            self.plans.clear()
            for plan in persisted_plans:
                self.plans[plan.plan_id].append(plan)
            active = [plan for plan in persisted_plans if plan.status == "ACTIVE"]
            self.active_plan_id = active[-1].plan_id if active else None
        else:
            await self.repository.save_plan(self.latest_plan)
        self.memory.restore(await self.repository.load_memory())
        approvals, used_nonces = await self.repository.load_approvals()
        self.approvals.restore(approvals, used_nonces)
        self.audit.restore(await self.repository.load_audit_events())
        await self.repository.prune_expired_conversations()

    async def save_plan(self, plan: PlanVersion) -> None:
        if self.repository:
            await self.repository.save_plan(plan)

    async def record_audit(self, event_type: str, actor: str, payload: dict) -> None:
        event = self.audit.append(event_type, actor, payload)
        if self.repository:
            await self.repository.save_audit_event(event)

    async def close(self) -> None:
        await self.onemap.client.aclose()
        await self.lta.client.aclose()
        await self.nea.client.aclose()
        if self.repository:
            await self.repository.close()

    @property
    def latest_plan(self) -> PlanVersion:
        return max(
            (versions[-1] for versions in self.plans.values()), key=lambda plan: plan.created_at
        )
