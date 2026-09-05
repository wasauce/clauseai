"""Shared pytest fixtures for ClauseAI."""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("BASE_URL", "http://testserver")
os.environ.setdefault("SLACK_WEBHOOK_URL", "")


@pytest.fixture
def simple_client() -> Generator[TestClient, None, None]:
    """HTTP client for the standalone ClauseAI app."""
    from clauseai.main import app

    with TestClient(app) as client:
        yield client
