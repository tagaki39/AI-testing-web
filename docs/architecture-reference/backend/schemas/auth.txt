"""Schemas for local cookie-session authentication."""

from __future__ import annotations

from pydantic import Field, field_validator

from app.schemas.dsl import DSLModel


class LoginRequest(DSLModel):
    email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError("Invalid email format.")
        return v


class CurrentUserResponse(DSLModel):
    id: int = Field(ge=1)
    email: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=100)


class LogoutResponse(DSLModel):
    success: bool = True
