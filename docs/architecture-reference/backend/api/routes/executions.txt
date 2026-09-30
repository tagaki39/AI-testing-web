"""Case execution routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.auth import require_demo_user
from app.db import get_db_session
from app.models import User
from app.schemas.executions import (
    CaseExecutionRequest,
    ExecutionStatus,
    ExecutionsOverview,
    StoredCaseExecutionDetail,
    StoredCaseExecutionSummary,
)
from app.services import (
    EntityNotFoundError,
    delete_execution,
    execute_case,
    get_executions_overview,
    get_case_execution,
    list_case_executions,
    list_executions,
)


router = APIRouter(tags=["executions"])


@router.post("/cases/{case_id}/execute", response_model=StoredCaseExecutionDetail)
def execute_case_route(
    case_id: int,
    payload: CaseExecutionRequest,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> StoredCaseExecutionDetail:
    try:
        return execute_case(session, case_id, payload.model_copy(update={"actor_user_id": current_user.id}))
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/cases/{case_id}/executions", response_model=list[StoredCaseExecutionSummary])
def list_case_executions_route(
    case_id: int,
    session: Session = Depends(get_db_session),
) -> list[StoredCaseExecutionSummary]:
    try:
        return list_case_executions(session, case_id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/executions", response_model=list[StoredCaseExecutionSummary])
def list_executions_route(
    project_id: int | None = Query(default=None, ge=1),
    case_id: int | None = Query(default=None, ge=1),
    status_filter: ExecutionStatus | None = Query(default=None, alias="status"),
    window_days: int | None = Query(default=None),
    failure_category: str | None = Query(default=None),
    failure_fingerprint: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> list[StoredCaseExecutionSummary]:
    if window_days not in {None, 7, 14, 30}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="window_days must be one of: 7, 14, 30.",
        )
    return list_executions(
        session,
        project_id=project_id,
        case_id=case_id,
        status=status_filter,
        window_days=window_days,
        failure_category=failure_category,
        failure_fingerprint=failure_fingerprint,
        limit=limit,
        offset=offset,
    )


@router.get("/executions/overview", response_model=ExecutionsOverview)
def get_executions_overview_route(
    scope_type: str | None = Query(default=None),
    project_id: int | None = Query(default=None, ge=1),
    case_id: int | None = Query(default=None, ge=1),
    window_days: int | None = Query(default=None),
    failure_fingerprint: str | None = Query(default=None, min_length=1),
    session: Session = Depends(get_db_session),
) -> ExecutionsOverview:
    if window_days not in {None, 7, 14, 30}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="window_days must be one of: 7, 14, 30.",
        )
    if scope_type not in {None, "global", "project", "case"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="scope_type must be one of: global, project, case.",
        )
    return get_executions_overview(
        session,
        scope_type=scope_type,
        project_id=project_id,
        case_id=case_id,
        window_days=window_days,
        failure_fingerprint=failure_fingerprint,
    )


@router.get("/executions/{execution_id}", response_model=StoredCaseExecutionDetail)
def get_case_execution_route(
    execution_id: int,
    session: Session = Depends(get_db_session),
) -> StoredCaseExecutionDetail:
    execution = get_case_execution(session, execution_id)
    if execution is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution not found.")
    return execution


@router.delete("/executions/{execution_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_execution_route(
    execution_id: int,
    session: Session = Depends(get_db_session),
) -> None:
    try:
        delete_execution(session, execution_id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
