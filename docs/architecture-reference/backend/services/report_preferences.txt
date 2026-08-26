"""Report center preference services."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Project, ProjectMember, ReportPreference, TestCase, TestCaseRun
from app.schemas.reports import ReportPreferencePayload
from app.services.cases import EntityNotFoundError


def get_report_preference(session: Session, *, user_id: int) -> ReportPreferencePayload:
    accessible_project_ids = _get_accessible_project_ids(session, user_id=user_id)
    preference = session.scalar(select(ReportPreference).where(ReportPreference.user_id == user_id))
    if preference is not None:
        normalized = _normalize_saved_preference(session, preference=preference, accessible_project_ids=accessible_project_ids)
        if normalized is not None:
            return normalized
    return _resolve_default_preference(session, user_id=user_id, accessible_project_ids=accessible_project_ids)


def update_report_preference(
    session: Session,
    *,
    user_id: int,
    payload: ReportPreferencePayload,
) -> ReportPreferencePayload:
    accessible_project_ids = _get_accessible_project_ids(session, user_id=user_id)
    _validate_preference_target(session, payload=payload, accessible_project_ids=accessible_project_ids)

    record = session.scalar(select(ReportPreference).where(ReportPreference.user_id == user_id))
    if record is None:
        record = ReportPreference(user_id=user_id)
    record.scope_type = payload.scope_type
    record.project_id = payload.project_id
    record.case_id = payload.case_id
    record.window_days = payload.window_days
    session.add(record)
    session.commit()
    session.refresh(record)
    return ReportPreferencePayload(
        scope_type=record.scope_type,
        project_id=record.project_id,
        case_id=record.case_id,
        window_days=record.window_days,
    )


def _normalize_saved_preference(
    session: Session,
    *,
    preference: ReportPreference,
    accessible_project_ids: set[int],
) -> ReportPreferencePayload | None:
    try:
        payload = ReportPreferencePayload(
            scope_type=preference.scope_type,
            project_id=preference.project_id,
            case_id=preference.case_id,
            window_days=preference.window_days,
        )
    except ValueError:
        return None

    try:
        _validate_preference_target(session, payload=payload, accessible_project_ids=accessible_project_ids)
    except EntityNotFoundError:
        return None
    return payload


def _resolve_default_preference(
    session: Session,
    *,
    user_id: int,
    accessible_project_ids: set[int],
) -> ReportPreferencePayload:
    recent_project_id = _get_recent_active_project_id(
        session,
        user_id=user_id,
        accessible_project_ids=accessible_project_ids,
    )
    if recent_project_id is not None:
        return ReportPreferencePayload(scope_type="project", project_id=recent_project_id, window_days=7)

    fallback_project = session.scalar(
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user_id)
        .order_by(Project.name.asc(), Project.id.asc())
        .limit(1)
    )
    if fallback_project is not None:
        return ReportPreferencePayload(scope_type="project", project_id=fallback_project.id, window_days=7)
    return ReportPreferencePayload(scope_type="global", window_days=7)


def _get_recent_active_project_id(
    session: Session,
    *,
    user_id: int,
    accessible_project_ids: set[int],
) -> int | None:
    if not accessible_project_ids:
        return None

    latest_execution = session.execute(
        select(TestCaseRun.project_id, TestCaseRun.started_at)
        .where(TestCaseRun.triggered_by == user_id, TestCaseRun.project_id.in_(accessible_project_ids))
        .order_by(TestCaseRun.started_at.desc(), TestCaseRun.id.desc())
        .limit(1)
    ).first()
    latest_case = session.execute(
        select(TestCase.project_id, TestCase.updated_at)
        .where(
            TestCase.project_id.in_(accessible_project_ids),
            or_(TestCase.created_by == user_id, TestCase.updated_by == user_id),
        )
        .order_by(TestCase.updated_at.desc(), TestCase.id.desc())
        .limit(1)
    ).first()
    candidates: list[tuple[datetime, int]] = []
    for row in (latest_execution, latest_case):
        if row is None:
            continue
        project_id, activity_at = row
        if activity_at is None:
            continue
        candidates.append((activity_at, project_id))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _get_accessible_project_ids(session: Session, *, user_id: int) -> set[int]:
    statement = select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
    return set(session.scalars(statement).all())


def _validate_preference_target(
    session: Session,
    *,
    payload: ReportPreferencePayload,
    accessible_project_ids: set[int],
) -> None:
    if payload.project_id is not None and payload.project_id not in accessible_project_ids:
        raise EntityNotFoundError(f"Project {payload.project_id} not found.")
    if payload.case_id is None:
        return

    case = session.get(TestCase, payload.case_id)
    if case is None:
        raise EntityNotFoundError(f"Case {payload.case_id} not found.")
    if payload.project_id is not None and case.project_id != payload.project_id:
        raise EntityNotFoundError(f"Case {payload.case_id} not found.")
