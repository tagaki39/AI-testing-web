"""Services for AI planning sessions and drafts."""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlunparse, urlparse, parse_qs, urlencode

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.structured_logging import get_structured_logger
from sqlalchemy.orm import Session

from app.ai.test_planning_agent import REQUIRED_REQUIREMENT_SLOTS, run_planning_turn, stream_planning_turn
from app.models import AIPlanningDraft, AIPlanningMessage, AIPlanningSession, DslGenerationRun, Project, SessionProject, TestCase
from app.models.ai_planning_tool_result import AIPlanningToolResult
from app.schemas.ai_planning import (
    AIPlanningDraft as AIPlanningDraftSchema,
    AIPlanningMessage as AIPlanningMessageSchema,
    AIPlanningRequirements,
    AIPlanningSession as AIPlanningSessionSchema,
    AIPlanningSessionDetail,
    AIPlanningSessionSummary,
    AIPlanningToolCall,
    AIPlanningTurnResponse,
    CreateAIPlanningSessionRequest,
    ExecutionSummaryResult,
    GenerateAIPlanningDraftsRequest,
    LinkProjectRequest,
    ProjectSummaryInSession,
    SavedCaseResult,
    UpdateAIPlanningDraftStatusRequest,
)
from app.schemas.cases import CaseCreateRequest
from app.schemas.dsl import GenerateDslRequest
from app.schemas.executions import CaseExecutionRequest
from app.services.cases import EntityNotFoundError, _ensure_project_member, create_case
from app.services.dsl import generate_dsl_case
from app.services.executions import execute_case, execute_case_streaming


logger = logging.getLogger(__name__)
slog = get_structured_logger(__name__)


class AIPlanningAccessError(ValueError):
    """Raised when a planning session or draft is inaccessible."""


def list_planning_sessions(
    session: Session,
    *,
    actor_user_id: int,
) -> list[AIPlanningSessionSummary]:
    q = session.query(AIPlanningSession).filter(AIPlanningSession.actor_user_id == actor_user_id)
    q = q.order_by(AIPlanningSession.updated_at.desc())
    rows = q.all()
    return [
        AIPlanningSessionSummary(
            id=r.id,
            title=r.title or (r.requirements_json or {}).get("app_under_test"),
            status=r.status,
            created_at=r.created_at,
            updated_at=r.updated_at,
            projects=[
                ProjectSummaryInSession(id=p.id, name=p.name, description=p.description)
                for p in (r.projects or [])
            ],
        )
        for r in rows
    ]


def create_planning_session(
    session: Session,
    payload: CreateAIPlanningSessionRequest,
    *,
    actor_user_id: int,
) -> AIPlanningSessionDetail:
    record = AIPlanningSession(
        actor_user_id=actor_user_id,
        case_id=payload.case_id,
        status="collecting",
        requirements_json=AIPlanningRequirements().model_dump(mode="json"),
        missing_slots_json=list(REQUIRED_REQUIREMENT_SLOTS),
    )
    session.add(record)
    session.flush()

    # Stage 1: auto-create default project when none provided
    if payload.project_id is None:
        default_project = Project(
            name=f"default-{record.id}",
            description="auto-created temporary project",
            is_default=True,
        )
        session.add(default_project)
        session.flush()
        sp = SessionProject(
            session_id=record.id,
            project_id=default_project.id,
        )
        session.add(sp)
    else:
        sp = SessionProject(
            session_id=record.id,
            project_id=payload.project_id,
        )
        session.add(sp)

    session.commit()
    session.refresh(record)
    return get_planning_session_detail(session, record.id, actor_user_id=actor_user_id)


def get_planning_session_detail(session: Session, planning_session_id: int, *, actor_user_id: int) -> AIPlanningSessionDetail:
    planning_session = _get_session(session, planning_session_id, actor_user_id=actor_user_id)
    messages = session.scalars(
        select(AIPlanningMessage).where(AIPlanningMessage.session_id == planning_session_id).order_by(AIPlanningMessage.id.asc())
    ).all()
    drafts = session.scalars(
        select(AIPlanningDraft).where(AIPlanningDraft.session_id == planning_session_id).order_by(AIPlanningDraft.id.asc())
    ).all()
    return AIPlanningSessionDetail(
        session=_to_session_schema(planning_session),
        messages=[_to_message_schema(item) for item in messages],
        drafts=[_to_draft_schema(item) for item in drafts],
    )


def send_planning_message(
    session: Session,
    planning_session_id: int,
    *,
    actor_user_id: int,
    content: str,
) -> AIPlanningTurnResponse:
    planning_session = _get_session(session, planning_session_id, actor_user_id=actor_user_id)
    project_ids = _get_session_project_ids(planning_session)
    session.add(
        AIPlanningMessage(
            session_id=planning_session.id,
            role="user",
            turn_type="user",
            content=content,
            structured_payload_json=None,
        )
    )
    session.flush()

    transcript_records = session.scalars(
        select(AIPlanningMessage).where(AIPlanningMessage.session_id == planning_session.id).order_by(AIPlanningMessage.id.asc())
    ).all()
    base_transcript = [{"role": item.role, "content": item.content} for item in transcript_records if item.turn_type != "tool_call"]
    base_transcript = _inject_auto_context(base_transcript, planning_session, session, len(transcript_records))
    agent_response = run_planning_turn(
        transcript=base_transcript,
        existing_requirements=AIPlanningRequirements.model_validate(planning_session.requirements_json or {}),
        db_session=session,
        project_id=project_ids[0] if project_ids else 0,
        actor_user_id=actor_user_id,
        planning_session_id=planning_session.id,
    )

    planning_session.status = agent_response.session_status
    planning_session.requirements_json = agent_response.requirements.model_dump(mode="json")
    if agent_response.plan is not None:
        plan_dict = agent_response.plan.model_dump(mode="json")
        from app.ai.test_planning_agent import _extract_raw_page_results
        plan_dict["_page_results"] = _extract_raw_page_results(agent_response.tool_calls)
        planning_session.plan_json = plan_dict
    planning_session.missing_slots_json = agent_response.missing_slots
    planning_session.title = planning_session.title or agent_response.requirements.business_goal or "AI 测试规划"
    planning_session.last_error_message = (
        agent_response.assistant_message if agent_response.session_status == "error" else None
    )

    for tool_call in agent_response.tool_calls:
        session.add(
            AIPlanningMessage(
                session_id=planning_session.id,
                role="assistant",
                turn_type="tool_call",
                content=f"调用工具 {tool_call.tool}",
                structured_payload_json={
                    "type": "tool_call",
                    **tool_call.model_dump(mode="json"),
                },
            )
        )

    turn_type = "system_error" if agent_response.session_status == "error" else ("plan" if agent_response.plan is not None else "followup")
    session.add(
        AIPlanningMessage(
            session_id=planning_session.id,
            role="assistant",
            turn_type=turn_type,
            content=agent_response.assistant_message,
            structured_payload_json={
                "missing_slots": agent_response.missing_slots,
                "suggested_questions": agent_response.suggested_questions,
                "plan": agent_response.plan.model_dump(mode="json") if agent_response.plan is not None else None,
                "tool_calls": [item.model_dump(mode="json") for item in agent_response.tool_calls],
                "todo_list": [item.model_dump(mode="json") for item in agent_response.todo_list],
            },
        )
    )
    session.commit()
    session.refresh(planning_session)
    return agent_response


def _flush_streaming_msg_to_db(
    session: Session,
    message_id: int,
    content: str,
    *,
    phase: str | None = None,
    phase_message: str | None = None,
) -> None:
    """Incrementally persist accumulated streaming text + status to the stub message."""
    try:
        if not session.is_active:
            session.rollback()
        msg = session.merge(session.get(AIPlanningMessage, message_id))
        msg.content = content
        # Also persist the current phase so a refresh shows meaningful status.
        payload = msg.structured_payload_json or {}
        payload["_streaming"] = True
        if phase is not None:
            payload["_phase"] = phase
        if phase_message is not None:
            payload["_phaseMessage"] = phase_message
        msg.structured_payload_json = payload
        session.commit()
    except Exception:
        logger.warning("Failed to flush streaming message %d, skipping", message_id, exc_info=True)
        if session.is_active:
            session.rollback()


_STREAMING_FLUSH_INTERVAL = 3  # flush to DB every N text_chunks (reduced from 5)


