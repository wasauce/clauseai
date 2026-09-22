"""Logging configuration using loguru."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from loguru import logger

from clauseai.config import (
    get_environment,
    get_log_dir,
    get_log_retention,
    get_log_rotation,
)

ACCESS_CHANNEL = "access"

_configured = False

_STDERR_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan> - <level>{message}</level>"
)
_APP_FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name} - {message}"
_ACCESS_FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {message}"


def _is_access_record(record: dict[str, Any]) -> bool:
    return record["extra"].get("channel") == ACCESS_CHANNEL


def _is_app_record(record: dict[str, Any]) -> bool:
    return not _is_access_record(record)


def file_logging_enabled() -> bool:
    """Write log files except under the test environment."""
    return get_environment() != "test"


def add_rotating_file_sinks(
    log_dir: str | Path | None = None,
    rotation: str | int | None = None,
    retention: int | None = None,
    *,
    enqueue: bool = True,
) -> tuple[int, int]:
    """Add rotating app and access files. Returns the two sink ids."""
    directory = Path(log_dir) if log_dir is not None else Path(get_log_dir())
    directory.mkdir(parents=True, exist_ok=True)
    rotation_value = get_log_rotation() if rotation is None else rotation
    retention_value = get_log_retention() if retention is None else retention
    app_id = logger.add(
        directory / "app.log",
        format=_APP_FILE_FORMAT,
        level="INFO",
        filter=_is_app_record,
        rotation=rotation_value,
        retention=retention_value,
        compression="gz",
        enqueue=enqueue,
        encoding="utf-8",
    )
    access_id = logger.add(
        directory / "access.log",
        format=_ACCESS_FILE_FORMAT,
        level="INFO",
        filter=_is_access_record,
        rotation=rotation_value,
        retention=retention_value,
        compression="gz",
        enqueue=enqueue,
        encoding="utf-8",
    )
    return app_id, access_id


def setup_logging() -> None:
    """Configure loguru once for the process."""
    global _configured
    if _configured:
        return
    logger.remove()
    logger.add(
        sys.stderr,
        format=_STDERR_FORMAT,
        level="INFO",
        filter=_is_app_record,
    )
    if file_logging_enabled():
        add_rotating_file_sinks()
    _configured = True


def get_logger(name: str):
    """Return a loguru logger bound to a module name."""
    setup_logging()
    return logger.bind(name=name)


def get_access_logger():
    """Return a logger whose lines are written only to the access file."""
    setup_logging()
    return logger.bind(channel=ACCESS_CHANNEL)
