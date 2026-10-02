"""OpenAI plugin package, download links, and listing routes."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauseai.downloads import (
    DownloadSigningUnavailable,
    DownloadTokenError,
    build_download_url,
    read_download_token,
)
from clauseai.mcp import (
    SERVER_INSTRUCTIONS,
    generate_document,
    mcp,
)

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "clauseai"


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_plugin_package_meets_listing_limits() -> None:
    """The portable plugin manifest is the directory submission package."""
    manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    mcp_manifest = json.loads((PLUGIN / "mcp.json").read_text(encoding="utf-8"))
    interface = manifest["extensions"]["com.openai"]["interface"]

    assert manifest["$schema"].endswith("/plugin.schema.json")
    assert manifest["name"] == "clauseai"
    assert manifest["version"] == "1.0.0"
    assert manifest["author"]["name"] == interface["developerName"]
    assert interface["displayName"] == "ClauseAI"
    assert len(interface["displayName"]) <= 30
    assert interface["shortDescription"] == "Draft startup legal documents"
    assert len(interface["shortDescription"]) <= 30
    assert interface["category"] == "Business & Operations"
    assert len(interface["longDescription"]) <= 4000
    assert len(interface["defaultPrompt"]) == 3
    assert all(
        len(prompt) <= 128 and "@" not in prompt
        for prompt in interface["defaultPrompt"]
    )
    for key in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
        assert interface[key].startswith("https://clauseai.exe.xyz")
        assert len(interface[key]) <= 1024
    assert interface["supportURL"].endswith("/support")
    assert "screenshots" not in interface
    assert manifest["extensions"]["com.openai"]["onboardingSkill"] == (
        "./skills/draft-legal-document/SKILL.md"
    )

    review = manifest["extensions"]["com.openai"]["review"]
    assert review["commerce"] is False
    assert len(review["test_cases"]["positive"]) == 5
    assert len(review["test_cases"]["negative"]) == 3
    for case in review["test_cases"]["positive"]:
        assert case["prompt"]
        assert case["tools_triggered"]
        assert case["expected_behavior"]
    for case in review["test_cases"]["negative"]:
        assert set(case) == {"description", "prompt"}
    assert manifest["extensions"]["com.openai"]["publication"]["release_notes"]

    assert mcp_manifest["mcpServers"]["clauseai"] == {
        "type": "streamable-http",
        "url": "https://clauseai.exe.xyz/mcp",
    }
    assert not (PLUGIN / ".app.json").exists()
    assert not (PLUGIN / "hooks").exists()

    for name in ("logo.png", "composer-icon.png"):
        width, height = _png_size(PLUGIN / "assets" / name)
        assert width == height
        assert width >= 48


def test_draft_skill_declares_mcp_dependency() -> None:
    """The plugin skill tells the model to use the ClauseAI tools."""
    skill = (PLUGIN / "skills" / "draft-legal-document" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert skill.startswith("---\nname: draft-legal-document\n")
    description = skill.split("---", 2)[1]
    assert "description:" in description
    body_description = description.split("description:", 1)[1].strip().strip('"')
    assert len(body_description) <= 1024
    assert "list_templates" in skill
    assert "get_template_fields" in skill
    assert "generate_document" in skill
    assert "download_url" in skill
    assert "__omit__" in skill
    assert "not legal advice" in skill

    agent = (
        PLUGIN / "skills" / "draft-legal-document" / "agents" / "openai.yaml"
    ).read_text(encoding="utf-8")
    assert "display_name:" in agent
    assert "short_description:" in agent
    assert "allow_implicit_invocation: true" in agent
    assert "CHAT" in agent and "CODEX" in agent
    assert "value: clauseai" in agent
    assert "transport: streamable_http" in agent
    assert "https://clauseai.exe.xyz/mcp" in agent


def test_repo_marketplace_points_at_plugin() -> None:
    """Local ChatGPT and Codex installs discover the plugin from the repo."""
    marketplace = json.loads(
        (ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )
    entry = marketplace["plugins"][0]
    assert entry["name"] == "clauseai"
    assert entry["source"]["path"] == "./plugins/clauseai"
    assert entry["policy"]["installation"] == "AVAILABLE"
    assert entry["policy"]["authentication"] == "ON_INSTALL"
    assert entry["category"] == "Business & Operations"
    evaluation = (ROOT / ".agents" / "plugins" / "evaluation.md").read_text(
        encoding="utf-8"
    )
    assert "## Direct" in evaluation
    assert "## Indirect" in evaluation
    assert "## Incomplete" in evaluation
    assert "## Out of scope" in evaluation


def test_download_token_round_trip() -> None:
    """A signed token reopens the same slug, format, and answers."""
    url = build_download_url(
        slug="mutual-nda",
        fmt="markdown",
        answers={"company_name": "Acme Inc."},
    )
    token = url.split("token=", 1)[1]
    slug, fmt, answers = read_download_token(token)
    assert slug == "mutual-nda"
    assert fmt == "markdown"
    assert answers == {"company_name": "Acme Inc."}


def test_download_token_rejects_tampering() -> None:
    """Changing the payload invalidates the signature."""
    url = build_download_url(slug="mutual-nda", fmt="markdown", answers={})
    token = url.split("token=", 1)[1]
    body, signature = token.split(".", 1)
    with pytest.raises(DownloadTokenError):
        read_download_token(f"{body[:-1]}A.{signature}")


def test_production_download_signing_requires_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production does not fall back to the development signing secret."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("DOWNLOAD_SIGNING_SECRET", raising=False)
    with pytest.raises(DownloadSigningUnavailable):
        build_download_url(slug="mutual-nda", fmt="pdf", answers={})


@pytest.mark.asyncio
async def test_mcp_generate_returns_markdown_and_download_url(
    simple_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP tool returns text and a link, not PDF bytes."""
    monkeypatch.setenv("BASE_URL", "https://clauseai.exe.xyz")
    result = await generate_document(
        "mutual-nda",
        answers={"company_name": "Acme Inc."},
        format="markdown",
    )
    payload = result.model_dump()
    assert "content_base64" not in payload
    assert "Acme Inc." in result.markdown
    assert result.filename == "mutual-nda.md"
    assert result.download_url.startswith(
        "https://clauseai.exe.xyz/api/downloads?token="
    )

    token = result.download_url.split("token=", 1)[1]
    downloaded = simple_client.get("/api/downloads", params={"token": token})
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("text/markdown")
    assert "mutual-nda.md" in downloaded.headers["content-disposition"]
    assert b"Acme Inc." in downloaded.content
    assert "no-store" in downloaded.headers["cache-control"]


