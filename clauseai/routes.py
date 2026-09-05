"""ClauseAI public document builder: gallery, wizard, JSON API, and skill."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, Field, ValidationError

from clauseai.config import get_base_url
from clauseai.log import get_logger
from clauseai.service import (
    OMIT_ANSWER,
    InvalidFormatError,
    RenderUnavailableError,
    TemplateNotFoundError,
    answered_fields,
    generate_document,
    generation_payload,
    load_template,
    normalize_format,
    record_generation,
    summarize_templates,
    template_schema,
)

logger = get_logger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(ROOT_DIR / "templates"))

router = APIRouter(tags=["clauseai"], responses={404: {"description": "Not found"}})
well_known_router = APIRouter(tags=["clauseai"])


class GenerateBody(BaseModel):
    """JSON body for agent and human generate requests."""

    answers: dict[str, Any] = Field(default_factory=dict)
    format: str = "pdf"
    email: Optional[str] = None
    response: str = "file"


def _page_context(request: Request, **kwargs: Any) -> dict[str, Any]:
    base_url = get_base_url()
    context = {
        "request": request,
        "noindex": True,
        "base_url": base_url,
        "skill_url": f"{base_url}/skill.md",
        "mcp_url": f"{base_url}/mcp",
        "api_url": f"{base_url}/api/templates",
        "agent_prompt": (
            f"Read the ClauseAI skill at {base_url}/skill.md "
            "and follow it to build the legal documents I need. Fill in "
            "every field you already know about me and my company before "
            "asking questions. Use the JSON API or the MCP server. No "
            "registration is required."
        ),
    }
    context.update(kwargs)
    return context


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _normalize_email(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    try:

        class EmailModel(BaseModel):
            email: EmailStr

        EmailModel(email=value)
    except ValidationError:
        raise HTTPException(status_code=400, detail="Invalid email address") from None
    return value


def _answers_from_form(form: dict[str, Any], slug: str) -> dict[str, Any]:
    raw = form.get("answers")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=400, detail="answers must be valid JSON"
            ) from None

    loaded = load_template(slug)
    answers: dict[str, Any] = {}
    for field in loaded.manifest.fields:
        value = form.get(f"field_{field.key}") or form.get(field.key)
        if value not in (None, ""):
            answers[field.key] = value
    return answers


async def _run_generation(
    *,
    request: Request,
    slug: str,
    answers: dict[str, Any],
    fmt: str,
    email: Optional[str],
    source: str,
    response_mode: str,
) -> Response:
    try:
        loaded = load_template(slug)
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        normalized = normalize_format(fmt)
        cleaned = answered_fields(slug, answers)
        rendered = await generate_document(slug, cleaned, normalized)
    except InvalidFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RenderUnavailableError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    email_value = _normalize_email(email)
    payload = generation_payload(
        slug=loaded.manifest.slug,
        fmt=normalized,
        answers=cleaned,
        email=email_value,
        source=source,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await record_generation(payload=payload)

    if response_mode == "json":
        return JSONResponse(
            {
                "slug": slug,
                "title": loaded.manifest.title,
                "format": normalized,
                "filename": rendered.filename,
                "media_type": rendered.media_type,
                "content_base64": base64.b64encode(rendered.content).decode("ascii"),
            }
        )

    return Response(
        content=rendered.content,
        media_type=rendered.media_type,
        headers={
            "Content-Disposition": (f'attachment; filename="{rendered.filename}"')
        },
    )


def build_skill_markdown(base_url: Optional[str] = None) -> str:
    """Return the ClauseAI agent skill document."""
    root = (base_url or get_base_url()).rstrip("/")
    api = f"{root}/api/templates"
    mcp = f"{root}/mcp"
    lines = [
        "---",
        "name: clauseai",
        "description: Generate attorney-drafted startup legal documents "
        "(NDA, MSA, privacy policy, offer letter, DPA, BAA, terms of use) "
        "as PDF, ODT, or Markdown. Use when a user wants a legal template "
        "filled in or downloaded. No registration required.",
        "---",
        "",
        "# ClauseAI",
        "",
        "ClauseAI fills General Legal CC0 templates from a short list of",
        "fields. Do not rewrite the document; only the published fields",
        "are fillable. Fill in as many fields as you can from what you",
        "already know before asking the user anything. Every field is",
        "optional, so the user can skip any question and download",
        "immediately.",
        "",
        "## Endpoints",
        "",
        f"- List templates: `GET {api}`",
        f"- Field schema: `GET {api}/{{slug}}`",
        f"- Generate: `POST {api}/{{slug}}/generate`",
        f"- MCP: `{mcp}`",
        "",
        "## Generate request",
        "",
        "```json",
        "{",
        '  "answers": {"company_name": "Acme Inc."},',
        '  "format": "pdf",',
        '  "email": "optional@example.com",',
        '  "response": "json"',
        "}",
        "```",
        "",
        "`format` is `pdf`, `odt`, or `markdown`. `response` of `json`",
        "returns base64 content; omit it or use `file` for a download.",
        "Email is optional. If the user gives one, include it.",
        "Choice fields may include `__omit__`. Send that value to",
        "remove a placeholder. Empty strings are unanswered, not omit.",
        "",
        "## Workflow",
        "",
        "1. List templates and pick the best slug.",
        "2. Fetch the field schema.",
        "3. Fill in as many answers as you can yourself before asking",
        "   the user anything. Use the conversation, the user's files and",
        "   codebase, and anything you know about their company: name,",
        "   legal entity, domain, address, contact emails, governing",
        "   state, and today's date for effective dates. Make reasonable",
        "   inferences.",
        "4. Only ask the user for fields you genuinely cannot determine.",
        "   Do it in one short message that also shows the values you",
        "   inferred so they can correct them. Never block on missing",
        "   answers; every field is optional.",
        "5. POST generate with whatever answers you have.",
        "6. Return the file to the user.",
        "",
        "These documents are templates, not legal advice.",
    ]
    return "\n".join(lines) + "\n"


@router.get("/", response_class=HTMLResponse, summary="ClauseAI gallery")
async def clauseai_gallery(request: Request) -> HTMLResponse:
    """List available legal templates."""
    return templates.TemplateResponse(
        request,
        "index.html",
        _page_context(
            request,
            title="ClauseAI",
            description=(
                "Pick a legal template, answer a few questions, and "
                "download a PDF, ODT, or Markdown document."
            ),
            template_cards=summarize_templates(),
        ),
    )


@router.get("/health", summary="Health check")
async def health() -> JSONResponse:
    """Return a simple health payload."""
    return JSONResponse({"status": "ok"})


@router.get("/skill.md", summary="ClauseAI agent skill")
async def clauseai_skill() -> Response:
    """Serve the agent skill document."""
    return Response(
        content=build_skill_markdown(),
        media_type="text/markdown; charset=utf-8",
    )


@well_known_router.get(
    "/.well-known/skills/clauseai/skill.md",
    include_in_schema=False,
)
async def clauseai_well_known_skill() -> Response:
    """Alias used by skill installers.

    Installers request SKILL.md; LowercasePathMiddleware lowercases
    that path onto this route.
    """
    return Response(
        content=build_skill_markdown(),
        media_type="text/markdown; charset=utf-8",
    )


@router.get("/api/templates", summary="List ClauseAI templates")
async def api_list_templates() -> JSONResponse:
    """Return template slugs, titles, and descriptions."""
    return JSONResponse({"templates": summarize_templates()})


@router.get("/api/templates/{slug}", summary="ClauseAI template fields")
async def api_template_fields(slug: str) -> JSONResponse:
    """Return the question schema for one template."""
    try:
        return JSONResponse(template_schema(slug))
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/api/templates/{slug}/generate",
    summary="Generate a ClauseAI document",
)
async def api_generate_template(
    slug: str,
    request: Request,
    body: GenerateBody,
) -> Response:
    """Fill a template from JSON and return a file or base64 payload."""
    return await _run_generation(
        request=request,
        slug=slug,
        answers=body.answers,
        fmt=body.format,
        email=body.email,
        source="api",
        response_mode=body.response,
    )


_RESERVED_SLUGS = {"api", "mcp", "skill.md", "health"}


@router.get("/{slug}", response_class=HTMLResponse, summary="ClauseAI wizard")
async def clauseai_wizard(request: Request, slug: str) -> HTMLResponse:
    """Show the short question form for one template."""
    if slug in _RESERVED_SLUGS:
        raise HTTPException(status_code=404, detail="Unknown template")
    try:
        loaded = load_template(slug)
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return templates.TemplateResponse(
        request,
        "wizard.html",
        _page_context(
            request,
            title=f"{loaded.manifest.title} | ClauseAI",
            description=loaded.manifest.description,
            template=loaded.manifest,
            fields=loaded.manifest.fields,
            omit_sentinel=OMIT_ANSWER,
        ),
    )


@router.post("/{slug}/generate", summary="Download a filled ClauseAI document")
async def clauseai_generate(slug: str, request: Request) -> Response:
    """Accept a wizard form or JSON body and return the document."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Invalid JSON body") from None
        try:
            body = GenerateBody.model_validate(payload)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc
        answers = body.answers
        fmt = body.format
        email = body.email
        response_mode = body.response
        source = "web"
    else:
        form = await request.form()
        form_data = {key: str(value) for key, value in form.items()}
        try:
            answers = _answers_from_form(form_data, slug)
        except TemplateNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        fmt = form_data.get("format") or "pdf"
        email = form_data.get("email")
        response_mode = form_data.get("response") or "file"
        source = "web"

    return await _run_generation(
        request=request,
        slug=slug,
        answers=answers,
        fmt=fmt,
        email=email,
        source=source,
        response_mode=response_mode,
    )
