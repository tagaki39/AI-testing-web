"""Schemas for report center preferences."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.schemas.dsl import DSLModel


ReportScopeType = Literal["global", "project", "case"]


class ReportPreferencePayload(DSLModel):
    scope_type: ReportScopeType = "project"
    project_id: int | None = Field(default=None, ge=1)
    case_id: int | None = Field(default=None, ge=1)
    window_days: Literal[7, 14, 30] = 7

    @model_validator(mode="after")
    def validate_scope(self) -> "ReportPreferencePayload":
        if self.scope_type == "global":
            if self.project_id is not None or self.case_id is not None:
                raise ValueError("Global scope cannot include project_id or case_id.")
        elif self.scope_type == "project":
            if self.project_id is None:
                raise ValueError("Project scope requires project_id.")
            if self.case_id is not None:
                raise ValueError("Project scope cannot include case_id.")
        elif self.project_id is None or self.case_id is None:
            raise ValueError("Case scope requires both project_id and case_id.")
        return self
