"""Project management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.auth import require_demo_user
from app.db import get_db_session
from app.models import User
from app.schemas.projects import ProjectCreate, ProjectDetail, ProjectSummary, ProjectUpdate
from app.services import (
    create_project,
    delete_project,
    get_project,
    list_projects,
    ProjectAccessError,
    update_project,
)


router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectDetail, status_code=status.HTTP_201_CREATED)
def create_project_route(
    payload: ProjectCreate,
    response: Response,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> ProjectDetail:
    """Create a new project. The current user becomes the owner."""
    created_project = create_project(session, payload, current_user.id)
    response.headers["Location"] = f"/api/v1/projects/{created_project.id}"
    return created_project


@router.get("", response_model=list[ProjectDetail])
def list_projects_route(
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> list[ProjectDetail]:
    """List all projects accessible to the current user."""
    return list_projects(session, current_user.id)


@router.get("/{project_id}", response_model=ProjectDetail)
def get_project_route(
    project_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> ProjectDetail:
    """Get a specific project by ID. User must have access to the project."""
    project = get_project(session, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found."
        )
    if not _is_project_member(session, project_id, current_user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this project."
        )
    return project


@router.put("/{project_id}", response_model=ProjectDetail)
def update_project_route(
    project_id: int,
    payload: ProjectUpdate,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> ProjectDetail:
    """Update a project. User must be a member."""
    try:
        return update_project(session, project_id, payload, current_user.id)
    except ProjectAccessError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc)
        )


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project_route(
    project_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> None:
    """Delete a project. User must be an owner and project must not have test cases."""
    try:
        delete_project(session, project_id, current_user.id)
    except ProjectAccessError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc)
        )


def _is_project_member(session: Session, project_id: int, user_id: int) -> bool:
    """Check if a user is a member of a project."""
    from app.services.project_management import _is_project_member as is_member
    return is_member(session, project_id, user_id)
