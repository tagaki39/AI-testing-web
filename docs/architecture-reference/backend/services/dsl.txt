"""DSL validation service."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from datetime import UTC, datetime, timedelta
from threading import Lock

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.ai.dsl_generator import (
    DEFAULT_GOVERNANCE_REJECTION_REASONS,
    SETTLED_GOVERNANCE_REJECTION_REASONS,
    AI_DSL_PROMPT_VERSION,
    DslGenerationConfigError,
    DslGenerationError,
    generate_segmented_case_draft,
    resolve_generation_mode,
    resolve_generation_profile,
    resolve_prompt_version,
)
from app.core.config import get_settings
from app.models import DslGenerationRun, Project, TestCase, User
from app.schemas.dsl import (
    DSLCase,
    DSLValidationResult,
    DslGenerationFeedbackRequest,
    DslGenerationFeedbackStatus,
    DslGenerationPromptVariant,
    DslGenerationRejectionReasonCode,
    GenerateDslMeta,
    GenerateDslMode,
    GenerateDslImportMode,
    GenerateDslRequest,
    GenerateDslResponse,
    StoredDslGenerationRunDetail,
    StoredDslGenerationRunSummary,
)
from app.schemas.settings import (
    AIDslGenerationErrorTypeCount,
    AIDslGenerationContextProfileBreakdown,
    AIDslGenerationGovernanceFocusSummary,
    AIDslGenerationImportModeCount,
    AIDslGenerationModeBreakdown,
    AIDslGenerationModelOutcome,
    AIDslGenerationPromptVariantBreakdown,
    AIDslGenerationPromptVersionBreakdown,
    AIDslGenerationRejectionReasonCount,
    AIDslGenerationRejectionReasonByVariant,
    AIDslGenerationRetryAcceptanceByReason,
    AIDslGenerationStats,
)
from app.services.cases import EntityNotFoundError


SUPPORTED_DSL_ACTIONS = [
    "goto",
    "click",
    "input",
    "wait_for",
    "assert_text",
    "assert_url_contains",
    "capture_text",
]


@dataclass
class DslGenerationRuntimeStats:
    total_requests: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_model: str | None = None
    last_error_type: str | None = None
    last_error_message: str | None = None


@dataclass(frozen=True)
class GovernanceFocusReasonStats:
    rejection_reason_code: DslGenerationRejectionReasonCode
    rejected_count: int = 0
    affected_prompt_variants: int = 0
    retry_requests: int = 0
    retry_accepted_count: int = 0

    @property
    def retry_acceptance_rate(self) -> float:
        if self.retry_requests <= 0:
            return 0.0
        return self.retry_accepted_count / self.retry_requests

    @property
    def retry_unresolved_count(self) -> int:
        return max(0, self.retry_requests - self.retry_accepted_count)


_RUNTIME_STATS = DslGenerationRuntimeStats()
_RUNTIME_STATS_LOCK = Lock()
GOVERNANCE_FOCUS_SELECTION_NOTE = (
    "按 rejected 数量优先，次序参考 retry 未收敛量与受影响 prompt variant 覆盖；"
    "已排除 wrong_actions / invalid_structure / other。"
)


class DslGenerationFeedbackConflictError(RuntimeError):
    """Raised when generation feedback was already recorded with a different decision."""


class DslGenerationFeedbackPermissionError(RuntimeError):
    """Raised when a user tries to record feedback for another actor's generation run."""


class DslGenerationRetryPermissionError(RuntimeError):
    """Raised when a user tries to retry another actor's generation run."""


class DslGenerationRetryValidationError(RuntimeError):
    """Raised when retry context does not match the source rejected generation run."""


def validate_dsl_case(test_case: DSLCase) -> DSLValidationResult:
    return DSLValidationResult(
        case=test_case,
        supported_actions=SUPPORTED_DSL_ACTIONS,
    )


def generate_dsl_case(session: Session, payload: GenerateDslRequest) -> GenerateDslResponse:
    _ensure_user_exists(session, payload.actor_user_id)
    if payload.project_id is not None:
        _ensure_project_exists(session, payload.project_id)
    if payload.case_id is not None:
        _ensure_case_exists(session, payload.case_id)
    if payload.retry_from_generation_id is not None:
        _validate_retry_generation_source(session, payload=payload)
    resolved_generation_mode = resolve_generation_mode(payload.generation_mode)
    governance_focus_reasons = _select_governance_focus_reasons(session)

    with _RUNTIME_STATS_LOCK:
        _RUNTIME_STATS.total_requests += 1

    # Build flow_steps + a11y_nodes_by_state for segmented generation
    flow_steps = payload.flow_steps or []
    # If caller provided grouped a11y nodes (e.g., from explore_flow cache),
    # use them as the element context for each page_state.
    a11y_nodes_by_state: dict[str, list] = dict(payload.a11y_nodes_by_state or {})
    if not flow_steps:
        # Single-segment fallback: wrap prompt as one segment
        flow_steps = [{"page_state": "S0", "steps": []}]

    try:
        generated_case, warnings, normalization_notes, generation_meta = generate_segmented_case_draft(
            payload=payload,
            flow_steps=flow_steps,
            a11y_nodes_by_state=a11y_nodes_by_state,
            db_session=session,
        )
    except (DslGenerationConfigError, DslGenerationError) as exc:
        model_name = get_settings().ai_dsl_model
        _record_generation_failure(model_name=model_name, error=exc)
        _persist_generation_run(
            session,
            payload=payload,
            generation_mode=resolved_generation_mode,
            success=False,
            model_name=model_name,
            warnings=[],
            normalization_notes=[],
            governance_focus_reasons=governance_focus_reasons,
            generated_case=None,
            generation_meta=None,
            error=exc,
        )
        raise

    _record_generation_success(generation_meta)
    generation_run = _persist_generation_run(
        session,
        payload=payload,
        generation_mode=generation_meta.generation_mode,
        success=True,
        model_name=generation_meta.model,
        warnings=warnings,
        normalization_notes=normalization_notes,
        governance_focus_reasons=governance_focus_reasons,
        generated_case=generated_case,
        generation_meta=generation_meta,
        error=None,
    )
    # Auto-capture anti-patterns from warnings
    _capture_anti_patterns_from_warnings(session, warnings, generated_case, payload)
    return GenerateDslResponse(
        generation_id=generation_run.id,
        case=generated_case,
        supported_actions=SUPPORTED_DSL_ACTIONS,
        warnings=warnings,
        normalization_notes=normalization_notes,
        generation_meta=generation_meta,
    )


