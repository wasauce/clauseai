"""ClauseAI public document builder: gallery, wizard, JSON API, and skill."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Optional

import markdown
from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, Field, ValidationError

from clauseai.config import (
    GITHUB_REPO,
    GROKBOT_URL,
    LISTING_DESCRIPTION,
    PRODUCTION_BASE_URL,
    SKILL_INSTALL_COMMAND,
    get_base_url,
)
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
        "noindex": False,
        "base_url": base_url,
        "skill_url": f"{base_url}/skill.md",
        "mcp_url": f"{base_url}/mcp",
        "api_url": f"{base_url}/api/templates",
        "grokbot_url": GROKBOT_URL,
        "skill_install": SKILL_INSTALL_COMMAND,
        "show_template_notice": True,
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


SKILL_PATH = ROOT_DIR / "skills" / "clauseai" / "SKILL.md"
WALKTHROUGH_PATH = ROOT_DIR / "examples" / "generate-nda.md"
LEGAL_DIR = Path(__file__).resolve().parent / "data" / "legal"


def build_skill_markdown(base_url: Optional[str] = None) -> str:
    """Return the ClauseAI agent skill document."""
    root = (base_url or get_base_url()).rstrip("/")
    return SKILL_PATH.read_text(encoding="utf-8").replace(PRODUCTION_BASE_URL, root)


def build_llms_txt(base_url: Optional[str] = None) -> str:
    """Return a concise index for agents navigating the site."""
    root = (base_url or get_base_url()).rstrip("/")
    github = f"https://github.com/{GITHUB_REPO}"
    return "\n".join(
        [
            "# ClauseAI",
            "",
            f"> {LISTING_DESCRIPTION}",
            "",
            "ClauseAI fills attorney-drafted General Legal CC0 templates.",
            "Connect over MCP, install the skill, or use the GrokBot. No account is required.",
            "",
            "## Skill and MCP",
            "",
            f"- [Agent skill]({root}/skill.md): Instructions for filling templates",
            f"- [Well-known skill]({root}/.well-known/skills/clauseai/SKILL.md): Installer alias",
            f"- [MCP server]({root}/mcp): Streamable HTTP tools for listing, inspecting, and generating documents",
            f"- [Install the skill]({github}): `{SKILL_INSTALL_COMMAND}`",
            f"- [GrokBot]({GROKBOT_URL}): GrokBot version of ClauseAI",
            "",
            "## API",
            "",
            f"- [Interactive docs]({root}/docs): FastAPI Swagger UI",
            f"- [OpenAPI]({root}/openapi.json): Machine-readable API schema",
            f"- [Template catalog (JSON)]({root}/api/templates): Slugs, titles, and descriptions",
            "",
            "## Templates",
            "",
            f"- [Gallery]({root}/): Human-readable template catalog",
            f"- [Generate a mutual NDA]({root}/examples/generate-nda.md): Walkthrough with install command, MCP URL, GrokBot, and sample output",
            "",
            "## Legal",
            "",
            f"- [Terms of Use]({root}/terms): Terms for using ClauseAI",
            f"- [Privacy Policy]({root}/privacy): How ClauseAI handles personal information",
            "",
            "## Optional",
            "",
            "- [General Legal templates](https://github.com/General-Legal/legal-templates): Upstream CC0 source",
            "",
        ]
    )


def render_legal_markdown(page: str) -> str:
    """Render a published terms or privacy page to HTML."""
    text = (LEGAL_DIR / f"{page}.md").read_text(encoding="utf-8")
    return markdown.markdown(
        text,
        extensions=["tables", "sane_lists", "nl2br"],
    )


def _legal_page(
    request: Request, *, page: str, title: str, description: str
) -> HTMLResponse:
    """Serve one published legal page without the gallery disclaimer."""
    return templates.TemplateResponse(
        request,
        "legal.html",
        _page_context(
            request,
            title=title,
            description=description,
            body=render_legal_markdown(page),
            show_template_notice=False,
        ),
    )


@router.get("/", response_class=HTMLResponse, summary="ClauseAI gallery")
async def clauseai_gallery(request: Request) -> HTMLResponse:
    """List available legal templates."""
    return templates.TemplateResponse(
        request,
        "index.html",
        _page_context(
            request,
            title="ClauseAI",
            description=LISTING_DESCRIPTION,
            template_cards=summarize_templates(),
        ),
    )


@router.get("/llms.txt", summary="Agent index")
async def llms_txt() -> Response:
    """Serve a concise index of skill, API, and template surfaces."""
    return Response(
        content=build_llms_txt(),
        media_type="text/markdown; charset=utf-8",
    )


@router.get("/robots.txt", include_in_schema=False)
async def robots_txt() -> Response:
    """Allow crawlers on public pages; hide the health endpoint."""
    return Response(
        content="User-agent: *\nAllow: /\nDisallow: /health\n",
        media_type="text/plain; charset=utf-8",
    )


@router.get("/examples/generate-nda.md", summary="Mutual NDA walkthrough")
async def generate_nda_walkthrough() -> Response:
    """Serve the published example walkthrough."""
    return Response(
        content=WALKTHROUGH_PATH.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
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


_RESERVED_SLUGS = {
    "api",
    "mcp",
    "skill.md",
    "health",
    "llms.txt",
    "robots.txt",
    "examples",
    "docs",
    "redoc",
    "openapi.json",
    "terms",
    "privacy",
}


@router.get("/terms", response_class=HTMLResponse, summary="Terms of Use")
async def terms_page(request: Request) -> HTMLResponse:
    """Serve ClauseAI's Terms of Use."""
    return _legal_page(
        request,
        page="terms",
        title="Terms of Use | ClauseAI",
        description="Terms of Use for the ClauseAI website, API, and MCP server.",
    )


@router.get("/privacy", response_class=HTMLResponse, summary="Privacy Policy")
async def privacy_page(request: Request) -> HTMLResponse:
    """Serve ClauseAI's Privacy Policy."""
    return _legal_page(
        request,
        page="privacy",
        title="Privacy Policy | ClauseAI",
        description=(
            "Privacy Policy for ClauseAI, including what the service collects "
            "and the choices available under U.S. state privacy laws."
        ),
    )


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
