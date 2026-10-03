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
    "Download PDF, Word, ODT, or Markdown through MCP or an agent skill. "
    "No account required."
)
MCP_REGISTRY_DESCRIPTION = (
    "Generate attorney-drafted NDAs, MSAs, DPAs, and more as "
    "PDF, Word, or Markdown. No account required."
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


def get_log_dir() -> str:
    """Return the directory for rotating log files."""
    return os.getenv("LOG_DIR", "logs")


def get_log_rotation() -> str:
    """Return the Loguru size at which each log file rotates."""
    return os.getenv("LOG_ROTATION", "10 MB")


def get_log_retention() -> int:
    """Return how many rotated log files to keep for each stream."""
    return int(os.getenv("LOG_RETENTION", "10"))


_DEV_DOWNLOAD_SIGNING_SECRET = "clauseai-dev-download-secret"


def get_download_signing_secret() -> str:
    """Return the HMAC secret for stateless document download links.

    Production must set DOWNLOAD_SIGNING_SECRET. Other environments use a
    fixed development secret when the variable is empty.
    """
    raw = os.getenv("DOWNLOAD_SIGNING_SECRET", "").strip()
    if raw:
        return raw
    if get_environment() == "production":
        raise RuntimeError("DOWNLOAD_SIGNING_SECRET is required in production")
    return _DEV_DOWNLOAD_SIGNING_SECRET