def delete_dsl_generation_run(session: Session, generation_id: int) -> None:
    """Delete a DSL generation run record."""
    record = session.get(DslGenerationRun, generation_id)
    if record is None:
        raise EntityNotFoundError(f"DSL generation run {generation_id} not found.")
    session.delete(record)
    session.commit()


def reset_dsl_generation_runtime_stats() -> None:
    with _RUNTIME_STATS_LOCK:
        _RUNTIME_STATS.total_requests = 0
        _RUNTIME_STATS.success_count = 0
        _RUNTIME_STATS.failure_count = 0
        _RUNTIME_STATS.last_model = None
        _RUNTIME_STATS.last_error_type = None
        _RUNTIME_STATS.last_error_message = None


def list_dsl_generation_runs(
    session: Session,
    *,
    status: str | None = None,
    feedback_status: DslGenerationFeedbackStatus | None = None,
    generation_mode: GenerateDslMode | None = None,
    import_mode: GenerateDslImportMode | None = None,
    prompt_variant: DslGenerationPromptVariant | None = None,
    rejection_reason_code: DslGenerationRejectionReasonCode | None = None,
    has_risk_flags: bool | None = None,
    model_name: str | None = None,
    project_id: int | None = None,
    case_id: int | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[StoredDslGenerationRunSummary]:
    statement = select(DslGenerationRun).order_by(DslGenerationRun.created_at.desc(), DslGenerationRun.id.desc())
    if status == "success":
        statement = statement.where(DslGenerationRun.success.is_(True))
    elif status == "failed":
        statement = statement.where(DslGenerationRun.success.is_(False))
    if feedback_status is not None:
        statement = statement.where(DslGenerationRun.feedback_status == feedback_status)
    if generation_mode is not None:
        statement = statement.where(DslGenerationRun.generation_mode == generation_mode)
    if import_mode is not None:
        statement = statement.where(DslGenerationRun.import_mode == import_mode)
    if prompt_variant is not None:
        statement = statement.where(DslGenerationRun.prompt_variant == prompt_variant)
    if rejection_reason_code is not None:
        statement = statement.where(DslGenerationRun.rejection_reason_code == rejection_reason_code)
    if has_risk_flags is not None:
        comparator = _json_array_length_expression(session, DslGenerationRun.risk_flags_json)
        statement = statement.where(comparator > 0 if has_risk_flags else comparator == 0)
    if model_name:
        statement = statement.where(DslGenerationRun.model_name == model_name)
    if project_id is not None:
        statement = statement.where(DslGenerationRun.project_id == project_id)
    if case_id is not None:
        statement = statement.where(DslGenerationRun.case_id == case_id)
    if created_from is not None:
        statement = statement.where(DslGenerationRun.created_at >= _normalize_filter_datetime(created_from))
    if created_to is not None:
        statement = statement.where(DslGenerationRun.created_at <= _normalize_filter_datetime(created_to))
    statement = statement.limit(limit).offset(offset)
    records = session.scalars(statement).all()
    return [_to_stored_dsl_generation_run_summary(record) for record in records]


def get_dsl_generation_run_detail(session: Session, generation_id: int) -> StoredDslGenerationRunDetail:
    record = session.get(DslGenerationRun, generation_id)
    if record is None:
        raise EntityNotFoundError(f"DSL generation run {generation_id} not found.")
    return _to_stored_dsl_generation_run_detail(record)


def record_dsl_generation_feedback(
    session: Session,
    generation_id: int,
    payload: DslGenerationFeedbackRequest,
) -> StoredDslGenerationRunSummary:
    _ensure_user_exists(session, payload.actor_user_id)
    generation_run = _get_generation_run_for_feedback(session, generation_id)
    if generation_run is None:
        raise EntityNotFoundError(f"DSL generation run {generation_id} not found.")
    if generation_run.actor_user_id != payload.actor_user_id:
        raise DslGenerationFeedbackPermissionError("Only the actor who generated this draft can record feedback.")

    if generation_run.feedback_status == "pending":
        generation_run.feedback_status = payload.feedback_status
        generation_run.feedback_import_mode = payload.feedback_import_mode
        generation_run.rejection_reason_code = payload.rejection_reason_code
        generation_run.feedback_note = payload.feedback_note
        generation_run.feedback_recorded_at = datetime.now(UTC).replace(tzinfo=None)
        try:
            session.commit()
        except Exception:
            session.rollback()
            raise
        session.refresh(generation_run)
        return _to_stored_dsl_generation_run_summary(generation_run)

    if (
        generation_run.feedback_status == payload.feedback_status
        and generation_run.feedback_import_mode == payload.feedback_import_mode
        and generation_run.rejection_reason_code == payload.rejection_reason_code
        and generation_run.feedback_note == payload.feedback_note
    ):
        return _to_stored_dsl_generation_run_summary(generation_run)

    raise DslGenerationFeedbackConflictError("该生成记录的反馈已写入不同决策，不能覆盖。")


def get_dsl_generation_durable_stats(session: Session) -> AIDslGenerationStats:
    current_governance_focus_reasons = _select_governance_focus_reasons(session)
    governance_focus_stats = {
        item.rejection_reason_code: item for item in _list_governance_focus_reason_stats(session)
    }
    total_requests = session.scalar(select(func.count()).select_from(DslGenerationRun)) or 0
    success_count = (
        session.scalar(
            select(func.count()).select_from(DslGenerationRun).where(DslGenerationRun.success.is_(True))
    )
    or 0
    )
    failure_count = max(0, total_requests - success_count)
    accepted_count = (
        session.scalar(
            select(func.count())
            .select_from(DslGenerationRun)
            .where(DslGenerationRun.feedback_status == "accepted")
        )
        or 0
    )
    rejected_count = (
        session.scalar(
            select(func.count())
            .select_from(DslGenerationRun)
            .where(DslGenerationRun.feedback_status == "rejected")
        )
        or 0
    )
    pending_count = max(0, total_requests - accepted_count - rejected_count)

    latest_record = session.scalar(
        select(DslGenerationRun).order_by(DslGenerationRun.created_at.desc(), DslGenerationRun.id.desc()).limit(1)
    )

    last_24h_threshold = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=24)
    last_24h_requests = (
        session.scalar(
            select(func.count()).select_from(DslGenerationRun).where(DslGenerationRun.created_at >= last_24h_threshold)
        )
        or 0
    )
    last_24h_success_count = (
        session.scalar(
            select(func.count())
            .select_from(DslGenerationRun)
            .where(
                DslGenerationRun.created_at >= last_24h_threshold,
                DslGenerationRun.success.is_(True),
            )
        )
        or 0
    )
    last_24h_failure_count = max(0, last_24h_requests - last_24h_success_count)
    last_24h_auto_repair_count = (
        session.scalar(
            select(func.count())
            .select_from(DslGenerationRun)
            .where(
                DslGenerationRun.created_at >= last_24h_threshold,
                or_(
                    DslGenerationRun.repaired_invalid_actions > 0,
                    DslGenerationRun.removed_invalid_steps > 0,
                    DslGenerationRun.removed_invalid_contracts > 0,
                ),
            )
        )
        or 0
    )
    retry_requests = (
        session.scalar(
            select(func.count())
            .select_from(DslGenerationRun)
            .where(DslGenerationRun.retry_from_generation_id.is_not(None))
        )
        or 0
    )
    retry_accepted_count = (
        session.scalar(
            select(func.count())
            .select_from(DslGenerationRun)
            .where(
                DslGenerationRun.retry_from_generation_id.is_not(None),
                DslGenerationRun.feedback_status == "accepted",
            )
        )
        or 0
    )
    retry_rejected_count = (
        session.scalar(
            select(func.count())
            .select_from(DslGenerationRun)
            .where(
                DslGenerationRun.retry_from_generation_id.is_not(None),
                DslGenerationRun.feedback_status == "rejected",
            )
        )
        or 0
    )
    top_error_rows = session.execute(
        select(
            DslGenerationRun.error_type,
            func.count().label("count"),
        )
        .where(
            DslGenerationRun.created_at >= last_24h_threshold,
            DslGenerationRun.success.is_(False),
            DslGenerationRun.error_type.is_not(None),
        )
        .group_by(DslGenerationRun.error_type)
        .order_by(func.count().desc(), DslGenerationRun.error_type.asc())
        .limit(5)
    ).all()
    accepted_import_mode_rows = session.execute(
        select(
            DslGenerationRun.feedback_import_mode,
            func.count().label("count"),
        )
        .where(
            DslGenerationRun.feedback_status == "accepted",
            DslGenerationRun.feedback_import_mode.is_not(None),
        )
        .group_by(DslGenerationRun.feedback_import_mode)
        .order_by(func.count().desc(), DslGenerationRun.feedback_import_mode.asc())
    ).all()
    rejection_reason_rows = session.execute(
        select(
            DslGenerationRun.rejection_reason_code,
            func.count().label("count"),
        )
        .where(
            DslGenerationRun.feedback_status == "rejected",
            DslGenerationRun.rejection_reason_code.is_not(None),
        )
        .group_by(DslGenerationRun.rejection_reason_code)
        .order_by(func.count().desc(), DslGenerationRun.rejection_reason_code.asc())
        .limit(5)
    ).all()
    model_outcome_rows = session.execute(
        select(
            DslGenerationRun.model_name,
            func.count().label("total_requests"),
            func.sum(case((DslGenerationRun.success.is_(True), 1), else_=0)).label("success_count"),
            func.sum(case((DslGenerationRun.feedback_status == "accepted", 1), else_=0)).label("accepted_count"),
            func.sum(case((DslGenerationRun.feedback_status == "rejected", 1), else_=0)).label("rejected_count"),
        )
        .group_by(DslGenerationRun.model_name)
        .order_by(func.count().desc(), DslGenerationRun.model_name.asc())
    ).all()
    generation_mode_rows = session.execute(
        select(
            DslGenerationRun.generation_mode,
            func.count().label("total_requests"),
            func.sum(case((DslGenerationRun.success.is_(True), 1), else_=0)).label("success_count"),
            func.sum(case((DslGenerationRun.feedback_status == "accepted", 1), else_=0)).label("accepted_count"),
            func.sum(case((DslGenerationRun.feedback_status == "rejected", 1), else_=0)).label("rejected_count"),
        )
        .group_by(DslGenerationRun.generation_mode)
        .order_by(func.count().desc(), DslGenerationRun.generation_mode.asc())
    ).all()
    prompt_variant_rows = session.execute(
        select(
            DslGenerationRun.prompt_variant,
            func.count().label("total_requests"),
            func.sum(case((DslGenerationRun.success.is_(True), 1), else_=0)).label("success_count"),
            func.sum(case((DslGenerationRun.feedback_status == "accepted", 1), else_=0)).label("accepted_count"),
            func.sum(case((DslGenerationRun.feedback_status == "rejected", 1), else_=0)).label("rejected_count"),
        )
        .group_by(DslGenerationRun.prompt_variant)
        .order_by(func.count().desc(), DslGenerationRun.prompt_variant.asc())
    ).all()
    prompt_version_rows = session.execute(
        select(
            DslGenerationRun.prompt_version,
            func.count().label("total_requests"),
            func.sum(case((DslGenerationRun.success.is_(True), 1), else_=0)).label("success_count"),
            func.sum(case((DslGenerationRun.feedback_status == "accepted", 1), else_=0)).label("accepted_count"),
            func.sum(case((DslGenerationRun.feedback_status == "rejected", 1), else_=0)).label("rejected_count"),
            func.sum(case((DslGenerationRun.retry_from_generation_id.is_not(None), 1), else_=0)).label("retry_requests"),
            func.sum(
                case(
                    (
                        and_(
                            DslGenerationRun.retry_from_generation_id.is_not(None),
                            DslGenerationRun.feedback_status == "accepted",
                        ),
                        1,
                    ),
                    else_=0,
                ),
            ).label("retry_accepted_count"),
        )
        .group_by(DslGenerationRun.prompt_version)
        .order_by(func.count().desc(), DslGenerationRun.prompt_version.desc())
        .limit(5)
    ).all()
    context_profile_rows = session.execute(
        select(
            DslGenerationRun.context_profile,
            func.count().label("total_requests"),
            func.sum(case((DslGenerationRun.success.is_(True), 1), else_=0)).label("success_count"),
            func.sum(case((DslGenerationRun.feedback_status == "accepted", 1), else_=0)).label("accepted_count"),
            func.sum(case((DslGenerationRun.feedback_status == "rejected", 1), else_=0)).label("rejected_count"),
        )
        .group_by(DslGenerationRun.context_profile)
        .order_by(func.count().desc(), DslGenerationRun.context_profile.asc())
    ).all()
    rejection_reason_by_variant_rows = session.execute(
        select(
            DslGenerationRun.prompt_variant,
            DslGenerationRun.rejection_reason_code,
            func.count().label("count"),
        )
        .where(
            DslGenerationRun.feedback_status == "rejected",
            DslGenerationRun.rejection_reason_code.is_not(None),
        )
        .group_by(DslGenerationRun.prompt_variant, DslGenerationRun.rejection_reason_code)
        .order_by(func.count().desc(), DslGenerationRun.prompt_variant.asc(), DslGenerationRun.rejection_reason_code.asc())
    ).all()
    retry_reason_rows = session.execute(
        select(
            DslGenerationRun.retry_reason_code,
            func.count().label("retry_requests"),
            func.sum(case((DslGenerationRun.feedback_status == "accepted", 1), else_=0)).label("accepted_count"),
        )
        .where(
            DslGenerationRun.retry_from_generation_id.is_not(None),
            DslGenerationRun.retry_reason_code.is_not(None),
        )
        .group_by(DslGenerationRun.retry_reason_code)
        .order_by(func.count().desc(), DslGenerationRun.retry_reason_code.asc())
    ).all()

    return AIDslGenerationStats(
        current_prompt_version=AI_DSL_PROMPT_VERSION,
        current_governance_focus_reasons=current_governance_focus_reasons,
        prompt_version_observation_note="总请求 / 采纳 / 放弃 / 重试采纳",
        governance_focus_selection_note=GOVERNANCE_FOCUS_SELECTION_NOTE,
        total_requests=total_requests,
        success_count=success_count,
        failure_count=failure_count,
        accepted_count=accepted_count,
        rejected_count=rejected_count,
        pending_count=pending_count,
        decision_coverage_rate=((accepted_count + rejected_count) / total_requests if total_requests else 0.0),
        last_model=latest_record.model_name if latest_record is not None else None,
        last_error_type=latest_record.error_type if latest_record is not None else None,
        last_error_message=latest_record.error_message if latest_record is not None else None,
        last_24h_requests=last_24h_requests,
        last_24h_success_count=last_24h_success_count,
        last_24h_failure_count=last_24h_failure_count,
        last_24h_auto_repair_rate=(
            last_24h_auto_repair_count / last_24h_requests if last_24h_requests else 0.0
        ),
        retry_requests=retry_requests,
        retry_accepted_count=retry_accepted_count,
        retry_rejected_count=retry_rejected_count,
        top_error_types=[
            AIDslGenerationErrorTypeCount(error_type=error_type, count=count)
            for error_type, count in top_error_rows
            if error_type is not None
        ],
        accepted_import_mode_breakdown=[
            AIDslGenerationImportModeCount(import_mode=import_mode, count=count)
            for import_mode, count in accepted_import_mode_rows
            if import_mode is not None
        ],
        top_rejection_reasons=[
            AIDslGenerationRejectionReasonCount(rejection_reason_code=rejection_reason_code, count=count)
            for rejection_reason_code, count in rejection_reason_rows
            if rejection_reason_code is not None
        ],
        prompt_variant_breakdown=[
            AIDslGenerationPromptVariantBreakdown(
                prompt_variant=prompt_variant,
                total_requests=total_requests,
                success_count=success_count or 0,
                accepted_count=accepted_count or 0,
                rejected_count=rejected_count or 0,
            )
            for prompt_variant, total_requests, success_count, accepted_count, rejected_count in prompt_variant_rows
        ],
        prompt_version_breakdown=[
            AIDslGenerationPromptVersionBreakdown(
                prompt_version=prompt_version,
                total_requests=total_requests,
                success_count=success_count or 0,
                accepted_count=accepted_count or 0,
                rejected_count=rejected_count or 0,
                retry_requests=retry_requests or 0,
                retry_accepted_count=retry_accepted_count or 0,
            )
            for (
                prompt_version,
                total_requests,
                success_count,
                accepted_count,
                rejected_count,
                retry_requests,
                retry_accepted_count,
            ) in prompt_version_rows
        ],
        context_profile_breakdown=[
            AIDslGenerationContextProfileBreakdown(
                context_profile=context_profile,
                total_requests=total_requests,
                success_count=success_count or 0,
                accepted_count=accepted_count or 0,
                rejected_count=rejected_count or 0,
            )
            for context_profile, total_requests, success_count, accepted_count, rejected_count in context_profile_rows
        ],
        rejection_reason_by_variant=[
            AIDslGenerationRejectionReasonByVariant(
                prompt_variant=prompt_variant,
                rejection_reason_code=rejection_reason_code,
                count=count,
            )
            for prompt_variant, rejection_reason_code, count in rejection_reason_by_variant_rows
            if rejection_reason_code is not None
        ],
        model_outcome_breakdown=[
            AIDslGenerationModelOutcome(
                model_name=model_name,
                total_requests=total_requests,
                success_count=success_count or 0,
                accepted_count=accepted_count or 0,
                rejected_count=rejected_count or 0,
            )
            for model_name, total_requests, success_count, accepted_count, rejected_count in model_outcome_rows
        ],
        generation_mode_breakdown=[
            AIDslGenerationModeBreakdown(
                generation_mode=generation_mode,
                total_requests=total_requests,
                success_count=success_count or 0,
                accepted_count=accepted_count or 0,
                rejected_count=rejected_count or 0,
            )
            for generation_mode, total_requests, success_count, accepted_count, rejected_count in generation_mode_rows
        ],
        retry_acceptance_by_reason=[
            AIDslGenerationRetryAcceptanceByReason(
                rejection_reason_code=rejection_reason_code,
                retry_requests=retry_requests,
                accepted_count=accepted_count or 0,
                acceptance_rate=((accepted_count or 0) / retry_requests if retry_requests else 0.0),
            )
            for rejection_reason_code, retry_requests, accepted_count in retry_reason_rows
            if rejection_reason_code is not None
        ],
        current_governance_focus_breakdown=[
            AIDslGenerationGovernanceFocusSummary(
                rejection_reason_code=rejection_reason_code,
                rejected_count=governance_focus_stats.get(
                    rejection_reason_code,
                    GovernanceFocusReasonStats(rejection_reason_code=rejection_reason_code),
                ).rejected_count,
                affected_prompt_variants=governance_focus_stats.get(
                    rejection_reason_code,
                    GovernanceFocusReasonStats(rejection_reason_code=rejection_reason_code),
                ).affected_prompt_variants,
                retry_requests=governance_focus_stats.get(
                    rejection_reason_code,
                    GovernanceFocusReasonStats(rejection_reason_code=rejection_reason_code),
                ).retry_requests,
                retry_accepted_count=governance_focus_stats.get(
                    rejection_reason_code,
                    GovernanceFocusReasonStats(rejection_reason_code=rejection_reason_code),
                ).retry_accepted_count,
                retry_acceptance_rate=governance_focus_stats.get(
                    rejection_reason_code,
                    GovernanceFocusReasonStats(rejection_reason_code=rejection_reason_code),
                ).retry_acceptance_rate,
            )
            for rejection_reason_code in current_governance_focus_reasons
        ],
    )