def stream_planning_message(
    session: Session,
    planning_session_id: int,
    *,
    actor_user_id: int,
    content: str,
    session_factory=None,
):
    """Generator: save user msg, stream AI turn, save AI msg, yield events."""
    from typing import Generator

    start_time = time.monotonic()
    logger.info("[session:%d] Planning message stream start, content_len=%d", planning_session_id, len(content))

    planning_session = _get_session(session, planning_session_id, actor_user_id=actor_user_id)
    project_ids = _get_session_project_ids(planning_session)
    session.add(
        AIPlanningMessage(
            session_id=planning_session.id,
            role="user",
            turn_type="user",
            content=content,
            structured_payload_json=None,
        )
    )
    session.flush()
    session.commit()

    transcript_records = session.scalars(
        select(AIPlanningMessage).where(AIPlanningMessage.session_id == planning_session.id).order_by(AIPlanningMessage.id.asc())
    ).all()

    base_transcript = [{"role": item.role, "content": item.content} for item in transcript_records if item.turn_type != "tool_call"]
    base_transcript = _inject_auto_context(base_transcript, planning_session, session, len(transcript_records))
    stream = stream_planning_turn(
        transcript=base_transcript,
        existing_requirements=AIPlanningRequirements.model_validate(planning_session.requirements_json or {}),
        db_session=session,
        project_id=project_ids[0] if project_ids else 0,
        actor_user_id=actor_user_id,
        planning_session_id=planning_session.id,
    )

    # Create a stub assistant message so that a page refresh mid-stream shows partial content.
    streaming_msg = AIPlanningMessage(
        session_id=planning_session.id,
        role="assistant",
        turn_type="streaming",
        content="",
        structured_payload_json={"_streaming": True},
    )
    session.add(streaming_msg)
    session.flush()
    session.commit()
    streaming_msg_id = streaming_msg.id

    # Event log writer — NO DB query on init, writes inline during streaming.
    from app.services.sse_event_log import EventLogWriter
    event_log = EventLogWriter(
        session_factory=session_factory,
        session_id=planning_session_id,
        message_id=streaming_msg_id,
        flush_interval=_STREAMING_FLUSH_INTERVAL,
    )

    text_buffer = ""
    chunks_since_flush = 0
    current_phase = None
    current_phase_message = None

    response = None
    while True:
        try:
            event = next(stream)
            # Inline persist — if table missing, this is a silent no-op.
            event_log.write(event.get("type", "unknown"), event)

            if event.get("type") == "text_chunk" and not event.get("thinking"):
                text_buffer += event.get("text", "")
                chunks_since_flush += 1
                if chunks_since_flush >= _STREAMING_FLUSH_INTERVAL:
                    _flush_streaming_msg_to_db(session, streaming_msg_id, text_buffer,
                                               phase=current_phase, phase_message=current_phase_message)
                    chunks_since_flush = 0
            elif event.get("type") == "status":
                current_phase = event.get("phase")
                current_phase_message = event.get("message")
                # Status events also trigger a flush so the stub shows the latest phase.
                _flush_streaming_msg_to_db(session, streaming_msg_id, text_buffer,
                                           phase=current_phase, phase_message=current_phase_message)
            yield event
        except StopIteration as stop:
            response = stop.value
            break

    # Flush any remaining buffered events.
    event_log.flush()

    # Tool calls may have left the session in PendingRollbackError (e.g. UniqueViolation).
    # Recover so we can persist the AI response.
    if not session.is_active:
        logger.warning("[session:%d] Session became inactive after tool calls, rolling back to recover", planning_session_id)
        session.rollback()

    planning_session = _get_session(session, planning_session_id, actor_user_id=actor_user_id)
    planning_session.status = response.session_status
    planning_session.requirements_json = response.requirements.model_dump(mode="json")
    if response.plan is not None:
        plan_dict = response.plan.model_dump(mode="json")
        from app.ai.test_planning_agent import _extract_raw_page_results
        plan_dict["_page_results"] = _extract_raw_page_results(response.tool_calls)
        planning_session.plan_json = plan_dict
    planning_session.missing_slots_json = response.missing_slots
    planning_session.title = planning_session.title or response.requirements.business_goal or "AI 测试规划"
    planning_session.last_error_message = (
        response.assistant_message if response.session_status == "error" else None
    )

    for tool_call in response.tool_calls:
        tool_dict = tool_call.model_dump(mode="json")
        tool_dict.pop("result", None)  # exclude raw result from message payload
        msg = AIPlanningMessage(
            session_id=planning_session.id,
            role="assistant",
            turn_type="tool_call",
            content=f"调用工具 {tool_call.tool}",
            structured_payload_json={
                "type": "tool_call",
                **tool_dict,
                "result_summary": getattr(tool_call, "_compressed_result", None),
            },
        )
        session.add(msg)
        session.flush()  # get message.id

        # Persist raw + summary for heavy tools
        compressed = getattr(tool_call, "_compressed_result", None)
        logger.info(
            "[persist_tool_result] tool=%s, has_compressed=%s, result_type=%s, result_is_dict=%s",
            tool_call.tool,
            compressed is not None,
            type(tool_call.result).__name__,
            isinstance(tool_call.result, dict),
        )
        if compressed is not None:
            raw_json = tool_call.result if isinstance(tool_call.result, dict) else None
            logger.info(
                "[persist_tool_result] Saving to AIPlanningToolResult: tool=%s, raw_json_keys=%s",
                tool_call.tool,
                list(raw_json.keys()) if raw_json else None,
            )
            session.add(AIPlanningToolResult(
                session_id=planning_session.id,
                message_id=msg.id,
                tool_name=tool_call.tool,
                raw_result_json=raw_json,
                summary_json=compressed,
            ))

    # Update the streaming stub in-place to become the final assistant message.
    turn_type = "system_error" if response.session_status == "error" else ("plan" if response.plan is not None else "followup")
    streaming_msg = session.merge(session.get(AIPlanningMessage, streaming_msg_id))
    streaming_msg.turn_type = turn_type
    streaming_msg.content = response.assistant_message
    streaming_msg.structured_payload_json = {
        "missing_slots": response.missing_slots,
        "suggested_questions": response.suggested_questions,
        "plan": response.plan.model_dump(mode="json") if response.plan is not None else None,
        "tool_calls": [
            {
                "tool": item.tool,
                "params": item.params,
                "result_summary": getattr(item, "_compressed_result", None),
            }
            for item in response.tool_calls
        ],
        "todo_list": [item.model_dump(mode="json") for item in response.todo_list],
    }
    session.commit()
    elapsed = time.monotonic() - start_time
    assistant_preview = (response.assistant_message or "")[:120]
    logger.info(
        "[session:%d] Planning message stream done, status=%s, tool_calls=%d, todo=%d, duration=%.2fs, assistant=%s",
        planning_session_id, response.session_status, len(response.tool_calls),
        len(response.todo_list), elapsed, assistant_preview,
    )
    return response


def _load_a11y_nodes_for_scenario(
    session: Session,
    planning_session_id: int,
    *,
    scenario: dict | None = None,
) -> list[dict] | None:
    """Load a11y_nodes from ALL explore results for this session.

    Aggregates pages across multiple explore_flow / explore_page calls,
    deduplicating by URL so that re-exploring the same page with different
    actions doesn't produce duplicate nodes.  Earlier calls that explored
    more pages are no longer silently discarded.
    """
    # Step 1: Query ALL explore results for this session
    result_records = list(session.scalars(
        select(AIPlanningToolResult)
        .where(AIPlanningToolResult.session_id == planning_session_id)
        .where(AIPlanningToolResult.tool_name.in_(["explore_flow", "explore_page"]))
        .order_by(AIPlanningToolResult.id.asc())
    ).all())

    # Step 2: Check if any records exist
    if not result_records:
        logger.warning(
            "[_load_a11y_nodes] NO RECORD FOUND in AIPlanningToolResult for session %d. "
            "This means tool results were NOT persisted. Check stream_planning_turn logic.",
            planning_session_id,
        )
        all_results = session.scalars(
            select(AIPlanningToolResult)
            .where(AIPlanningToolResult.session_id == planning_session_id)
        ).all()
        logger.warning(
            "[_load_a11y_nodes] Total tool results for session %d: %d",
            planning_session_id,
            len(all_results),
        )
        if all_results:
            for r in all_results[:5]:
                logger.warning(
                    "[_load_a11y_nodes]   - id=%d, tool=%s, raw_type=%s",
                    r.id, r.tool_name, type(r.raw_result_json).__name__,
                )
        return None

    logger.info(
        "[_load_a11y_nodes] Found %d explore records for session %d",
        len(result_records), planning_session_id,
    )

    # Step 3: Aggregate pages from ALL records, deduplicating by URL
    # Key: normalized URL → best (most nodes) page data
    pages_by_url: dict[str, dict] = {}
    state_counter = 0

    for record in result_records:
        raw = record.raw_result_json
        if not isinstance(raw, dict):
            logger.warning(
                "[_load_a11y_nodes]   - id=%d tool=%s: raw_result_json is NOT a dict (type=%s), skipping",
                record.id, record.tool_name, type(raw).__name__,
            )
            continue

        # Extract pages from explore_flow result
        if "pages" in raw:
            for page in raw.get("pages", []):
                url = (page.get("url") or "").strip().rstrip("/").lower()
                if not url:
                    continue

                # Check if page has actions (new format)
                actions = page.get("actions", [])
                if actions:
                    # New format: page -> actions -> a11y_nodes
                    # Keep all actions with their nodes
                    existing = pages_by_url.get(url)
                    if existing is None:
                        pages_by_url[url] = {
                            "url": page.get("url"),
                            "page_state": f"S{state_counter}",
                            "description": page.get("description", ""),
                            "actions": actions,
                        }
                        state_counter += 1
                        logger.info(
                            "[_load_a11y_nodes]   - id=%d: page url=%s, actions=%d (new)",
                            record.id, url, len(actions),
                        )
                    else:
                        # Merge actions from different records
                        existing_actions = existing.get("actions", [])
                        existing_actions.extend(actions)
                        logger.info(
                            "[_load_a11y_nodes]   - id=%d: page url=%s, actions=%d (merged, total=%d)",
                            record.id, url, len(actions), len(existing_actions),
                        )
                else:
                    # Old format: page -> a11y_nodes
                    nodes = page.get("a11y_nodes", [])
                    if not nodes:
                        continue
                    existing = pages_by_url.get(url)
                    if existing is None or len(nodes) > len(existing.get("a11y_nodes", [])):
                        # Keep the version with more nodes
                        pages_by_url[url] = page
                        logger.info(
                            "[_load_a11y_nodes]   - id=%d: page url=%s, nodes=%d (new/better)",
                            record.id, url, len(nodes),
                        )
                    else:
                        logger.info(
                            "[_load_a11y_nodes]   - id=%d: page url=%s, nodes=%d (kept existing %d)",
                            record.id, url, len(nodes), len(existing.get("a11y_nodes", [])),
                        )

        # Extract a11y_nodes directly from explore_page result
        elif "a11y_nodes" in raw:
            nodes = raw.get("a11y_nodes") or []
            url = (raw.get("url") or "").strip().rstrip("/").lower()
            if url and nodes:
                existing = pages_by_url.get(url)
                if existing is None or len(nodes) > len(existing.get("a11y_nodes", [])):
                    pages_by_url[url] = {"url": raw.get("url"), "page_state": f"S{state_counter}", "a11y_nodes": nodes}
                    state_counter += 1

    if not pages_by_url:
        logger.warning("[_load_a11y_nodes] All explore records had empty pages/nodes!")
        return None

    # Step 4: Assign sequential page_states and flatten nodes
    all_nodes: list[dict] = []
    state_counter = 0
    for url in pages_by_url:
        page = pages_by_url[url]
        state = f"S{state_counter}"
        page["page_state"] = state
        state_counter += 1

        # Handle new format (page -> actions -> a11y_nodes)
        actions = page.get("actions", [])
        if actions:
            for action in actions:
                action_nodes = action.get("a11y_nodes", [])
                action_desc = action.get("action_description", "")
                logger.info(
                    "[_load_a11y_nodes] aggregated page: state=%s, url=%s, action=%s, nodes=%d",
                    state, page.get("url", "?"), action_desc, len(action_nodes),
                )
                for n in action_nodes:
                    n = dict(n)
                    n["page_state"] = state
                    n["action_description"] = action_desc
                    all_nodes.append(n)
        else:
            # Handle old format (page -> a11y_nodes)
            a11y_nodes = page.get("a11y_nodes", [])
            logger.info(
                "[_load_a11y_nodes] aggregated page: state=%s, url=%s, nodes=%d",
                state, page.get("url", "?"), len(a11y_nodes),
            )
            for n in a11y_nodes:
                n = dict(n)
                n["page_state"] = state
                all_nodes.append(n)

    logger.info(
        "[_load_a11y_nodes] total: %d pages, %d nodes from %d explore records",
        len(pages_by_url), len(all_nodes), len(result_records),
    )
    return all_nodes if all_nodes else None


