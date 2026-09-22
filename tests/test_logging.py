"""Tests for rotating access and application log files."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from loguru import logger

from clauseai.log import add_rotating_file_sinks, get_access_logger


def test_access_log_records_requests_and_skips_health(
    simple_client: TestClient, tmp_path: Path
) -> None:
    """Completed requests are logged; health probes and query strings are not."""
    app_id, access_id = add_rotating_file_sinks(
        tmp_path,
        rotation="10 MB",
        retention=10,
        enqueue=False,
    )
    try:
        response = simple_client.get(
            "/api/templates?email=secret@example.com",
            headers={"X-Forwarded-For": "203.0.113.8, 10.0.0.1"},
        )
        assert response.status_code == 200
        health = simple_client.get("/health")
        assert health.status_code == 200
    finally:
        logger.remove(app_id)
        logger.remove(access_id)

    access_log = (tmp_path / "access.log").read_text(encoding="utf-8")
    lines = [line for line in access_log.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "203.0.113.8 | GET /api/templates | 200 |" in lines[0]
    assert lines[0].endswith("ms")
    assert "secret@example.com" not in access_log
    assert "/health" not in access_log
    app_log = tmp_path / "app.log"
    assert not app_log.exists() or "/api/templates" not in app_log.read_text(
        encoding="utf-8"
    )


def test_rotating_file_sink_keeps_last_n(tmp_path: Path) -> None:
    """Size rotation gzips archives and deletes all but the newest N."""
    retention = 3
    app_id, access_id = add_rotating_file_sinks(
        tmp_path,
        rotation=100,
        retention=retention,
        enqueue=False,
    )
    access_logger = get_access_logger()
    try:
        for index in range(30):
            access_logger.info("x" * 80 + f" {index}")
    finally:
        logger.remove(app_id)
        logger.remove(access_id)

    archives = [
        path
        for path in tmp_path.iterdir()
        if path.name != "access.log" and path.name.startswith("access")
    ]
    assert len(archives) == retention
    assert all(path.suffix == ".gz" for path in archives)
    assert (tmp_path / "access.log").is_file()