def _record_generation_success(meta: GenerateDslMeta) -> None:
    with _RUNTIME_STATS_LOCK:
        _RUNTIME_STATS.success_count += 1
        _RUNTIME_STATS.last_model = meta.model
        _RUNTIME_STATS.last_error_type = None
        _RUNTIME_STATS.last_error_message = None


def _record_generation_failure(*, model_name: str | None, error: Exception) -> None:
    with _RUNTIME_STATS_LOCK:
        _RUNTIME_STATS.failure_count += 1
        _RUNTIME_STATS.last_model = model_name
        _RUNTIME_STATS.last_error_type = type(error).__name__
        _RUNTIME_STATS.last_error_message = str(error)


def _ensure_user_exists(session: Session, user_id: int) -> None:
    if session.get(User, user_id) is None:
        raise EntityNotFoundError(f"User {user_id} not found.")


def _ensure_project_exists(session: Session, project_id: int) -> None:
    if session.get(Project, project_id) is None:
        raise EntityNotFoundError(f"Project {project_id} not found.")


def _ensure_case_exists(session: Session, case_id: int) -> None:
    if session.get(TestCase, case_id) is None:
        raise EntityNotFoundError(f"Case {case_id} not found.")


def _validate_retry_generation_source(session: Session, *, payload: GenerateDslRequest) -> None:
    if payload.retry_from_generation_id is None:
        return

    source_generation = session.get(DslGenerationRun, payload.retry_from_generation_id)
    if source_generation is None:
        raise EntityNotFoundError(f"DSL generation run {payload.retry_from_generation_id} not found.")
    if source_generation.actor_user_id != payload.actor_user_id:
        raise DslGenerationRetryPermissionError("Only the actor who created the rejected draft can retry it.")
    if source_generation.feedback_status != "rejected":
        raise DslGenerationRetryValidationError("retry_from_generation_id 必须指向一条已 rejected 的生成记录。")
    if source_generation.rejection_reason_code != payload.retry_reason_code:
        raise DslGenerationRetryValidationError("retry_reason_code 必须与来源 rejected 记录的 rejection_reason_code 一致。")


