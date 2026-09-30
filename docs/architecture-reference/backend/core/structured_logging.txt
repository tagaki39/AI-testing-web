"""Structured JSON logging for AI planning, tool calls, DSL execution, and locator fallback."""

from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

# ── Context vars for automatic trace injection ──────────────────────────────

_current_session_id: ContextVar[int | None] = ContextVar("session_id", default=None)
_current_execution_id: ContextVar[int | None] = ContextVar("execution_id", default=None)

# ── Category constants ──────────────────────────────────────────────────────

CATEGORY_AI_THINKING = "ai_thinking"
CATEGORY_TOOL_CALL = "tool_call"
CATEGORY_DSL_EXECUTION = "dsl_execution"
CATEGORY_LOCATOR_FALLBACK = "locator_fallback"


# ── LogContext ──────────────────────────────────────────────────────────────

@contextmanager
def LogContext(
    *,
    session_id: int | None = None,
    execution_id: int | None = None,
):
    """Set session_id / execution_id for all structured logs in this scope."""
    tokens = []
    if session_id is not None:
        tokens.append(_current_session_id.set(session_id))
    if execution_id is not None:
        tokens.append(_current_execution_id.set(execution_id))
    try:
        yield
    finally:
        for t in tokens:
            t.var.set(t.old_value)


# ── Structured JSON Formatter ──────────────────────────────────────────────

class StructuredJsonFormatter(logging.Formatter):
    """Serialize LogRecord to a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        # Extract structured fields from extra
        category = getattr(record, "category", None)
        event_type = getattr(record, "event_type", None)
        data = getattr(record, "data", None)
        message_override = getattr(record, "message_override", None)

        # Build trace
        trace: dict[str, Any] = {}
        sid = getattr(record, "session_id", None) or _current_session_id.get(None)
        eid = getattr(record, "execution_id", None) or _current_execution_id.get(None)
        if sid is not None:
            trace["session_id"] = sid
        if eid is not None:
            trace["execution_id"] = eid

        envelope: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "category": category,
            "event_type": event_type,
            "message": message_override or record.getMessage(),
        }
        if data is not None:
            # Truncate large fields to prevent log bloat
            envelope["data"] = _truncate_data(data)
        if trace:
            envelope["trace"] = trace

        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def _truncate_data(data: Any, max_field_len: int = 4096) -> Any:
    """Truncate string fields in data dict to max_field_len."""
    if not isinstance(data, dict):
        return data
    result = {}
    for k, v in data.items():
        if isinstance(v, str) and len(v) > max_field_len:
            result[k] = v[:max_field_len] + "...[truncated]"
        elif isinstance(v, dict):
            result[k] = _truncate_data(v, max_field_len)
        else:
            result[k] = v
    return result


# ── CategoryLogger ──────────────────────────────────────────────────────────

class CategoryLogger:
    """Thin wrapper around logging.Logger with category convenience methods."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def ai_thinking(
        self,
        event_type: str,
        *,
        data: dict[str, Any] | None = None,
        message: str = "",
        session_id: int | None = None,
        level: int = logging.INFO,
    ) -> None:
        self._log(level, CATEGORY_AI_THINKING, event_type, message, data, session_id=session_id)

    def tool_call(
        self,
        event_type: str,
        *,
        data: dict[str, Any] | None = None,
        message: str = "",
        session_id: int | None = None,
        level: int = logging.INFO,
    ) -> None:
        self._log(level, CATEGORY_TOOL_CALL, event_type, message, data, session_id=session_id)

    def dsl_execution(
        self,
        event_type: str,
        *,
        data: dict[str, Any] | None = None,
        message: str = "",
        execution_id: int | None = None,
        level: int = logging.INFO,
    ) -> None:
        self._log(level, CATEGORY_DSL_EXECUTION, event_type, message, data, execution_id=execution_id)

    def locator_fallback(
        self,
        event_type: str,
        *,
        data: dict[str, Any] | None = None,
        message: str = "",
        execution_id: int | None = None,
        level: int = logging.INFO,
    ) -> None:
        self._log(level, CATEGORY_LOCATOR_FALLBACK, event_type, message, data, execution_id=execution_id)

    def _log(
        self,
        level: int,
        category: str,
        event_type: str,
        message: str,
        data: dict[str, Any] | None,
        *,
        session_id: int | None = None,
        execution_id: int | None = None,
    ) -> None:
        if not self._logger.isEnabledFor(level):
            return
        extra = {
            "category": category,
            "event_type": event_type,
            "data": data,
            "message_override": message or None,
        }
        if session_id is not None:
            extra["session_id"] = session_id
        if execution_id is not None:
            extra["execution_id"] = execution_id
        self._logger._log(level, message or event_type, (), extra=extra)


def get_structured_logger(name: str) -> CategoryLogger:
    """Get a CategoryLogger for the given module name."""
    return CategoryLogger(logging.getLogger(name))


# ── Timing helper ──────────────────────────────────────────────────────────

class Timer:
    """Simple timer for measuring duration in ms."""

    def __init__(self) -> None:
        self._start: float = 0.0

    def start(self) -> None:
        self._start = time.perf_counter()

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)
