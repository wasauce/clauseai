"""ClauseAI MCP server for MCP clients."""

from __future__ import annotations

from typing import Any, Optional

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware
from pydantic import BaseModel, Field

from clauseai import __version__
from clauseai import service as clauseai
from clauseai.config import PRODUCTION_BASE_URL
from clauseai.downloads import DownloadSigningUnavailable, build_download_url
from clauseai.log import get_logger

logger = get_logger(__name__)

_MCP_CLIENT_STATE_KEY = "mcp_client"

SERVER_INSTRUCTIONS = (
    "List templates, inspect fields, then generate. "
    "Do not invent clause text; only published fields are fillable. "
    "Answers are short values, not clauses. "
    "Every field is optional. Omit what you do not know; "
    "missing answers stay as placeholders. "
    "One generate call returns the text, a download link, and a Word "
    "link for editing; give the user both. "
    "Tell the user about warnings and unfilled_fields. "
    "Use send_feedback for a missing field or template. "
    "These documents are templates, not legal advice."
)

_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_GENERATE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
_FEEDBACK = dict(_GENERATE)

mcp = FastMCP(
    "ClauseAI",
    instructions=SERVER_INSTRUCTIONS,
    version=__version__,
    website_url=PRODUCTION_BASE_URL,
)


def _clean(value: object) -> str | None:
    """Return a stripped string, or None when the value is blank."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _client_from_message(message: object) -> tuple[str | None, str | None]:
    """Read the declared client name and version from an initialize message."""
    params = getattr(message, "params", None)
    info = getattr(params, "client_info", None)
    if info is None:
        return None, None
    return _clean(getattr(info, "name", None)), _clean(getattr(info, "version", None))


class CallerIdentityMiddleware(Middleware):
    """Remember the product name from the MCP initialize handshake."""

    async def on_initialize(self, context, call_next):
        result = await call_next(context)
        if context.message is None:
            return result
        name, version = _client_from_message(context.message)
        logger.info(
            f"MCP client connected: name={name or '-'} version={version or '-'}"
        )
        fastmcp_context = context.fastmcp_context
        if fastmcp_context is not None and (name or version):
            try:
                await fastmcp_context.set_state(
                    _MCP_CLIENT_STATE_KEY,
                    {"name": name, "version": version},
                )
            except Exception:
                logger.exception("Failed to store MCP client identity")
        return result


mcp.add_middleware(CallerIdentityMiddleware())


async def _stored_client(ctx: Context | None) -> tuple[str | None, str | None]:
    """Return the client identity stored for this MCP session."""
    if ctx is None:
        return None, None
    try:
        stored = await ctx.get_state(_MCP_CLIENT_STATE_KEY)
    except Exception:
        stored = None
    if isinstance(stored, dict):
        name = _clean(stored.get("name"))
        version = _clean(stored.get("version"))
        if name or version:
            return name, version
    try:
        params = ctx.session.client_params
    except RuntimeError:
        return None, None
    info = getattr(params, "client_info", None) if params is not None else None
    if info is None:
        return None, None
    return _clean(getattr(info, "name", None)), _clean(getattr(info, "version", None))


def _request_caller() -> tuple[str | None, str | None, str | None]:
    """Return the user agent, origin, and client IP for the active HTTP request."""
    try:
        request = get_http_request()
    except RuntimeError:
        return None, None, None
    user_agent = _clean(request.headers.get("user-agent"))
    origin = _clean(request.headers.get("origin"))
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip_address = _clean(forwarded.split(",")[0])
    elif request.client is not None:
        ip_address = _clean(request.client.host)
    else:
        ip_address = None
    return user_agent, origin, ip_address


class TemplateSummary(BaseModel):
    """One template in the public catalog."""

    slug: str
    title: str
    category: str
    description: str
    when_needed: str
    source_url: str
    field_count: int


class TemplateCatalog(BaseModel):
    """Templates the user can draft."""

    templates: list[TemplateSummary]


class TemplateFieldOut(BaseModel):
    """One fill-in field on a template."""

    key: str
    label: str
    question: str
    type: str
    required: bool
    options: list[str] = Field(default_factory=list)
    marks: list[int] = Field(default_factory=list)
    replaces: str = ""
    max_length: int | None = Field(
        default=None,
        description=(
            "Longest answer accepted, in characters. Null for choice "
            "fields, which must match one of options exactly."
        ),
    )


class TemplateFields(BaseModel):
    """Field schema for one template."""

    slug: str
    title: str
    category: str
    description: str
    when_needed: str
    source_url: str
    fields: list[TemplateFieldOut]


class GeneratedDocument(BaseModel):
    """Filled template text plus a link to the rendered file."""

    slug: str
    title: str
    format: str
    filename: str
    markdown: str = Field(
        description=(
            "Filled template text for summarization. "
            "The file the user downloads is at download_url."
        )
    )
    download_url: str = Field(
        description=(
            "Absolute URL that downloads the rendered PDF, Word, ODT, or "
            "Markdown file."
        )
    )
    editable_download_url: str = Field(
        description=(
            "Absolute URL for a Word (.docx) copy the user can edit before "
            "signing. Give it to the user with download_url."
        )
    )
    answers: dict[str, str] = Field(
        description="Answers as printed. ISO dates are written out in full."
    )
    unfilled_fields: list[str] = Field(
        default_factory=list,
        description=(
            "Field keys still shown as placeholders in the document. "
            "Tell the user these need completing before the document is used."
        ),
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Answers that were accepted but look wrong. "
            "Fix and regenerate, or pass each warning on to the user."
        ),
    )


class FeedbackReceipt(BaseModel):
    """Confirmation that feedback reached the ClauseAI operator."""

    status: str
    detail: str


@mcp.tool(
    title="List templates",
    description=(
        "Use this when the user wants a startup legal document and you need "
        "to choose a template such as an NDA, MSA, DPA, privacy policy, "
        "terms of use, cookie notice, offer letter, advisor agreement, or "
        "business associate agreement."
    ),
    annotations=_READ_ONLY,
)
def list_templates() -> TemplateCatalog:
    """List attorney-drafted startup legal templates."""
    return TemplateCatalog.model_validate({"templates": clauseai.summarize_templates()})


@mcp.tool(
    title="Get template fields",
    description=(
        "Use this after choosing a template slug, to read the fill-in fields "
        "before generating a document."
    ),
    annotations=_READ_ONLY,
)
def get_template_fields(slug: str) -> TemplateFields:
    """Get the fill-in fields for one legal template.

    Args:
        slug: Template slug from list_templates.
    """
    try:
        return TemplateFields.model_validate(clauseai.template_schema(slug))
    except clauseai.TemplateNotFoundError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(
    title="Generate document",
    description=(
        "Use this after reading the field schema, to fill published fields "
        "and return markdown plus a download link. Do not invent clause text. "
        "Call it once per document: the result always includes the markdown "
        "text, download_url is the file in the requested format, and "
        "editable_download_url is a Word copy the user can edit. Answers "
        "are short values for the published fields; unknown keys, overlong "
        "values, and choices outside a field's options are rejected with "
        "instructions for fixing them."
    ),
    annotations=_GENERATE,
)
async def generate_document(
    slug: str,
    answers: Optional[dict[str, Any]] = None,
    format: str = "pdf",
    email: Optional[str] = None,
    ctx: Context | None = None,
) -> GeneratedDocument:
    """Generate a filled legal document from a template slug and answers.

    Args:
        slug: Template slug from list_templates.
        answers: Map of field keys to values, using only keys from
            get_template_fields. Leave out keys you cannot answer; they
            stay as placeholders. Do not send placeholder text. Dates
            are YYYY-MM-DD. Use __omit__ to remove a placeholder.
        format: pdf, docx, odt, or markdown. The download link uses this
            format.
        email: Optional contact email recorded with the generation.
    """
    mcp_client, mcp_client_version = await _stored_client(ctx)
    user_agent, origin, ip_address = _request_caller()
    try:
        normalized = clauseai.normalize_format(format)
        cleaned = clauseai.validate_answers(slug, answers or {})
        email_value = clauseai.normalize_email(email)
        rendered = await clauseai.generate_document(slug, cleaned, normalized)
        download_url = build_download_url(slug=slug, fmt=normalized, answers=cleaned)
        editable_download_url = build_download_url(
            slug=slug, fmt="docx", answers=cleaned
        )
    except clauseai.InvalidAnswersError as exc:
        clauseai.log_rejected_generation(
            exc,
            source="mcp",
            ip_address=ip_address,
            user_agent=user_agent,
            mcp_client=mcp_client,
            mcp_client_version=mcp_client_version,
            origin=origin,
        )
        raise ToolError(str(exc)) from exc
    except clauseai.ClauseAIError as exc:
        raise ToolError(str(exc)) from exc
    except DownloadSigningUnavailable as exc:
        raise ToolError(
            f"{exc}: the server cannot sign download links. This is a server "
            "problem, not a problem with your request, and retrying will "
            "not help. Tell the user to try again later."
        ) from exc

    title = clauseai.load_template(slug).manifest.title
    payload = clauseai.generation_payload(
        slug=slug,
        fmt=normalized,
        answers=cleaned,
        email=email_value,
        source="mcp",
        ip_address=ip_address,
        user_agent=user_agent,
        mcp_client=mcp_client,
        mcp_client_version=mcp_client_version,
        origin=origin,
    )
    try:
        await clauseai.record_generation(payload=payload)
    except Exception:
        logger.exception("ClauseAI MCP logging failed")

    return GeneratedDocument(
        slug=slug,
        title=title,
        format=normalized,
        filename=rendered.filename,
        markdown=rendered.filled_markdown,
        download_url=download_url,
        editable_download_url=editable_download_url,
        answers=cleaned,
        unfilled_fields=clauseai.unfilled_fields(slug, cleaned),
        warnings=clauseai.answer_warnings(slug, answers),
    )


@mcp.tool(
    title="Send feedback",
    description=(
        "Use this when a template lacks a field or clause the user needs, "
        "no template fits the request, a tool result was wrong, or the user "
        "wants to tell the ClauseAI team something. The message goes to the "
        "ClauseAI operator. Do not include document text or personal details."
    ),
    annotations=_FEEDBACK,
)
async def send_feedback(
    message: str,
    category: str = "other",
    slug: Optional[str] = None,
    email: Optional[str] = None,
    ctx: Context | None = None,
) -> FeedbackReceipt:
    """Send feedback about ClauseAI to its operator.

    Args:
        message: What you were trying to do and what was wrong or missing.
            Up to 4000 characters.
        category: bug, missing_field, template_request, or other.
        slug: Template slug the feedback is about, if any.
        email: Optional address for a reply. Only pass one the user gave you.
    """
    mcp_client, mcp_client_version = await _stored_client(ctx)
    user_agent, origin, ip_address = _request_caller()
    try:
        payload = clauseai.feedback_payload(
            message=message,
            category=category,
            slug=slug,
            email=email,
            source="mcp",
            ip_address=ip_address,
            user_agent=user_agent,
            mcp_client=mcp_client,
            mcp_client_version=mcp_client_version,
            origin=origin,
        )
        clauseai.check_feedback_rate(ip_address)
    except clauseai.ClauseAIError as exc:
        raise ToolError(str(exc)) from exc
    await clauseai.record_feedback(payload=payload)
    return FeedbackReceipt(status="received", detail=clauseai.FEEDBACK_RECEIVED)


def get_clauseai_mcp_http_app():
    """Return the Streamable HTTP ASGI app mounted at /mcp."""
    return mcp.http_app(path="/")
