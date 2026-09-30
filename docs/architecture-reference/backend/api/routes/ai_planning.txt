"""Routes for AI planning sessions and drafts."""

from __future__ import annotations

import logging
import traceback as _traceback

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.auth import get_demo_user_or_raise, require_demo_user
from app.core.config import get_settings
from app.db import get_db_session
from app.db.session import get_session_factory
from app.models import User
from app.schemas.ai_planning import (
    AIPlanningDraft,
    AIPlanningMessageCreateRequest,
    AIPlanningSessionDetail,
    AIPlanningSessionSummary,
    AIPlanningTurnResponse,
    CreateAIPlanningSessionRequest,
    CreateProjectInSessionRequest,
    GenerateAIPlanningDraftsRequest,
    LinkProjectRequest,
    ProjectSummaryInSession,
    UpdateAIPlanningDraftStatusRequest,
)
from app.schemas.dsl import DSLModel
from pydantic import Field
from app.services.ai_planning import (
    AIPlanningAccessError,
    create_planning_session,
    create_project_in_session,
    delete_planning_draft,
    delete_planning_session,
    generate_planning_drafts,
    get_planning_session_detail,
    link_project_to_session,
    list_planning_sessions,
    list_session_projects,
    retest_cases,
    save_and_execute_selected_drafts,
    send_planning_message,
    unlink_project_from_session,
    update_planning_draft_status,
)
from app.services.ai_planning_streaming import (
    CancellationManager,
    sse_event,
    stream_planning_chat,
    stream_planning_drafts,
    stream_save_and_execute,
)
from app.services.cases import EntityNotFoundError

logger = logging.getLogger(__name__)

_cancellation_manager = CancellationManager()


router = APIRouter(prefix="/ai-planning", tags=["ai-planning"])


