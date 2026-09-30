"""Report center preference routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.auth import require_authenticated_user
from app.db import get_db_session
from app.models import User
from app.schemas.reports import ReportPreferencePayload
from app.services import EntityNotFoundError
from app.services.report_preferences import get_report_preference, update_report_preference


router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/preferences", response_model=ReportPreferencePayload)
def get_report_preference_route(
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_authenticated_user),
) -> ReportPreferencePayload:
    return get_report_preference(session, user_id=current_user.id)


@router.put("/preferences", response_model=ReportPreferencePayload)
def update_report_preference_route(
    payload: ReportPreferencePayload,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_authenticated_user),
) -> ReportPreferencePayload:
    try:
        return update_report_preference(session, user_id=current_user.id, payload=payload)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
