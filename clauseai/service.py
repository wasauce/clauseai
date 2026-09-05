"""Fill and render General Legal templates for ClauseAI."""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

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


class ClauseAIError(Exception):
    """Base error for ClauseAI operations."""


class TemplateNotFoundError(ClauseAIError):
    """Raised when a template slug does not exist."""


class InvalidFormatError(ClauseAIError):
    """Raised when a download format is not supported."""


class RenderUnavailableError(ClauseAIError):
    """Raised when a renderer dependency is missing."""


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

    @field_validator("options")
    @classmethod
    def _normalize_omit_options(cls, options: list[str]) -> list[str]:
        return [OMIT_ANSWER if not option.strip() else option for option in options]


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
        raise TemplateNotFoundError(f"Unknown template: {slug}")

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
    branches are never published together.
    """
    loaded = load_template(slug)
    values = _answers_by_mark(loaded.manifest, answers or {})
    choice_placeholders = _choice_placeholders_by_mark(loaded.manifest)
    parts: list[str] = []
    cursor = 0
    for index, match in enumerate(MARK_RE.finditer(loaded.markdown)):
        parts.append(loaded.markdown[cursor : match.start()])
        if index in values:
            parts.append(values[index])
        elif index in choice_placeholders:
            parts.append(choice_placeholders[index])
        else:
            parts.append(match.group(1))
        cursor = match.end()
    parts.append(loaded.markdown[cursor:])
    return "".join(parts)


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
            f"Unsupported format: {fmt}. Use pdf, odt, or markdown."
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
            "ODT export requires the pandoc binary to be installed."
        )
    try:
        import pypandoc
    except ImportError as exc:
        raise RenderUnavailableError(
            "ODT export requires the pypandoc package."
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
        raise RenderUnavailableError(f"ODT conversion failed: {exc}") from exc
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


def generation_payload(
    *,
    slug: str,
    fmt: str,
    answers: dict[str, str],
    email: str | None,
    source: str,
    ip_address: str | None,
    user_agent: str | None,
) -> dict[str, Any]:
    """Build the shared log / Slack payload for a generation."""
    loaded = load_template(slug)
    return {
        "template_slug": slug,
        "template_title": loaded.manifest.title,
        "format": fmt,
        "source": source,
        "answers": answers,
        "email": email or None,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def record_generation(*, payload: dict[str, Any]) -> None:
    """Log a generation to loguru and optionally Slack."""
    email = payload.get("email")
    logger.info(
        "ClauseAI document generated: "
        f"slug={payload.get('template_slug')} "
        f"format={payload.get('format')} "
        f"source={payload.get('source')} "
        f"email={email or 'none'} "
        f"answers={payload.get('answers')}"
    )

    from clauseai.notify import notify_generation

    async def _notify() -> None:
        try:
            await notify_generation(payload)
        except Exception:
            logger.exception("Failed to send ClauseAI Slack notification")

    try:
        asyncio.get_running_loop().create_task(_notify())
    except RuntimeError:
        await _notify()


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
