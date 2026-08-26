"""Persisted audit records for AI DSL generation requests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DslGenerationRun(Base):
    """Immutable-ish generation audit row for prompt governance and debugging."""

    __tablename__ = "dsl_generation_runs"
    __table_args__ = (
        CheckConstraint(
            "generation_mode IN ('draft', 'strict_steps_only')",
            name="ck_dsl_generation_runs_generation_mode",
        ),
        CheckConstraint(
            "import_mode IN ('replace', 'steps_only', 'contracts_only')",
            name="ck_dsl_generation_runs_import_mode",
        ),
        CheckConstraint(
            "base_url_source IN ('ai_output', 'request', 'current_case', 'none')",
            name="ck_dsl_generation_runs_base_url_source",
        ),
        CheckConstraint(
            "prompt_variant IN ('baseline_draft', 'rewrite_from_case', 'repair_steps', 'contracts_focus')",
            name="ck_dsl_generation_runs_prompt_variant",
        ),
        CheckConstraint(
            "context_profile IN ('blank_request', 'rewrite_from_case', 'repair_steps', 'contracts_focus')",
            name="ck_dsl_generation_runs_context_profile",
        ),
        CheckConstraint(
            "feedback_status IN ('pending', 'accepted', 'rejected')",
            name="ck_dsl_generation_runs_feedback_status",
        ),
        CheckConstraint(
            "("
            "feedback_status = 'accepted' AND feedback_import_mode IN ('replace', 'steps_only', 'contracts_only')"
            ") OR ("
            "feedback_status IN ('pending', 'rejected') AND feedback_import_mode IS NULL"
            ")",
            name="ck_dsl_generation_runs_feedback_import_mode",
        ),
        CheckConstraint(
            "("
            "feedback_status = 'rejected' AND rejection_reason_code IN ("
            "'wrong_actions', 'invalid_structure', 'context_mismatch', 'bad_contracts', 'other'"
            ")"
            ") OR ("
            "feedback_status IN ('pending', 'accepted') AND rejection_reason_code IS NULL"
            ")",
            name="ck_dsl_generation_runs_rejection_reason_code",
        ),
        CheckConstraint(
            "retry_reason_code IS NULL OR retry_reason_code IN ("
            "'wrong_actions', 'invalid_structure', 'context_mismatch', 'bad_contracts', 'other'"
            ")",
            name="ck_dsl_generation_runs_retry_reason_code",
        ),
        CheckConstraint(
            "("
            "retry_from_generation_id IS NULL AND retry_reason_code IS NULL AND retry_note IS NULL"
            ") OR ("
            "retry_from_generation_id IS NOT NULL AND retry_reason_code IS NOT NULL"
            ")",
            name="ck_dsl_generation_runs_retry_context",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    project_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("projects.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    case_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("test_cases.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    prompt_preview: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_variant: Mapped[str] = mapped_column(String(32), nullable=False, default="baseline_draft")
    retry_from_generation_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("dsl_generation_runs.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    retry_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    request_base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    generation_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    import_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean(), index=True, nullable=False, default=False)
    error_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    used_current_case_context: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    used_current_steps_context: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    context_profile: Mapped[str] = mapped_column(String(32), nullable=False, default="blank_request")
    base_url_source: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    base_url_backfilled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    repaired_invalid_actions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    removed_invalid_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    removed_invalid_contracts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    preserve_contracts_requested: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    preserve_contracts_applied: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    warnings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    normalization_notes_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warnings_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    normalization_notes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    governance_focus_reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    risk_flags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    generated_case_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    feedback_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    feedback_import_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rejection_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feedback_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    feedback_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
