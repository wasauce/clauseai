"""Application configuration."""

from __future__ import annotations

import os


def get_base_url() -> str:
    """Return the public base URL without a trailing slash."""
    return os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")


def get_port() -> int:
    """Return the HTTP listen port."""
    return int(os.getenv("PORT", "8000"))


def get_environment() -> str:
    """Return the current environment name."""
    return os.getenv("ENVIRONMENT", "development").lower()
