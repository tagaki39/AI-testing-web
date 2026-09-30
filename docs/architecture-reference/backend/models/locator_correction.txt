"""Persisted human locator corrections."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LocatorCorrection(Base):
    """Reusable manual correction for a page URL pattern and target description."""

    __tablename__ = "locator_corrections"
    __table_args__ = (
        CheckConstraint(
            "correction_type IN ('css', 'xpath', 'test_id')",
            name="ck_locator_corrections_correction_type",
        ),
        Index(
            "ix_locator_corrections_lookup",
            "page_url_pattern",
            "normalized_target_description",
        ),
        Index(
            "uq_locator_corrections_active_lookup",
            "page_url_pattern",
            "normalized_target_description",
            unique=True,
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    page_url_pattern: Mapped[str] = mapped_column(String(500), nullable=False)
    target_description: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_target_description: Mapped[str] = mapped_column(String(200), nullable=False)
    correction_type: Mapped[str] = mapped_column(String(20), nullable=False)
    correction_value: Mapped[str] = mapped_column(Text(), nullable=False)
    verified_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    source_execution_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("test_case_runs.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    created_by: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
