"""Case management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.auth import require_demo_user
from app.db import get_db_session
from app.models import User
from app.schemas.cases import (
    BatchDeleteRequest,
    BatchUpdateRequest,
    CaseCreateRequest,
    CaseUpdateRequest,
    PaginatedCases,
    ProjectTestCaseStats,
    StoredCaseDetail,
)
from app.services import (
    batch_delete_cases,
    batch_update_cases,
    create_case,
    delete_case,
    get_case,
    list_cases_paginated,
    update_case,
)
from app.services.cases import (
    EntityNotFoundError,
    get_project_test_case_stats,
    _ensure_project_member,
)


router = APIRouter(prefix="/cases", tags=["cases"])


@router.post("", response_model=StoredCaseDetail, status_code=status.HTTP_201_CREATED)
def create_case_route(
    payload: CaseCreateRequest,
    response: Response,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> StoredCaseDetail:
    """Create a new test case."""
    try:
        created_case = create_case(
            session,
            payload.model_copy(update={"actor_user_id": current_user.id}),
            current_user.id
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    response.headers["Location"] = f"/api/v1/cases/{created_case.id}"
    return created_case


@router.get("/project/{project_id}", response_model=PaginatedCases)
def list_project_cases_route(
    project_id: int,
    search: str | None = None,
    created_by: int | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> PaginatedCases:
    """List test cases within a specific project with optional filtering and pagination."""
    from app.schemas.cases import CaseListFilter
    from app.services.project_management import get_project
    from app.services.cases import _ensure_project_member

    # Verify project exists and user has access
    project = get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    # Check project membership
    try:
        _ensure_project_member(session, project_id, current_user.id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))

    filter_params = CaseListFilter(
        project_id=project_id,
        search=search,
        created_by=created_by,
    )

    items, total = list_cases_paginated(session, filter_params, page, page_size, current_user.id)

    total_pages = (total + page_size - 1) // page_size

    return PaginatedCases(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_prev=page > 1,
    )


@router.get("/stats/{project_id}", response_model=ProjectTestCaseStats)
def get_project_stats_route(
    project_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> ProjectTestCaseStats:
    """Get statistics for test cases in a project."""
    # Verify project exists and user has access
    from app.services.project_management import get_project
    from app.services.cases import _ensure_project_member

    project = get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    # Check project membership
    try:
        _ensure_project_member(session, project_id, current_user.id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))

    stats_data = get_project_test_case_stats(session, project_id)
    return ProjectTestCaseStats(**stats_data)


@router.get("", response_model=PaginatedCases)
def list_cases_route(
    project_id: int | None = None,
    search: str | None = None,
    created_by: int | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> PaginatedCases:
    """List test cases with optional filtering and pagination."""
    from app.schemas.cases import CaseListFilter

    # If project_id is provided, check project membership
    if project_id is not None:
        from app.services.project_management import get_project
        from app.services.cases import _ensure_project_member

        project = get_project(session, project_id)
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

        try:
            _ensure_project_member(session, project_id, current_user.id)
        except EntityNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))

    filter_params = CaseListFilter(
        project_id=project_id,
        search=search,
        created_by=created_by,
    )

    items, total = list_cases_paginated(session, filter_params, page, page_size, current_user.id)

    total_pages = (total + page_size - 1) // page_size

    return PaginatedCases(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_prev=page > 1,
    )


@router.get("/{case_id}", response_model=StoredCaseDetail)
def get_case_route(
    case_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> StoredCaseDetail:
    """Get a specific test case by ID."""
    try:
        stored_case = get_case(session, case_id, current_user.id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))

    if stored_case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")

    return stored_case


@router.post("/batch", response_model=list[StoredCaseDetail])
def batch_update_cases_route(
    payload: BatchUpdateRequest,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> list[StoredCaseDetail]:
    """Update multiple test cases in a single request."""
    try:
        return batch_update_cases(session, payload, current_user.id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.delete("/batch", status_code=status.HTTP_204_NO_CONTENT)
def batch_delete_cases_route(
    payload: BatchDeleteRequest,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> None:
    """Delete multiple test cases in a single request."""
    deleted_count = batch_delete_cases(session, payload)
    if deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No cases found to delete.")


@router.put("/{case_id}", response_model=StoredCaseDetail)
def update_case_route(
    case_id: int,
    payload: CaseUpdateRequest,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> StoredCaseDetail:
    """Update a test case."""
    try:
        return update_case(session, case_id, payload.model_copy(update={"actor_user_id": current_user.id}), current_user.id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))


@router.delete("/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_case_route(
    case_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> None:
    """Delete a single test case."""
    try:
        delete_case(session, case_id, current_user.id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
