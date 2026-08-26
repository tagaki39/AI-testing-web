"""Test case persistence services."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models import Project, ProjectMember, TestCase, User
from app.schemas.cases import (
    BatchDeleteRequest,
    BatchUpdateRequest,
    CaseCreateRequest,
    CaseListFilter,
    CaseUpdateRequest,
    StoredCaseDetail,
    StoredCaseSummary,
)
from app.schemas.dsl import DSLCase
from pydantic import ValidationError


class EntityNotFoundError(ValueError):
    """Raised when a required entity does not exist."""


def create_case(session: Session, payload: CaseCreateRequest, actor_user_id: int | None = None) -> StoredCaseDetail:
    _ensure_project_exists(session, payload.project_id)

    # Check project membership if actor_user_id is provided
    if actor_user_id is not None:
        _ensure_project_member(session, payload.project_id, actor_user_id)
    else:
        _ensure_user_exists(session, payload.actor_user_id)

    case = TestCase(
        project_id=payload.project_id,
        created_by=payload.actor_user_id,
        updated_by=payload.actor_user_id,
        name=payload.name,
        description=payload.description,
        dsl=payload.model_dump(mode="json", exclude={"project_id", "actor_user_id"}),
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    return _to_stored_case_detail(case)


def update_case(session: Session, case_id: int, payload: CaseUpdateRequest, actor_user_id: int | None = None) -> StoredCaseDetail:
    case = session.get(TestCase, case_id)
    if case is None:
        raise EntityNotFoundError(f"Case {case_id} not found.")

    _ensure_project_exists(session, payload.project_id)

    # Check project membership for both target project and current case project
    if actor_user_id is not None:
        _ensure_project_member(session, payload.project_id, actor_user_id)
        _ensure_project_member(session, case.project_id, actor_user_id)

    _ensure_user_exists(session, payload.actor_user_id)
    case = session.get(TestCase, case_id)
    if case is None:
        raise EntityNotFoundError(f"Case {case_id} not found.")

    _ensure_project_exists(session, payload.project_id)
    _ensure_user_exists(session, payload.actor_user_id)

    case.project_id = payload.project_id
    case.updated_by = payload.actor_user_id
    case.name = payload.name
    case.description = payload.description
    case.dsl = payload.model_dump(mode="json", exclude={"project_id", "actor_user_id"})
    session.add(case)
    session.commit()
    session.refresh(case)
    return _to_stored_case_detail(case)


def _ensure_project_member(session: Session, project_id: int, user_id: int) -> None:
    """Check if a user is a member of a project."""
    statement = select(ProjectMember).where(
        ProjectMember.project_id == project_id,
        ProjectMember.user_id == user_id,
    )
    if session.scalar(statement) is None:
        raise EntityNotFoundError(f"User {user_id} is not a member of project {project_id}.")


def delete_case(session: Session, case_id: int, actor_user_id: int | None = None) -> bool:
    """Delete a test case."""
    case = session.get(TestCase, case_id)
    if case is None:
        raise EntityNotFoundError(f"Case {case_id} not found.")

    # Check project membership if actor_user_id is provided
    if actor_user_id is not None:
        _ensure_project_member(session, case.project_id, actor_user_id)

    session.delete(case)
    session.commit()
    return True


def get_case(session: Session, case_id: int, actor_user_id: int | None = None) -> StoredCaseDetail | None:
    """Get a test case by ID with optional project member check."""
    record = session.get(TestCase, case_id)
    if record is None:
        return None

    # Check project membership if actor_user_id is provided
    if actor_user_id is not None:
        _ensure_project_member(session, record.project_id, actor_user_id)

    return _to_stored_case_detail(record)


def _ensure_project_exists(session: Session, project_id: int) -> None:
    if session.get(Project, project_id) is None:
        raise EntityNotFoundError(f"Project {project_id} not found.")


def _ensure_user_exists(session: Session, user_id: int) -> None:
    if session.get(User, user_id) is None:
        raise EntityNotFoundError(f"User {user_id} not found.")


def _to_stored_case_summary(record: TestCase) -> StoredCaseSummary:
    try:
        normalized_case = DSLCase.model_validate(record.dsl)
    except ValidationError:
        # Return a degraded summary when stored DSL is malformed
        return StoredCaseSummary(
            id=record.id,
            project_id=record.project_id,
            name=record.name,
            description=record.description,
            base_url=None,
            input_contract=[],
            output_contract=[],
            steps=[],
            created_by=record.created_by,
            updated_by=record.updated_by,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
    return StoredCaseSummary(
        id=record.id,
        project_id=record.project_id,
        name=record.name,
        description=record.description,
        base_url=normalized_case.base_url,
        input_contract=normalized_case.input_contract,
        output_contract=normalized_case.output_contract,
        steps=normalized_case.steps,
        created_by=record.created_by,
        updated_by=record.updated_by,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def list_cases_filtered(session: Session, filter_params: CaseListFilter) -> Select:
    """Build a query for filtered test cases."""
    statement = select(TestCase)

    if filter_params.project_id:
        statement = statement.where(TestCase.project_id == filter_params.project_id)

    if filter_params.search:
        escaped = filter_params.search.replace('%', '\\%').replace('_', '\\_')
        search_term = f"%{escaped}%"
        statement = statement.where(
            TestCase.name.ilike(search_term) |
            TestCase.description.ilike(search_term)
        )

    if filter_params.created_by:
        statement = statement.where(TestCase.created_by == filter_params.created_by)

    return statement.order_by(TestCase.created_at.desc(), TestCase.id.desc())


def list_cases_paginated(
    session: Session,
    filter_params: CaseListFilter,
    page: int,
    page_size: int,
    actor_user_id: int | None = None
) -> tuple[list[StoredCaseSummary], int]:
    """List test cases with pagination."""
    # Get total count
    count_statement = select(func.count(TestCase.id))
    where_clauses = []

    if filter_params.project_id:
        count_statement = count_statement.where(TestCase.project_id == filter_params.project_id)
        # Check project membership if actor_user_id is provided
        if actor_user_id is not None:
            _ensure_project_member(session, filter_params.project_id, actor_user_id)

    if filter_params.search:
        escaped = filter_params.search.replace('%', '\\%').replace('_', '\\_')
        search_term = f"%{escaped}%"
        count_statement = count_statement.where(
            TestCase.name.ilike(search_term) |
            TestCase.description.ilike(search_term)
        )

    if filter_params.created_by:
        count_statement = count_statement.where(TestCase.created_by == filter_params.created_by)

    total = session.scalar(count_statement) or 0

    # Get paginated results
    statement = list_cases_filtered(session, filter_params)
    offset = (page - 1) * page_size
    statement = statement.offset(offset).limit(page_size)

    records = session.scalars(statement).all()
    cases = [_to_stored_case_summary(record) for record in records]

    return cases, total


def batch_update_cases(session: Session, payload: BatchUpdateRequest, actor_user_id: int | None = None) -> list[StoredCaseDetail]:
    """Update multiple test cases at once."""
    updated_cases = []
    for case_id in payload.case_ids:
        try:
            case = update_case(session, case_id, payload.updates, actor_user_id)
            updated_cases.append(case)
        except EntityNotFoundError:
            # Skip non-existent cases
            continue

    if not updated_cases:
        raise EntityNotFoundError("No valid cases found to update.")

    return updated_cases


def batch_delete_cases(session: Session, payload: BatchDeleteRequest) -> int:
    """Delete multiple test cases at once."""
    statement = select(TestCase).where(TestCase.id.in_(payload.case_ids))
    cases = session.scalars(statement).all()

    deleted_count = len(cases)
    for case in cases:
        session.delete(case)

    session.commit()
    return deleted_count


def get_project_test_case_stats(session: Session, project_id: int) -> dict[str, Any]:
    """Get statistics for test cases in a project."""
    # Total count
    total_count = session.scalar(
        select(func.count(TestCase.id)).where(TestCase.project_id == project_id)
    ) or 0

    # Cases by month — use strftime (SQLite) or to_char (PostgreSQL)
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "sqlite":
        month_expr = func.strftime('%Y-%m', TestCase.created_at)
    else:
        month_expr = func.to_char(TestCase.created_at, 'YYYY-MM')
    month_counts = session.execute(
        select(
            month_expr.label('month'),
            func.count(TestCase.id).label('count')
        )
        .where(TestCase.project_id == project_id)
        .group_by(month_expr)
        .order_by('month')
    ).fetchall()

    # Get user names for cases created by
    user_ids = session.execute(
        select(TestCase.created_by).distinct().where(TestCase.project_id == project_id)
    ).fetchall()
    user_names = {row[0]: f"User_{row[0]}" for row in user_ids}  # Simplified, would need User query

    # Recent cases (last 5)
    recent_statement = (
        select(TestCase)
        .where(TestCase.project_id == project_id)
        .order_by(TestCase.created_at.desc())
        .limit(5)
    )
    recent_cases = session.scalars(recent_statement).all()

    return {
        'project_id': project_id,
        'total_cases': total_count,
        'created_by_month': {row.month: row.count for row in month_counts},
        'created_by_user': user_names,
        'recent_cases': [_to_stored_case_summary(case) for case in recent_cases],
    }


def _to_stored_case_detail(record: TestCase) -> StoredCaseDetail:
    summary = _to_stored_case_summary(record)
    return StoredCaseDetail(**summary.model_dump())
