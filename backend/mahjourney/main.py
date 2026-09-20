from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html

from .api import events_socket, public_router, require_admin, router
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
    # The built-in /docs, /redoc, /openapi.json are disabled here and
    # re-registered below behind require_admin, so the API's shape (every
    # route, parameter, and schema) isn't handed to an anonymous visitor.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type", "Authorization", "X-Telegram-Bot-Api-Secret-Token"],
)
app.include_router(public_router)
app.include_router(router, dependencies=[Depends(require_admin)])
app.add_api_websocket_route("/ws/events", events_socket)


@app.get("/openapi.json", dependencies=[Depends(require_admin)], include_in_schema=False)
def protected_openapi_schema() -> dict[str, Any]:
    return app.openapi()


@app.get("/docs", dependencies=[Depends(require_admin)], include_in_schema=False)
def protected_swagger_docs() -> Any:
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Swagger UI")


@app.get("/redoc", dependencies=[Depends(require_admin)], include_in_schema=False)
def protected_redoc_docs() -> Any:
    return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")
