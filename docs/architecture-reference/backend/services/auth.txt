"""Authentication service helpers."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import verify_password
from app.models import User


def authenticate_user(session: Session, email: str, password: str) -> User | None:
    statement = select(User).where(User.email == email.strip().lower())
    user = session.scalar(statement)
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def get_user_by_id(session: Session, user_id: int) -> User | None:
    return session.get(User, user_id)
