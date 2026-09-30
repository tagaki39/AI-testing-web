"""Project management services."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Project, ProjectMember, User
from app.schemas.projects import ProjectCreate, ProjectDetail, ProjectUpdate


class ProjectAccessError(ValueError):
    """Raised when user doesn't have access to a project."""


class ProjectConflictError(ValueError):
    """Raised when a project with the same name already exists."""


def create_project(
    session: Session,
    payload: ProjectCreate,
    owner_user_id: int
) -> ProjectDetail:
    """Create a new project with the specified user as owner."""
    project = Project(
        name=payload.name,
        description=payload.description,
    )
    session.add(project)

    try:
        session.flush()  # Get ID without committing
    except IntegrityError:
        session.rollback()
        raise ProjectConflictError(f"Project with name '{payload.name}' already exists.")

    # Add as owner
    member = ProjectMember(
        project_id=project.id,
        user_id=owner_user_id,
        role="owner",
    )
    session.add(member)

    session.commit()
    session.refresh(project)
    return _to_project_detail(project)


def get_project(session: Session, project_id: int) -> ProjectDetail | None:
    """Get a project by ID."""
    record = session.get(Project, project_id)
    if record is None:
        return None
    return _to_project_detail(record)


def update_project(
    session: Session,
    project_id: int,
    payload: ProjectUpdate,
    actor_user_id: int
) -> ProjectDetail:
    """Update a project. User must be a member."""
    project = session.get(Project, project_id)
    if project is None:
        raise ProjectAccessError(f"Project {project_id} not found.")

    # Check permissions
    if not _is_project_member(session, project_id, actor_user_id):
        raise ProjectAccessError(f"User {actor_user_id} is not a member of project {project_id}.")

    if payload.name is not None:
        project.name = payload.name
    if payload.description is not None:
        project.description = payload.description

    session.add(project)
    session.commit()
    session.refresh(project)
    return _to_project_detail(project)


def delete_project(session: Session, project_id: int, actor_user_id: int) -> bool:
    """Delete a project and all its test cases (cascade). User must be an owner."""
    project = session.get(Project, project_id)
    if project is None:
        raise ProjectAccessError(f"Project {project_id} not found.")

    # Must be owner to delete
    if not _is_project_owner(session, project_id, actor_user_id):
        raise ProjectAccessError(f"Only project owners can delete projects.")

    # test_cases cascade deleted via FK; ai_planning_sessions SET NULL via FK
    session.delete(project)
    session.commit()
    return True


def list_projects(session: Session, user_id: int) -> list[ProjectDetail]:
    """List all projects that the user has access to."""
    statement = (
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user_id)
        .order_by(Project.name.asc(), Project.id.asc())
    )
    records = session.scalars(statement).all()
    return [_to_project_detail(record) for record in records]


def _to_project_detail(record: Project) -> ProjectDetail:
    """Convert project model to ProjectDetail schema."""
    return ProjectDetail(
        id=record.id,
        name=record.name,
        description=record.description,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _is_project_member(session: Session, project_id: int, user_id: int) -> bool:
    """Check if a user is a member of a project."""
    statement = select(ProjectMember).where(
        ProjectMember.project_id == project_id,
        ProjectMember.user_id == user_id,
    )
    return session.scalar(statement) is not None


def _is_project_owner(session: Session, project_id: int, user_id: int) -> bool:
    """Check if a user is an owner of a project."""
    statement = select(ProjectMember).where(
        ProjectMember.project_id == project_id,
        ProjectMember.user_id == user_id,
        ProjectMember.role == "owner",
    )
    return session.scalar(statement) is not None