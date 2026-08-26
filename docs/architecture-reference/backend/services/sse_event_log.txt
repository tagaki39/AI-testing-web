"""SSE event persistence — inline, resilient, no upfront dependencies.

Design principles:
- NO initialization step that queries the DB (no __init__ that touches the table)
- Each write is independent and wrapped in try-catch
- If the table doesn't exist, the first write fails → all subsequent writes are skipped
- The main streaming flow is NEVER blocked by event logging
- **Uses a separate DB session** so failures don't corrupt the main session

Usage::

    # Create a logger — NO DB query happens here
    event_log = EventLogWriter(session_factory, session_id)

    for event in stream:
        event_log.write(event["type"], event)  # inline, resilient
        yield event
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


class EventLogWriter:
    """Writes SSE events to ``ai_planning_event_logs`` inline during streaming.

    This class uses a **separate DB session** so that failures in event
    logging never corrupt the main streaming session.  It does NOT query
    the database on initialization.  The first ``write()`` call determines
    whether the table exists.  If it doesn't, all subsequent writes become
    no-ops.
    """

    def __init__(
        self,
        session_factory: sessionmaker,
        session_id: int,
        message_id: int | None = None,
        flush_interval: int = 5,
    ) -> None:
        self._session_factory = session_factory
        self._session_id = session_id
        self._message_id = message_id
        self._flush_interval = flush_interval
        self._pending_count = 0
        self._next_seq = 1
        self._enabled = True
        self._session: Session | None = None  # Lazy init on first write

    def _get_session(self) -> Session | None:
        """Get or create a dedicated session for event logging."""
        if self._session is None:
            try:
                self._session = self._session_factory()
            except Exception as exc:
                logger.warning("Failed to create event log session: %s", exc)
                self._enabled = False
                return None
        return self._session

    def with_message_id(self, message_id: int) -> "EventLogWriter":
        """Return self with updated message_id (for chaining)."""
        self._message_id = message_id
        return self

    def write(self, event_type: str, event_data: dict[str, Any]) -> None:
        """Persist one event.  No-op if logging is disabled.

        If the table doesn't exist or any DB error occurs, logging is
        silently disabled for the rest of this writer's lifetime.
        """
        if not self._enabled:
            return

        session = self._get_session()
        if session is None:
            return

        try:
            from app.models.ai_planning_event_log import AIPlanningEventLog

            session.add(AIPlanningEventLog(
                session_id=self._session_id,
                message_id=self._message_id,
                event_type=event_type,
                event_data=event_data,
                seq=self._next_seq,
            ))
            self._next_seq += 1
            self._pending_count += 1

            if self._pending_count >= self._flush_interval:
                self.flush()
        except Exception as exc:
            # Table doesn't exist, import error, or other DB error — disable.
            self._enabled = False
            self._pending_count = 0
            logger.warning(
                "SSE event logging disabled (session %d): %s",
                self._session_id, exc,
            )
            # Rollback the dedicated session (not the main session).
            try:
                if session.is_active:
                    session.rollback()
            except Exception:
                pass

    def flush(self) -> None:
        """Commit buffered events.  No-op if logging is disabled."""
        if not self._enabled or self._pending_count == 0:
            return

        session = self._get_session()
        if session is None:
            return

        try:
            session.commit()
            logger.debug(
                "Flushed %d SSE events for session %d",
                self._pending_count, self._session_id,
            )
        except Exception as exc:
            logger.warning(
                "Failed to flush SSE events (session %d), disabling: %s",
                self._session_id, exc,
            )
            try:
                if session.is_active:
                    session.rollback()
            except Exception:
                pass
            self._enabled = False
        finally:
            self._pending_count = 0

    def close(self) -> None:
        """Close the dedicated session."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