def generate_planning_drafts(
    session: Session,
    planning_session_id: int,
    payload: GenerateAIPlanningDraftsRequest,
    *,
    actor_user_id: int,
) -> AIPlanningTurnResponse:
    planning_session = _get_session(session, planning_session_id, actor_user_id=actor_user_id)
    project_ids = _get_session_project_ids(planning_session)
    if not project_ids:
        raise ValueError("请先关联至少一个项目再生成 DSL 草稿。")
    plan = planning_session.plan_json or {}
    scenarios = {
        item["scenario_key"]: item
        for item in plan.get("scenarios", [])
        if isinstance(item, dict) and isinstance(item.get("scenario_key"), str)
    }
    drafts: list[AIPlanningDraftSchema] = []
    base_url = _normalize_base_url(planning_session.requirements_json or {})
    logger.info(
        "[generate_drafts] session=%d, requirements_json_keys=%s, entry_url_or_page=%s, base_url=%s",
        planning_session_id,
        list((planning_session.requirements_json or {}).keys()),
        (planning_session.requirements_json or {}).get("entry_url_or_page"),
        base_url,
    )
    invalid_scenarios: list[str] = []

    # Build user_context: original requirements summary for DSL generator
    _req = planning_session.requirements_json or {}
    _user_ctx_parts: list[str] = []
    if _req.get("app_under_test"):
        _user_ctx_parts.append(f"被测系统：{_req['app_under_test']}")
    if _req.get("business_goal"):
        _user_ctx_parts.append(f"业务目标：{_req['business_goal']}")
    if _req.get("core_user_flow"):
        _user_ctx_parts.append(f"核心流程：{_req['core_user_flow']}")
    if _req.get("main_assertions"):
        _user_ctx_parts.append(f"关键断言：{'; '.join(_req['main_assertions'])}")
    if _req.get("test_data_or_account"):
        _user_ctx_parts.append(f"测试数据：{_req['test_data_or_account']}")
    if _req.get("scope_limits"):
        _user_ctx_parts.append(f"范围限制：{_req['scope_limits']}")

    # 注入执行错误上下文
    error_context = _build_execution_error_context(session, planning_session)
    if error_context:
        _user_ctx_parts.append(error_context)

    user_context = "\n".join(_user_ctx_parts) if _user_ctx_parts else None
    if user_context:
        logger.info("[generate_drafts] user_context built, len=%d", len(user_context))

    for scenario_key in payload.scenario_keys:
        scenario = scenarios.get(scenario_key)
        if scenario is None:
            invalid_scenarios.append(scenario_key)
            record = AIPlanningDraft(
                session_id=planning_session.id,
                scenario_key=scenario_key,
                title=f"场景 {scenario_key} 不存在",
                status="failed",
                dsl_generation_id=None,
                dsl_case_json=None,
                warnings_json=[f"场景 '{scenario_key}' 未在 AI 生成的测试计划中找到"],
                normalization_notes_json=[],
                error_message=f"场景 '{scenario_key}' 不存在于当前测试计划中。",
            )
            session.add(record)
            session.flush()
            drafts.append(_to_draft_schema(record))
            continue

        existing = session.scalar(
            select(AIPlanningDraft).where(
                AIPlanningDraft.session_id == planning_session.id,
                AIPlanningDraft.scenario_key == scenario_key,
            )
        )
        # Self-healing: reuse successful drafts; regenerate failed ones with anti-pattern learning
        retry_reason_code: str | None = None
        if existing is not None:
            if existing.status == "generated":
                drafts.append(_to_draft_schema(existing))
                continue
            if existing.status in ("imported", "rejected"):
                drafts.append(_to_draft_schema(existing))
                continue
            # status == "failed": delete and regenerate with anti-patterns as few-shot
            prev_error = existing.error_message or ""
            if "缺少页面导航" in prev_error or "缺少导航" in prev_error:
                retry_reason_code = "missing_navigation"
            elif "缺少 input" in prev_error or "输入步骤" in prev_error:
                retry_reason_code = "missing_step"
            elif "capture_text" in prev_error:
                retry_reason_code = "missing_capture_text"
            else:
                retry_reason_code = "invalid_structure"
            logger.info(
                "Self-healing: deleting failed draft #%d for scenario '%s', retry=%s",
                existing.id, scenario_key, retry_reason_code,
            )
            session.delete(existing)
            session.flush()

        # Load a11y_nodes from the most recent explore result
        a11y_nodes_raw = _load_a11y_nodes_for_scenario(session, planning_session_id, scenario=scenario)
        logger.info(
            "[generate_drafts] scenario='%s', a11y_nodes_raw=%s, type=%s, len=%s",
            scenario_key,
            "None" if a11y_nodes_raw is None else "list",
            type(a11y_nodes_raw).__name__,
            len(a11y_nodes_raw) if a11y_nodes_raw else 0,
        )
        if not a11y_nodes_raw:
            logger.warning(
                "Skipping DSL generation for scenario '%s': no A11y elements collected",
                scenario_key,
            )
            record = AIPlanningDraft(
                session_id=planning_session.id,
                scenario_key=scenario_key,
                title=scenario["title"],
                status="failed",
                dsl_generation_id=None,
                dsl_case_json=None,
                warnings_json=[],
                normalization_notes_json=[],
                error_message="页面元素采集失败（探索超时或 URL 不可达），无法生成 DSL 草案。请检查入口 URL 或稍后重试。",
            )
            session.add(record)
            session.flush()
            drafts.append(_to_draft_schema(record))
            continue

        try:
            flow_steps = scenario.get("flow_steps", [])
            scenario_variables = scenario.get("variables", []) or []
            settings_local = get_settings()

            logger.info(
                "[session:%d] DSL generation for scenario '%s': flow_steps=%d, a11y_nodes=%d, has_page_elements=%s, flow_steps_enabled=%s, scenario_variables=%d",
                planning_session_id, scenario_key, len(flow_steps), len(a11y_nodes_raw),
                bool(scenario.get("page_elements")), settings_local.ai_planning_flow_steps_enabled,
                len(scenario_variables),
            )

            if flow_steps and settings_local.ai_planning_flow_steps_enabled:
                from app.ai.dsl_generator import generate_segmented_case_draft

                a11y_nodes_by_state: dict[str, list[dict]] = {}
                for n in a11y_nodes_raw:
                    ps = n.get("page_state", "S0") or "S0"
                    a11y_nodes_by_state.setdefault(ps, []).append(n)

                logger.info(
                    "[session:%d] Using segmented DSL generation: a11y_nodes_by_state=%s",
                    planning_session_id,
                    {k: len(v) for k, v in a11y_nodes_by_state.items()},
                )

                case_obj, gen_warnings, gen_notes, gen_meta = generate_segmented_case_draft(
                    payload=GenerateDslRequest(
                        prompt=scenario["draft_prompt"],
                        base_url=base_url,
                        actor_user_id=actor_user_id,
                        project_id=project_ids[0],
                        case_id=planning_session.case_id,
                        current_steps=payload.current_steps,
                        current_input_contract=payload.current_input_contract,
                        current_output_contract=payload.current_output_contract,
                        preserve_contracts=payload.preserve_contracts,
                        flow_steps=flow_steps,
                        scenario_variables=scenario_variables or None,
                        user_context=user_context,
                        retry_reason_code=retry_reason_code,
                    ),
                    flow_steps=flow_steps,
                    a11y_nodes_by_state=a11y_nodes_by_state,
                    scenario_variables=scenario_variables or None,
                    db_session=session,
                )
                # Wrap to match the existing interface
                generated = type("GeneratedHolder", (), {
                    "case": case_obj,
                    "warnings": gen_warnings,
                    "normalization_notes": gen_notes,
                    "generation_id": None,
                })()
            else:
                # No structured flow_steps from scenario. Pass a11y_nodes (grouped
                # by page_state) via payload so generate_dsl_case → segmented
                # generator still has element context.
                a11y_nodes_by_state: dict[str, list[dict]] = {}
                for n in a11y_nodes_raw:
                    ps = n.get("page_state", "S0") or "S0"
                    a11y_nodes_by_state.setdefault(ps, []).append(n)

                logger.info(
                    "[session:%d] Using single-segment DSL generation: a11y_nodes=%d, a11y_nodes_by_state=%s",
                    planning_session_id, len(a11y_nodes_raw), {k: len(v) for k, v in a11y_nodes_by_state.items()},
                )
                generated = generate_dsl_case(
                    session,
                    GenerateDslRequest(
                        prompt=scenario["draft_prompt"],
                        base_url=base_url,
                        actor_user_id=actor_user_id,
                        project_id=project_ids[0],
                        case_id=planning_session.case_id,
                        current_steps=payload.current_steps,
                        current_input_contract=payload.current_input_contract,
                        current_output_contract=payload.current_output_contract,
                        preserve_contracts=payload.preserve_contracts,
                        a11y_nodes_by_state=a11y_nodes_by_state or None,
                        scenario_variables=scenario_variables or None,
                        user_context=user_context,
                        retry_reason_code=retry_reason_code,
                    ),
                )
            # --- Locator preflight ---
            dsl_dict = generated.case.model_dump(mode="json")
            preflight_warnings: list[str] = []
            preflight_rejected = False

            if not a11y_nodes_raw:
                preflight_rejected = True
                raise ValueError(
                    "No page exploration data available for locator verification. "
                    "AI must call explore_page/explore_flow to collect page elements "
                    "before generating DSL. Currently no explored elements exist."
                )

            try:
                from app.ai.locator_preflight import apply_preflight_to_dsl
                dsl_dict = apply_preflight_to_dsl(dsl_dict, a11y_nodes_raw)
                pf = dsl_dict.pop("_preflight", {})
                preflight_warnings = pf.get("warnings", [])
                preflight_confidence = pf.get("locator_confidence", "unknown")
                step_results = pf.get("step_results", [])
                # --- Preflight gate: reject low-quality locators ---
                total_targets = len(step_results)
                unmatched = sum(1 for sr in step_results if sr.get("match_count", 0) == 0)
                low_conf = sum(1 for sr in step_results if sr.get("confidence") == "low")
                unmatched_ratio = unmatched / total_targets if total_targets > 0 else 0
                low_ratio = low_conf / total_targets if total_targets > 0 else 0

                if unmatched_ratio > 0.5:
                    preflight_rejected = True
                    unresolved_states: set[str] = set()
                    for sr in step_results:
                        if sr.get("match_count", 0) == 0 and sr.get("target"):
                            unresolved_states.add(sr["target"][:80])
                    rejection_msg = (
                        f"Preflight gate: {unmatched}/{total_targets} steps have targets "
                        f"not found in {len(a11y_nodes_raw)} explored elements.\n"
                        f"Missing: {', '.join(sorted(unresolved_states)[:5])}"
                    )
                    raise ValueError(rejection_msg)

                if low_ratio > 0.5 and unmatched_ratio < 0.5:
                    preflight_rejected = True
                    _low_suggestions: list[str] = []
                    for sr in step_results:
                        if sr.get("confidence") != "low":
                            continue
                        _t = sr.get("target", "")[:60]
                        _alts: list[str] = []
                        for me in sr.get("matched_elements", [])[:2]:
                            _text = (me.get("text") or "").strip()
                            if _text and _text not in _alts and f"'{_text}'" != _t[:len(_text)+2]:
                                _alts.append(f"'{_text}'")
                        _hint = f"  {_t} → 建议用 {', '.join(_alts)}" if _alts else f"  {_t}"
                        _low_suggestions.append(_hint)
                    rejection_msg = (
                        f"Preflight gate: {low_conf}/{total_targets} steps ({low_ratio*100:.0f}%) "
                        f"have low-confidence locators.\n"
                        f"请使用页面元素清单中的实际可见文本作为 target：\n"
                        + "\n".join(_low_suggestions[:8])
                    )
                    raise ValueError(rejection_msg)
                logger.info(
                    "Preflight for scenario '%s': confidence=%s, warnings=%d, elements=%d, unmatched=%d/%d",
                    scenario_key, preflight_confidence, len(preflight_warnings),
                    len(a11y_nodes_raw), unmatched, total_targets,
                )
            except Exception as exc:
                if preflight_rejected:
                    logger.warning("Preflight gate rejected scenario '%s': %s", scenario_key, exc)
                    raise
                logger.warning("Preflight failed for scenario '%s': %s", scenario_key, exc)

            all_warnings = list(generated.warnings) + preflight_warnings

            record = AIPlanningDraft(
                session_id=planning_session.id,
                scenario_key=scenario_key,
                title=scenario["title"],
                status="generated",
                dsl_generation_id=(
                    generated.generation_id if session.get(DslGenerationRun, generated.generation_id) is not None else None
                ),
                dsl_case_json=dsl_dict,
                warnings_json=all_warnings,
                normalization_notes_json=generated.normalization_notes,
                error_message=None,
            )
        except Exception as exc:
            logger.error(
                "Failed to generate DSL case for scenario '%s' in session %s",
                scenario_key,
                planning_session.id,
                exc_info=True,
            )
            record = AIPlanningDraft(
                session_id=planning_session.id,
                scenario_key=scenario_key,
                title=scenario["title"],
                status="failed",
                dsl_generation_id=None,
                dsl_case_json=None,
                warnings_json=[],
                normalization_notes_json=[],
                error_message=str(exc),
            )
            # --- Self-healing: record anti-pattern for failed draft ---
            try:
                from app.services.anti_patterns import (
                    record_anti_pattern,
                    MISSING_NAVIGATION, MISSING_STEP,
                    MISSING_CAPTURE_TEXT, MISSING_INPUT_BEFORE_ASSERT,
                )
                err_msg = str(exc)
                # Classify error message to anti-pattern category
                if "缺少页面导航" in err_msg or "缺少导航" in err_msg:
                    category = MISSING_NAVIGATION
                elif "缺少 input" in err_msg or "输入步骤" in err_msg:
                    category = MISSING_INPUT_BEFORE_ASSERT
                elif "capture_text" in err_msg and "assert" in err_msg:
                    category = MISSING_CAPTURE_TEXT
                else:
                    category = MISSING_STEP
                # Capture the wrong step snippet from the error context if available
                snippet: dict[str, Any] = {"error": err_msg[:500]}
                context_note = err_msg[:500] if len(err_msg) <= 500 else err_msg[:497] + "..."
                record_anti_pattern(
                    session,
                    error_category=category,
                    wrong_snippet=snippet,
                    context_note=context_note,
                    source="auto",
                    project_id=project_ids[0],
                )
            except Exception as ap_exc:
                logger.warning("Failed to record anti-pattern: %s", ap_exc)
        session.add(record)
        session.flush()
        drafts.append(_to_draft_schema(record))

    message = "已根据所选场景生成 DSL 草案。"
    failed_count = sum(1 for d in drafts if d.status == "failed")
    generated_count = sum(1 for d in drafts if d.status == "generated")
    first_error = next((d.error_message for d in drafts if d.error_message), None)
    if generated_count == 0 and failed_count > 0:
        message = f"所有 {failed_count} 个草案均生成失败。"
        if first_error:
            message += f"\n失败原因：{first_error}"
        message += "\n请检查入口 URL 是否可访问后重试。"
    elif failed_count > 0:
        message = f"已生成 {generated_count} 个 DSL 草案，{failed_count} 个失败。"
    if invalid_scenarios:
        message += f" 注意：以下场景不存在于当前测试计划中：{', '.join(invalid_scenarios)}"

    planning_session.status = "drafts_ready"
    session.add(
        AIPlanningMessage(
            session_id=planning_session.id,
            role="assistant",
            turn_type="plan",
            content=message,
            structured_payload_json={
                "type": "draft_generation_result",
                "drafts": [item.model_dump(mode="json") for item in drafts],
            },
        )
    )
    session.commit()
    session.refresh(planning_session)

    return AIPlanningTurnResponse(
        assistant_message=message,
        session_status="drafts_ready",
        requirements=AIPlanningRequirements.model_validate(planning_session.requirements_json or {}),
        missing_slots=planning_session.missing_slots_json or [],
        suggested_questions=[],
        plan=_to_session_schema(planning_session).plan,
        drafts=drafts,
        next_action="drafts_generated",
        tool_calls=[],
    )


