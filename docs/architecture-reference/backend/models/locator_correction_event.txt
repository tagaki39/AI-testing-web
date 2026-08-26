"""Persisted events for locator correction operations and outcomes."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LocatorCorrectionEvent(Base):
    """Immutable event log for correction lifecycle and Tier 0 outcomes."""

    __tablename__ = "locator_correction_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    correction_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("locator_corrections.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    page_url_pattern: Mapped[str] = mapped_column(String(500), nullable=False)
    target_description: Mapped[str] = mapped_column(String(200), nullable=False)
    execution_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("test_case_runs.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    verified_count_after: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_failures_after: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active_after: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        server_default=func.now(),
        nullable=False,
    )
