"""Cross-session project-level test insights."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TestPointInsight(Base):
    """Persisted project-level insights: failure patterns, flaky flags, regression risk."""

    __tablename__ = "test_point_insights"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("projects.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    flaky_case_ids: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    failure_patterns: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    regression_risk: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_analysis_summary: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