def stream_generate_planning_drafts(
    session: Session,
    planning_session_id: int,
    payload: GenerateAIPlanningDraftsRequest,
    *,
    actor_user_id: int,
    session_factory=None,
):
    """Generator: yield draft_generating events, then delegate to generate_planning_drafts."""
    from app.services.sse_event_log import EventLogWriter
    event_log = EventLogWriter(
        session_factory=session_factory,
        session_id=planning_session_id,
        flush_interval=3,
    )

    logger.info(
        "[session:%d] Draft generation start, scenarios=%s",
        planning_session_id, payload.scenario_keys,
    )
    for scenario_key in payload.scenario_keys:
        logger.info("[session:%d] Generating draft for scenario '%s'", planning_session_id, scenario_key)
        event = {
            "type": "draft_generating",
            "scenario_key": scenario_key,
            "message": f"正在生成 {scenario_key} 的 DSL...",
        }
        event_log.write("draft_generating", event)
        yield event

    result = generate_planning_drafts(
        session,
        planning_session_id,
        payload,
        actor_user_id=actor_user_id,
    )
    complete_event = {
        "type": "turn_complete",
        "session_status": result.session_status,
        "payload": {
            "assistant_message": result.assistant_message,
            "drafts": [item.model_dump(mode="json") for item in result.drafts],
            "plan": result.plan.model_dump(mode="json") if result.plan else None,
        },
    }
    event_log.write("turn_complete", complete_event)
    event_log.flush()
    yield complete_event
    return result


def update_planning_draft_status(
    session: Session,
    draft_id: int,
    payload: UpdateAIPlanningDraftStatusRequest,
    *,
    actor_user_id: int,
) -> AIPlanningDraftSchema:
    draft = session.get(AIPlanningDraft, draft_id)
    if draft is None:
        raise EntityNotFoundError(f"AI planning draft {draft_id} not found.")
    _get_session(session, draft.session_id, actor_user_id=actor_user_id)
    draft.status = payload.status
    session.add(draft)
    session.commit()
    session.refresh(draft)
    return _to_draft_schema(draft)


def _should_run_analysis(
    execution_summaries: list[ExecutionSummaryResult],
) -> bool:
    """Return True if any execution result is not passed."""
    return any(s.status != "passed" for s in execution_summaries)


def _build_analysis_context(
    execution_summaries: list[ExecutionSummaryResult],
    db_session: Session,
) -> str:
    """Build a context message for the analysis turn from execution summaries."""
    lines = ["本轮执行已完成，请分析以下结果：\n"]
    for ex in execution_summaries:
        icon = "✅" if ex.status == "passed" else "❌"
        failure_info = ""
        if ex.status != "passed":
            failure_info = f" [失败步骤: {ex.failed_steps}步]"
        lines.append(
            f"{icon} {ex.case_name} — {ex.status} "
            f"({ex.passed_steps}/{ex.total_steps}步){failure_info}"
        )
    lines.append("\n请使用 analyze_results 模式输出分析报告。")
    return "\n".join(lines)


def _run_analysis_turn(
    *,
    execution_summaries: list[ExecutionSummaryResult],
    db_session: Session,
    project_id: int,
) -> AIPlanningTurnResponse | None:
    """Run an analysis turn using the AI agent with execution results as context."""
    try:
        context_message = _build_analysis_context(execution_summaries, db_session)
        transcript = [{"role": "user", "content": context_message}]
        response = run_planning_turn(
            transcript=transcript,
            existing_requirements=None,
            db_session=db_session,
            project_id=project_id,
        )
        # Auto-update insights after analysis
        _auto_update_insights(db_session, project_id, execution_summaries, response)
        return response
    except Exception:
        logger.warning("Auto-analysis turn failed", exc_info=True)
        return None


def _auto_update_insights(
    db_session: Session,
    project_id: int,
    execution_summaries: list[ExecutionSummaryResult],
    analysis_response: AIPlanningTurnResponse | None = None,
) -> None:
    """Auto-update TestPointInsight after analysis with flaky detection and risk assessment."""
    try:
        from sqlalchemy import select as sa_select
        from app.models import TestPointInsight, TestCase, TestCaseRun as Run

        insight = db_session.scalar(
            sa_select(TestPointInsight).where(TestPointInsight.project_id == project_id)
        )
        if insight is None:
            insight = TestPointInsight(project_id=project_id)
            db_session.add(insight)
            db_session.flush()

        # Detect flaky cases with improved scoring
        cases = db_session.scalars(
            sa_select(TestCase).where(TestCase.project_id == project_id)
        ).all()

        flaky_ids: list[int] = []
        pattern_data: dict[str, dict] = {}
        for case in cases:
            recent_runs = db_session.scalars(
                sa_select(Run)
                .where(Run.case_id == case.id)
                .order_by(Run.started_at.desc())
                .limit(6)
            ).all()

            if len(recent_runs) < 3:
                continue

            statuses = [r.status for r in recent_runs]
            pass_count = sum(1 for s in statuses if s == "passed")
            fail_count = sum(1 for s in statuses if s in ("failed", "needs_intervention"))

            if pass_count > 0 and fail_count > 0:
                switches = sum(1 for i in range(1, len(statuses)) if statuses[i] != statuses[i - 1])
                switch_ratio = switches / max(len(statuses) - 1, 1)
                balance = 1.0 - abs(pass_count - fail_count) / len(statuses)
                score = round(switch_ratio * balance, 2)
                if score >= 0.4:
                    flaky_ids.append(case.id)

            # Track failure categories
            consecutive_failures = 0
            for s in statuses:
                if s in ("failed", "needs_intervention"):
                    consecutive_failures += 1
                else:
                    break
            if consecutive_failures >= 2:
                error_msg = recent_runs[0].error_message or "unknown"
                category = _categorize_error(error_msg)
                if category not in pattern_data:
                    pattern_data[category] = {"count": 0, "cases": []}
                pattern_data[category]["count"] += consecutive_failures
                if case.id not in pattern_data[category]["cases"]:
                    pattern_data[category]["cases"].append(case.id)

        # Determine regression risk
        failed_count = sum(1 for s in execution_summaries if s.status != "passed")
        total_count = len(execution_summaries)
        if total_count > 0:
            fail_ratio = failed_count / total_count
            if fail_ratio >= 0.8:
                risk = "critical"
            elif fail_ratio >= 0.5:
                risk = "high"
            elif fail_ratio >= 0.3:
                risk = "medium"
            else:
                risk = "low"
        else:
            risk = "low"

        insight.flaky_case_ids = flaky_ids
        if pattern_data:
            insight.failure_patterns = pattern_data
        insight.regression_risk = risk

        if analysis_response and analysis_response.execution_analysis:
            summary = analysis_response.execution_analysis.suspected_root_cause or ""
            if summary:
                insight.last_analysis_summary = summary[:2000]

        db_session.flush()
    except Exception:
        logger.warning("Auto-update insights failed", exc_info=True)


