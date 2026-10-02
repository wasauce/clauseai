"""ClauseAI FastAPI application."""

from __future__ import annotations

import time

from fastapi import FastAPI

from clauseai.config import LISTING_DESCRIPTION
from clauseai.log import get_access_logger, setup_logging
from clauseai.mcp import get_clauseai_mcp_http_app
from clauseai.routes import router, well_known_router

setup_logging()
_access_logger = get_access_logger()

mcp_app = get_clauseai_mcp_http_app()


class LowercasePathMiddleware:
    """Normalize URL paths to lowercase and send /mcp into the MCP mount."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            rewritten = path.lower()
            if rewritten == "/mcp":
                rewritten = "/mcp/"
            if path != rewritten:
                scope = dict(scope)
                scope["path"] = rewritten
                raw = scope.get("raw_path")
                if isinstance(raw, (bytes, bytearray)):
                    scope["raw_path"] = rewritten.encode("ascii")
        await self.app(scope, receive, send)


class AccessLogMiddleware:
    """Write one access-log line for each completed HTTP request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            path = scope.get("path") or "/"
            method = scope.get("method") or "-"
            ip_address = _client_ip(scope) or "-"
            user_agent = _header(scope, b"user-agent") or "-"
            user_agent = user_agent.replace("\r", " ").replace("\n", " ")
            _access_logger.info(
                f"{ip_address} | {method} {path} | {status_code} | "
                f"{elapsed_ms}ms | {user_agent}"
            )


def _header(scope, name: bytes) -> str | None:
    """Return one request header value from an ASGI scope."""
    for key, value in scope.get("headers") or []:
        if key.lower() != name:
            continue
        decoded = value.decode("latin-1").strip()
        if decoded:
            return decoded
    return None


def _client_ip(scope) -> str | None:
    """Return the first forwarded client address, else the socket peer."""
    forwarded = _header(scope, b"x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = scope.get("client")
    if client:
        return client[0]
    return None


app = FastAPI(
    title="ClauseAI",
    description=LISTING_DESCRIPTION,
    lifespan=mcp_app.lifespan,
)
app.add_middleware(LowercasePathMiddleware)
app.add_middleware(AccessLogMiddleware)
app.mount("/mcp", mcp_app)
app.include_router(router)
app.include_router(well_known_router)
