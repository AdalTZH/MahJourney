from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import events_socket, router
from .config import get_settings
from .state import AppState


class SecretRedactionFilter(logging.Filter):
    _pattern = re.compile(
        r"(?i)(authorization|accountkey|api[_-]?key|token|password)(\s*[:=]\s*)([^\s,;}]+)"
    )

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._pattern.sub(r"\1\2[REDACTED]", str(record.msg))
        record.args = ()
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactionFilter())
    logging.basicConfig(level=settings.log_level, handlers=[handler], force=True)
    app.state.services = AppState(settings)
    await app.state.services.initialize()
    await app.state.services.record_audit(
        "APPLICATION_STARTED", "system", {"environment": settings.app_env}
    )
    yield
    await app.state.services.close()


app = FastAPI(
    title="MahJourney API",
    version="0.1.0",
    description="Risk-aware, auditable dispatch planning for Singapore delivery fleets.",
    lifespan=lifespan,
)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type", "Authorization", "X-Telegram-Bot-Api-Secret-Token"],
)
app.include_router(router)
app.add_api_websocket_route("/ws/events", events_socket)
