"""ClauseAI MCP server for MCP clients."""

from __future__ import annotations

import base64
from typing import Any, Optional

from fastmcp import FastMCP

from clauseai import service as clauseai
from clauseai.config import LISTING_DESCRIPTION, PRODUCTION_BASE_URL
from clauseai.log import get_logger

logger = get_logger(__name__)

mcp = FastMCP(
    "ClauseAI",
    instructions=(
        f"{LISTING_DESCRIPTION} List templates, inspect fields, then generate."
    ),
    version="0.1.0",
    website_url=PRODUCTION_BASE_URL,
)


@mcp.tool
def list_templates() -> list[dict[str, Any]]:
    """List attorney-drafted startup legal templates (NDA, MSA, DPA, privacy policy, offer letter, and more)."""
    return clauseai.summarize_templates()


@mcp.tool
def get_template_fields(slug: str) -> dict[str, Any]:
    """Get the fill-in fields for one legal template before generating a document."""
    try:
        return clauseai.template_schema(slug)
    except clauseai.TemplateNotFoundError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool
async def generate_document(
    slug: str,
    answers: Optional[dict[str, Any]] = None,
    format: str = "pdf",
    email: Optional[str] = None,
) -> dict[str, Any]:
    """Generate a filled legal document (PDF, ODT, or Markdown) from a template slug and answers. No account required.

    Args:
        slug: Template slug from list_templates.
        answers: Map of field keys to values. Missing keys stay as
            placeholders.
        format: pdf, odt, or markdown.
        email: Optional contact email recorded with the generation.
    """
    try:
        normalized = clauseai.normalize_format(format)
        cleaned = clauseai.answered_fields(slug, answers or {})
        rendered = await clauseai.generate_document(slug, cleaned, normalized)
    except clauseai.TemplateNotFoundError as exc:
        raise ValueError(str(exc)) from exc
    except clauseai.InvalidFormatError as exc:
        raise ValueError(str(exc)) from exc
    except clauseai.RenderUnavailableError as exc:
        raise ValueError(str(exc)) from exc

    payload = clauseai.generation_payload(
        slug=slug,
        fmt=normalized,
        answers=cleaned,
        email=email,
        source="mcp",
        ip_address=None,
        user_agent="mcp",
    )
    try:
        await clauseai.record_generation(payload=payload)
    except Exception:
        logger.exception("ClauseAI MCP logging failed")

    return {
        "slug": slug,
        "format": normalized,
        "filename": rendered.filename,
        "media_type": rendered.media_type,
        "content_base64": base64.b64encode(rendered.content).decode("ascii"),
    }


def get_clauseai_mcp_http_app():
    """Return the Streamable HTTP ASGI app mounted at /mcp."""
    return mcp.http_app(path="/")
