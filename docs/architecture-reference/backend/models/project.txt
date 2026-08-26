"""Project model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.ai_planning_session import AIPlanningSession


class Project(Base):
    """Project is the top-level resource boundary for test cases."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean(), default=False, server_default="false")
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

    sessions: Mapped[list["AIPlanningSession"]] = relationship(
        "AIPlanningSession", secondary="session_projects", back_populates="projects", lazy="noload",
    )
