"""ClauseAI FastAPI application."""

from __future__ import annotations

from fastapi import FastAPI

from clauseai.config import LISTING_DESCRIPTION
from clauseai.log import setup_logging
from clauseai.mcp import get_clauseai_mcp_http_app
from clauseai.routes import router, well_known_router

setup_logging()

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


app = FastAPI(
    title="ClauseAI",
    description=LISTING_DESCRIPTION,
    lifespan=mcp_app.lifespan,
)
app.add_middleware(LowercasePathMiddleware)
app.mount("/mcp", mcp_app)
app.include_router(router)
app.include_router(well_known_router)
