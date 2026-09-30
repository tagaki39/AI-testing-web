"""Authentication dependencies and helpers."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.auth import SESSION_USER_ID_KEY
from app.db import get_db_session
from app.models import User
from app.services.auth import get_user_by_id

DEFAULT_DEMO_USER_ID = 1


def require_authenticated_user(
    request: Request,
    session: Session = Depends(get_db_session),
) -> User:
    user_id = request.session.get(SESSION_USER_ID_KEY)
    if not isinstance(user_id, int):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或登录态已失效。")

    user = get_user_by_id(session, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或登录态已失效。")
    return user


def require_demo_user(
    session: Session = Depends(get_db_session),
) -> User:
    """Return a fixed demo user (ID=1) without real authentication.

    WARNING: Development/demo only. In production, replace with
    ``require_authenticated_user`` for proper session-based auth.
    """
    user = get_user_by_id(session, DEFAULT_DEMO_USER_ID)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Demo user 1 is missing.",
        )
    return user


def get_demo_user_or_raise(session: Session, *, user_id: int | None = None) -> User:
    """Lookup a user by id or fall back to the default demo user.

    Used by the WebSocket route where FastAPI dependency injection is not available.
    """
    resolved_user_id = user_id or DEFAULT_DEMO_USER_ID
    user = get_user_by_id(session, resolved_user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive.",
        )
    return user
