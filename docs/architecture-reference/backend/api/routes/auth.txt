"""Authentication routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.auth import require_authenticated_user
from app.core.auth import SESSION_USER_ID_KEY
from app.db import get_db_session
from app.schemas.auth import CurrentUserResponse, LoginRequest, LogoutResponse
from app.services.auth import authenticate_user


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=CurrentUserResponse, summary="Login with local email/password")
def login_route(
    payload: LoginRequest,
    request: Request,
    session: Session = Depends(get_db_session),
) -> CurrentUserResponse:
    user = authenticate_user(session, payload.email, payload.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码错误。")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前账号已停用。")

    request.session.clear()
    request.session[SESSION_USER_ID_KEY] = user.id
    return CurrentUserResponse(id=user.id, email=user.email, display_name=user.display_name)


@router.get("/me", response_model=CurrentUserResponse, summary="Get current authenticated user")
def me_route(current_user=Depends(require_authenticated_user)) -> CurrentUserResponse:
    return CurrentUserResponse(
        id=current_user.id,
        email=current_user.email,
        display_name=current_user.display_name,
    )


@router.post("/logout", response_model=LogoutResponse, summary="Clear current login session")
def logout_route(request: Request) -> LogoutResponse:
    request.session.clear()
    return LogoutResponse(success=True)
