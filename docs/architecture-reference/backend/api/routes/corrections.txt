"""Locator correction routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.auth import require_demo_user
from app.db import get_db_session
from app.models import User
from app.schemas.corrections import (
    BatchUpdateCorrectionStateRequest,
    CreateCorrectionRequest,
    LocatorCorrectionsOverview,
    StoredLocatorCorrection,
    StoredLocatorCorrectionEvent,
    UpdateCorrectionStateRequest,
)
from app.services import (
    CorrectionConflictError,
    EntityNotFoundError,
    batch_update_correction_state,
    create_correction,
    delete_correction,
    get_corrections_overview,
    list_corrections,
    list_correction_events,
    update_correction_state,
)


router = APIRouter(prefix="/corrections", tags=["corrections"])


@router.post("", response_model=StoredLocatorCorrection, status_code=status.HTTP_201_CREATED)
def create_correction_route(
    payload: CreateCorrectionRequest,
    response: Response,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> StoredLocatorCorrection:
    try:
        correction = create_correction(session, payload.model_copy(update={"created_by": current_user.id}))
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CorrectionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    response.headers["Location"] = f"/api/v1/corrections/{correction.id}"
    return correction


@router.get("", response_model=list[StoredLocatorCorrection])
def list_corrections_route(
    target_description: str | None = Query(default=None, min_length=1),
    page_url: str | None = Query(default=None, min_length=1),
    is_active: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> list[StoredLocatorCorrection]:
    return list_corrections(
        session,
        target_description=target_description,
        page_url=page_url,
        is_active=is_active,
        limit=limit,
        offset=offset,
    )


@router.get("/overview", response_model=LocatorCorrectionsOverview)
def get_corrections_overview_route(
    window_days: int = Query(default=7),
    session: Session = Depends(get_db_session),
) -> LocatorCorrectionsOverview:
    if window_days not in {7, 14, 30}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="window_days must be one of: 7, 14, 30.",
        )
    return get_corrections_overview(session, window_days=window_days)


@router.get("/{correction_id}/events", response_model=list[StoredLocatorCorrectionEvent])
def list_correction_events_route(
    correction_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> list[StoredLocatorCorrectionEvent]:
    try:
        return list_correction_events(
            session,
            correction_id,
            limit=limit,
            offset=offset,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.patch("/bulk", response_model=list[StoredLocatorCorrection])
def batch_update_correction_state_route(
    payload: BatchUpdateCorrectionStateRequest,
    session: Session = Depends(get_db_session),
) -> list[StoredLocatorCorrection]:
    try:
        return batch_update_correction_state(session, payload)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CorrectionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.patch("/{correction_id}", response_model=StoredLocatorCorrection)
def update_correction_state_route(
    correction_id: int,
    payload: UpdateCorrectionStateRequest,
    session: Session = Depends(get_db_session),
) -> StoredLocatorCorrection:
    try:
        return update_correction_state(session, correction_id, payload)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CorrectionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.delete("/{correction_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_correction_route(
    correction_id: int,
    session: Session = Depends(get_db_session),
) -> None:
    try:
        delete_correction(session, correction_id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
