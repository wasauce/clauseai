"""ClauseAI FastAPI application."""

from __future__ import annotations

from fastapi import FastAPI

from clauseai.log import setup_logging
from clauseai.mcp import get_clauseai_mcp_http_app
from clauseai.routes import router, well_known_router

setup_logging()

mcp_app = get_clauseai_mcp_http_app()


class LowercasePathMiddleware:
    """Normalize URL paths to lowercase for case-insensitive routing."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            lowered = path.lower()
            if path != lowered:
                scope = dict(scope)
                scope["path"] = lowered
        await self.app(scope, receive, send)


app = FastAPI(
    title="ClauseAI",
    description=(
        "Fill attorney-drafted General Legal templates and download "
        "PDF, ODT, or Markdown. No registration is required."
    ),
    lifespan=mcp_app.lifespan,
)
app.add_middleware(LowercasePathMiddleware)
app.mount("/mcp", mcp_app)
app.include_router(router)
app.include_router(well_known_router)
