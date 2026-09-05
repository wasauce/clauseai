"""Logging configuration using loguru."""

from __future__ import annotations

import sys

from loguru import logger

_configured = False


def setup_logging() -> None:
    """Configure loguru once for the process."""
    global _configured
    if _configured:
        return
    logger.remove()
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan> - <level>{message}</level>"
        ),
        level="INFO",
    )
    _configured = True


def get_logger(name: str):
    """Return a loguru logger bound to a module name."""
    setup_logging()
    return logger.bind(name=name)