def _get_generation_run_for_feedback(session: Session, generation_id: int) -> DslGenerationRun | None:
    if _supports_for_update(session):
        statement = (
            select(DslGenerationRun)
            .where(DslGenerationRun.id == generation_id)
            .with_for_update()
        )
        return session.scalars(statement).first()
    return session.get(DslGenerationRun, generation_id)


def _supports_for_update(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def _json_array_length_expression(session: Session, column):
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql" and isinstance(column.property.columns[0].type, JSONB):
        return func.jsonb_array_length(column)
    return func.json_array_length(column)


def _capture_anti_patterns_from_warnings(
    session: Session,
    warnings: list[str],
    generated_case: Any,
    payload: GenerateDslRequest,
) -> None:
    """Auto-capture anti-patterns from draft generation warnings.

    Parses warning strings to extract specific error patterns and records
    them as DSLAntiPattern entries for future few-shot injection.
    """
    import re
    import json as _json
    from app.services.anti_patterns import (
        record_anti_pattern,
        TARGET_NOT_FOUND, MISSING_STEP, MISSING_NAVIGATION,
        MISSING_CAPTURE_TEXT, WRONG_PAGE_STATE,
    )

    if not warnings:
        return

    steps = []
    if generated_case and hasattr(generated_case, "steps"):
        steps = generated_case.steps or []

    for warning in warnings:
        # Pattern: target "X" 在已采集的 N 个元素中未找到匹配
        m = re.search(r'target\s+"([^"]+)"\s+在已采集.*未找到匹配', warning)
        if m:
            target = m.group(1)
            for step in steps:
                step_dict = step if isinstance(step, dict) else (step.model_dump() if hasattr(step, "model_dump") else {})
                if step_dict.get("target") == target:
                    record_anti_pattern(
                        session,
                        error_category=TARGET_NOT_FOUND,
                        wrong_snippet=step_dict,
                        context_note=f'target "{target}" 在页面元素中未找到匹配，可能需要不同的定位策略',
                        source="preflight",
                        project_id=payload.project_id,
                    )
                    break

        # Pattern: 步骤 #N 校验失败（action=X target=Y value=Z）
        m = re.search(r'步骤 #(\d+) 校验失败.*action=(\S+)\s+target=([^\s]+)', warning)
        if m:
            step_idx = int(m.group(1))
            action = m.group(2)
            if 0 <= step_idx < len(steps):
                step = steps[step_idx]
                step_dict = step if isinstance(step, dict) else (step.model_dump() if hasattr(step, "model_dump") else {})
                record_anti_pattern(
                    session,
                    error_category=MISSING_STEP,
                    wrong_snippet=step_dict,
                    context_note=f"步骤校验失败: action={action}",
                    source="validation",
                    project_id=payload.project_id,
                )

        # Pattern: click "X" 可能触发页面跳转但下一步非验证步骤
        m = re.search(r'步骤 (\d+) click "([^"]+)" 可能触发页面跳转.*下一步是 (\S+) ', warning)
        if m:
            step_idx = int(m.group(1)) - 1
            target_clicked = m.group(2)
            if 0 <= step_idx < len(steps):
                step = steps[step_idx]
                step_dict = step if isinstance(step, dict) else (step.model_dump() if hasattr(step, "model_dump") else {})
                record_anti_pattern(
                    session,
                    error_category=MISSING_NAVIGATION,
                    wrong_snippet=step_dict,
                    context_note=f'click "{target_clicked}" 后缺少 wait_for/assert 验证步骤',
                    rule_violated="R3",
                    source="auto",
                    project_id=payload.project_id,
                )

        # Pattern: capture_text without following assert_text
        m = re.search(r'capture_text.*(\w+).*但未', warning)
        if m:
            context_key = m.group(1)
            for step in steps:
                step_dict = step if isinstance(step, dict) else (step.model_dump() if hasattr(step, "model_dump") else {})
                if step_dict.get("action") == "assert_text" and str(step_dict.get("value", "")).find(context_key) >= 0:
                    break
            else:
                # No assert found referencing this capture
                record_anti_pattern(
                    session,
                    error_category=MISSING_CAPTURE_TEXT,
                    wrong_snippet={"warning": warning[:300]},
                    context_note=f"capture_text 缺少对应的 assert_text: {warning[:200]}",
                    source="auto",
                    project_id=payload.project_id,
                )


def _persist_generation_run(
    session: Session,
    *,
    payload: GenerateDslRequest,
    generation_mode: str,
    success: bool,
    model_name: str | None,
    warnings: list[str],
    normalization_notes: list[str],
    governance_focus_reasons: list[DslGenerationRejectionReasonCode],
    generated_case: DSLCase | None,
    generation_meta: GenerateDslMeta | None,
    error: Exception | None,
) -> DslGenerationRun:
    derived_prompt_variant, derived_context_profile = resolve_generation_profile(
        payload=payload,
        generation_mode=generation_mode,
    )
    prompt_version = resolve_prompt_version(payload)
    effective_governance_focus_reasons = (
        list(generation_meta.active_governance_focus_reasons)
        if generation_meta is not None
        else list(governance_focus_reasons or DEFAULT_GOVERNANCE_REJECTION_REASONS)
    )
    generation_run = DslGenerationRun(
        actor_user_id=payload.actor_user_id,
        project_id=payload.project_id,
        case_id=payload.case_id,
        prompt_preview=_build_prompt_preview(payload.prompt),
        prompt_sha256=_hash_prompt(payload.prompt),
        prompt_version=prompt_version,
        prompt_variant=generation_meta.prompt_variant if generation_meta is not None else derived_prompt_variant,
        retry_from_generation_id=payload.retry_from_generation_id,
        retry_reason_code=payload.retry_reason_code,
        retry_note=payload.retry_note,
        request_base_url=payload.base_url,
        generation_mode=generation_mode,
        import_mode=payload.import_mode,
        model_name=model_name,
        success=success,
        error_type=type(error).__name__ if error is not None else None,
        error_message=str(error) if error is not None else None,
        used_current_case_context=(
            generation_meta.used_current_case_context if generation_meta is not None else payload.current_case is not None
        ),
        used_current_steps_context=(
            generation_meta.used_current_steps_context if generation_meta is not None else payload.current_steps is not None
        ),
        context_profile=generation_meta.context_profile if generation_meta is not None else derived_context_profile,
        base_url_source=generation_meta.base_url_source if generation_meta is not None else "none",
        base_url_backfilled=generation_meta.base_url_backfilled if generation_meta is not None else False,
        repaired_invalid_actions=generation_meta.repaired_invalid_actions if generation_meta is not None else 0,
        removed_invalid_steps=generation_meta.removed_invalid_steps if generation_meta is not None else 0,
        removed_invalid_contracts=generation_meta.removed_invalid_contracts if generation_meta is not None else 0,
        preserve_contracts_requested=payload.preserve_contracts,
        preserve_contracts_applied=generation_meta.preserve_contracts_applied if generation_meta is not None else False,
        warnings_count=len(warnings),
        normalization_notes_count=len(normalization_notes),
        warnings_json=warnings,
        normalization_notes_json=normalization_notes,
        governance_focus_reasons_json=effective_governance_focus_reasons,
        risk_flags_json=list(generation_meta.risk_flags) if generation_meta is not None else [],
        generated_case_json=generated_case.model_dump(mode="json") if generated_case is not None else None,
        feedback_status="pending",
        feedback_import_mode=None,
        rejection_reason_code=None,
        feedback_note=None,
        feedback_recorded_at=None,
    )
    session.add(generation_run)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(generation_run)
    return generation_run


def _select_governance_focus_reasons(
    session: Session,
    *,
    limit: int = 2,
) -> list[DslGenerationRejectionReasonCode]:
    """Governance is no longer active; return default reasons for DB compatibility."""
    return list(DEFAULT_GOVERNANCE_REJECTION_REASONS[:limit])


def _list_governance_focus_reason_stats(session: Session) -> list[GovernanceFocusReasonStats]:
    rejected_rows = session.execute(
        select(
            DslGenerationRun.rejection_reason_code,
            func.count().label("count"),
        )
        .where(
            DslGenerationRun.feedback_status == "rejected",
            DslGenerationRun.rejection_reason_code.is_not(None),
            DslGenerationRun.rejection_reason_code.not_in(SETTLED_GOVERNANCE_REJECTION_REASONS),
            DslGenerationRun.rejection_reason_code != "other",
        )
        .group_by(DslGenerationRun.rejection_reason_code)
    ).all()
    variant_rows = session.execute(
        select(
            DslGenerationRun.rejection_reason_code,
            func.count(func.distinct(DslGenerationRun.prompt_variant)).label("count"),
        )
        .where(
            DslGenerationRun.feedback_status == "rejected",
            DslGenerationRun.rejection_reason_code.is_not(None),
            DslGenerationRun.rejection_reason_code.not_in(SETTLED_GOVERNANCE_REJECTION_REASONS),
            DslGenerationRun.rejection_reason_code != "other",
        )
        .group_by(DslGenerationRun.rejection_reason_code)
    ).all()
    retry_rows = session.execute(
        select(
            DslGenerationRun.retry_reason_code,
            func.count().label("retry_requests"),
            func.sum(case((DslGenerationRun.feedback_status == "accepted", 1), else_=0)).label("accepted_count"),
        )
        .where(
            DslGenerationRun.retry_from_generation_id.is_not(None),
            DslGenerationRun.retry_reason_code.is_not(None),
            DslGenerationRun.retry_reason_code.not_in(SETTLED_GOVERNANCE_REJECTION_REASONS),
            DslGenerationRun.retry_reason_code != "other",
        )
        .group_by(DslGenerationRun.retry_reason_code)
    ).all()

    rejected_count_by_reason = {
        rejection_reason_code: count
        for rejection_reason_code, count in rejected_rows
        if rejection_reason_code is not None
    }
    variant_count_by_reason = {
        rejection_reason_code: count
        for rejection_reason_code, count in variant_rows
        if rejection_reason_code is not None
    }
    retry_stats_by_reason = {
        rejection_reason_code: (retry_requests, accepted_count or 0)
        for rejection_reason_code, retry_requests, accepted_count in retry_rows
        if rejection_reason_code is not None
    }

    all_reasons = set(rejected_count_by_reason) | set(variant_count_by_reason) | set(retry_stats_by_reason)
    ranked = [
        GovernanceFocusReasonStats(
            rejection_reason_code=rejection_reason_code,
            rejected_count=rejected_count_by_reason.get(rejection_reason_code, 0),
            affected_prompt_variants=variant_count_by_reason.get(rejection_reason_code, 0),
            retry_requests=retry_stats_by_reason.get(rejection_reason_code, (0, 0))[0],
            retry_accepted_count=retry_stats_by_reason.get(rejection_reason_code, (0, 0))[1],
        )
        for rejection_reason_code in all_reasons
    ]
    ranked.sort(
        key=lambda item: (
            -item.rejected_count,
            -item.retry_unresolved_count,
            -item.affected_prompt_variants,
            item.rejection_reason_code,
        )
    )
    return ranked


def _build_prompt_preview(prompt: str) -> str:
    return prompt.strip()[:200] or prompt[:200]


def _hash_prompt(prompt: str) -> str:
    return hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()


def _normalize_filter_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _to_stored_dsl_generation_run_summary(record: DslGenerationRun) -> StoredDslGenerationRunSummary:
    return StoredDslGenerationRunSummary(
        id=record.id,
        created_at=record.created_at,
        success=record.success,
        model_name=record.model_name,
        generation_mode=record.generation_mode,
        import_mode=record.import_mode,
        prompt_variant=record.prompt_variant,
        project_id=record.project_id,
        case_id=record.case_id,
        prompt_version=record.prompt_version,
        retry_from_generation_id=record.retry_from_generation_id,
        retry_reason_code=record.retry_reason_code,
        retry_note=record.retry_note,
        error_type=record.error_type,
        error_message=record.error_message,
        repaired_invalid_actions=record.repaired_invalid_actions,
        removed_invalid_steps=record.removed_invalid_steps,
        removed_invalid_contracts=record.removed_invalid_contracts,
        warnings_count=record.warnings_count,
        normalization_notes_count=record.normalization_notes_count,
        prompt_preview=record.prompt_preview,
        governance_focus_reasons=list(record.governance_focus_reasons_json or []),
        risk_flags=list(record.risk_flags_json or []),
        feedback_status=record.feedback_status,
        feedback_import_mode=record.feedback_import_mode,
        rejection_reason_code=record.rejection_reason_code,
        feedback_recorded_at=record.feedback_recorded_at,
    )


def _to_stored_dsl_generation_run_detail(record: DslGenerationRun) -> StoredDslGenerationRunDetail:
    return StoredDslGenerationRunDetail(
        **_to_stored_dsl_generation_run_summary(record).model_dump(),
        request_base_url=record.request_base_url,
        generated_case_json=(
            DSLCase.model_validate(record.generated_case_json) if record.generated_case_json is not None else None
        ),
        warnings_json=list(record.warnings_json or []),
        normalization_notes_json=list(record.normalization_notes_json or []),
        feedback_note=record.feedback_note,
        context_profile=record.context_profile,
        used_current_case_context=record.used_current_case_context,
        used_current_steps_context=record.used_current_steps_context,
        preserve_contracts_requested=record.preserve_contracts_requested,
        preserve_contracts_applied=record.preserve_contracts_applied,
    )


__all__ = [
    "DslGenerationConfigError",
    "DslGenerationError",
    "DslGenerationFeedbackConflictError",
    "DslGenerationFeedbackPermissionError",
    "DslGenerationRetryPermissionError",
    "DslGenerationRetryValidationError",
    "SUPPORTED_DSL_ACTIONS",
    "get_dsl_generation_run_detail",
    "get_dsl_generation_durable_stats",
    "generate_dsl_case",
    "get_dsl_generation_runtime_stats",
    "list_dsl_generation_runs",
    "record_dsl_generation_feedback",
    "reset_dsl_generation_runtime_stats",
    "validate_dsl_case",
]
