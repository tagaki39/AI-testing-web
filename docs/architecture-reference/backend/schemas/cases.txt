"""Schemas for persisted test cases."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.schemas.dsl import DSLCase, DSLCaseInputContract, DSLCaseOutputContract, DSLModel, DSLStep


class CaseCreateRequest(DSLCase):
    project_id: int = Field(ge=1)
    actor_user_id: int = Field(default=1, ge=1)


class CaseUpdateRequest(DSLCase):
    project_id: int = Field(ge=1)
    actor_user_id: int = Field(default=1, ge=1)


class StoredCaseSummary(DSLModel):
    id: int
    project_id: int
    name: str
    description: str | None = None
    base_url: str | None = None
    input_contract: list[DSLCaseInputContract] = Field(default_factory=list)
    output_contract: list[DSLCaseOutputContract] = Field(default_factory=list)
    steps: list[DSLStep]
    created_by: int
    updated_by: int
    created_at: datetime
    updated_at: datetime


class CaseListFilter(DSLModel):
    """Filter parameters for listing test cases."""
    project_id: int | None = Field(default=None, ge=1)
    search: str | None = Field(default=None, max_length=200)
    created_by: int | None = Field(default=None, ge=1)


class CaseListParams(DSLModel):
    """Query parameters for listing test cases."""
    project_id: int | None = Field(default=None, ge=1)
    search: str | None = Field(default=None, max_length=200)
    created_by: int | None = Field(default=None, ge=1)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100, description="Maximum 100 items per page")

    @field_validator("page_size")
    @classmethod
    def validate_page_size(cls, v: int) -> int:
        if v > 100:
            raise ValueError("Page size cannot exceed 100")
        return v


class PaginatedCases(DSLModel):
    """Paginated response for test cases."""
    items: list[StoredCaseSummary]
    total: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_prev: bool


class BatchUpdateRequest(DSLModel):
    """Request for batch updating test cases."""
    case_ids: list[int] = Field(..., min_length=1, max_length=100)
    updates: CaseUpdateRequest


class BatchDeleteRequest(DSLModel):
    """Request for batch deleting test cases."""
    case_ids: list[int] = Field(..., min_length=1, max_length=100)


class ProjectTestCaseStats(DSLModel):
    """Statistics for test cases in a project."""
    project_id: int
    total_cases: int
    created_by_month: dict[str, int]  # Format: "YYYY-MM": count
    created_by_user: dict[int, str] = Field(default_factory=dict)  # user_id: username (would need user service)
    recent_cases: list[StoredCaseSummary] = Field(default_factory=list)


class StoredCaseDetail(StoredCaseSummary):
    pass
