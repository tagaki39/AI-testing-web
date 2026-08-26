"""Stored anti-patterns from failed DSL generations for few-shot self-healing."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DSLAntiPattern(Base):
    """A concrete example of a wrong DSL pattern, used as few-shot negative example.

    When injected into the DSL generator prompt, these examples help the LLM
    avoid repeating the same mistakes. Only ``wrong_snippet`` is stored;
    the correct approach is inferred by the LLM from R1-R8 rules.
    """

    __tablename__ = "dsl_anti_patterns"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("projects.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    error_category: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False,
        doc=(
            "Classification of the error: missing_navigation, missing_wait_for, "
            "missing_input_before_assert, missing_capture_text, target_not_found, "
            "missing_step, wrong_page_state"
        ),
    )
    wrong_snippet: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False,
        doc="The wrong step(s) as JSON — what the LLM generated.",
    )
    context_note: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
        doc="Brief explanation of what's wrong and which rule is violated.",
    )
    rule_violated: Mapped[str | None] = mapped_column(
        String(10), nullable=True,
        doc="The R-number violated (R1, R2, ..., R8), if applicable.",
    )
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="auto",
        doc="How this anti-pattern was captured: auto, manual, execution, preflight.",
    )
    frequency: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1,
        doc="How many times this pattern has been observed. Incremented on duplicates.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), server_default=func.now(), nullable=False, index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), server_default=func.now(), onupdate=func.now(), nullable=False,
    )
