"""Structured logging for every locator attempt during test execution."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LocatorAttemptLog(Base):
    """Records every locator attempt with full scoring details for data-loop training."""

    __tablename__ = "locator_attempt_logs"
    __table_args__ = (
        Index("ix_lal_run_step", "run_id", "step_index"),
        Index("ix_lal_domain_strategy", "domain", "selector_type"),
        Index("ix_lal_project_success", "project_id", "overall_success"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("test_case_runs.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_action: Mapped[str] = mapped_column(String(20), nullable=False)
    target_description: Mapped[str] = mapped_column(String(200), nullable=False)
    page_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    page_url_pattern: Mapped[str] = mapped_column(String(500), nullable=False)

    candidates_json: Mapped[str] = mapped_column(Text, nullable=False)
    selected_candidate: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_used: Mapped[str] = mapped_column(String(50), nullable=False)
    fallback_tier_reached: Mapped[int] = mapped_column(Integer, nullable=False)

    pre_features: Mapped[str | None] = mapped_column(Text, nullable=True)
    runtime_features: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_score: Mapped[float] = mapped_column(Float, nullable=False)

    action_success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    postcondition_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    postcondition_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    click_recovery_used: Mapped[str | None] = mapped_column(String(50), nullable=True)

    overall_success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    element_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    selector_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(200), nullable=True)
    route: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