@router.post("/sessions", response_model=AIPlanningSessionDetail, status_code=status.HTTP_201_CREATED)
def create_planning_session_route(
    payload: CreateAIPlanningSessionRequest,
    response: Response,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> AIPlanningSessionDetail:
    try:
        detail = create_planning_session(session, payload, actor_user_id=current_user.id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    response.headers["Location"] = f"/api/v1/ai-planning/sessions/{detail.session.id}"
    return detail


@router.get("/sessions", response_model=list[AIPlanningSessionSummary])
def list_planning_sessions_route(
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> list[AIPlanningSessionSummary]:
    return list_planning_sessions(session, actor_user_id=current_user.id)


@router.get("/sessions/{session_id}", response_model=AIPlanningSessionDetail)
def get_planning_session_route(
    session_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> AIPlanningSessionDetail:
    try:
        return get_planning_session_detail(session, session_id, actor_user_id=current_user.id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIPlanningAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_planning_session_route(
    session_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> Response:
    try:
        delete_planning_session(session, session_id, actor_user_id=current_user.id)
    except (EntityNotFoundError, AIPlanningAccessError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sessions/{session_id}/messages", response_model=AIPlanningTurnResponse)
def send_planning_message_route(
    session_id: int,
    payload: AIPlanningMessageCreateRequest,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> AIPlanningTurnResponse:
    try:
        return send_planning_message(session, session_id, actor_user_id=current_user.id, content=payload.content)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIPlanningAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/drafts:generate", response_model=AIPlanningTurnResponse)
def generate_planning_drafts_route(
    session_id: int,
    payload: GenerateAIPlanningDraftsRequest,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> AIPlanningTurnResponse:
    try:
        return generate_planning_drafts(session, session_id, payload, actor_user_id=current_user.id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIPlanningAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.patch("/drafts/{draft_id}", response_model=AIPlanningDraft)
def update_planning_draft_status_route(
    draft_id: int,
    payload: UpdateAIPlanningDraftStatusRequest,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> AIPlanningDraft:
    try:
        return update_planning_draft_status(session, draft_id, payload, actor_user_id=current_user.id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIPlanningAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.delete("/drafts/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_planning_draft_route(
    draft_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> Response:
    try:
        delete_planning_draft(session, draft_id, actor_user_id=current_user.id)
    except (EntityNotFoundError, AIPlanningAccessError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class SaveAndExecuteRequest(DSLModel):
    draft_ids: list[int]
    execute: bool = True
    input_values: dict[str, str] = Field(
        default_factory=dict,
        description="Variable substitutions for ${context_key} placeholders.",
    )


class RetestRequest(DSLModel):
    case_ids: list[int] | None = Field(default=None, description="要复测的用例 ID 列表")
    failed_only: bool = Field(default=False, description="仅复测最近失败的用例")
    input_values: dict[str, str] = Field(default_factory=dict)


class ChatSSERequest(DSLModel):
    content: str
    scenario_keys: list[str] = Field(default_factory=list)


class ExecuteSSERequest(DSLModel):
    draft_ids: list[int]


@router.post("/sessions/{session_id}/retest", response_model=AIPlanningTurnResponse)
def retest_cases_route(
    session_id: int,
    payload: RetestRequest,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> AIPlanningTurnResponse:
    try:
        return retest_cases(
            session, session_id,
            actor_user_id=current_user.id,
            case_ids=payload.case_ids,
            failed_only=payload.failed_only,
            input_values=payload.input_values,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIPlanningAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Session-Project association endpoints
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/projects", response_model=list[ProjectSummaryInSession])
def list_session_projects_route(
    session_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> list[ProjectSummaryInSession]:
    try:
        return list_session_projects(session, session_id, actor_user_id=current_user.id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIPlanningAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/projects", response_model=ProjectSummaryInSession, status_code=status.HTTP_201_CREATED)
def link_project_route(
    session_id: int,
    payload: LinkProjectRequest,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> ProjectSummaryInSession:
    try:
        return link_project_to_session(session, session_id, project_id=payload.project_id, actor_user_id=current_user.id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIPlanningAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/sessions/{session_id}/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def unlink_project_route(
    session_id: int,
    project_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> Response:
    try:
        unlink_project_from_session(session, session_id, project_id=project_id, actor_user_id=current_user.id)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIPlanningAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sessions/{session_id}/projects:create", response_model=ProjectSummaryInSession, status_code=status.HTTP_201_CREATED)
def create_project_in_session_route(
    session_id: int,
    payload: CreateProjectInSessionRequest,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> ProjectSummaryInSession:
    try:
        return create_project_in_session(
            session, session_id,
            name=payload.name, description=payload.description,
            actor_user_id=current_user.id,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIPlanningAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/drafts:save-and-execute", response_model=AIPlanningTurnResponse)
def save_and_execute_route(
    session_id: int,
    payload: SaveAndExecuteRequest,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_demo_user),
) -> AIPlanningTurnResponse:
    try:
        return save_and_execute_selected_drafts(session, session_id, payload.draft_ids, current_user.id, payload.execute, input_values=payload.input_values)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AIPlanningAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/chat")
async def chat_sse(
    session_id: int,
    req: ChatSSERequest,
    current_user: User = Depends(require_demo_user),
) -> StreamingResponse:
    """SSE stream for AI planning chat (ReAct mode)."""
    session_factory = get_session_factory()

    # Validate session exists before starting stream
    with session_factory() as db:
        from app.services.ai_planning import _get_session
        try:
            _get_session(db, session_id, actor_user_id=current_user.id)
        except EntityNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def event_generator():
        try:
            async for event in stream_planning_chat(
                session_factory=session_factory,
                planning_session_id=session_id,
                content=req.content,
                actor_user_id=current_user.id,
            ):
                yield sse_event(event.get("type", "message"), event)
        except Exception as exc:
            logger.exception("SSE chat streaming error for session %s", session_id)
            yield sse_event("error", {
                "message": str(exc),
                "error_type": type(exc).__name__,
                "phase": "chat",
                "traceback": _traceback.format_exc()[:2000] if get_settings().debug else None,
            })
        yield sse_event("done", {})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/drafts")
async def drafts_sse(
    session_id: int,
    req: GenerateAIPlanningDraftsRequest,
    current_user: User = Depends(require_demo_user),
) -> StreamingResponse:
    """SSE stream for draft generation."""
    session_factory = get_session_factory()

    # Validate session exists before starting stream
    with session_factory() as db:
        from app.services.ai_planning import _get_session
        try:
            _get_session(db, session_id, actor_user_id=current_user.id)
        except EntityNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def event_generator():
        try:
            async for event in stream_planning_drafts(
                session_factory=session_factory,
                planning_session_id=session_id,
                payload=req,
                actor_user_id=current_user.id,
            ):
                yield sse_event(event.get("type", "message"), event)
        except Exception as exc:
            logger.exception("SSE draft streaming error for session %s", session_id)
            yield sse_event("error", {
                "message": str(exc),
                "error_type": type(exc).__name__,
                "phase": "drafts",
                "traceback": _traceback.format_exc()[:2000] if get_settings().debug else None,
            })
        yield sse_event("done", {})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/execute")
async def execute_sse(
    session_id: int,
    req: ExecuteSSERequest,
    current_user: User = Depends(require_demo_user),
) -> StreamingResponse:
    """SSE stream for save-and-execute."""
    session_factory = get_session_factory()

    # Validate session exists before starting stream
    with session_factory() as db:
        from app.services.ai_planning import _get_session
        try:
            _get_session(db, session_id, actor_user_id=current_user.id)
        except EntityNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    cancel_event = _cancellation_manager.register(session_id)

    async def event_generator():
        try:
            async for event in stream_save_and_execute(
                session_factory=session_factory,
                planning_session_id=session_id,
                draft_ids=req.draft_ids,
                actor_user_id=current_user.id,
                cancel_event=cancel_event,
            ):
                yield sse_event(event.get("type", "message"), event)
        except Exception as exc:
            logger.exception("SSE execute streaming error for session %s", session_id)
            yield sse_event("error", {
                "message": str(exc),
                "error_type": type(exc).__name__,
                "phase": "execute",
                "traceback": _traceback.format_exc()[:2000] if get_settings().debug else None,
            })
        yield sse_event("done", {})
        _cancellation_manager.clear(session_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/cancel")
async def cancel_execution(
    session_id: int,
    current_user: User = Depends(require_demo_user),
) -> dict:
    """Cancel the in-progress execution for a planning session."""
    cancel_event = _cancellation_manager.get(session_id)
    if cancel_event is not None:
        cancel_event.set()
        _cancellation_manager.clear(session_id)
        return {"status": "cancelled"}
    return {"status": "no_active_execution"}


@router.get("/sessions/{session_id}/events")
async def get_session_events(
    session_id: int,
    after_seq: int = 0,
    current_user: User = Depends(require_demo_user),
) -> list[dict]:
    """Retrieve SSE event logs for a planning session.

    Used by the frontend to replay missed events after a page refresh.
    Returns events with ``seq > after_seq``, ordered by creation time.
    """
    from sqlalchemy import select
    from app.models.ai_planning_event_log import AIPlanningEventLog

    session_factory = get_session_factory()
    with session_factory() as db:
        # Verify session exists and user has access.
        from app.services.ai_planning import _get_session
        _get_session(db, session_id, actor_user_id=current_user.id)

        # Use created_at for ordering (seq is per-stream, not global).
        query = (
            select(AIPlanningEventLog)
            .where(AIPlanningEventLog.session_id == session_id)
            .order_by(AIPlanningEventLog.created_at.asc(), AIPlanningEventLog.seq.asc())
            .limit(500)
        )
        if after_seq > 0:
            # For backward compatibility, still support after_seq filtering.
            query = query.where(AIPlanningEventLog.seq > after_seq)

        events = db.scalars(query).all()

        return [
            {
                "seq": e.seq,
                "event_type": e.event_type,
                "event_data": e.event_data,
                "message_id": e.message_id,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ]


@router.post("/test/locator")
async def test_locator(
    url: str = "https://automationexercise.com/",
    target: str = 'link="Signup / Login"',
) -> dict:
    """Test locator resolution with a11y tree."""
    from playwright.sync_api import sync_playwright
    from app.ai.page_explorer import collect_a11y_nodes, format_a11y_nodes_for_prompt
    from app.locators import resolve_with_fallback

    result = {
        "url": url,
        "target": target,
        "a11y_nodes_count": 0,
        "sample_nodes": [],
        "locator_result": None,
        "error": None,
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            # Navigate
            page.goto(url, timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=30000)

            # Collect a11y nodes
            nodes = collect_a11y_nodes(page, page_state="S0")
            result["a11y_nodes_count"] = len(nodes)

            # Sample nodes
            interactive_nodes = [n for n in nodes if n.get("focusable")]
            result["sample_nodes"] = [
                {"role": n["role"], "name": n["name"], "id": n["node_id"]}
                for n in interactive_nodes[:10]
            ]

            # Test locator
            try:
                resolved = resolve_with_fallback(
                    page,
                    target,
                    require_visible=True,
                )
                result["locator_result"] = {
                    "success": True,
                    "strategy": resolved.strategy,
                    "locator_found": True,
                }
            except Exception as e:
                result["locator_result"] = {
                    "success": False,
                    "error_type": type(e).__name__,
                    "error_message": str(e)[:200],
                }

            browser.close()

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"

    return result
