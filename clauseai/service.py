"""Fill and render General Legal templates for ClauseAI."""

from __future__ import annotations

import asyncio
import difflib
import html
import json
import os
import re
import shutil
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from clauseai.log import get_logger

logger = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "data" / "templates"
MARK_RE = re.compile(r"<mark>(.*?)</mark>", re.DOTALL)
SUPPORTED_FORMATS = ("pdf", "odt", "markdown")
FormatName = Literal["pdf", "odt", "markdown"]

MEDIA_TYPES = {
    "pdf": "application/pdf",
    "odt": "application/vnd.oasis.opendocument.text",
    "markdown": "text/markdown; charset=utf-8",
}

# Nonempty so omit survives answered_fields and form parsing.
OMIT_ANSWER = "__omit__"


# Default answer length caps by field type. A manifest field may override
# its own cap with max_length. Choice answers must match an option instead.
DEFAULT_MAX_LENGTHS = {"text": 300, "textarea": 2000, "email": 254, "date": 60}
MAX_REPORTED_ISSUES = 20

FEEDBACK_CATEGORIES = ("bug", "missing_field", "template_request", "other")
FEEDBACK_MAX_LENGTH = 4000
FEEDBACK_RATE_LIMIT = 5
FEEDBACK_RATE_WINDOW_SECONDS = 600
FEEDBACK_RECEIVED = (
    "Feedback received. Nobody replies through this channel, so do not "
    "wait for a response or send the same feedback again."
)

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_PLACEHOLDER_RE = re.compile(
    r"\[[^\]\n]{2,}\]"
    r"|\b(?:placeholder|platzhalter|tbd|tba|lorem ipsum"
    r"|to be (?:determined|confirmed|decided))\b",
    re.IGNORECASE,
)
_EMAIL_ADAPTER = TypeAdapter(EmailStr)


class ClauseAIError(Exception):
    """Base error for ClauseAI operations.

    Messages are read by AI agents as well as people: each one says what
    was wrong, what is valid, and what to do next.
    """


class TemplateNotFoundError(ClauseAIError):
    """Raised when a template slug does not exist."""


class InvalidFormatError(ClauseAIError):
    """Raised when a download format is not supported."""


class RenderUnavailableError(ClauseAIError):
    """Raised when a renderer dependency is missing."""


class InvalidEmailError(ClauseAIError):
    """Raised when the optional contact email is not an address."""


class InvalidFeedbackError(ClauseAIError):
    """Raised when a feedback submission cannot be accepted."""


