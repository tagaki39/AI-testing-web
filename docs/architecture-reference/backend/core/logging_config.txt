"""Centralized logging configuration for the backend."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from app.core.structured_logging import StructuredJsonFormatter

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers that should stay quiet
_THIRD_PARTY_LOGGERS = [
    "uvicorn.access",
    "httpx",
    "httpcore",
    "sqlalchemy.engine",
    "aiosqlite",
    "multipart",
]


def setup_logging(level: str | None = None) -> None:
    """Configure structured logging for the entire application.

    Args:
        level: Override log level. Falls back to env var ``LOG_LEVEL``,
               then defaults to ``INFO``.
    """
    effective_level = level or os.getenv("LOG_LEVEL", "INFO").upper()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # Console handler (for development)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    handlers: list[logging.Handler] = [console]

    # Structured JSON file handler
    backend_root = Path(__file__).resolve().parents[2]
    structured_log_file = backend_root / "backend_structured.log"
    structured_handler = logging.handlers.RotatingFileHandler(
        structured_log_file,
        maxBytes=50 * 1024 * 1024,  # 50MB
        backupCount=5,
        encoding="utf-8",
    )
    structured_handler.setFormatter(StructuredJsonFormatter())
    handlers.append(structured_handler)

    # Root logger
    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(logging.WARNING)

    # Application loggers
    app_logger = logging.getLogger("app")
    app_logger.setLevel(getattr(logging, effective_level, logging.INFO))
    app_logger.handlers.clear()
    for h in handlers:
        app_logger.addHandler(h)
    app_logger.propagate = False

    # Quiet third-party loggers
    for name in _THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.setLevel(logging.WARNING)
        third_party.propagate = False


def get_uvicorn_log_config() -> dict:
    """Return a uvicorn-compatible log config dict that matches our format."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": LOG_FORMAT,
                "datefmt": DATE_FORMAT,
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["default"],
                "level": "WARNING",
                "propagate": False,
            },
        },
    }