def _categorize_error(error_message: str) -> str:
    """Categorize an error message into a failure pattern type."""
    msg = error_message.lower()
    if "locator" in msg or "not found" in msg or "no element" in msg:
        return "locator_stale"
    if "assertion" in msg or "expect" in msg or "mismatch" in msg:
        return "assertion_mismatch"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "network" in msg or "connection" in msg or "econnrefused" in msg:
        return "network_error"
    return "unknown"


def _build_session_context_preamble(
    planning_session: AIPlanningSession,
    db_session: Session,
    existing_msg_count: int,
) -> str | None:
    """Build an auto-context preamble with current project test status and cross-session insights.

    Returns None if injection is not needed (first turn or no project).
    """
    project_ids = _get_session_project_ids(planning_session)
    if not project_ids or existing_msg_count <= 1:
        return None

    from app.ai.planning_tools import _handle_get_project_test_status
    try:
        status = _handle_get_project_test_status(
            params={}, db_session=db_session, project_id=project_ids[0],
        )
    except Exception:
        logger.warning("Auto-context injection: failed to query project status", exc_info=True)
        return None

    conclusion_labels = {
        "all_passed": "全部通过", "partial": "部分通过",
        "all_failed": "全部失败", "no_runs": "无执行记录",
    }
    conclusion = status.get("conclusion", "unknown")
    if conclusion == "no_runs":
        return None

    lines = ["[系统自动注入 - 当前项目测试状态]"]
    lines.append(f"整体结论：{conclusion_labels.get(conclusion, conclusion)}")
    for case in status.get("cases", []):
        cs = case.get("latest_status", "unknown")
        if cs == "no_runs":
            continue
        icon = "✅" if cs == "passed" else "❌"
        p = case.get("passed_steps", 0)
        t = case.get("total_steps", 0)
        err = case.get("error_message", "")
        line = f"{icon} {case.get('case_name', '?')} — {cs} ({p}/{t}步)"
        if err:
            line += f" | 错误: {err}"
        lines.append(line)

    # Session-level test_context
    requirements = AIPlanningRequirements.model_validate(planning_session.requirements_json or {})
    tc = requirements.test_context
    if tc:
        if tc.get("suspected_root_cause"):
            lines.append(f"上次分析根因：{tc['suspected_root_cause']}")
        if tc.get("next_action"):
            lines.append(f"上次建议动作：{tc['next_action']}")
        if tc.get("regression_scope"):
            lines.append(f"上次回归范围：{tc['regression_scope']}")

    # Cross-session insights from TestPointInsight
    try:
        from app.ai.planning_tools import _handle_get_project_insights
        insights = _handle_get_project_insights(
            params={}, db_session=db_session, project_id=project_ids[0],
        )
        if insights.get("has_insights"):
            lines.append("")
            lines.append("[历史洞察 - 跨会话积累]")
            if insights.get("regression_risk"):
                lines.append(f"回归风险等级：{insights['regression_risk']}")
            if insights.get("flaky_case_ids"):
                lines.append(f"已知 Flaky 用例 ID：{', '.join(str(i) for i in insights['flaky_case_ids'])}")
            if insights.get("last_analysis_summary"):
                lines.append(f"上次分析摘要：{insights['last_analysis_summary']}")
            fp = insights.get("failure_patterns", {})
            if fp:
                for pattern_name, pattern_info in fp.items():
                    if isinstance(pattern_info, dict):
                        lines.append(f"失败模式 {pattern_name}：出现 {pattern_info.get('count', '?')} 次")
    except Exception:
        logger.warning("Auto-context injection: failed to load cross-session insights", exc_info=True)

    return "\n".join(lines)


def _build_tool_call_summary(
    db_session: Session,
    session_id: int,
    limit: int = 20,
) -> str | None:
    """从 DB 重建之前 turn 的工具调用摘要"""
    from app.models import AIPlanningMessage
    import json as _json

    tool_messages = db_session.scalars(
        select(AIPlanningMessage)
        .where(AIPlanningMessage.session_id == session_id)
        .where(AIPlanningMessage.turn_type == "tool_call")
        .order_by(AIPlanningMessage.id.desc())
        .limit(limit)
    ).all()

    if not tool_messages:
        return None

    summaries = []
    tool_names = []
    for msg in tool_messages:
        payload = msg.structured_payload_json or {}
        tool_name = payload.get("tool", "unknown")
        tool_names.append(tool_name)
        params = payload.get("params", {})
        result_summary = payload.get("result_summary")

        # 压缩显示
        params_str = _json.dumps(params, ensure_ascii=False)[:150]
        if result_summary:
            result_str = _json.dumps(result_summary, ensure_ascii=False)[:300]
        else:
            result_str = "无"
        summaries.append(f"- {tool_name}({params_str}) → {result_str}")

    # 记录结构化日志
    slog.tool_call(
        "tool_history_injection",
        message=f"Injecting {len(tool_messages)} tool call summaries",
        data={
            "session_id": session_id,
            "tool_count": len(tool_messages),
            "tool_names": list(set(tool_names)),
        },
        session_id=session_id,
    )

    return "[系统自动注入 - 之前的工具调用历史]\n\n" + "\n".join(summaries)


def _build_anti_pattern_context(
    db_session: Session,
    planning_session: AIPlanningSession,
) -> str | None:
    """从 DB 获取相关的 anti-patterns"""
    from app.services.anti_patterns import retrieve_relevant_anti_patterns, format_anti_patterns_for_prompt

    project_ids = _get_session_project_ids(planning_session)
    if not project_ids:
        return None

    try:
        patterns = retrieve_relevant_anti_patterns(
            db_session,
            project_id=project_ids[0],
            limit=3,
        )
    except Exception:
        logger.warning("Failed to retrieve anti-patterns for context injection", exc_info=True)
        return None

    if not patterns:
        return None

    # 记录结构化日志
    pattern_categories = [p.error_category for p in patterns]
    slog.ai_thinking(
        "anti_pattern_injection",
        message=f"Injecting {len(patterns)} anti-patterns",
        data={
            "session_id": planning_session.id,
            "project_id": project_ids[0],
            "pattern_count": len(patterns),
            "pattern_categories": pattern_categories,
        },
        session_id=planning_session.id,
    )

    return format_anti_patterns_for_prompt(patterns)


def _build_execution_error_context(
    db_session: Session,
    planning_session: AIPlanningSession,
) -> str | None:
    """从 DB 获取最近的执行错误

    当 case_id 存在时，从该用例的最近执行记录中查找。
    当 case_id 为 null 时，从项目的最近执行记录中查找。
    """
    from app.models import TestCaseRun, TestCase

    case_id = planning_session.case_id

    try:
        if case_id:
            # 从该用例的最近执行记录中查找
            latest_run = db_session.execute(
                select(TestCaseRun)
                .where(TestCaseRun.case_id == case_id)
                .order_by(TestCaseRun.id.desc())
                .limit(1)
            ).scalar_one_or_none()
        else:
            # 从项目的最近执行记录中查找
            project_ids = [p.id for p in planning_session.projects]
            if not project_ids:
                return None

            # 查找项目下最近的执行记录
            latest_run = db_session.execute(
                select(TestCaseRun)
                .join(TestCase, TestCaseRun.case_id == TestCase.id)
                .where(TestCase.project_id.in_(project_ids))
                .order_by(TestCaseRun.id.desc())
                .limit(1)
            ).scalar_one_or_none()
    except Exception:
        logger.warning("Failed to query execution errors for context injection", exc_info=True)
        return None

    if not latest_run or not latest_run.report:
        return None

    report = latest_run.report if isinstance(latest_run.report, dict) else {}
    steps = report.get("steps") or []

    errors = []
    error_actions = []
    for step in steps:
        if step.get("status") == "failed":
            action = step.get("action", "unknown")
            target = step.get("target", "unknown")
            error_msg = step.get("error_message", "未知")
            error_actions.append(action)
            errors.append(f"- {action} → {target}: {error_msg}")

    if not errors:
        return None

    # 记录结构化日志
    slog.dsl_execution(
        "execution_error_injection",
        message=f"Injecting {len(errors)} execution errors",
        data={
            "session_id": planning_session.id,
            "case_id": case_id,
            "error_count": len(errors),
            "error_actions": list(set(error_actions)),
        },
        execution_id=latest_run.id,
    )

    return "[系统自动注入 - 最近一次执行的错误]\n\n" + "\n".join(errors)


def _build_auto_context_preamble(
    planning_session: AIPlanningSession,
    db_session: Session,
    existing_msg_count: int,
) -> str | None:
    """Build auto-context preamble from various sources.

    Returns a single string containing all context sections, or None if no context.
    This function can be used by both the ReAct loop and the DSL generator.
    """
    preamble_parts = []
    injected_sections = []

    # 1. 项目测试状态和跨会话洞察（现有逻辑）
    session_context = _build_session_context_preamble(planning_session, db_session, existing_msg_count)
    if session_context:
        preamble_parts.append(session_context)
        injected_sections.append("session_context")

    # 2. 之前 turn 的工具调用摘要
    tool_summary = _build_tool_call_summary(db_session, planning_session.id)
    if tool_summary:
        preamble_parts.append(tool_summary)
        injected_sections.append("tool_call_history")

    # 3. Anti-patterns 上下文
    anti_pattern_context = _build_anti_pattern_context(db_session, planning_session)
    if anti_pattern_context:
        preamble_parts.append(anti_pattern_context)
        injected_sections.append("anti_patterns")

    # 4. 执行错误上下文
    error_context = _build_execution_error_context(db_session, planning_session)
    if error_context:
        preamble_parts.append(error_context)
        injected_sections.append("execution_errors")

    if not preamble_parts:
        return None

    # 记录结构化日志
    preamble = "\n\n---\n\n".join(preamble_parts)
    slog.ai_thinking(
        "context_injection",
        message=f"Built {len(injected_sections)} context sections: {', '.join(injected_sections)}",
        data={
            "session_id": planning_session.id,
            "injected_sections": injected_sections,
            "preamble_length": len(preamble),
        },
        session_id=planning_session.id,
    )

    return preamble


def _inject_auto_context(
    transcript: list[dict[str, str]],
    planning_session: AIPlanningSession,
    db_session: Session,
    existing_msg_count: int,
) -> list[dict[str, str]]:
    """Prepend auto-context preamble to transcript if applicable."""
    preamble = _build_auto_context_preamble(planning_session, db_session, existing_msg_count)
    if not preamble:
        return transcript

    return [{"role": "system", "content": preamble}, *transcript]