def test_download_route_does_not_record_another_generation(
    simple_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening a download link re-renders without a second generation record."""
    url = build_download_url(
        slug="mutual-nda",
        fmt="markdown",
        answers={"company_name": "Acme Inc."},
    )
    token = url.split("token=", 1)[1]

    async def _record(*, payload: dict) -> None:
        raise AssertionError(payload)

    monkeypatch.setattr("clauseai.routes.record_generation", _record)
    response = simple_client.get("/api/downloads", params={"token": token})
    assert response.status_code == 200


def test_download_route_rejects_bad_token(simple_client: TestClient) -> None:
    """A forged token is a client error."""
    response = simple_client.get("/api/downloads", params={"token": "not-a-token"})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail.startswith("Invalid download token")
    assert "Generate the document again" in detail


def test_openai_domain_challenge(
    simple_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The challenge route returns the exact token, or 404 when unset."""
    monkeypatch.delenv("OPENAI_APPS_CHALLENGE", raising=False)
    missing = simple_client.get("/.well-known/openai-apps-challenge")
    assert missing.status_code == 404

    monkeypatch.setenv("OPENAI_APPS_CHALLENGE", "challenge-token")
    present = simple_client.get("/.well-known/openai-apps-challenge")
    assert present.status_code == 200
    assert present.text == "challenge-token"
    assert present.headers["content-type"].startswith("text/plain")


def test_support_page(simple_client: TestClient) -> None:
    """The listing support URL is a contact page, not a template wizard."""
    page = simple_client.get("/support")
    assert page.status_code == 200
    assert "wferrell@gmail.com" in page.text
    assert "Download now" not in page.text
    assert 'href="/support"' in page.text

    index = simple_client.get("/llms.txt")
    assert "http://testserver/support" in index.text


@pytest.mark.asyncio
async def test_mcp_tool_annotations_and_instructions() -> None:
    """Directory review requires explicit tool annotations and a short instruction."""
    assert len(SERVER_INSTRUCTIONS) <= 512
    assert SERVER_INSTRUCTIONS.startswith(
        "List templates, inspect fields, then generate."
    )
    assert "Do not invent clause text" in SERVER_INSTRUCTIONS

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert set(tools) == {
        "list_templates",
        "get_template_fields",
        "generate_document",
        "send_feedback",
    }
    for name in ("list_templates", "get_template_fields"):
        hints = tools[name].annotations
        assert hints is not None
        assert hints.read_only_hint is True
        assert hints.destructive_hint is False
        assert hints.open_world_hint is False
        assert tools[name].description.startswith("Use this")
    generate = tools["generate_document"].annotations
    assert generate is not None
    assert generate.read_only_hint is False
    assert generate.destructive_hint is False
    assert generate.open_world_hint is False
    feedback = tools["send_feedback"]
    assert feedback.annotations is not None
    assert feedback.annotations.read_only_hint is False
    assert feedback.annotations.destructive_hint is False
    assert feedback.description.startswith("Use this")
    schema = tools["generate_document"].output_schema
    assert schema is not None
    assert "download_url" in schema["properties"]
    assert "markdown" in schema["properties"]
    assert "content_base64" not in schema["properties"]
