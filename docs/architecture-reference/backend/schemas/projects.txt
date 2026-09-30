"""Schemas for project management."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.dsl import DSLModel


class ProjectSummary(DSLModel):
    id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)


class ProjectCreate(DSLModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)


class ProjectUpdate(DSLModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)


class ProjectDetail(ProjectSummary):
    created_at: datetime
    updated_at: datetime