def _record_execution_anti_patterns(
    session: Session,
    case_id: int,
    scenario_key: str,
    project_id: int,
) -> None:
    """Analyze the latest execution of *case_id* and record failed steps as anti-patterns.

    Only processes the most recent execution run. Each failed step becomes an
    anti-pattern entry that the DSL generator can use as a few-shot negative example
    on the next retry — the AI sees what went wrong and self-corrects.
    """
    from sqlalchemy import desc
    from app.models import TestCaseRun
    from app.services.anti_patterns import (
        record_anti_pattern, TARGET_NOT_FOUND, MISSING_STEP, WRONG_PAGE_STATE,
    )

    latest_run = session.execute(
        select(TestCaseRun)
        .where(TestCaseRun.case_id == case_id)
        .order_by(desc(TestCaseRun.id))
        .limit(1)
    ).scalar_one_or_none()

    if not latest_run or not latest_run.report:
        return

    report = latest_run.report if isinstance(latest_run.report, dict) else {}
    steps = report.get("steps") or []
    if not steps:
        return

    for step in steps:
        if step.get("status") != "failed":
            continue
        action = step.get("action", "")
        target = step.get("target") or ""
        error_msg = step.get("error_message") or ""
        resolved_by = step.get("resolved_by") or "unknown"
        dom_text = ""
        dom_snap = step.get("dom_summary") or {}
        if isinstance(dom_snap, dict):
            dom_text = dom_snap.get("text_preview", "")[:200]

        # Classify the failure
        if action == "assert_text":
            # Extract expected vs actual from error message
            import re
            expected_match = re.search(r"to contain text '([^']*)'", error_msg)
            actual_match = re.search(r"unexpected value \"([^\"]*)\"", error_msg)
            expected_val = expected_match.group(1) if expected_match else ""
            actual_val = actual_match.group(1) if actual_match else ""
            context_note = (
                f"assert_text target='{target[:80]}' value='{expected_val[:50]}' 失败"
                f"——实际定位到的是 '{actual_val[:50]}'"
                f"（定位策略: {resolved_by}）。↓"
                f"可能原因: 1) target 文本在页面上匹配了错误元素"
                f" 2) 缺少来自无障碍树预检的 verified candidate"
                f" 3) 商品/列表场景应使用可预检的结构化候选，而不是裸文本 target"
            )
        elif action == "click":
            if "timeout" in error_msg.lower() or "not found" in error_msg.lower():
                context_note = (
                    f"click target='{target[:80]}' 失败——元素未找到或不可见。"
                    f"↓ 需要检查 target 是否与实际页面文本一致"
                    f"（DOM 片段: {dom_text[:100]}）"
                )
            else:
                context_note = f"click target='{target[:80]}' 失败: {error_msg[:200]}"
        elif action == "input":
            context_note = (
                f"input target='{target[:80]}' 失败: {error_msg[:200]}。"
                f"↓ 检查 target 是否匹配正确的输入框"
            )
        else:
            context_note = f"{action} target='{target[:80]}' 失败: {error_msg[:200]}"

        # Build the wrong snippet
        snippet: dict[str, Any] = {
            "action": action,
            "target": target,
            "value": step.get("value"),
            "resolved_by": resolved_by,
        }

        # Determine category
        if "assert_text" in action and actual_val and expected_val != actual_val:
            category = WRONG_PAGE_STATE  # assertion matched wrong element
        elif "timeout" in error_msg.lower() or "not found" in error_msg.lower():
            category = TARGET_NOT_FOUND
        else:
            category = MISSING_STEP

        record_anti_pattern(
            session,
            error_category=category,
            wrong_snippet=snippet,
            context_note=context_note,
            source="execution",
            project_id=project_id,
        )
        logger.info(
            "Execution anti-pattern recorded: case=%d step=%s target=%s category=%s",
            case_id, action, target[:60], category,
        )


def _build_input_values_from_session(
    requirements_json: dict[str, Any],
    dsl_case_jsons: list[dict[str, Any] | None],
) -> dict[str, str]:
    """Read input_values from contract defaults, with heuristic fallback for legacy cases.

    New DSL generator populates input_contract[].value directly from the user's
    test data, so this function primarily just reads those values.  The heuristic
    parsing of test_data_or_account text is kept as a fallback for old drafts.
    """
    result: dict[str, str] = {}

    # Primary: read values directly from contract defaults (new generator path)
    for case_json in dsl_case_jsons:
        if not case_json:
            continue
        for ic in case_json.get("input_contract", []) or []:
            key = (ic.get("context_key") or "").strip()
            val = ic.get("value")
            if key and val is not None and str(val).strip():
                result[key] = str(val).strip()

    if result:
        logger.info("[_build_input_values] Read %d values from contract defaults: %s",
                     len(result), {k: v[:10] for k, v in result.items()})
        return result

    # Legacy fallback: parse test_data_or_account text for old drafts
    import re
    raw = (requirements_json.get("test_data_or_account") or "").strip()
    if not raw:
        return result

    # Collect context_keys from contracts (for matching)
    context_keys: set[str] = set()
    for case_json in dsl_case_jsons:
        if not case_json:
            continue
        for ic in case_json.get("input_contract", []) or []:
            key = (ic.get("context_key") or "").strip()
            if key:
                context_keys.add(key)

    # Simple key:value pair extraction
    _CN_KEY_MAP: dict[str, list[str]] = {
        "账号": ["email", "username", "login"], "邮箱": ["email", "mail"],
        "用户名": ["username", "user", "login"], "密码": ["password", "pass", "pwd"],
        "口令": ["password", "pass", "pwd"],
    }
    pairs: dict[str, str] = {}
    for entry in re.split(r"[\n,，;；]+", raw):
        entry = re.sub(r'^\d+\.\s*', '', entry.strip())
        m = re.match(r"(.+?)[：:=]\s*(.+)", entry) if entry else None
        if m:
            pairs[m.group(1).strip()] = m.group(2).strip()
        elif entry and "@" in entry:
            pairs.setdefault("email", entry)

    for ck in context_keys:
        ck_lower = ck.lower()
        for label, value in pairs.items():
            label_lower = label.lower()
            if ck_lower in label_lower or label_lower in ck_lower:
                result[ck] = value
                break
            for cn_key, en_keys in _CN_KEY_MAP.items():
                if cn_key in label_lower and ck_lower in en_keys:
                    result[ck] = value
                    break
            else:
                continue
            break
        else:
            if "email" in ck_lower or "mail" in ck_lower:
                for v in pairs.values():
                    if "@" in v:
                        result[ck] = v
                        break
            elif "password" in ck_lower or "pass" in ck_lower or "pwd" in ck_lower:
                for v in pairs.values():
                    if "@" not in v and len(v) >= 4:
                        result[ck] = v
                        break

    if result:
        logger.info("[_build_input_values] Legacy fallback resolved: %s",
                     {k: v[:10] for k, v in result.items()})
    return result


def save_and_execute_selected_drafts(
    session: Session,
    planning_session_id: int,
    draft_ids: list[int],
    actor_user_id: int,
    execute: bool = True,
    input_values: dict[str, str] | None = None,
) -> AIPlanningTurnResponse:
    planning_session = _get_session(session, planning_session_id, actor_user_id=actor_user_id)
    project_ids = _get_session_project_ids(planning_session)
    if not project_ids:
        raise ValueError("请先关联至少一个项目再保存和执行用例。")

    # Ensure user is a member of all linked projects (fix for projects created before ProjectMember fix)
    ensure_project_member_for_session_projects(session, planning_session_id, actor_user_id)

    drafts = (
        session.query(AIPlanningDraft)
        .filter(
            AIPlanningDraft.session_id == planning_session_id,
            AIPlanningDraft.id.in_(draft_ids),
        )
        .all()
    )

    saved_cases: list[SavedCaseResult] = []
    for draft in drafts:
        if not draft.dsl_case_json:
            continue
        case_payload = CaseCreateRequest(
            project_id=project_ids[0],
            actor_user_id=actor_user_id,
            **draft.dsl_case_json,
        )
        case = create_case(session, case_payload, actor_user_id=actor_user_id)
        saved_cases.append(SavedCaseResult(case_id=case.id, case_name=case.name))
        draft.status = "imported"

    if not execute or not saved_cases:
        assistant_message = f"已保存 {len(saved_cases)} 个测试用例。" + ("\n是否立即执行？" if saved_cases else "")
        planning_session.status = "saving"
        session.add(
            AIPlanningMessage(
                session_id=planning_session.id,
                role="assistant",
                turn_type="followup",
                content=assistant_message,
                structured_payload_json={
                    "type": "save_result",
                    "saved_cases": [item.model_dump(mode="json") for item in saved_cases],
                },
            )
        )
        session.commit()
        session.refresh(planning_session)
        return AIPlanningTurnResponse(
            assistant_message=assistant_message,
            session_status="saving",
            requirements=AIPlanningRequirements.model_validate(planning_session.requirements_json or {}),
            missing_slots=[],
            plan=None,
            drafts=[],
            next_action="ask_followup",
            saved_cases=saved_cases,
        )

    # Auto-fill input_values from session test data if not provided by caller
    if not input_values:
        input_values = _build_input_values_from_session(
            planning_session.requirements_json or {},
            [d.dsl_case_json for d in drafts if d.dsl_case_json],
        )
        logger.info(
            "Auto-resolved input_values from session data: %s",
            {k: v[:3] + '***' for k, v in input_values.items()} if input_values else {},
        )

    execution_summaries: list[ExecutionSummaryResult] = []
    for saved in saved_cases:
        payload = CaseExecutionRequest(actor_user_id=actor_user_id, input_values=input_values or {})
        result = execute_case(session, saved.case_id, payload)
        passed = sum(1 for s in (result.report.steps or []) if s.status == "passed")
        failed = sum(1 for s in (result.report.steps or []) if s.status == "failed")
        execution_summaries.append(ExecutionSummaryResult(
            execution_id=result.id,
            case_id=saved.case_id,
            case_name=result.case_name,
            status=result.status,
            total_steps=result.total_steps,
            passed_steps=passed,
            failed_steps=failed,
            duration_ms=result.duration_ms,
            screenshot_url=result.latest_screenshot_url,
            report_url=f"/run/{result.id}",
        ))

    # --- Self-healing: record execution failures as anti-patterns ---
    for saved in saved_cases:
        draft = next((d for d in drafts if d.dsl_case_json and d.dsl_case_json.get("name") == saved.case_name), None)
        if draft is None:
            continue
        try:
            _record_execution_anti_patterns(
                session, saved.case_id, draft.scenario_key, project_ids[0],
            )
        except Exception as ap_exc:
            logger.warning("Execution anti-pattern recording failed: %s", ap_exc)

    lines = ["测试执行完成：\n"]
    for ex in execution_summaries:
        icon = "✅" if ex.status == "passed" else "❌"
        lines.append(f"{icon} {ex.case_name} — {ex.status} ({ex.passed_steps}/{ex.total_steps}步)")

    assistant_message = "\n".join(lines)
    planning_session.status = "completed"
    session.add(
        AIPlanningMessage(
            session_id=planning_session.id,
            role="assistant",
            turn_type="plan",
            content=assistant_message,
            structured_payload_json={
                "type": "execution_summary",
                "saved_cases": [item.model_dump(mode="json") for item in saved_cases],
                "execution_summaries": [item.model_dump(mode="json") for item in execution_summaries],
            },
        )
    )
    session.commit()
    session.refresh(planning_session)

    execution_analysis = None
    if _should_run_analysis(execution_summaries):
        analysis_response = _run_analysis_turn(
            execution_summaries=execution_summaries,
            db_session=session,
            project_id=project_ids[0],
        )
        if analysis_response and analysis_response.execution_analysis:
            execution_analysis = analysis_response.execution_analysis
            analysis_msg = analysis_response.assistant_message
            session.add(
                AIPlanningMessage(
                    session_id=planning_session.id,
                    role="assistant",
                    turn_type="followup",
                    content=analysis_msg,
                    structured_payload_json={
                        "type": "execution_analysis",
                        "analysis": execution_analysis.model_dump(mode="json"),
                    },
                )
            )
            session.commit()
            assistant_message = f"{assistant_message}\n\n---\n\n{analysis_msg}"

    return AIPlanningTurnResponse(
        assistant_message=assistant_message,
        session_status="completed",
        requirements=AIPlanningRequirements.model_validate(planning_session.requirements_json or {}),
        missing_slots=[],
        suggested_questions=[],
        plan=None,
        drafts=[],
        next_action="ask_followup",
        saved_cases=saved_cases,
        execution_summaries=execution_summaries,
        execution_analysis=execution_analysis,
    )


