"""Application configuration."""

from __future__ import annotations

import os

PRODUCTION_BASE_URL = "https://clauseai.exe.xyz"
GROKBOT_URL = "https://x.ai/bot/lBf5ZVjFUDPgn_OVzU8_R"
GITHUB_REPO = "wasauce/clauseai"
SKILL_INSTALL_COMMAND = "npx skills add wasauce/clauseai --skill clauseai"
LISTING_DESCRIPTION = (
    "Generate startup legal documents from attorney-drafted templates: "
    "NDAs, MSAs, DPAs, privacy policies, offer letters, and more. "
    "Download PDF, ODT, or Markdown through MCP or an agent skill. "
    "No account required."
)
MCP_REGISTRY_DESCRIPTION = (
    "Generate attorney-drafted NDAs, MSAs, DPAs, and more as "
    "PDF, ODT, or Markdown. No account required."
)


def get_base_url() -> str:
    """Return the public base URL without a trailing slash."""
    return os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")


def get_port() -> int:
    """Return the HTTP listen port."""
    return int(os.getenv("PORT", "8000"))


def get_environment() -> str:
    """Return the current environment name."""
    return os.getenv("ENVIRONMENT", "development").lower()
