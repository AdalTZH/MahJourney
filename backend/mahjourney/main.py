from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html

from .api import events_socket, public_router, require_admin, router, voice_socket
from .config import get_settings
from .state import AppState


class SecretRedactionFilter(logging.Filter):
    _pattern = re.compile(
        r"(?i)(authorization|accountkey|api[_-]?key|token|password)(\s*[:=]\s*)([^\s,;}]+)"
    )

    def filter(self, record: logging.LogRecord) -> bool:
        # Interpolate the record's lazy %-args first, then redact, then clear
        # args. getMessage() applies "msg % args"; doing it here (instead of
        # only rewriting record.msg and dropping args) means log calls that use
        # lazy formatting — logger.info("cost=%.2f", cost) — render their values
        # before redaction instead of emitting the literal "%.2f" placeholder.
        record.msg = self._pattern.sub(r"\1\2[REDACTED]", record.getMessage())
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
    await _register_telegram_webhook(app.state.services)
    # Keep the webhook in sync while the app runs — re-registers automatically
    # whenever ngrok restarts and gets a new URL.
    watcher_task = asyncio.create_task(
        _webhook_watcher(app.state.services), name="telegram-webhook-watcher"
    )
    await app.state.services.record_audit(
        "APPLICATION_STARTED", "system", {"environment": settings.app_env}
    )
    yield
    watcher_task.cancel()
    await app.state.services.close()


# ngrok's agent API hosts to probe, in order. In Docker the ngrok container is
# reachable by its service name; on the host it's localhost. We try both so the
# same code works in either environment.
_NGROK_API_HOSTS = ("http://ngrok:4040", "http://localhost:4040")


async def _get_ngrok_url(client) -> str | None:
    """Return the current public HTTPS URL from the ngrok agent API.

    ngrok exposes /api/tunnels on port 4040 while it is running. Tries the
    Docker service name first, then localhost. Returns None when ngrok is not
    running or has no HTTPS tunnel active.
    """
    for host in _NGROK_API_HOSTS:
        try:
            response = await client.get(f"{host}/api/tunnels", timeout=2)
            tunnels = response.json().get("tunnels", [])
            for tunnel in tunnels:
                url: str = tunnel.get("public_url", "")
                if url.startswith("https://"):
                    return url
        except Exception:  # noqa: BLE001
            continue
    return None


async def _set_telegram_webhook(client, bot_token: str, webhook_url: str, secret: str) -> bool:
    """Call Telegram setWebhook and return True on success."""
    try:
        response = await client.post(
            f"https://api.telegram.org/bot{bot_token}/setWebhook",
            json={
                "url": webhook_url,
                "secret_token": secret,
                "allowed_updates": ["message"],
                "drop_pending_updates": False,
            },
            timeout=10,
        )
        return response.json().get("ok", False)
    except Exception:  # noqa: BLE001
        return False


async def _get_registered_webhook_url(client, bot_token: str) -> str | None:
    """Return the webhook URL Telegram currently has registered, or None.

    Used so the watcher can detect not just a changed ngrok URL but also a
    webhook that was deleted or de-registered out of band — and re-register it.
    """
    try:
        response = await client.get(
            f"https://api.telegram.org/bot{bot_token}/getWebhookInfo", timeout=10
        )
        return response.json().get("result", {}).get("url", "")
    except Exception:  # noqa: BLE001
        return None


async def _register_telegram_webhook(services: AppState) -> None:
    """Register the Telegram webhook at startup.

    Resolution order for the public URL:
    1. ngrok local API (http://localhost:4040) — always current, works on free plan
    2. APP_PUBLIC_URL from settings — fallback for production / non-ngrok deploys

    Never raises — a Telegram misconfiguration should not block startup.
    """
    log = logging.getLogger(__name__)
    settings = services.settings

    if not settings.telegram_bot_token:
        log.debug("Telegram bot token not configured — skipping webhook registration")
        return

    import httpx
    async with httpx.AsyncClient() as client:
        public_url = await _get_ngrok_url(client)
        if public_url:
            log.info("Detected ngrok tunnel: %s", public_url)
        elif settings.app_public_url and not settings.app_public_url.startswith("http://localhost"):
            public_url = settings.app_public_url.rstrip("/")
        else:
            log.warning(
                "No ngrok tunnel detected and APP_PUBLIC_URL is localhost — "
                "Telegram webhook not registered. Start ngrok and the watcher will pick it up."
            )
            return

        webhook_url = f"{public_url}/api/v1/telegram/webhook"
        ok = await _set_telegram_webhook(
            client, settings.telegram_bot_token, webhook_url, settings.telegram_webhook_secret
        )
        if ok:
            log.info("Telegram webhook registered: %s", webhook_url)
            services._current_webhook_url = webhook_url
        else:
            log.warning("Telegram webhook registration failed for: %s", webhook_url)


async def _webhook_watcher(services: AppState) -> None:
    """Background task: re-register the Telegram webhook when the ngrok URL changes.

    Polls the ngrok agent API every 15 seconds. When ngrok starts (or restarts
    with a new URL), this picks it up and calls setWebhook automatically — no
    container restart or manual intervention needed. This is also what performs
    the FIRST registration when the API booted before ngrok was ready.
    """
    log = logging.getLogger(__name__)
    settings = services.settings

    if not settings.telegram_bot_token:
        return

    import httpx
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(15)
            try:
                public_url = await _get_ngrok_url(client)
                if not public_url:
                    continue
                webhook_url = f"{public_url}/api/v1/telegram/webhook"
                # Compare against what Telegram ACTUALLY has registered, not a
                # local cache. This self-heals a webhook that was deleted or
                # changed out of band, and re-registers on a new ngrok URL.
                registered = await _get_registered_webhook_url(
                    client, settings.telegram_bot_token
                )
                if registered == webhook_url:
                    continue  # already correctly registered
                log.info(
                    "Webhook mismatch (registered=%r, want=%r) → re-registering",
                    registered, webhook_url,
                )
                ok = await _set_telegram_webhook(
                    client,
                    settings.telegram_bot_token,
                    webhook_url,
                    settings.telegram_webhook_secret,
                )
                if ok:
                    log.info("Telegram webhook updated: %s", webhook_url)
                    services._current_webhook_url = webhook_url
                else:
                    log.warning("Telegram webhook update failed for: %s", webhook_url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("Webhook watcher error: %s", exc)


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
app.add_api_websocket_route("/ws/voice", voice_socket)


@app.get("/openapi.json", dependencies=[Depends(require_admin)], include_in_schema=False)
def protected_openapi_schema() -> dict[str, Any]:
    return app.openapi()


@app.get("/docs", dependencies=[Depends(require_admin)], include_in_schema=False)
def protected_swagger_docs() -> Any:
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Swagger UI")


@app.get("/redoc", dependencies=[Depends(require_admin)], include_in_schema=False)
def protected_redoc_docs() -> Any:
    return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")