def save_and_execute_selected_drafts_streaming(
    session: Session,
    planning_session_id: int,
    draft_ids: list[int],
    actor_user_id: int,
    *,
    input_values: dict[str, str] | None = None,
    cancel_event=None,
    session_factory=None,
):
    """Generator version of save_and_execute_selected_drafts for WebSocket streaming.

    Yields progress event dicts. After all cases complete, persists the execution
    summary message and yields a ``done`` event.
    """
    from threading import Event as ThreadEvent
    from app.runners.playwright_runner import RunnerCancelledError
    from app.services.sse_event_log import EventLogWriter

    if cancel_event is None:
        cancel_event = ThreadEvent()

    start_time = time.monotonic()
    logger.info("[session:%d] Save-and-execute streaming start, draft_ids=%s", planning_session_id, draft_ids)

    # Event log writer — NO DB query on init, writes inline during streaming.
    event_log = EventLogWriter(
        session_factory=session_factory,
        session_id=planning_session_id,
        flush_interval=5,
    )

    planning_session = _get_session(session, planning_session_id, actor_user_id=actor_user_id)
    project_ids = _get_session_project_ids(planning_session)
    if not project_ids:
        raise ValueError("请先关联至少一个项目再保存和执行用例。")

    # Ensure user is a member of all linked projects (fix for projects created before ProjectMember fix)
    ensure_project_member_for_session_projects(session, planning_session_id, actor_user_id)

    drafts = (
        session.query(AIPlanningDraft)
        .filter(
            AIPlanningDraft.session_id == planning_session_id,
            AIPlanningDraft.id.in_(draft_ids),
        )
        .all()
    )

    saved_cases: list[SavedCaseResult] = []
    for draft in drafts:
        if cancel_event.is_set():
            raise RunnerCancelledError("Execution cancelled by user.", step_results=[])
        if not draft.dsl_case_json:
            continue
        case_payload = CaseCreateRequest(
            project_id=project_ids[0],
            actor_user_id=actor_user_id,
            **draft.dsl_case_json,
        )
        case = create_case(session, case_payload, actor_user_id=actor_user_id)
        saved_cases.append(SavedCaseResult(case_id=case.id, case_name=case.name))
        draft.status = "imported"
        logger.info("[session:%d] Saved case '%s' (id=%d)", planning_session_id, case.name, case.id)
        save_event = {
            "type": "save_progress",
            "saved_count": len(saved_cases),
            "total": len(drafts),
            "case_name": case.name,
        }
        event_log.log("save_progress", save_event)
        yield save_event

    if not saved_cases:
        planning_session.status = "saving"
        session.commit()
        # Check if any drafts failed (e.g. due to exploration failure)
        failed_errors: list[str] = []
        for d in drafts:
            if d.error_message and d.error_message not in failed_errors:
                failed_errors.append(d.error_message)
        detail = "; ".join(failed_errors[:2]) if failed_errors else "所有选中草案均无有效 DSL"
        error_event = {
            "type": "error",
            "message": f"没有可保存的测试用例。{detail}",
            "error_type": "no_saved_cases",
            "phase": "execute",
        }
        event_log.log("error", error_event)
        event_log.flush()
        yield error_event
        return

    execution_summaries: list[ExecutionSummaryResult] = []
    for saved in saved_cases:
        if cancel_event.is_set():
            raise RunnerCancelledError("Execution cancelled by user.", step_results=[])

        payload = CaseExecutionRequest(actor_user_id=actor_user_id, input_values=input_values or {})
        dsl_case = None
        case_record = session.query(TestCase).get(saved.case_id)
        if case_record:
            from app.schemas.dsl import DSLCase
            dsl_case = DSLCase.model_validate(case_record.dsl)

        case_start_event = {
            "type": "case_start",
            "case_id": saved.case_id,
            "case_name": saved.case_name,
            "total_steps": len(dsl_case.steps) if dsl_case else 0,
        }
        event_log.log("case_start", case_start_event)
        yield case_start_event

        case_start_time = time.monotonic()
        logger.info("[session:%d] Executing case '%s' (id=%d), steps=%d", planning_session_id, saved.case_name, saved.case_id, len(dsl_case.steps) if dsl_case else 0)

        try:
            stream = execute_case_streaming(
                session, saved.case_id, payload, cancel_event=cancel_event,
            )
            detail = None
            try:
                while True:
                    step_event = next(stream)
                    step_dict = {
                        "type": step_event.type,
                        "case_id": saved.case_id,
                        "step_index": step_event.step_index,
                        "action": step_event.action,
                        **({"target": step_event.target} if step_event.target is not None else {}),
                        **({"value": step_event.value} if step_event.value is not None else {}),
                        **({"status": step_event.status} if step_event.status is not None else {}),
                        **({"duration_ms": step_event.duration_ms} if step_event.duration_ms is not None else {}),
                    }
                    event_log.log(step_event.type, step_dict)
                    yield step_dict
            except StopIteration as stop:
                detail = stop.value

            if detail is not None:
                passed = sum(1 for s in (detail.report.steps or []) if s.status == "passed")
                failed = sum(1 for s in (detail.report.steps or []) if s.status == "failed")
                case_elapsed = time.monotonic() - case_start_time
                logger.info(
                    "[session:%d] Case '%s' done, status=%s, passed=%d, failed=%d, duration=%.2fs",
                    planning_session_id, detail.case_name, detail.status, passed, failed, case_elapsed,
                )
                execution_summaries.append(ExecutionSummaryResult(
                    execution_id=detail.id,
                    case_id=saved.case_id,
                    case_name=detail.case_name,
                    status=detail.status,
                    total_steps=detail.total_steps,
                    passed_steps=passed,
                    failed_steps=failed,
                    duration_ms=detail.duration_ms,
                    screenshot_url=detail.latest_screenshot_url,
                    report_url=f"/run/{detail.id}",
                ))
        except RunnerCancelledError:
            raise

    # Persist execution summary message
    lines = ["测试执行完成：\n"]
    for ex in execution_summaries:
        icon = "✅" if ex.status == "passed" else "❌"
        lines.append(f"{icon} {ex.case_name} — {ex.status} ({ex.passed_steps}/{ex.total_steps}步)")

    assistant_message = "\n".join(lines)
    planning_session.status = "completed"
    session.add(
        AIPlanningMessage(
            session_id=planning_session.id,
            role="assistant",
            turn_type="plan",
            content=assistant_message,
            structured_payload_json={
                "type": "execution_summary",
                "saved_cases": [item.model_dump(mode="json") for item in saved_cases],
                "execution_summaries": [item.model_dump(mode="json") for item in execution_summaries],
            },
        )
    )
    session.commit()

    if _should_run_analysis(execution_summaries):
        status_event = {"type": "status", "phase": "analyzing", "message": "正在分析执行结果..."}
        event_log.log("status", status_event)
        yield status_event
        analysis_response = _run_analysis_turn(
            execution_summaries=execution_summaries,
            db_session=session,
            project_id=project_ids[0],
        )
        if analysis_response and analysis_response.execution_analysis:
            analysis_msg = analysis_response.assistant_message
            session.add(
                AIPlanningMessage(
                    session_id=planning_session.id,
                    role="assistant",
                    turn_type="followup",
                    content=analysis_msg,
                    structured_payload_json={
                        "type": "execution_analysis",
                        "analysis": analysis_response.execution_analysis.model_dump(mode="json"),
                    },
                )
            )
            session.commit()
            analysis_event = {
                "type": "analysis_complete",
                "analysis": analysis_response.execution_analysis.model_dump(mode="json"),
                "message": analysis_msg,
            }
            event_log.log("analysis_complete", analysis_event)
            yield analysis_event

    elapsed_total = time.monotonic() - start_time
    logger.info(
        "[session:%d] Save-and-execute streaming done, cases=%d, duration=%.2fs",
        planning_session_id, len(saved_cases), elapsed_total,
    )
    done_event = {"type": "done"}
    event_log.log("done", done_event)
    event_log.flush()
    yield done_event


def retest_cases(
    session: Session,
    planning_session_id: int,
    *,
    actor_user_id: int,
    case_ids: list[int] | None = None,
    failed_only: bool = False,
    input_values: dict[str, str] | None = None,
) -> AIPlanningTurnResponse:
    """Re-execute existing test cases from a planning session and run auto-analysis."""
    planning_session = _get_session(session, planning_session_id, actor_user_id=actor_user_id)
    project_ids = _get_session_project_ids(planning_session)

    if not case_ids and failed_only:
        from app.ai.planning_tools import _handle_get_recommended_retest
        recommendation = _handle_get_recommended_retest(
            params={}, db_session=session, project_id=project_ids[0] if project_ids else 0,
        )
        case_ids = recommendation.get("retest_case_ids", [])
        if not case_ids:
            return AIPlanningTurnResponse(
                assistant_message="当前没有需要复测的失败用例。",
                session_status="completed",
                requirements=AIPlanningRequirements.model_validate(planning_session.requirements_json or {}),
                missing_slots=[],
                suggested_questions=[],
                plan=None,
                drafts=[],
                next_action="ask_followup",
                saved_cases=[],
                execution_summaries=[],
            )
    elif not case_ids:
        return AIPlanningTurnResponse(
            assistant_message="请指定要复测的用例 ID 或使用 failed_only=true。",
            session_status="completed",
            requirements=AIPlanningRequirements.model_validate(planning_session.requirements_json or {}),
            missing_slots=[],
            suggested_questions=[],
            plan=None,
            drafts=[],
            next_action="ask_followup",
            saved_cases=[],
            execution_summaries=[],
        )

    # Auto-fill input_values from session test data if not provided
    if not input_values:
        # Collect dsl_case_json from all cases being retested
        cases_json: list[dict[str, Any] | None] = []
        for cid in case_ids:
            cr = session.get(TestCase, cid)
            cases_json.append(cr.dsl if cr else None)
        input_values = _build_input_values_from_session(
            planning_session.requirements_json or {}, cases_json,
        )
        logger.info(
            "Retest auto-resolved input_values: %s",
            {k: v[:3] + '***' for k, v in input_values.items()} if input_values else {},
        )

    execution_summaries: list[ExecutionSummaryResult] = []
    for case_id in case_ids:
        case_record = session.get(TestCase, case_id)
        if case_record is None or (project_ids and case_record.project_id != project_ids[0]):
            continue
        payload = CaseExecutionRequest(actor_user_id=actor_user_id, input_values=input_values or {})
        result = execute_case(session, case_id, payload)
        passed = sum(1 for s in (result.report.steps or []) if s.status == "passed")
        failed = sum(1 for s in (result.report.steps or []) if s.status == "failed")
        execution_summaries.append(ExecutionSummaryResult(
            execution_id=result.id,
            case_id=case_id,
            case_name=result.case_name,
            status=result.status,
            total_steps=result.total_steps,
            passed_steps=passed,
            failed_steps=failed,
            duration_ms=result.duration_ms,
            screenshot_url=result.latest_screenshot_url,
            report_url=f"/run/{result.id}",
        ))

    lines = [f"复测完成（{len(execution_summaries)} 个用例）：\n"]
    for ex in execution_summaries:
        icon = "✅" if ex.status == "passed" else "❌"
        lines.append(f"{icon} {ex.case_name} — {ex.status} ({ex.passed_steps}/{ex.total_steps}步)")

    assistant_message = "\n".join(lines)
    planning_session.status = "completed"

    execution_analysis = None
    if _should_run_analysis(execution_summaries):
        analysis_response = _run_analysis_turn(
            execution_summaries=execution_summaries,
            db_session=session,
            project_id=project_ids[0] if project_ids else 0,
        )
        if analysis_response and analysis_response.execution_analysis:
            execution_analysis = analysis_response.execution_analysis
            assistant_message = f"{assistant_message}\n\n---\n\n{analysis_response.assistant_message}"

    session.add(
        AIPlanningMessage(
            session_id=planning_session.id,
            role="assistant",
            turn_type="plan",
            content=assistant_message,
            structured_payload_json={
                "type": "retest_summary",
                "retest_case_ids": case_ids,
                "execution_summaries": [item.model_dump(mode="json") for item in execution_summaries],
                "analysis": execution_analysis.model_dump(mode="json") if execution_analysis else None,
            },
        )
    )
    session.commit()
    session.refresh(planning_session)

    return AIPlanningTurnResponse(
        assistant_message=assistant_message,
        session_status="completed",
        requirements=AIPlanningRequirements.model_validate(planning_session.requirements_json or {}),
        missing_slots=[],
        suggested_questions=[],
        plan=None,
        drafts=[],
        next_action="ask_followup",
        saved_cases=[],
        execution_summaries=execution_summaries,
        execution_analysis=execution_analysis,
    )


