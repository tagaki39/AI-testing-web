"""DSL validation routes."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.auth import require_demo_user
from app.db import get_db_session
from app.models import User
from app.schemas.dsl import (
    DSLCase,
    DSLValidationResult,
    DslGenerationFeedbackRequest,
    DslGenerationFeedbackStatus,
    DslGenerationPromptVariant,
    DslGenerationRejectionReasonCode,
    DslGenerationRunStatus,
    GenerateDslImportMode,
    GenerateDslMode,
    GenerateDslRequest,
    GenerateDslResponse,
    StoredDslGenerationRunDetail,
    StoredDslGenerationRunSummary,
)
from app.services import EntityNotFoundError
from app.services.dsl import (
    DslGenerationConfigError,
    DslGenerationError,
    DslGenerationFeedbackConflictError,
    DslGenerationFeedbackPermissionError,
    DslGenerationRetryPermissionError,
    DslGenerationRetryValidationError,
    delete_dsl_generation_run,
    generate_dsl_case,
    get_dsl_generation_run_detail,
    list_dsl_generation_runs,
    record_dsl_generation_feedback,
    validate_dsl_case,
)


router = APIRouter(prefix="/dsl", tags=["dsl"])


@router.post("/validate", response_model=DSLValidationResult, summary="Validate structured DSL")
def validate_case(payload: DSLCase) -> DSLValidationResult:
    return validate_dsl_case(payload)


@router.post("/generate", response_model=GenerateDslResponse, summary="Generate structured DSL draft")
def generate_case(
    payload: GenerateDslRequest,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> GenerateDslResponse:
    try:
        return generate_dsl_case(session, payload.model_copy(update={"actor_user_id": current_user.id}))
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DslGenerationRetryPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DslGenerationRetryValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DslGenerationConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except DslGenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/generations", response_model=list[StoredDslGenerationRunSummary], summary="List DSL generation runs")
def list_generation_runs_route(
    status: DslGenerationRunStatus | None = Query(default=None),
    feedback_status: DslGenerationFeedbackStatus | None = Query(default=None),
    generation_mode: GenerateDslMode | None = Query(default=None),
    import_mode: GenerateDslImportMode | None = Query(default=None),
    prompt_variant: DslGenerationPromptVariant | None = Query(default=None),
    rejection_reason_code: DslGenerationRejectionReasonCode | None = Query(default=None),
    has_risk_flags: bool | None = Query(default=None),
    model_name: str | None = Query(default=None, min_length=1, max_length=200),
    project_id: int | None = Query(default=None, ge=1),
    case_id: int | None = Query(default=None, ge=1),
    created_from: str | None = Query(default=None),
    created_to: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> list[StoredDslGenerationRunSummary]:
    try:
        return list_dsl_generation_runs(
            session,
            status=status,
            feedback_status=feedback_status,
            generation_mode=generation_mode,
            import_mode=import_mode,
            prompt_variant=prompt_variant,
            rejection_reason_code=rejection_reason_code,
            has_risk_flags=has_risk_flags,
            model_name=model_name,
            project_id=project_id,
            case_id=case_id,
            created_from=_parse_optional_datetime(created_from, "created_from"),
            created_to=_parse_optional_datetime(created_to, "created_to"),
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/generations/{generation_id}", response_model=StoredDslGenerationRunDetail, summary="Get DSL generation run detail")
def get_generation_run_detail_route(
    generation_id: int,
    session: Session = Depends(get_db_session),
) -> StoredDslGenerationRunDetail:
    try:
        return get_dsl_generation_run_detail(session, generation_id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/generations/{generation_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a DSL generation run")
def delete_generation_run_route(
    generation_id: int,
    session: Session = Depends(get_db_session),
) -> None:
    try:
        delete_dsl_generation_run(session, generation_id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch(
    "/generations/{generation_id}/feedback",
    response_model=StoredDslGenerationRunSummary,
    summary="Record DSL generation feedback",
)
def record_generation_feedback_route(
    generation_id: int,
    payload: DslGenerationFeedbackRequest,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> StoredDslGenerationRunSummary:
    try:
        return record_dsl_generation_feedback(
            session,
            generation_id,
            payload.model_copy(update={"actor_user_id": current_user.id}),
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DslGenerationFeedbackPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DslGenerationFeedbackConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _parse_optional_datetime(value: str | None, field_name: str):
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须是合法 ISO datetime。") from exc