class FeedbackRateLimitError(ClauseAIError):
    """Raised when one caller sends feedback too quickly."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(
            f"Feedback limit reached ({FEEDBACK_RATE_LIMIT} messages per "
            f"{FEEDBACK_RATE_WINDOW_SECONDS // 60} minutes). Earlier feedback "
            f"was received. Wait {retry_after} seconds before sending more, "
            "and do not retry in a loop."
        )


class InvalidAnswersError(ClauseAIError):
    """Raised when submitted answers cannot be used to fill a template.

    ``issues`` holds one ``{"field", "code", "message"}`` dict per problem
    so a caller can fix everything in a single retry.
    """

    def __init__(self, slug: str, issues: list[dict[str, str]]) -> None:
        self.slug = slug
        self.issues = issues
        shown = issues[:MAX_REPORTED_ISSUES]
        count = len(issues)
        noun = "answer needs" if count == 1 else "answers need"
        lines = [
            f"No document was generated for {slug}: {count} {noun} fixing. "
            "Fix every item below, then send the request again."
        ]
        lines.extend(f"- {issue['field']}: {issue['message']}" for issue in shown)
        if count > len(shown):
            lines.append(f"- ...and {count - len(shown)} more.")
        lines.append(
            "Field schema: get_template_fields (MCP) or "
            f"GET /api/templates/{slug}. If the template lacks a field the "
            "user needs, report it with send_feedback (MCP) or "
            "POST /api/feedback."
        )
        super().__init__("\n".join(lines))


class TemplateField(BaseModel):
    """One wizard question, optionally mapped to <mark> indices."""

    key: str
    label: str
    question: str
    type: str = "text"
    required: bool = False
    options: list[str] = Field(default_factory=list)
    marks: list[int] = Field(default_factory=list)
    replaces: str = ""
    max_length: int | None = None

    @field_validator("options")
    @classmethod
    def _normalize_omit_options(cls, options: list[str]) -> list[str]:
        return [OMIT_ANSWER if not option.strip() else option for option in options]

    @model_validator(mode="after")
    def _default_max_length(self) -> "TemplateField":
        if self.type == "choice":
            self.max_length = None
        elif self.max_length is None:
            self.max_length = DEFAULT_MAX_LENGTHS.get(
                self.type, DEFAULT_MAX_LENGTHS["text"]
            )
        return self


class TemplateTransform(BaseModel):
    """A curation edit applied to the vendored markdown at load time.

    Transforms live in the manifest so they survive template re-syncs;
    loading fails if a transform no longer matches the vendored text.
    """

    find: str
    replace: str = ""
    regex: bool = False


class TemplateManifest(BaseModel):
    """Curated metadata and questions for a vendored template."""

    slug: str
    title: str
    category: str
    description: str
    source_url: str
    when_needed: str = ""
    transforms: list[TemplateTransform] = Field(default_factory=list)
    fields: list[TemplateField] = Field(default_factory=list)


@dataclass(frozen=True)
class LoadedTemplate:
    """A template plus its curated manifest."""

    manifest: TemplateManifest
    markdown: str
    mark_count: int


@dataclass(frozen=True)
class RenderedDocument:
    """Bytes ready to download."""

    content: bytes
    filename: str
    media_type: str
    format: str
    filled_markdown: str


def _template_dir(slug: str) -> Path:
    return TEMPLATES_DIR / slug


def list_template_slugs() -> list[str]:
    """Return slugs that have both a template and a manifest."""
    if not TEMPLATES_DIR.exists():
        return []
    slugs: list[str] = []
    for path in sorted(TEMPLATES_DIR.iterdir()):
        if not path.is_dir():
            continue
        if (path / "template.md").exists() and (path / "manifest.json").exists():
            slugs.append(path.name)
    return slugs


@lru_cache(maxsize=32)
def load_template(slug: str) -> LoadedTemplate:
    """Load and validate a vendored template."""
    dest = _template_dir(slug)
    manifest_path = dest / "manifest.json"
    template_path = dest / "template.md"
    if not manifest_path.exists() or not template_path.exists():
        raise TemplateNotFoundError(_unknown_template_message(slug))

    manifest = TemplateManifest.model_validate(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    markdown = _apply_transforms(manifest, template_path.read_text(encoding="utf-8"))
    mark_count = len(MARK_RE.findall(markdown))
    _validate_manifest_marks(manifest, mark_count)
    return LoadedTemplate(
        manifest=manifest,
        markdown=markdown,
        mark_count=mark_count,
    )


def _unknown_template_message(slug: str) -> str:
    """Name the valid slugs so a caller can retry without another lookup."""
    known = list_template_slugs()
    message = f"Unknown template: {_quote(slug)}."
    close = difflib.get_close_matches(str(slug).lower(), known, n=1)
    if close:
        message += f" Did you mean {close[0]}?"
    if known:
        message += f" Available slugs: {', '.join(known)}."
    return (
        message + " If none fits, tell the user ClauseAI has no template "
        "for this instead of substituting another document."
    )


def _quote(value: object, limit: int = 60) -> str:
    """Repr a caller-supplied value, shortened for error text and logs."""
    text = str(value)
    if len(text) > limit:
        text = text[:limit] + "..."
    return repr(text)


def _apply_transforms(manifest: TemplateManifest, markdown: str) -> str:
    """Apply curation edits (e.g. removing an alternate clause) to a template."""
    for transform in manifest.transforms:
        if transform.regex:
            updated, count = re.subn(
                transform.find, transform.replace, markdown, flags=re.DOTALL
            )
        else:
            count = markdown.count(transform.find)
            updated = markdown.replace(transform.find, transform.replace)
        if count == 0:
            raise ValueError(
                f"{manifest.slug} transform matched nothing: "
                f"{transform.find[:80]!r}"
            )
        markdown = updated
    return markdown


def _validate_manifest_marks(manifest: TemplateManifest, mark_count: int) -> None:
    seen: set[int] = set()
    for field in manifest.fields:
        for index in field.marks:
            if index < 0 or index >= mark_count:
                raise ValueError(
                    f"{manifest.slug} field {field.key} has invalid "
                    f"mark index {index} (template has {mark_count} marks)"
                )
            if index in seen:
                raise ValueError(
                    f"{manifest.slug} mark index {index} is mapped more " f"than once"
                )
            seen.add(index)


def list_templates() -> list[TemplateManifest]:
    """Return manifests for every vendored template."""
    return [load_template(slug).manifest for slug in list_template_slugs()]


def fill_template(slug: str, answers: dict[str, Any] | None) -> str:
    """Replace answered marks; leave unanswered marks as placeholder text.

    Unanswered choice marks become ``[<label>]`` so alternative legal
    branches are never published together. Editorial brackets around a
    replaced mark, as in ``[<mark>Delaware</mark>]``, are dropped.
    """
    loaded = load_template(slug)
    markdown = loaded.markdown
    values = _answers_by_mark(loaded.manifest, answers or {})
    choice_placeholders = _choice_placeholders_by_mark(loaded.manifest)
    parts: list[str] = []
    cursor = 0
    for index, match in enumerate(MARK_RE.finditer(markdown)):
        start, end = match.start(), match.end()
        if index in values:
            replacement = values[index]
        elif index in choice_placeholders:
            replacement = choice_placeholders[index]
        else:
            parts.append(markdown[cursor:start])
            parts.append(match.group(1))
            cursor = end
            continue
        if _is_bracketed(markdown, start, end):
            start, end = start - 1, end + 1
        parts.append(markdown[cursor:start])
        parts.append(replacement)
        cursor = end
    parts.append(markdown[cursor:])
    return "".join(parts)


def _is_bracketed(markdown: str, start: int, end: int) -> bool:
    """True when a mark sits inside ``[...]`` that is not Markdown link text."""
    return (
        start > 0
        and markdown[start - 1] == "["
        and markdown[end : end + 1] == "]"
        and markdown[end + 1 : end + 2] != "("
    )


def _choice_placeholders_by_mark(manifest: TemplateManifest) -> dict[int, str]:
    """Map choice-field marks to a non-assertive ``[label]`` placeholder."""
    placeholders: dict[int, str] = {}
    for field in manifest.fields:
        if field.type != "choice":
            continue
        for index in field.marks:
            placeholders[index] = f"[{field.label}]"
    return placeholders


def _escape_answer(value: str) -> str:
    """Treat a user answer as literal text in Markdown later parsed as HTML."""
    escaped = html.escape(value, quote=True)
    # Markdown images survive HTML escaping and still fetch during PDF render.
    return escaped.replace("![", "&#33;[")


def _answers_by_mark(
    manifest: TemplateManifest, answers: dict[str, Any]
) -> dict[int, str]:
    mapped: dict[int, str] = {}
    for field in manifest.fields:
        raw = answers.get(field.key)
        if raw is None:
            continue
        value = str(raw).strip()
        if value == "":
            continue
        replacement = "" if value == OMIT_ANSWER else _escape_answer(value)
        for index in field.marks:
            mapped[index] = replacement
    _apply_token_replacements(manifest, answers, mapped)
    return mapped


def _apply_token_replacements(
    manifest: TemplateManifest,
    answers: dict[str, Any],
    mapped: dict[int, str],
) -> None:
    """Fill companion tokens such as ``[INSERT DESCRIPTION]`` in mapped marks."""
    for field in manifest.fields:
        if not field.replaces:
            continue
        raw = answers.get(field.key)
        value = str(raw).strip() if raw is not None else ""
        fill = _escape_answer(value) if value else f"[{field.label}]"
        for index, text in mapped.items():
            if field.replaces in text:
                mapped[index] = text.replace(field.replaces, fill)


def normalize_format(fmt: str | None) -> FormatName:
    """Normalize a user-supplied format name."""
    value = (fmt or "pdf").strip().lower()
    if value in {"md", "markdown", "text"}:
        return "markdown"
    if value not in SUPPORTED_FORMATS:
        raise InvalidFormatError(
            f"Unsupported format: {_quote(fmt)}. Use pdf, odt, or markdown."
        )
    return value  # type: ignore[return-value]


async def render_document(markdown: str, fmt: str) -> bytes:
    """Render filled markdown to the requested download format."""
    normalized = normalize_format(fmt)
    if normalized == "markdown":
        return markdown.encode("utf-8")
    if normalized == "pdf":
        return await _render_pdf(markdown)
    return _render_odt(markdown)


async def _render_pdf(markdown: str) -> bytes:
    from clauseai.pdf import markdown_to_pdf

    return await markdown_to_pdf(markdown)


def _render_odt(markdown: str) -> bytes:
    if shutil.which("pandoc") is None:
        raise RenderUnavailableError(
            "ODT export is unavailable on this server (pandoc is not "
            "installed). Retrying will not help; use format pdf or markdown."
        )
    try:
        import pypandoc
    except ImportError as exc:
        raise RenderUnavailableError(
            "ODT export is unavailable on this server (pypandoc is not "
            "installed). Retrying will not help; use format pdf or markdown."
        ) from exc

    handle = tempfile.NamedTemporaryFile(suffix=".odt", delete=False)
    output_path = handle.name
    handle.close()
    try:
        # Templates use --- rules that pandoc otherwise reads as YAML.
        pypandoc.convert_text(
            markdown,
            "odt",
            format="markdown-yaml_metadata_block",
            outputfile=output_path,
        )
        with open(output_path, "rb") as handle:
            return handle.read()
    except Exception as exc:
        logger.exception("ODT conversion failed")
        raise RenderUnavailableError(
            f"ODT conversion failed: {exc}. Use format pdf or markdown instead."
        ) from exc
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


def build_filename(slug: str, fmt: str) -> str:
    """Return a download filename for a generated document."""
    normalized = normalize_format(fmt)
    extension = "md" if normalized == "markdown" else normalized
    return f"{slug}.{extension}"


async def generate_document(
    slug: str,
    answers: dict[str, Any] | None,
    fmt: str,
) -> RenderedDocument:
    """Fill a template and render it in the requested format."""
    filled = fill_template(slug, answers)
    content = await render_document(filled, fmt)
    normalized = normalize_format(fmt)
    return RenderedDocument(
        content=content,
        filename=build_filename(slug, normalized),
        media_type=MEDIA_TYPES[normalized],
        format=normalized,
        filled_markdown=filled,
    )


def answered_fields(slug: str, answers: dict[str, Any] | None) -> dict[str, str]:
    """Return only non-empty answers that match known field keys."""
    loaded = load_template(slug)
    cleaned: dict[str, str] = {}
    for field in loaded.manifest.fields:
        raw = (answers or {}).get(field.key)
        if raw is None:
            continue
        value = str(raw).strip()
        if value:
            cleaned[field.key] = value
    return cleaned


def _issue(field: str, code: str, message: str) -> dict[str, str]:
    return {"field": field, "code": code, "message": message}


def _long_date(value: date) -> str:
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def _parse_iso_date(value: str) -> date | None:
    """Return a date for YYYY-MM-DD text, or None when it is not ISO shaped.

    Raises ValueError when the text is ISO shaped but not a real date.
    """
    match = _ISO_DATE_RE.match(value)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    return date(year, month, day)


def _match_option(field: TemplateField, value: str) -> str | None:
    """Return the published option a choice answer refers to, if any."""
    if value in field.options:
        return value
    folded = " ".join(value.split()).casefold()
    for option in field.options:
        if " ".join(option.split()).casefold() == folded:
            return option
    return None


def _describe_options(field: TemplateField) -> str:
    described = []
    for option in field.options:
        if option == OMIT_ANSWER:
            described.append(f'"{OMIT_ANSWER}" (removes the clause)')
        else:
            described.append(f'"{option}"')
    return "; ".join(described)


def validate_answers(slug: str, answers: dict[str, Any] | None) -> dict[str, str]:
    """Return answers ready to fill a template, or raise InvalidAnswersError.

    Empty answers are dropped as unanswered. ISO dates become long-form
    dates. Every problem is collected so one retry can fix them all.
    """
    manifest = load_template(slug).manifest
    fields = {field.key: field for field in manifest.fields}
    valid_keys = ", ".join(fields)
    issues: list[dict[str, str]] = []
    cleaned: dict[str, str] = {}

    for key, raw in (answers or {}).items():
        field = fields.get(str(key))
        if field is None:
            message = f"not a field on this template. Valid keys: {valid_keys}."
            close = difflib.get_close_matches(str(key), list(fields), n=1)
            if close:
                message += f" Did you mean {close[0]}?"
            message += (
                " Remove this key. Templates are fixed text, so terms without "
                "a field cannot be added here; tell the user to add them to "
                "the downloaded document with their attorney."
            )
            issues.append(_issue(str(key)[:60], "unknown_field", message))
            continue
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            issues.append(
                _issue(
                    field.key,
                    "wrong_type",
                    f"must be a string, got {type(raw).__name__}. "
                    f'Send plain text answering "{field.question}"',
                )
            )
            continue
        value = str(raw).strip()
        if not value:
            continue
        problem = _check_answer(field, value)
        if isinstance(problem, dict):
            issues.append(problem)
        else:
            cleaned[field.key] = problem

    if issues:
        raise InvalidAnswersError(slug, issues)
    # Manifest order keeps logs and download tokens stable.
    return {key: cleaned[key] for key in fields if key in cleaned}


def _check_answer(field: TemplateField, value: str) -> str | dict[str, str]:
    """Return the cleaned value for one answer, or an issue describing it."""
    if field.type == "choice":
        option = _match_option(field, value)
        if option is None:
            return _issue(
                field.key,
                "invalid_choice",
                f"{_quote(value)} is not one of this field's options. Send one "
                f"option exactly as written: {_describe_options(field)}. Omit "
                "the field to leave a placeholder for the user to decide.",
            )
        return option

    limit = field.max_length or DEFAULT_MAX_LENGTHS["text"]
    if len(value) > limit:
        return _issue(
            field.key,
            "too_long",
            f"{len(value)} characters; the limit is {limit}. This field "
            f'fills a short blank answering "{field.question}" Send only '
            "that value. Templates are fixed text: extra clauses, "
            "definitions, governing law, or translations cannot be added "
            "through a field. Tell the user that other terms must be added "
            "to the downloaded document by them or their attorney.",
        )

    if field.type == "email":
        try:
            _EMAIL_ADAPTER.validate_python(value)
        except ValidationError:
            return _issue(
                field.key,
                "invalid_email",
                f"{_quote(value)} is not an email address. Send one address "
                "such as privacy@example.com, or omit the field.",
            )
        return value

    if field.type == "date":
        try:
            parsed = _parse_iso_date(value)
        except ValueError:
            return _issue(
                field.key,
                "invalid_date",
                f"{_quote(value)} is not a real calendar date. Send "
                "YYYY-MM-DD (for example 2026-01-31) or a written date.",
            )
        if parsed is not None:
            return _long_date(parsed)
    return value


def answer_warnings(slug: str, answers: dict[str, Any] | None) -> list[str]:
    """Return notes about accepted answers the caller should double-check.

    Pass the answers as submitted, so ISO dates can still be read.
    """
    manifest = load_template(slug).manifest
    today = datetime.now(timezone.utc).date()
    warnings: list[str] = []
    for field in manifest.fields:
        raw = (answers or {}).get(field.key)
        if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
            continue
        value = str(raw).strip()
        if not value or field.type == "choice":
            continue
        if _PLACEHOLDER_RE.search(value):
            warnings.append(
                f"{field.key} looks like placeholder text ({_quote(value)}) "
                "and was printed in the document as written. If the real "
                "value is unknown, ask the user or omit the field so the "
                "template's own placeholder is kept."
            )
        if field.type == "date":
            try:
                parsed = _parse_iso_date(value)
            except ValueError:
                parsed = None
            if parsed is not None and (today - parsed).days > 365:
                warnings.append(
                    f"{field.key} is {_long_date(parsed)}, more than a year "
                    f"before today ({_long_date(today)}). Confirm the date "
                    "with the user unless they gave it to you."
                )
    return warnings


def unfilled_fields(slug: str, answers: dict[str, str] | None) -> list[str]:
    """Return field keys still showing a placeholder in the document."""
    manifest = load_template(slug).manifest
    given = answers or {}
    missing: list[str] = []
    for field in manifest.fields:
        if field.key in given:
            continue
        if field.replaces and not any(
            field.replaces in value for value in given.values()
        ):
            # Companion text for a choice the caller did not pick.
            continue
        missing.append(field.key)
    return missing


def normalize_email(raw: str | None) -> str | None:
    """Return the optional contact email, or raise InvalidEmailError."""
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    try:
        _EMAIL_ADAPTER.validate_python(value)
    except ValidationError:
        raise InvalidEmailError(
            f"Invalid email address: {_quote(value)}. email is optional: "
            "send one address such as founder@example.com, or leave it out. "
            "Do not invent an address for the user."
        ) from None
    return value


def generation_payload(
    *,
    slug: str,
    fmt: str,
    answers: dict[str, str],
    email: str | None,
    source: str,
    ip_address: str | None,
    user_agent: str | None,
    mcp_client: str | None = None,
    mcp_client_version: str | None = None,
    origin: str | None = None,
) -> dict[str, Any]:
    """Build the shared log / Slack payload for a generation."""
    loaded = load_template(slug)
    payload: dict[str, Any] = {
        "template_slug": slug,
        "template_title": loaded.manifest.title,
        "format": fmt,
        "source": source,
        "answers": answers,
        "email": email or None,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "mcp_client": mcp_client or None,
        "mcp_client_version": mcp_client_version or None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if origin:
        payload["origin"] = origin
    return payload


async def record_generation(*, payload: dict[str, Any]) -> None:
    """Log a generation to loguru and optionally Slack."""
    email = payload.get("email")
    message = (
        "ClauseAI document generated: "
        f"slug={payload.get('template_slug')} "
        f"format={payload.get('format')} "
        f"source={payload.get('source')} "
        f"email={email or 'none'} "
        f"answers={payload.get('answers')}"
    )
    logger.info(message + _caller_suffix(payload))

    from clauseai.notify import notify_generation

    await _notify_in_background(notify_generation, payload)


def _caller_suffix(payload: dict[str, Any]) -> str:
    """Format who made a request for a log line."""
    suffix = ""
    client = payload.get("mcp_client")
    if client:
        suffix += f" client={client}"
        version = payload.get("mcp_client_version")
        if version:
            suffix += f" version={version}"
    if payload.get("origin"):
        suffix += f" origin={payload.get('origin')}"
    if payload.get("ip_address"):
        suffix += f" ip={payload.get('ip_address')}"
    if payload.get("user_agent"):
        user_agent = " ".join(str(payload["user_agent"]).split())
        suffix += f" ua={user_agent[:200]!r}"
    return suffix


_background_tasks: set[asyncio.Task[None]] = set()


async def _notify_in_background(notify, payload: dict[str, Any]) -> None:
    """Send a Slack notification without delaying the response."""

    async def _notify() -> None:
        try:
            await notify(payload)
        except Exception:
            logger.exception("Failed to send ClauseAI Slack notification")

    try:
        task = asyncio.get_running_loop().create_task(_notify())
    except RuntimeError:
        await _notify()
        return
    # The loop keeps only weak references to tasks.
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def log_rejected_generation(
    exc: InvalidAnswersError,
    *,
    source: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
    mcp_client: str | None = None,
    mcp_client_version: str | None = None,
    origin: str | None = None,
) -> None:
    """Log which answers were refused, without the answer text."""
    issues = ",".join(f"{issue['field']}:{issue['code']}" for issue in exc.issues)
    caller = {
        "ip_address": ip_address,
        "user_agent": user_agent,
        "mcp_client": mcp_client,
        "mcp_client_version": mcp_client_version,
        "origin": origin,
    }
    logger.warning(
        f"ClauseAI generation rejected: slug={exc.slug} source={source} "
        f"issues={issues}" + _caller_suffix(caller)
    )


_feedback_times: dict[str, deque[float]] = {}


def check_feedback_rate(ip_address: str | None, *, now: float | None = None) -> None:
    """Allow a few feedback messages per caller per window, in memory."""
    current = time.monotonic() if now is None else now
    cutoff = current - FEEDBACK_RATE_WINDOW_SECONDS
    for key in [key for key, times in _feedback_times.items() if times[-1] <= cutoff]:
        del _feedback_times[key]
    times = _feedback_times.setdefault(ip_address or "unknown", deque())
    while times and times[0] <= cutoff:
        times.popleft()
    if len(times) >= FEEDBACK_RATE_LIMIT:
        raise FeedbackRateLimitError(
            max(1, int(times[0] + FEEDBACK_RATE_WINDOW_SECONDS - current) + 1)
        )
    times.append(current)


def feedback_payload(
    *,
    message: str | None,
    category: str | None,
    slug: str | None,
    email: str | None,
    source: str,
    ip_address: str | None,
    user_agent: str | None,
    mcp_client: str | None = None,
    mcp_client_version: str | None = None,
    origin: str | None = None,
) -> dict[str, Any]:
    """Validate a feedback submission and build its log / Slack payload."""
    text = str(message or "").strip()
    if not text:
        raise InvalidFeedbackError(
            "message is required. Say what you were trying to do, which "
            "template you used, and what went wrong or was missing."
        )
    if len(text) > FEEDBACK_MAX_LENGTH:
        raise InvalidFeedbackError(
            f"message is {len(text)} characters; the limit is "
            f"{FEEDBACK_MAX_LENGTH}. Summarize the problem and leave out "
            "document text and personal details."
        )
    kind = str(category or "other").strip().lower() or "other"
    if kind not in FEEDBACK_CATEGORIES:
        raise InvalidFeedbackError(
            f"Unknown category: {_quote(kind)}. Use one of "
            f"{', '.join(FEEDBACK_CATEGORIES)}, or leave it out."
        )
    template_slug = str(slug or "").strip() or None
    if template_slug is not None:
        template_slug = load_template(template_slug).manifest.slug
    payload: dict[str, Any] = {
        "message": text,
        "category": kind,
        "template_slug": template_slug,
        "source": source,
        "email": normalize_email(email),
        "ip_address": ip_address,
        "user_agent": user_agent,
        "mcp_client": mcp_client or None,
        "mcp_client_version": mcp_client_version or None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if origin:
        payload["origin"] = origin
    return payload


async def record_feedback(*, payload: dict[str, Any]) -> None:
    """Log feedback to loguru and post it to Slack when configured."""
    logger.info(
        "ClauseAI feedback received: "
        f"category={payload.get('category')} "
        f"slug={payload.get('template_slug') or 'none'} "
        f"source={payload.get('source')} "
        f"email={payload.get('email') or 'none'} "
        f"message={payload.get('message')!r}" + _caller_suffix(payload)
    )

    from clauseai.notify import notify_feedback

    await _notify_in_background(notify_feedback, payload)


def summarize_templates() -> list[dict[str, Any]]:
    """Public list payload for humans and agents."""
    items: list[dict[str, Any]] = []
    for manifest in list_templates():
        items.append(
            {
                "slug": manifest.slug,
                "title": manifest.title,
                "category": manifest.category,
                "description": manifest.description,
                "when_needed": manifest.when_needed,
                "source_url": manifest.source_url,
                "field_count": len(manifest.fields),
            }
        )
    return items


def template_schema(slug: str) -> dict[str, Any]:
    """Public field schema for one template."""
    manifest = load_template(slug).manifest
    return {
        "slug": manifest.slug,
        "title": manifest.title,
        "category": manifest.category,
        "description": manifest.description,
        "when_needed": manifest.when_needed,
        "source_url": manifest.source_url,
        "fields": [field.model_dump() for field in manifest.fields],
    }