def delete_planning_session(
    session: Session,
    planning_session_id: int,
    *,
    actor_user_id: int,
) -> None:
    planning_session = _get_session(session, planning_session_id, actor_user_id=actor_user_id)
    session.delete(planning_session)
    session.commit()


def delete_planning_draft(
    session: Session,
    draft_id: int,
    *,
    actor_user_id: int,
) -> None:
    """Delete a single planning draft (owner only)."""
    draft = session.get(AIPlanningDraft, draft_id)
    if draft is None:
        raise EntityNotFoundError(f"AI planning draft {draft_id} not found.")
    # Verify the user owns the parent session
    _get_session(session, draft.session_id, actor_user_id=actor_user_id)
    session.delete(draft)
    session.commit()


def _get_session(session: Session, planning_session_id: int, *, actor_user_id: int) -> AIPlanningSession:
    planning_session = session.get(AIPlanningSession, planning_session_id)
    if planning_session is None:
        raise EntityNotFoundError(f"AI planning session {planning_session_id} not found.")
    if planning_session.actor_user_id != actor_user_id:
        raise AIPlanningAccessError("AI planning session access denied.")
    return planning_session


def _get_session_project_ids(planning_session: AIPlanningSession) -> list[int]:
    """Return project IDs associated with this session, ordered by link creation time."""
    return [p.id for p in (planning_session.projects or [])]


def link_project_to_session(
    session: Session,
    planning_session_id: int,
    *,
    project_id: int,
    actor_user_id: int,
) -> ProjectSummaryInSession:
    planning_session = _get_session(session, planning_session_id, actor_user_id=actor_user_id)
    project = session.get(Project, project_id)
    if project is None:
        raise EntityNotFoundError(f"Project {project_id} not found.")

    existing = session.scalar(
        select(SessionProject).where(
            SessionProject.session_id == planning_session_id,
            SessionProject.project_id == project_id,
        )
    )
    if existing is not None:
        raise ValueError(f"Project {project_id} already linked to session {planning_session_id}.")

    session.add(SessionProject(session_id=planning_session_id, project_id=project_id))
    session.commit()
    return ProjectSummaryInSession(id=project.id, name=project.name, description=project.description)


def unlink_project_from_session(
    session: Session,
    planning_session_id: int,
    *,
    project_id: int,
    actor_user_id: int,
) -> None:
    planning_session = _get_session(session, planning_session_id, actor_user_id=actor_user_id)
    link = session.scalar(
        select(SessionProject).where(
            SessionProject.session_id == planning_session_id,
            SessionProject.project_id == project_id,
        )
    )
    if link is None:
        raise EntityNotFoundError(f"Project {project_id} not linked to session {planning_session_id}.")
    session.delete(link)
    session.commit()


def list_session_projects(
    session: Session,
    planning_session_id: int,
    *,
    actor_user_id: int,
) -> list[ProjectSummaryInSession]:
    planning_session = _get_session(session, planning_session_id, actor_user_id=actor_user_id)
    return [
        ProjectSummaryInSession(id=p.id, name=p.name, description=p.description)
        for p in (planning_session.projects or [])
    ]


def create_project_in_session(
    session: Session,
    planning_session_id: int,
    *,
    name: str,
    description: str | None,
    actor_user_id: int,
) -> ProjectSummaryInSession:
    from app.models import ProjectMember

    planning_session = _get_session(session, planning_session_id, actor_user_id=actor_user_id)

    project = Project(name=name, description=description)
    session.add(project)
    session.flush()

    # Add user as project owner so they can manage cases later
    member = ProjectMember(
        project_id=project.id,
        user_id=actor_user_id,
        role="owner",
    )
    session.add(member)

    session.add(SessionProject(session_id=planning_session_id, project_id=project.id))
    session.commit()
    session.refresh(project)

    return ProjectSummaryInSession(id=project.id, name=project.name, description=project.description)


def ensure_project_member_for_session_projects(
    session: Session,
    planning_session_id: int,
    actor_user_id: int,
) -> None:
    """Ensure the user is a member of all projects linked to this session.

    This fixes projects created before the ProjectMember fix was applied.
    """
    from app.models import ProjectMember

    planning_session = _get_session(session, planning_session_id, actor_user_id=actor_user_id)
    project_ids = _get_session_project_ids(planning_session)

    for project_id in project_ids:
        existing = session.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == actor_user_id,
            )
        )
        if existing is None:
            logger.info(
                "[ensure_project_member] Adding user %d as owner of project %d",
                actor_user_id, project_id,
            )
            session.add(ProjectMember(
                project_id=project_id,
                user_id=actor_user_id,
                role="owner",
            ))

    session.commit()


def _ensure_project_access(session: Session, *, project_id: int, actor_user_id: int) -> None:
    if session.get(Project, project_id) is None:
        raise EntityNotFoundError(f"Project {project_id} not found.")
    _ensure_project_member(session, project_id, actor_user_id)


def _ensure_case_access(session: Session, *, case_id: int, project_id: int, actor_user_id: int) -> None:
    case_record = session.get(TestCase, case_id)
    if case_record is None:
        raise EntityNotFoundError(f"Case {case_id} not found.")
    if case_record.project_id != project_id:
        raise EntityNotFoundError(f"Case {case_id} does not belong to project {project_id}.")
    _ensure_project_member(session, project_id, actor_user_id)


def _normalize_base_url(requirements_json: dict) -> str | None:
    value = requirements_json.get("entry_url_or_page")
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def _to_session_schema(record: AIPlanningSession) -> AIPlanningSessionSchema:
    # Strip internal-only keys from plan_json before pydantic validation.
    plan_raw = dict(record.plan_json) if record.plan_json else None
    if plan_raw is not None:
        plan_raw.pop("_page_results", None)
        # Truncate massive page_elements to prevent API response overflow
        _MAX_PE_CHARS = 50000
        for sc in plan_raw.get("scenarios", []) or []:
            pe = sc.get("page_elements", "")
            if isinstance(pe, str) and len(pe) > _MAX_PE_CHARS:
                sc["page_elements"] = pe[:_MAX_PE_CHARS] + f"\n...[truncated {len(pe)-_MAX_PE_CHARS} chars]"
    return AIPlanningSessionSchema(
        id=record.id,
        actor_user_id=record.actor_user_id,
        case_id=record.case_id,
        title=record.title,
        status=record.status,
        requirements=AIPlanningRequirements.model_validate(record.requirements_json or {}),
        plan=plan_raw,
        missing_slots=record.missing_slots_json or [],
        last_error_message=record.last_error_message,
        created_at=record.created_at,
        updated_at=record.updated_at,
        projects=[
            ProjectSummaryInSession(id=p.id, name=p.name, description=p.description)
            for p in (record.projects or [])
        ],
    )


def _to_message_schema(record: AIPlanningMessage) -> AIPlanningMessageSchema:
    return AIPlanningMessageSchema(
        id=record.id,
        session_id=record.session_id,
        role=record.role,
        turn_type=record.turn_type,
        content=record.content,
        structured_payload=record.structured_payload_json,
        created_at=record.created_at,
    )


def _to_draft_schema(record: AIPlanningDraft) -> AIPlanningDraftSchema:
    return AIPlanningDraftSchema(
        id=record.id,
        session_id=record.session_id,
        scenario_key=record.scenario_key,
        title=record.title,
        status=record.status,
        dsl_generation_id=record.dsl_generation_id,
        dsl_case=record.dsl_case_json,
        warnings=record.warnings_json or [],
        normalization_notes=record.normalization_notes_json or [],
        error_message=record.error_message,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


# ── Cache helpers ─────────────────────────────────────────────────────────

_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_content",
                     "utm_term", "_t", "ref", "fbclid", "gclid"}


def _normalize_cache_url(raw_url: str) -> str:
    """Strip tracking params + drop fragment for cache key normalization."""
    p = urlparse(raw_url)
    qs = parse_qs(p.query)
    cleaned_qs = {k: v for k, v in qs.items() if k.lower() not in _TRACKING_PARAMS}
    query = urlencode(cleaned_qs, doseq=True)
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc.lower(), path, "", query, ""))


def _lookup_tool_cache(
    db_session: Session,
    key: tuple,
    *,
    ttl_hours: int = 4,
) -> dict | None:
    """Look up a cached explore result by composite key."""
    tool_name, session_id, normalized_url, *_ = key
    cutoff = datetime.now(UTC) - timedelta(hours=ttl_hours)

    records = db_session.scalars(
        select(AIPlanningToolResult).where(
            AIPlanningToolResult.session_id == session_id,
            AIPlanningToolResult.tool_name == tool_name,
            AIPlanningToolResult.created_at >= cutoff,
        ).order_by(AIPlanningToolResult.id.desc())
    ).all()

    for r in records:
        raw = r.raw_result_json
        if not isinstance(raw, dict):
            continue
        cached_url = _normalize_cache_url(raw.get("url", ""))
        if cached_url == normalized_url:
            return raw
    return None
