"""Atomic flow step entries mapping user workflow to page elements."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AIPlanningFlowStep(Base):
    """Structured index linking test flow steps to explored page elements.

    Each row represents one atomic user action (click, input, wait, etc.)
    with its page state, expected result, and element-level indices for
    knowledge distillation during DSL generation.
    """

    __tablename__ = "ai_planning_flow_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("ai_planning_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    scenario_key: Mapped[str] = mapped_column(
        String(100), index=True, nullable=False
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)

    action: Mapped[str] = mapped_column(String(50), nullable=False)
    target: Mapped[str | None] = mapped_column(String(500), nullable=True)
    value: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    trigger: Mapped[str | None] = mapped_column(String(50), nullable=True)
    expected_result: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    page_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    page_state: Mapped[str | None] = mapped_column(String(10), nullable=True)

    element_indices: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    element_target_keywords: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), server_default=func.now(), onupdate=func.now(), nullable=False
    )
