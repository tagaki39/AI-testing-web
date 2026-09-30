"""LLM-driven ReAct agent for AI test planning."""

from __future__ import annotations

import json
import logging
import re
import time
import traceback as _traceback
from typing import Any, Generator
from urllib import request

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.planning_tools import execute_tool
from app.ai.test_planning_prompts import FORCE_GENERATE_HINT, FORCE_GENERATE_MARKER, build_system_prompt
from app.core.config import get_settings
from app.core.structured_logging import LogContext, get_structured_logger, Timer
from app.schemas.ai_planning import (
    AIPlanningPlan,
    AIPlanningRequirements,
    AIPlanningScenario,
    AIPlanningTestDataRequirement,
    AIPlanningTodoItem,
    AIPlanningToolCall,
    AIPlanningTurnResponse,
)


logger = logging.getLogger(__name__)
slog = get_structured_logger(__name__)
URL_PATTERN = re.compile(r"https?://[^\s，。；;]+", re.IGNORECASE)


def _summarize_tool_result(tool_name: str, result: Any) -> str:
    """Produce a concise human-readable summary of a tool result for logging."""
    if not isinstance(result, dict):
        return str(result)[:300]
    if "error" in result:
        return f"error: {result['error']}"
    summary_parts: list[str] = []
    if tool_name == "explore_page":
        url = result.get("url", "?")
        count = result.get("element_count", "?")
        summary_parts.append(f"url={url}, element_count={count}")
        if "warning" in result:
            summary_parts.append(f"warning={result['warning']}")
    elif tool_name == "explore_flow":
        urls = result.get("urls", [])
        summary_parts.append(f"urls_count={len(urls)}")
        pages = result.get("pages", []) or result.get("page_results", [])
        summary_parts.append(f"pages_explored={len(pages)}")
    elif tool_name == "create_project":
        summary_parts.append(f"id={result.get('id')}, name={result.get('name')}")
    elif tool_name == "get_project_info":
        summary_parts.append(f"id={result.get('id')}, name={result.get('name')}")
    elif tool_name == "list_test_cases":
        summary_parts.append(f"total={result.get('total')}, returned={len(result.get('cases', []))}")
    elif tool_name == "capture_page_session":
        summary_parts.append(f"status={result.get('status')}, cookies={result.get('cookie_count', '?')}")
    else:
        for key in list(result.keys())[:5]:
            val = result[key]
            if isinstance(val, (str, int, float, bool)):
                summary_parts.append(f"{key}={val}")
            elif isinstance(val, list):
                summary_parts.append(f"{key}=[{len(val)} items]")
            elif isinstance(val, dict):
                summary_parts.append(f"{key}={{...{len(val)} keys}}")
    return ", ".join(summary_parts) if summary_parts else json.dumps(result, ensure_ascii=False)[:300]

_NEW_REQUIREMENT_KEYWORDS = [
    "新需求", "换一个", "重新", "改一下", "调整方案", "变更",
    "新增测试", "还有一个", "另外还要", "再来一个", "补充测试",
    "新增场景", "换种", "不同方案", "换个思路",
]

REQUIRED_REQUIREMENT_SLOTS = [
    "app_under_test",
    "business_goal",
    "entry_url_or_page",
    "core_user_flow",
    "main_assertions",
    "test_data_or_account",
    "scope_limits",
]


def _tool_call_signature(tool_name: str, params: dict[str, Any]) -> str | None:
    """Return a canonical signature for tool calls eligible for dedup.

    Returns None for tools where dedup doesn't apply. Same signature within
    the same turn means a redundant call (Bug C).
    """
    if not isinstance(params, dict):
        return None
    if tool_name == "create_project":
        name = (params.get("name") or "").strip().lower()
        if not name:
            return None
        return f"create_project::{name}"
    if tool_name == "explore_flow":
        base_url = (params.get("base_url") or "").strip().rstrip("/").lower()
        flow_desc = (params.get("flow_description") or "").strip().lower()
        steps = params.get("steps")
        urls = params.get("urls")
        if isinstance(steps, list):
            # Canonicalize: only url + action+target per step (ignore values like passwords)
            canon = []
            for s in steps:
                if not isinstance(s, dict):
                    continue
                url = str(s.get("url") or "").strip().rstrip("/").lower()
                actions = s.get("actions") or []
                acts: list[str] = []
                if isinstance(actions, list):
                    for a in actions:
                        if isinstance(a, dict):
                            acts.append(
                                f"{str(a.get('action') or '').lower()}:"
                                f"{str(a.get('target') or '').strip().lower()}"
                            )
                canon.append(url + "|" + ",".join(acts))
            step_sig = ";".join(canon)
        elif isinstance(urls, list):
            step_sig = ";".join(str(u).strip().rstrip("/").lower() for u in urls)
        else:
            step_sig = ""
        if not (base_url or step_sig):
            return None
        return f"explore_flow::{base_url}::{flow_desc}::{step_sig}"
    if tool_name == "explore_page":
        url = (params.get("url") or "").strip().rstrip("/").lower()
        if not url:
            return None
        return f"explore_page::{url}"
    return None


def _turn_complete_payload(response: AIPlanningTurnResponse) -> dict[str, Any]:
    return {
        "type": "turn_complete",
        "session_status": response.session_status,
        "payload": {
            "assistant_message": response.assistant_message,
            "missing_slots": response.missing_slots,
            "suggested_questions": response.suggested_questions,
            "plan": response.plan.model_dump(mode="json") if response.plan else None,
            "tool_calls": [
                {"tool": item.tool, "params": item.params}
                for item in response.tool_calls
            ],
            "todo_list": [t.model_dump(mode="json") for t in response.todo_list],
        },
    }


def run_planning_turn(
    *,
    transcript: list[dict[str, str]],
    existing_requirements: AIPlanningRequirements | None,
    db_session: Session,
    project_id: int,
    actor_user_id: int = 0,
    planning_session_id: int = 0,
) -> AIPlanningTurnResponse:
    """Synchronous wrapper around :func:`stream_planning_turn`.

    Consumes all events from the streaming generator and returns the final
    ``AIPlanningTurnResponse``.  Used by REST API fallback.
    """
    stream = stream_planning_turn(
        transcript=transcript,
        existing_requirements=existing_requirements,
        db_session=db_session,
        project_id=project_id,
        actor_user_id=actor_user_id,
        planning_session_id=planning_session_id,
    )
    try:
        while True:
            try:
                next(stream)
            except StopIteration as stop:
                return stop.value
    finally:
        if planning_session_id:
            from app.ai.page_explorer import BrowserSessionManager
            BrowserSessionManager.close_session(planning_session_id)


def stream_planning_turn(
    *,
    transcript: list[dict[str, str]],
    existing_requirements: AIPlanningRequirements | None,
    db_session: Session,
    project_id: int,
    actor_user_id: int = 0,
    planning_session_id: int = 0,
) -> Generator[dict[str, Any], None, AIPlanningTurnResponse]:
    """Streaming ReAct planning turn.

    Yields status / text_chunk / tool_call events during processing.
    Returns the final ``AIPlanningTurnResponse`` via the generator return value.
    """
    requirements = existing_requirements.model_copy(deep=True) if existing_requirements else AIPlanningRequirements()
    settings = get_settings()
    tool_calls: list[AIPlanningToolCall] = []
    transcript_messages, force_generate = _prepare_transcript_for_llm(
        transcript,
        requirements=existing_requirements,
        plan=None,
        tool_calls=None,
    )
    logger.info("Planning turn start, transcript_len=%d, ai_enabled=%s", len(transcript), _planning_llm_enabled(settings))
    slog.ai_thinking("react_round_start", message="Planning turn start", data={
        "transcript_length": len(transcript),
        "ai_enabled": _planning_llm_enabled(settings),
    }, session_id=planning_session_id)

    if not _planning_llm_enabled(settings):
        response = _run_fallback_turn(
            transcript=transcript,
            requirements=requirements,
            assistant_message=None,
            force_generate=force_generate,
            tool_calls=tool_calls,
        )
        yield _turn_complete_payload(response)
        return response

    conversation: list[dict[str, str]] = [{"role": "system", "content": build_system_prompt()}, *transcript_messages]
    safety_cap = max(1, settings.ai_planning_max_react_safety_cap)
    round_index = 0
    parse_retries = 0
    guard_continue_count = 0  # Track how many times generate_plan guard continues
    GUARD_CONTINUE_LIMIT = 10  # Force fallback after this many guard continues
    turn_start_time = time.monotonic()
    while round_index < safety_cap:
        round_index += 1
        yield {"type": "status", "phase": "thinking", "message": "正在分析需求..."}

        # Inject cache progress before each ReAct round
        cache_msg = _build_cache_progress_message(tool_calls)
        if cache_msg:
            conversation.append({"role": "system", "content": cache_msg})

        raw_response = ""
        llm_error_type = ""
        llm_error_detail = ""
        try:
            for event in _stream_planning_llm(
                messages=conversation,
                api_key=settings.ai_planning_api_key or "",
                model=settings.ai_planning_model or "",
                base_url=settings.ai_planning_base_url,
                timeout_seconds=max(1.0, settings.ai_planning_timeout_ms / 1000),
            ):
                if event["type"] in ("text_chunk", "status"):
                    yield event
                elif event["type"] == "raw_response":
                    raw_response = event["text"]
        except httpx.ConnectTimeout:
            llm_error_type = "ConnectTimeout"
            llm_error_detail = f"连接 AI 模型服务超时 (base_url={settings.ai_planning_base_url})"
            logger.error("LLM connection timeout in round %d: %s", round_index, llm_error_detail)
        except httpx.ReadTimeout:
            llm_error_type = "ReadTimeout"
            llm_error_detail = f"AI 模型响应超时 (timeout={settings.ai_planning_timeout_ms}ms)"
            logger.error("LLM read timeout in round %d: %s", round_index, llm_error_detail)
        except httpx.HTTPStatusError as exc:
            llm_error_type = "HTTPStatusError"
            llm_error_detail = f"AI 模型返回 HTTP {exc.response.status_code}: {exc.response.text[:500]}"
            logger.error("LLM HTTP error in round %d: %s", round_index, llm_error_detail)
        except httpx.ConnectError:
            llm_error_type = "ConnectError"
            llm_error_detail = f"无法连接 AI 模型服务 (base_url={settings.ai_planning_base_url})"
            logger.error("LLM connection error in round %d: %s", round_index, llm_error_detail)
        except Exception as exc:
            llm_error_type = type(exc).__name__
            llm_error_detail = str(exc)
            logger.exception("Streaming LLM call failed in round %d", round_index)

        if not raw_response:
            error_phase = "timeout" if "Timeout" in llm_error_type else ("connection" if "Connect" in llm_error_type else "llm_call")
            response = _error_response(
                requirements=requirements,
                tool_calls=tool_calls,
                error_type=llm_error_type or "empty_response",
                error_detail=llm_error_detail or "LLM 返回空响应",
                phase=error_phase,
            )
            yield _turn_complete_payload(response)
            return response

        parsed = _parse_llm_response(raw_response)
        logger.info("LLM response in round %d (len=%d): action=%s, action_input_keys=%s",
                     round_index, len(raw_response), parsed.get("action") if parsed else "parse_failed",
                     list((parsed.get("action_input") or {}).keys()) if parsed and isinstance(parsed.get("action_input"), dict) else [])
        slog.ai_thinking("llm_response", data={
            "round_index": round_index,
            "response_length": len(raw_response),
            "parse_success": parsed is not None,
            "action": parsed.get("action") if parsed else None,
        }, session_id=planning_session_id)
        if parsed is None:
            parse_retries += 1
            logger.warning("LLM response unparseable in round %d (parse retry %d), raw (first 300 chars): %s",
                           round_index, parse_retries, raw_response[:300])
            if parse_retries <= 2:
                conversation.extend([
                    {"role": "assistant", "content": _normalize_json_text(raw_response)},
                    {"role": "system", "content": (
                        "⚠️ 你的上一次回复不是有效的 JSON 格式，系统无法解析。"
                        "请严格返回一个 JSON 对象，不要包含 markdown 代码围栏、注释或任何额外文字。"
                        "正确的格式示例：\n"
                        '```json\n'
                        '{"action": "call_tool", "action_input": {"tool": "...", "params": {...}}, '
                        '"assistant_message": "...", "collected_info": {}, "todo_list": []}\n'
                        '```\n'
                        "请基于当前上下文重新回复。"
                    )},
                ])
                yield {"type": "status", "phase": "thinking", "message": f"解析失败，正在重试 ({parse_retries}/2)..."}
                continue
            # retries exhausted, fall back
            raw_preview = raw_response[:500].replace("{", "{{").replace("}", "}}")
            detail_msg = (
                f"JSON 解析连续失败 {parse_retries} 次，AI 返回的内容不是合法的 JSON 格式。\n\n"
                f"原始输出（前 500 字符）：\n```\n{raw_preview}\n```\n\n"
                f"常见原因：JSON 未闭合、嵌套了 markdown 代码块、包含未转义的特殊字符、或模型在 JSON 外多输出了额外文字。\n\n"
                f"我先按已有信息给你整理一个测试方案。"
            )
            response = _run_fallback_turn(
                transcript=transcript,
                requirements=requirements,
                assistant_message=detail_msg,
                force_generate=force_generate,
                tool_calls=tool_calls,
            )
            yield _turn_complete_payload(response)
            return response

        # reset retry counter on successful parse
        parse_retries = 0

        _merge_requirements(requirements, parsed.get("collected_info"))
        _merge_test_context(requirements, parsed.get("test_context"))
        action = str(parsed.get("action") or "").strip()
        action_input = parsed.get("action_input")
        if not isinstance(action_input, dict):
            action_input = {}

        logger.info("ReAct round %d: action=%s, assistant_msg_len=%d", round_index, action, len(parsed.get("assistant_message", "")))
        slog.ai_thinking("react_round_complete", data={
            "round_index": round_index,
            "action": action,
            "action_input_keys": list(action_input.keys()),
            "assistant_message_length": len(parsed.get("assistant_message", "")),
        }, session_id=planning_session_id)

        # --- Parse todo_list from LLM response ---
        _valid_statuses = {"done", "in_progress", "pending", "failed", "skipped"}
        raw_todo = parsed.get("todo_list") or []
        todo_items = [
            AIPlanningTodoItem(
                item=str(t.get("item", "")),
                status=t.get("status", "pending") if t.get("status") in _valid_statuses else "pending",
            )
            for t in raw_todo if isinstance(t, dict) and str(t.get("item", "")).strip()
        ]

        # --- Guard: ensure page exploration before generating plan (BUG-052 / BUG-059) ---
        if action == "generate_plan":
            has_explore = _has_explored_pages(tool_calls)
            has_flow = any(call.tool == "explore_flow" for call in tool_calls)

            # Guard round protection: force fallback after too many continues
            guard_continue_count += 1
            if guard_continue_count > GUARD_CONTINUE_LIMIT:
                logger.warning(
                    "generate_plan guard exceeded %d continues, forcing plan generation. "
                    "has_explore=%s, has_flow=%s",
                    GUARD_CONTINUE_LIMIT, has_explore, has_flow,
                )
                yield {"type": "status", "phase": "tool_call",
                       "message": "页面探索轮次已达上限，将基于已有数据生成方案"}
                # Allow fall-through to generate_plan logic below

            # Check exploration QUALITY, not just existence
            elif has_explore:
                exploration_elements = _count_explored_elements(tool_calls)
                if exploration_elements < 10:
                    yield {"type": "status", "phase": "tool_call",
                           "message": f"页面探索仅采集到 {exploration_elements} 个元素，数据不足，需要更多探索"}
                    conversation.append(
                        {"role": "system", "content": (
                            f"页面探索仅采集到 {exploration_elements} 个元素，数据严重不足。"
                            "请使用 explore_page 采集更多页面（如登录页、商品列表页、购物车页）。"
                            "没有足够元素数据时不要生成 DSL。"
                        )},
                    )
                    continue

                # Check page coverage against core_user_flow
                coverage, missing_pages = _check_page_coverage(
                    tool_calls, requirements.core_user_flow,
                )
                if coverage < 0.5 and missing_pages:
                    logger.warning(
                        "Guard: page coverage %.1f%% < 50%%, missing: %s",
                        coverage * 100, missing_pages,
                    )
                    yield {"type": "status", "phase": "tool_call",
                           "message": f"页面覆盖度不足（{coverage:.0%}），缺少: {', '.join(missing_pages)}"}
                    conversation.append(
                        {"role": "system", "content": (
                            f"页面探索覆盖度仅为 {coverage:.0%}，以下页面尚未探索：{', '.join(missing_pages)}。\n"
                            "请使用 explore_flow 工具补充探索这些页面。如果流程涉及登录、筛选等交互，"
                            "必须在 steps 参数中包含对应的 actions。\n"
                            "不要在页面覆盖度不足时生成测试方案。"
                        )},
                    )
                    continue

            if not has_explore:
                explored, tool_calls, internal_links = _auto_explore_entry_url(
                    requirements, tool_calls, db_session, project_id,
                    actor_user_id=actor_user_id, planning_session_id=planning_session_id,
                )
                if explored:
                    page_elements = _extract_page_elements(tool_calls)
                    exploration_error = _extract_exploration_error(tool_calls)
                    if not page_elements and exploration_error:
                        yield {"type": "status", "phase": "tool_call", "message": f"页面探索失败：{exploration_error}"}
                        conversation.append(
                            {"role": "system", "content": (
                                f"⚠️ 页面自动探索失败，错误信息：{exploration_error}\n"
                                "请向用户报告此错误，说明无法采集页面元素，建议用户：\n"
                                "1. 检查入口 URL 是否正确且可访问\n"
                                "2. 稍后重试（可能是网络波动）\n"
                                "3. 提供更多页面信息以辅助规划\n"
                                "不要在没有页面元素数据的情况下生成测试方案。"
                            )},
                        )
                        continue

                    yield {"type": "status", "phase": "tool_call", "message": "正在自动采集入口页面元素..."}

                    if internal_links:
                        # BUG-059: let LLM decide which links to explore
                        _track_link_presentation(conversation, internal_links)
                        conversation.append(
                            {"role": "system", "content": _build_link_selection_message(
                                internal_links, requirements.core_user_flow,
                            )},
                        )
                    else:
                        conversation.append(
                            {"role": "system", "content": (
                                "系统已自动采集了入口页面的可交互元素（见上方工具返回结果）。"
                                "请基于这些元素信息重新生成测试方案，"
                                "确保 target 使用元素的实际 label、placeholder 或 id。"
                            )},
                        )
                    continue

            elif not has_flow and _has_internal_links_in_tool_calls(tool_calls):
                # AI called explore_page but not explore_flow — present links for selection
                internal_links = _extract_links_from_tool_calls(tool_calls, requirements)
                if internal_links:
                    _track_link_presentation(conversation, internal_links)
                    yield {"type": "status", "phase": "tool_call", "message": "正在分析入口页面的导航链接..."}
                    conversation.append(
                        {"role": "system", "content": _build_link_selection_message(
                            internal_links, requirements.core_user_flow,
                        )},
                    )
                    continue

            # Safety net: LLM saw links but responded with generate_plan again
            if not has_flow and _was_link_list_presented(conversation):
                yield {"type": "status", "phase": "tool_call", "message": "正在自动补充采集导航页面元素..."}
                links_to_explore = _get_presented_links(conversation)
                if links_to_explore:
                    fallback_urls = _rank_links_by_flow_relevance(
                        links_to_explore, requirements.core_user_flow,
                    )[:4]
                    # Build flow steps with description so elements get page_state markers
                    flow_steps = _build_safety_net_steps(fallback_urls, requirements.core_user_flow)
                    # Extract base_url from requirements for relative URL resolution (BUG-065)
                    safety_net_params: dict[str, Any] = {"steps": flow_steps}
                    if requirements.entry_url_or_page:
                        from urllib.parse import urlparse
                        entry = requirements.entry_url_or_page
                        if entry.startswith("http"):
                            parsed = urlparse(entry)
                            safety_net_params["base_url"] = f"{parsed.scheme}://{parsed.netloc}"
                    logger.info("Safety-net: auto-exploring %d steps for URLs %s", len(flow_steps), fallback_urls)
                    try:
                        flow_result_text = execute_tool(
                            tool_name="explore_flow",
                            params=safety_net_params,
                            db_session=db_session,
                            project_id=project_id,
                            actor_user_id=actor_user_id,
                            planning_session_id=planning_session_id,
                        )
                        flow_result = _safe_parse_json(flow_result_text)
                        tool_calls.append(
                            AIPlanningToolCall(
                                tool="explore_flow",
                                params=safety_net_params,
                                result=flow_result,
                            )
                        )
                    except Exception as exc:
                        logger.warning("Safety-net explore_flow failed: %s", exc)
                    _clear_link_tracking(conversation)
                    conversation.append(
                        {"role": "system", "content": (
                            "系统已自动补充采集了入口页面的链接页面元素（仅静态 URL 跳转）。\n"
                            "页面元素已按页面状态（S0/S1/S2...）分组标记。\n\n"
                            "【重要】这仅是静态页面的采集结果。如果 core_user_flow 中涉及"
                            "登录、品牌筛选、加入购物车、表单提交等需要交互才能到达的页面状态，"
                            "你必须继续调用 explore_flow 并在 steps 参数中包含 actions 来执行这些交互，"
                            "否则采集到的元素不完整，DSL 生成会失败。\n\n"
                            "【关键】你必须探索用户流程中涉及的所有页面，包括：\n"
                            "1. 通过点击按钮/链接打开的模态框（如 Add to cart 后的模态框）\n"
                            "2. 通过交互跳转的新页面（如点击 View Cart 后的购物车页面）\n"
                            "3. 用户流程中明确提到的每个页面（如购物车页、登录页等）\n\n"
                            "只有当所有 core_user_flow 涉及的页面都已被探索后，才能调用 generate_plan。"
                        )},
                    )
                    continue

        if force_generate and action != "generate_plan":
            response = _plan_response(
                requirements=requirements,
                plan_payload=action_input,
                assistant_message="我先基于当前已知信息生成一版测试方案，缺失信息会体现在假设和风险里。",
                tool_calls=tool_calls,
                todo_list=todo_items,
            )

            # Persist tool results before returning (for force_generate turns)
            if planning_session_id:
                from app.models import AIPlanningToolResult
                from app.models.ai_planning_message import AIPlanningMessage
                _HEAVY_TOOLS_SET = {"explore_page", "explore_flow"}
                for tc in tool_calls:
                    if tc.tool not in _HEAVY_TOOLS_SET:
                        continue
                    compressed = getattr(tc, "_compressed_result", None)
                    if compressed is None:
                        continue
                    # Check if already persisted (avoid duplicates)
                    existing = db_session.scalar(
                        select(AIPlanningToolResult)
                        .where(AIPlanningToolResult.session_id == planning_session_id)
                        .where(AIPlanningToolResult.tool_name == tc.tool)
                        .order_by(AIPlanningToolResult.id.desc())
                        .limit(1)
                    )
                    if existing is not None:
                        continue
                    # Find or create a message_id for this tool call
                    msg = db_session.scalar(
                        select(AIPlanningMessage)
                        .where(AIPlanningMessage.session_id == planning_session_id)
                        .where(AIPlanningMessage.role == "assistant")
                        .where(AIPlanningMessage.turn_type == "tool_call")
                        .order_by(AIPlanningMessage.id.desc())
                        .limit(1)
                    )
                    raw_json = tc.result if isinstance(tc.result, dict) else None
                    db_session.add(AIPlanningToolResult(
                        session_id=planning_session_id,
                        message_id=msg.id if msg else None,
                        tool_name=tc.tool,
                        raw_result_json=raw_json,
                        summary_json=compressed,
                    ))
                    logger.info(
                        "[persist_tool_result_force_generate] Saved %s result to AIPlanningToolResult: session=%d, raw_keys=%s",
                        tc.tool, planning_session_id, list(raw_json.keys()) if raw_json else None,
                    )
                db_session.flush()

            yield _turn_complete_payload(response)
            return response

        if action == "call_tool":
            tool_name = str(action_input.get("tool") or "").strip()
            params = action_input.get("params")
            if not isinstance(params, dict):
                params = {}
            logger.info("Tool call: %s, params=%s", tool_name, json.dumps(params, ensure_ascii=False, default=str)[:500])
            logger.info("[debug] tool_name=%s, in_HEAVY_TOOLS=%s, _HEAVY_TOOLS=%s", tool_name, tool_name in _HEAVY_TOOLS, _HEAVY_TOOLS)

            # --- Bug C: Dedup duplicate tool calls within this turn ---
            dup_signature = _tool_call_signature(tool_name, params)
            prior_call = None
            if dup_signature:
                prior_call = next(
                    (
                        c for c in tool_calls
                        if _tool_call_signature(c.tool, c.params or {}) == dup_signature
                    ),
                    None,
                )
            if prior_call is not None:
                logger.info(
                    "Duplicate tool call detected: %s, signature=%s — reusing prior result without charging a round",
                    tool_name, dup_signature,
                )
                slog.tool_call("tool_dedup", data={
                    "tool_name": tool_name,
                    "signature": dup_signature,
                    "round_index": round_index,
                }, session_id=planning_session_id)
                yield {"type": "tool_call_start", "tool": tool_name, "params": params}
                yield {
                    "type": "tool_call_end", "tool": tool_name,
                    "result_summary": {"duplicate_of_prior_call": True},
                }
                prior_summary = _summarize_tool_result(prior_call.tool, prior_call.result or {})
                conversation.extend([
                    {"role": "assistant", "content": _normalize_json_text(raw_response)},
                    {
                        "role": "system",
                        "content": (
                            f"⚠️ 检测到重复调用：你刚刚已经调用过 {tool_name}（等价参数）。"
                            f"上次结果摘要：{prior_summary}。"
                            f"请不要重复调用 {tool_name}，立即转入下一步——"
                            f"选择不同的工具采集更多信息，或调用 generate_plan 输出方案。"
                        ),
                    },
                ])
                round_index -= 1  # don't charge a round for a redundant duplicate
                continue

            yield {"type": "tool_call_start", "tool": tool_name, "params": params}
            try:
                tool_result_text = execute_tool(
                    tool_name=tool_name,
                    params=params,
                    db_session=db_session,
                    project_id=project_id,
                    actor_user_id=actor_user_id,
                    planning_session_id=planning_session_id,
                )
            except Exception as exc:
                logger.error("Tool call %s failed: %s", tool_name, exc, exc_info=True)
                response = _error_response(
                    requirements=requirements,
                    tool_calls=tool_calls,
                    error_type=type(exc).__name__,
                    error_detail=str(exc),
                    phase="tool_call",
                )
                yield _turn_complete_payload(response)
                return response
            parsed_result = _safe_parse_json(tool_result_text)
            # After create_project succeeds, update the local project_id so that
            # subsequent tool calls (explore_page, capture_page_session, etc.)
            # can use the newly-created project immediately within the same turn.
            if tool_name == "create_project" and isinstance(parsed_result, dict) and isinstance(parsed_result.get("id"), int):
                new_id = int(parsed_result["id"])
                if new_id > 0:
                    logger.info("Updating project_id from %d to %d after create_project", project_id, new_id)
                    project_id = new_id
            # --- Deterministic compression for heavy tools ---
            compressed_result = None
            if tool_name in _HEAVY_TOOLS:
                logger.info(
                    "[compress_tool] tool=%s, parsed_result_type=%s, is_dict=%s",
                    tool_name,
                    type(parsed_result).__name__,
                    isinstance(parsed_result, dict),
                )
                if isinstance(parsed_result, dict):
                    compressed_result = _compress_tool_result(tool_name, parsed_result)
                    logger.info(
                        "[compress_tool] compressed_result keys=%s",
                        list(compressed_result.keys()) if compressed_result else None,
                    )
                else:
                    logger.warning(
                        "[compress_tool] parsed_result is NOT a dict (type=%s), skipping compression",
                        type(parsed_result).__name__,
                    )

            # --- SSE event ---
            if tool_name in _HEAVY_TOOLS:
                result_summary = compressed_result if compressed_result is not None else {
                    "url": parsed_result.get("url") if isinstance(parsed_result, dict) else None,
                    "element_count": parsed_result.get("element_count") if isinstance(parsed_result, dict) else None,
                    "summary_fallback": True,
                }
                yield {"type": "tool_call_end", "tool": tool_name, "result_summary": result_summary}
            else:
                yield {"type": "tool_call_end", "tool": tool_name, "result": parsed_result}

            # --- Store tool call with compressed result ---
            tc = AIPlanningToolCall(
                tool=tool_name or "unknown_tool",
                params=params,
                result=parsed_result,
            )
            if compressed_result is not None:
                tc._compressed_result = compressed_result  # type: ignore[attr-defined]
            tool_calls.append(tc)

            # --- Warn if capture_page_session used without explore_flow ---
            if tool_name == "capture_page_session":
                has_flow = any(c.tool == "explore_flow" for c in tool_calls)
                if not has_flow:
                    conversation.append({
                        "role": "system",
                        "content": (
                            "capture_page_session 仅保存了登录状态（cookie），没有采集页面元素。"
                            "你还需要调用 explore_flow 来采集登录后的页面元素（如 Products 链接、"
                            "品牌列表等），否则 DSL 生成器无法获得登录页的定位器候选。"
                            "请在 explore_flow 的 steps 中包含登录后的页面。"
                        ),
                    })

            # --- Context injection ---
            summary_for_log = _summarize_tool_result(tool_name, parsed_result)
            logger.info("Tool call %s completed: %s", tool_name, summary_for_log)

            conversation.extend([
                {"role": "assistant", "content": _normalize_json_text(raw_response)},
            ])

            if tool_name in _HEAVY_TOOLS:
                if compressed_result is not None:
                    conversation.append({
                        "role": "system",
                        "content": f"工具 {tool_name} 返回摘要：{json.dumps(compressed_result, ensure_ascii=False)}",
                    })
                else:
                    truncated = tool_result_text[:2000] + ("..." if len(tool_result_text) > 2000 else "")
                    conversation.append({
                        "role": "system",
                        "content": f"工具 {tool_name} 返回结果（已截断）：{truncated}",
                    })
            else:
                conversation.append({
                    "role": "system",
                    "content": f"工具 {tool_name} 返回结果：{tool_result_text}",
                })
            # Drop previous cache messages to prevent accumulation
            conversation = [m for m in conversation if not (
                isinstance(m, dict) and m.get("role") == "system"
                and str(m.get("content", "")).startswith("[Cache progress")
            )]
            continue

        if action == "generate_plan":
            # Log page coverage (blocking check is in the guard above)
            coverage, missing_pages = _check_page_coverage(
                tool_calls, requirements.core_user_flow,
            )
            if missing_pages:
                logger.info("Page coverage: %.1f%%, missing: %s", coverage * 100, missing_pages)

            logger.info("Generating plan after %d ReAct rounds, tool_calls=%d, coverage=%.1f%%",
                       round_index, len(tool_calls), coverage * 100)
            response = _plan_response(
                requirements=requirements,
                plan_payload=action_input,
                assistant_message="信息已经足够，我先给出结构化测试方案。",
                tool_calls=tool_calls,
                todo_list=todo_items,
            )

            # --- Auto-generate DSL drafts in the same context ---
            # Persist plan to session before calling generate_planning_drafts
            if planning_session_id:
                from app.models import AIPlanningSession
                ps = db_session.get(AIPlanningSession, planning_session_id)
                if ps:
                    ps.plan_json = response.plan.model_dump(mode="json") if response.plan else None
                    ps.status = "plan_ready"
                    db_session.flush()

                # Persist tool results to AIPlanningToolResult before generating drafts
                # (otherwise _load_a11y_nodes_for_scenario will find no records)
                from app.models.ai_planning_tool_result import AIPlanningToolResult
                from app.models.ai_planning_message import AIPlanningMessage
                _HEAVY_TOOLS_SET = {"explore_page", "explore_flow"}
                for tc in tool_calls:
                    if tc.tool not in _HEAVY_TOOLS_SET:
                        continue
                    compressed = getattr(tc, "_compressed_result", None)
                    if compressed is None:
                        continue
                    # Check if already persisted (avoid duplicates)
                    existing = db_session.scalar(
                        select(AIPlanningToolResult)
                        .where(AIPlanningToolResult.session_id == planning_session_id)
                        .where(AIPlanningToolResult.tool_name == tc.tool)
                        .order_by(AIPlanningToolResult.id.desc())
                        .limit(1)
                    )
                    if existing is not None:
                        continue
                    # Find or create a message_id for this tool call
                    msg = db_session.scalar(
                        select(AIPlanningMessage)
                        .where(AIPlanningMessage.session_id == planning_session_id)
                        .where(AIPlanningMessage.role == "assistant")
                        .where(AIPlanningMessage.turn_type == "tool_call")
                        .order_by(AIPlanningMessage.id.desc())
                        .limit(1)
                    )
                    raw_json = tc.result if isinstance(tc.result, dict) else None
                    db_session.add(AIPlanningToolResult(
                        session_id=planning_session_id,
                        message_id=msg.id if msg else None,
                        tool_name=tc.tool,
                        raw_result_json=raw_json,
                        summary_json=compressed,
                    ))
                    logger.info(
                        "[persist_tool_result_pre_drafts] Saved %s result to AIPlanningToolResult: session=%d, raw_keys=%s",
                        tc.tool, planning_session_id, list(raw_json.keys()) if raw_json else None,
                    )
                db_session.flush()

            # Update requirements_json BEFORE generating drafts, so base_url can be resolved
            if planning_session_id and response.requirements:
                from app.models import AIPlanningSession
                ps = db_session.get(AIPlanningSession, planning_session_id)
                if ps:
                    ps.requirements_json = response.requirements.model_dump(mode="json")
                    db_session.flush()
                    logger.info(
                        "[persist_requirements_pre_drafts] Updated requirements_json: entry_url_or_page=%s",
                        response.requirements.entry_url_or_page,
                    )

            yield {"type": "status", "phase": "dsl", "message": "正在基于方案生成 DSL 草案..."}
            dsl_auto_drafts: list[dict[str, Any]] = []
            try:
                from app.services.ai_planning import generate_planning_drafts
                from app.schemas.ai_planning import GenerateAIPlanningDraftsRequest
                # response.plan is an AIPlanningPlan Pydantic model; convert to dict
                # so .get() works (previously crashed with "object has no attribute 'get'").
                plan_data = response.plan.model_dump(mode="json") if response.plan else {}
                scenario_keys = [
                    s.get("scenario_key", "")
                    for s in plan_data.get("scenarios", [])
                    if isinstance(s, dict) and s.get("scenario_key")
                ]
                if scenario_keys:
                    dsl_req = GenerateAIPlanningDraftsRequest(
                        scenario_keys=scenario_keys[:2],  # top 2 scenarios
                    )
                    dsl_response = generate_planning_drafts(
                        db_session, planning_session_id, dsl_req,
                        actor_user_id=actor_user_id,
                    )
                    for d in dsl_response.drafts:
                        dsl_auto_drafts.append(d.model_dump(mode="json"))
                    response.drafts = dsl_response.drafts
                    response.next_action = "drafts_generated"
                    yield {
                        "type": "drafts",
                        "drafts": dsl_auto_drafts,
                        "next_action": "drafts_generated",
                    }
            except Exception as dsl_exc:
                logger.warning("Auto DSL generation failed: %s", dsl_exc)

            yield _turn_complete_payload(response)
            return response

        if action == "analyze_results":
            analysis_payload = action_input.get("analysis") if isinstance(action_input, dict) else None
            if not isinstance(analysis_payload, dict):
                analysis_payload = {}
            try:
                from app.schemas.ai_planning import ExecutionAnalysis
                analysis = ExecutionAnalysis.model_validate(analysis_payload)
            except Exception:
                analysis = ExecutionAnalysis(conclusion="partial")
            analysis_message = str(action_input.get("summary") or "").strip() if isinstance(action_input, dict) else ""
            if not analysis_message:
                analysis_message = _build_analysis_message(analysis)
            response = AIPlanningTurnResponse(
                assistant_message=analysis_message,
                session_status="completed",
                requirements=requirements,
                missing_slots=[],
                suggested_questions=[],
                plan=None,
                drafts=[],
                next_action="ask_followup",
                tool_calls=tool_calls,
                todo_list=todo_items,
                execution_analysis=analysis,
            )
            yield _turn_complete_payload(response)
            return response

        if action == "plan_regression":
            regression_summary = str(action_input.get("summary") or "").strip() if isinstance(action_input, dict) else ""
            if not regression_summary:
                regression_summary = "根据失败分析，建议进行回归测试。"
            response = _plan_response(
                requirements=requirements,
                plan_payload=action_input,
                assistant_message=regression_summary,
                tool_calls=tool_calls,
                todo_list=todo_items,
            )
            yield _turn_complete_payload(response)
            return response

        # ask_user or unsupported action — intercept when asking about explorable elements
        raw_message = str(action_input.get("message") or "").strip()
        if raw_message and _is_asking_about_explorable_elements(raw_message):
            # Try to find unexplored login URL from existing tool calls first
            login_url = _find_unexplored_login_url(tool_calls, requirements)
            # If no explore_page was ever called yet, auto-explore the entry URL first
            # so we can extract internal links (including /login) from it
            if login_url is None and not _has_explored_pages(tool_calls):
                login_url = _auto_explore_entry_and_find_login(
                    requirements, tool_calls,
                    db_session, project_id,
                    actor_user_id=actor_user_id,
                    planning_session_id=planning_session_id,
                )
            if login_url:
                logger.info("Intercepting ask_user about explorable elements, auto-exploring %s", login_url)
                yield {"type": "status", "phase": "tool_call", "message": "正在自动采集登录页面元素..."}
                try:
                    login_result_text = execute_tool(
                        tool_name="explore_page",
                        params={"url": login_url},
                        db_session=db_session,
                        project_id=project_id,
                        actor_user_id=actor_user_id,
                        planning_session_id=planning_session_id,
                    )
                    login_parsed = _safe_parse_json(login_result_text)
                    tool_calls.append(
                        AIPlanningToolCall(
                            tool="explore_page",
                            params={"url": login_url},
                            result=login_parsed,
                        )
                    )
                    conversation.append(
                        {"role": "system", "content": (
                            f"系统已自动采集了入口页面和登录页面 {login_url} 的可交互元素。"
                            "请基于所有已采集的页面元素信息重新生成测试方案，"
                            "确保 target 使用元素的实际 label、placeholder 或 id。"
                        )},
                    )
                    continue
                except Exception as exc:
                    logger.warning("Auto-explore login intercept failed for url=%s: %s", login_url, exc)

        message = raw_message or _default_followup_question(requirements)
        missing_slots = _collect_missing_slots(requirements)
        response = AIPlanningTurnResponse(
            assistant_message=message,
            session_status="collecting",
            requirements=requirements,
            missing_slots=missing_slots,
            suggested_questions=[message],
            plan=None,
            drafts=[],
            next_action="ask_followup",
            tool_calls=tool_calls,
            todo_list=todo_items,
        )
        yield _turn_complete_payload(response)
        return response

    # Exhausted safety cap — check if exploration was attempted but failed
    elapsed = time.monotonic() - turn_start_time
    logger.warning("Safety cap exhausted after %d rounds (%.2fs), forcing fallback plan", round_index, elapsed)
    slog.ai_thinking("safety_cap_exhausted", data={
        "round_index": round_index,
        "elapsed_seconds": round(elapsed, 2),
        "tool_call_count": len(tool_calls),
    }, session_id=planning_session_id, level=logging.WARNING)
    page_elements = _extract_page_elements(tool_calls)
    exploration_error = _extract_exploration_error(tool_calls)
    if exploration_error and not page_elements:
        response = _error_response(
            requirements=requirements,
            tool_calls=tool_calls,
            error_type="exploration_failed",
            error_detail=f"页面探索失败（{exploration_error}），无法生成有效的测试方案。请检查入口 URL 是否可访问后重试。",
            phase="tool_call",
        )
        yield _turn_complete_payload(response)
        return response
    response = _run_fallback_turn(
        transcript=transcript,
        requirements=requirements,
        assistant_message="我先根据当前上下文整理一版测试方案。",
        force_generate=True,
        tool_calls=tool_calls,
    )

    # Persist tool results before returning (for fallback turns)
    if planning_session_id:
        from app.models import AIPlanningToolResult
        from app.models.ai_planning_message import AIPlanningMessage
        _HEAVY_TOOLS_SET = {"explore_page", "explore_flow"}
        for tc in tool_calls:
            if tc.tool not in _HEAVY_TOOLS_SET:
                continue
            compressed = getattr(tc, "_compressed_result", None)
            if compressed is None:
                continue
            # Check if already persisted (avoid duplicates)
            existing = db_session.scalar(
                select(AIPlanningToolResult)
                .where(AIPlanningToolResult.session_id == planning_session_id)
                .where(AIPlanningToolResult.tool_name == tc.tool)
                .order_by(AIPlanningToolResult.id.desc())
                .limit(1)
            )
            if existing is not None:
                continue
            # Find or create a message_id for this tool call
            msg = db_session.scalar(
                select(AIPlanningMessage)
                .where(AIPlanningMessage.session_id == planning_session_id)
                .where(AIPlanningMessage.role == "assistant")
                .where(AIPlanningMessage.turn_type == "tool_call")
                .order_by(AIPlanningMessage.id.desc())
                .limit(1)
            )
            raw_json = tc.result if isinstance(tc.result, dict) else None
            db_session.add(AIPlanningToolResult(
                session_id=planning_session_id,
                message_id=msg.id if msg else None,
                tool_name=tc.tool,
                raw_result_json=raw_json,
                summary_json=compressed,
            ))
            logger.info(
                "[persist_tool_result_fallback] Saved %s result to AIPlanningToolResult: session=%d, raw_keys=%s",
                tc.tool, planning_session_id, list(raw_json.keys()) if raw_json else None,
            )
        db_session.flush()

    yield _turn_complete_payload(response)
    return response


def _planning_llm_enabled(settings: Any) -> bool:
    return bool(
        getattr(settings, "enable_ai_planning", False)
        and getattr(settings, "ai_planning_model", None)
        and getattr(settings, "ai_planning_api_key", None)
    )


def _is_new_requirement_intent(user_message: str) -> bool:
    return any(kw in user_message for kw in _NEW_REQUIREMENT_KEYWORDS)


def _build_context_summary(
    requirements: AIPlanningRequirements,
    plan: AIPlanningPlan | None,
    tool_calls: list[AIPlanningToolCall],
) -> str:
    parts = ["[历史对话摘要]"]

    filled = {}
    labels = {
        "app_under_test": "被测系统",
        "business_goal": "业务目标",
        "entry_url_or_page": "入口页面",
        "core_user_flow": "核心流程",
        "main_assertions": "关键断言",
        "test_data_or_account": "测试数据",
        "scope_limits": "范围限制",
    }
    for slot in REQUIRED_REQUIREMENT_SLOTS:
        val = getattr(requirements, slot, None)
        if slot == "main_assertions":
            if val:
                filled[labels[slot]] = ", ".join(val)
        elif val and str(val).strip():
            filled[labels[slot]] = str(val).strip()
    if filled:
        parts.append("- 用户需求：" + "；".join(f"{k}：{v}" for k, v in filled.items()))

    if plan:
        scenario_titles = ", ".join(s.title for s in plan.scenarios) if plan.scenarios else "无"
        parts.append(f"- 已有方案：{plan.summary}（场景：{scenario_titles}）")

    if tool_calls:
        explore_count = sum(1 for c in tool_calls if c.tool in ("explore_page", "explore_flow"))
        total = len(tool_calls)
        if explore_count:
            parts.append(f"- 已产生的结果：共调用 {total} 次工具，其中 {explore_count} 次页面采集")
        else:
            parts.append(f"- 已产生的结果：共调用 {total} 次工具")

    return "\n".join(parts)


def _prepare_transcript_for_llm(
    transcript: list[dict[str, str]],
    *,
    requirements: AIPlanningRequirements | None = None,
    plan: AIPlanningPlan | None = None,
    tool_calls: list[AIPlanningToolCall] | None = None,
) -> tuple[list[dict[str, str]], bool]:
    force_generate = False
    prepared: list[dict[str, str]] = []
    for item in transcript:
        role = item.get("role") or "user"
        content = item.get("content") or ""
        if role == "user" and FORCE_GENERATE_MARKER in content:
            force_generate = True
            content = content.replace(FORCE_GENERATE_MARKER, "").strip()
            content = f"{FORCE_GENERATE_HINT}{content}"
        prepared.append({"role": role, "content": content})

    # --- Context compression ---
    settings = get_settings()
    threshold = settings.ai_planning_context_compress_threshold
    keep_recent = settings.ai_planning_context_keep_recent
    if len(prepared) > threshold and requirements is not None:
        last_user_msg = ""
        for msg in reversed(prepared):
            if msg.get("role") == "user":
                last_user_msg = msg.get("content", "")
                break
        if _is_new_requirement_intent(last_user_msg):
            summary = _build_context_summary(requirements, plan, tool_calls or [])
            recent = prepared[-keep_recent:]
            logger.info(
                "Compressing context: %d messages -> summary + %d recent",
                len(prepared), len(recent),
            )
            prepared = [{"role": "system", "content": summary}, *recent]

    return prepared, force_generate


def _should_enable_thinking_mode(*, base_url: str, model: str) -> bool:
    normalized_base_url = base_url.strip().casefold()
    normalized_model = model.strip().casefold()
    return (
        "open.bigmodel.cn" in normalized_base_url
        or normalized_model.startswith("glm-")
    )


def _log_cache_usage(raw_payload: dict, model: str) -> None:
    """Log DeepSeek KV cache hit/miss metrics from the response usage."""
    usage = raw_payload.get("usage", {})
    hit = usage.get("prompt_cache_hit_tokens", 0)
    miss = usage.get("prompt_cache_miss_tokens", 0)
    total = usage.get("prompt_tokens", 0)
    if hit or miss:
        ratio = hit / (hit + miss) * 100 if (hit + miss) > 0 else 0
        logger.info(
            "DS cache: model=%s hit=%d miss=%d ratio=%.0f%% total_prompt=%d completion=%d",
            model, hit, miss, ratio,
            total, usage.get("completion_tokens", 0),
        )


def _build_cache_progress_message(
    tool_calls: list[AIPlanningToolCall],
) -> str | None:
    """Build 'already explored URLs' system message from past explore calls."""
    explored: list[dict[str, Any]] = []
    for tc in tool_calls:
        if tc.tool not in ("explore_page", "explore_flow"):
            continue
        result = tc.result
        if not isinstance(result, dict):
            continue
        url = result.get("url", "")
        nodes = result.get("a11y_nodes", result.get("elements", []))
        count = len(nodes) if isinstance(nodes, list) else result.get("element_count", 0)
        if url:
            explored.append({"url": url, "count": count})
    if not explored:
        return None
    lines = ["[Cache progress — already explored this session]"]
    for e in explored:
        lines.append(f"  - {e['url']} ({e['count']} nodes)")
    lines.append("Do NOT call explore_page/explore_flow on these URLs unless state changed.")
    return "\n".join(lines)


def _call_planning_llm(
    *,
    messages: list[dict[str, Any]],
    api_key: str,
    model: str,
    base_url: str,
    timeout_seconds: float,
) -> str:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if _should_enable_thinking_mode(base_url=base_url, model=model):
        payload["thinking"] = {"type": "enabled"}
        payload["max_tokens"] = 65536
    else:
        payload["temperature"] = 0.1
        payload["response_format"] = {"type": "json_object"}
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    http_request = request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with request.urlopen(http_request, timeout=timeout_seconds) as response:
        raw_payload = json.loads(response.read().decode("utf-8"))
    _log_cache_usage(raw_payload, model)
    return _extract_message_content(raw_payload)


def _stream_planning_llm(
    *,
    messages: list[dict[str, Any]],
    api_key: str,
    model: str,
    base_url: str,
    timeout_seconds: float,
) -> Generator[dict[str, str], None, None]:
    """Yield streaming events from an SSE-based LLM API call.

    Yields:
        ``{"type": "text_chunk", "text": "..."}`` for each incremental chunk.
        ``{"type": "raw_response", "text": "..."}`` once at the end with the full text.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if _should_enable_thinking_mode(base_url=base_url, model=model):
        payload["thinking"] = {"type": "enabled"}
        payload["max_tokens"] = 65536
    else:
        payload["temperature"] = 0.1
        payload["response_format"] = {"type": "json_object"}
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    full_text: list[str] = []
    reasoning_text: list[str] = []
    yielded_reasoning_chars = 0
    with httpx.Client(timeout=timeout_seconds) as client:
        with client.stream(
            "POST",
            endpoint,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        ) as response:
            response.raise_for_status()
            for raw_line in response.iter_lines():
                if not raw_line or not raw_line.startswith("data: "):
                    continue
                data = raw_line.removeprefix("data: ").strip()
                if data == "[DONE]":
                    break
                try:
                    chunk_payload = json.loads(data)
                    delta = chunk_payload["choices"][0].get("delta", {})
                    chunk = delta.get("content")
                    reasoning = delta.get("reasoning_content")
                except (json.JSONDecodeError, KeyError, IndexError):
                    logger.debug("SSE parse error, raw_line (first 200 chars): %s", data[:200])
                    continue
                if reasoning:
                    reasoning_text.append(reasoning)
                    yielded_reasoning_chars += len(reasoning)
                    # Forward reasoning content to frontend so user sees activity
                    yield {"type": "text_chunk", "text": reasoning, "thinking": True}
                    # Yield throttled status for phase label updates
                    prev_bucket = (yielded_reasoning_chars - len(reasoning)) // 200
                    cur_bucket = yielded_reasoning_chars // 200
                    if cur_bucket > prev_bucket:
                        yield {"type": "status", "phase": "thinking", "message": "正在深度推理分析需求..."}
                if chunk:
                    full_text.append(chunk)
                    yield {"type": "text_chunk", "text": chunk}
    content_text = "".join(full_text)
    if not content_text.strip() and reasoning_text:
        content_text = "".join(reasoning_text)
        logger.warning("LLM produced only reasoning_content, no content; using reasoning as fallback (len=%d)", len(content_text))
    yield {"type": "raw_response", "text": content_text}


def _parse_llm_response(response_text: str) -> dict[str, Any] | None:
    # Strip lone surrogates from DeepSeek streaming response
    response_text = re.sub(r"[\udc80-\udfff]", "", response_text)
    repaired = _repair_json_text(response_text)
    try:
        payload = json.loads(_extract_json_object(repaired))
    except json.JSONDecodeError:
        logger.warning("Planning LLM returned unparseable JSON: %r", response_text[:300])
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _merge_requirements(requirements: AIPlanningRequirements, collected_info: Any) -> None:
    if not isinstance(collected_info, dict):
        return
    for field_name in REQUIRED_REQUIREMENT_SLOTS:
        incoming = collected_info.get(field_name)
        if field_name == "main_assertions":
            if isinstance(incoming, list):
                existing = list(requirements.main_assertions)
                for item in incoming:
                    normalized = str(item).strip()
                    if normalized and normalized not in existing:
                        existing.append(normalized)
                requirements.main_assertions = existing
            elif isinstance(incoming, str):
                if incoming.strip() and incoming.strip() not in requirements.main_assertions:
                    requirements.main_assertions.append(incoming.strip())
            continue
        if incoming in (None, ""):
            continue
        # Normalize list fields: join items as numbered list instead of Python repr
        if field_name == "core_user_flow" and isinstance(incoming, list):
            items = [str(item).strip().rstrip(";") for item in incoming if str(item).strip()]
            incoming = "; ".join(f"{i+1}. {item}" for i, item in enumerate(items))
        if isinstance(incoming, list):
            incoming = "; ".join(str(item).strip() for item in incoming if str(item).strip())
        current = getattr(requirements, field_name)
        if not current:
            setattr(requirements, field_name, str(incoming).strip())


def _merge_test_context(requirements: AIPlanningRequirements, test_context: Any) -> None:
    if not isinstance(test_context, dict):
        return
    existing = requirements.test_context or {}
    merged = {**existing, **{k: v for k, v in test_context.items() if v is not None}}
    requirements.test_context = merged


def _run_fallback_turn(
    *,
    transcript: list[dict[str, str]],
    requirements: AIPlanningRequirements,
    assistant_message: str | None,
    force_generate: bool,
    tool_calls: list[AIPlanningToolCall],
) -> AIPlanningTurnResponse:
    user_text = "\n".join(item["content"] for item in transcript if item.get("role") == "user")
    _fill_requirements_from_text(requirements, user_text)
    missing_slots = _collect_missing_slots(requirements)

    if missing_slots and not force_generate:
        question = assistant_message or _default_followup_question(requirements)
        return AIPlanningTurnResponse(
            assistant_message=question,
            session_status="collecting",
            requirements=requirements,
            missing_slots=missing_slots,
            suggested_questions=[question],
            plan=None,
            drafts=[],
            next_action="ask_followup",
            tool_calls=tool_calls,
        )

    return _plan_response(
        requirements=requirements,
        plan_payload=None,
        assistant_message=assistant_message or "信息已经足够，我先给出结构化测试方案。",
        tool_calls=tool_calls,
    )


def _extract_page_elements(tool_calls: list[AIPlanningToolCall]) -> str | None:
    """Extract formatted page elements from the last explore_page or explore_flow tool call."""
    for call in reversed(tool_calls):
        if call.tool in ("explore_page", "explore_flow") and isinstance(call.result, dict):
            formatted = call.result.get("formatted")
            if isinstance(formatted, str) and formatted.strip():
                return formatted
    return None


def _extract_exploration_error(tool_calls: list[AIPlanningToolCall]) -> str | None:
    """Extract error message from failed explore_page or explore_flow calls."""
    errors = []
    for call in reversed(tool_calls):
        if call.tool in ("explore_page", "explore_flow") and isinstance(call.result, dict):
            error = call.result.get("error")
            if error:
                errors.append(str(error))
            # "info" responses from no-project or similar non-data results
            info = call.result.get("info")
            if info and not call.result.get("elements") and not call.result.get("pages"):
                errors.append(str(info))
            # For explore_flow, check individual page errors
            pages = call.result.get("pages", [])
            page_errors = [p.get("error", "") for p in pages if p.get("error")]
            if page_errors:
                errors.extend(page_errors)
    return "; ".join(errors) if errors else None


def _count_explored_elements(tool_calls: list[AIPlanningToolCall]) -> int:
    """Return total element count across all explore_page/explore_flow calls."""
    total = 0
    for call in tool_calls:
        if call.tool == "explore_page" and isinstance(call.result, dict):
            total += int(call.result.get("element_count", 0))
        elif call.tool == "explore_flow" and isinstance(call.result, dict):
            for page in call.result.get("pages", []) or call.result.get("page_results", []):
                if isinstance(page, dict):
                    total += int(page.get("element_count", 0))
    return total


def _has_explored_pages(tool_calls: list[AIPlanningToolCall]) -> bool:
    """Return True if any explore_page or explore_flow call exists in tool_calls."""
    return any(call.tool in ("explore_page", "explore_flow") for call in tool_calls)


def _check_page_coverage(
    tool_calls: list[AIPlanningToolCall],
    core_user_flow: str | None,
) -> tuple[float, list[str]]:
    """Check page exploration coverage against core_user_flow.

    Returns (coverage_ratio, list_of_missing_page_hints).
    Coverage is estimated by counting flow keywords that appear in explored URLs.
    """
    if not core_user_flow:
        return 1.0, []

    # Extract page-related keywords from core_user_flow
    page_keywords = set()
    flow_lower = core_user_flow.lower()
    # Common page indicators
    page_patterns = [
        ("login", "登录"), ("register", "注册"), ("product", "商品"),
        ("cart", "购物车"), ("checkout", "结算"), ("search", "搜索"),
        ("home", "首页"), ("profile", "个人"), ("order", "订单"),
        ("brand", "品牌"), ("category", "分类"),
    ]
    for en, cn in page_patterns:
        if en in flow_lower or cn in flow_lower:
            page_keywords.add(en)

    if not page_keywords:
        return 1.0, []

    # Collect explored URLs
    explored_urls: set[str] = set()
    for call in tool_calls:
        if call.tool == "explore_page" and isinstance(call.result, dict):
            url = call.result.get("url", "")
            if url:
                explored_urls.add(url.lower())
        elif call.tool == "explore_flow" and isinstance(call.result, dict):
            for page in call.result.get("pages", []) or call.result.get("page_results", []):
                if isinstance(page, dict):
                    url = page.get("url", "")
                    if url:
                        explored_urls.add(url.lower())

    all_urls_text = " ".join(explored_urls)

    # Check which keywords are covered
    covered = []
    missing = []
    for keyword in page_keywords:
        if keyword in all_urls_text:
            covered.append(keyword)
        else:
            missing.append(keyword)

    coverage = len(covered) / len(page_keywords) if page_keywords else 1.0
    return coverage, missing


# Essential attributes to keep per element for compression
_HEAVY_TOOLS = {"explore_page", "explore_flow"}


def _compress_tool_result(tool_name: str, result: dict) -> dict:
    """Deterministic compression of exploration results for ReAct context.

    Now operates on a11y_nodes instead of raw page elements. No LLM call.
    """
    _KEEP_KEYS = {"node_id", "role", "name", "level", "parent_id", "focusable", "disabled"}

    def _compress_page(page_data: dict) -> dict:
        raw = page_data.get("a11y_nodes", page_data.get("elements", []))
        nodes = []
        for n in raw:
            if not isinstance(n, dict):
                continue
            nodes.append({k: v for k, v in n.items() if k in _KEEP_KEYS and v not in (None, "")})
        return {
            "url": page_data.get("url", ""),
            "page_state": page_data.get("page_state", ""),
            "element_count": len(raw),
            "nodes": nodes[:60],
            "warning": page_data.get("warning"),
        }

    if tool_name == "explore_page":
        return _compress_page(result)
    elif tool_name == "explore_flow":
        pages = result.get("pages", [])
        return {
            "pages": [_compress_page(p) for p in pages if isinstance(p, dict)],
            "total_pages": result.get("total_pages", len(pages)),
            "total_elements": result.get("total_elements", 0),
            "warning": result.get("warning"),
        }
    return {}


def _extract_raw_page_results(tool_calls: list[AIPlanningToolCall]) -> list[dict[str, Any]]:
    """Extract raw page-results list from the most recent explore tool call.

    Returns the ``pages`` list from ``explore_flow``, or a single-element
    list from ``explore_page`` (wrapping its elements).
    """
    for call in reversed(tool_calls):
        if not isinstance(call.result, dict):
            continue
        if call.tool == "explore_flow":
            pages = call.result.get("pages")
            if isinstance(pages, list):
                return pages
        elif call.tool == "explore_page":
            nodes = call.result.get("a11y_nodes", call.result.get("elements"))
            url = call.result.get("url", "")
            if isinstance(nodes, list):
                return [{"url": url, "a11y_nodes": nodes}]
    return []


def _auto_explore_entry_url(
    requirements: AIPlanningRequirements,
    tool_calls: list[AIPlanningToolCall],
    db_session: Session,
    project_id: int,
    *,
    actor_user_id: int = 0,
    planning_session_id: int = 0,
) -> tuple[bool, list[AIPlanningToolCall], list[str]]:
    """Auto-invoke explore_page on the entry URL and extract navigable links.

    Does NOT auto-call explore_flow — that decision is deferred to the LLM
    (the caller injects the link list into the conversation).

    Returns (explored, tool_calls, internal_links) where *explored*
    indicates whether exploration was triggered and *internal_links* is
    the list of same-domain URLs found on the entry page.
    """
    entry_url = requirements.entry_url_or_page
    if not entry_url or not isinstance(entry_url, str):
        return False, tool_calls, []

    match = URL_PATTERN.search(entry_url)
    if not match:
        return False, tool_calls, []

    base_url = match.group(0)

    # Check if explore_page was already called by the AI
    existing_explore_result = None
    for call in tool_calls:
        if call.tool == "explore_page" and isinstance(call.result, dict):
            existing_explore_result = call.result

    if existing_explore_result is None:
        try:
            tool_result_text = execute_tool(
                tool_name="explore_page",
                params={"url": base_url},
                db_session=db_session,
                project_id=project_id,
                actor_user_id=actor_user_id,
                planning_session_id=planning_session_id,
            )
            parsed_result = _safe_parse_json(tool_result_text)
        except Exception as exc:
            logger.warning("Auto-explore failed for url=%s: %s", base_url, exc)
            parsed_result = {"error": str(exc), "url": base_url}

        tool_calls.append(
            AIPlanningToolCall(
                tool="explore_page",
                params={"url": base_url},
                result=parsed_result,
            )
        )
    else:
        parsed_result = existing_explore_result

    internal_links = _extract_internal_links(parsed_result, base_url)
    logger.info("Auto-explore: found %d internal links from %s", len(internal_links), base_url)

    # Auto-explore login page when requirements indicate login flow
    if _looks_like_login_requirements(requirements):
        login_urls = [url for url in internal_links if _is_login_url(url)]
        # Filter out already-explored URLs
        explored_urls: set[str] = set()
        for call in tool_calls:
            if call.tool == "explore_page" and isinstance(call.params, dict):
                eu = (call.params.get("url") or "").strip().rstrip("/")
                if eu:
                    explored_urls.add(eu)
        login_urls = [u for u in login_urls if u.rstrip("/") not in explored_urls]
        if login_urls:
            login_url = login_urls[0]
            try:
                login_result_text = execute_tool(
                    tool_name="explore_page",
                    params={"url": login_url},
                    db_session=db_session,
                    project_id=project_id,
                    actor_user_id=actor_user_id,
                    planning_session_id=planning_session_id,
                )
                login_parsed = _safe_parse_json(login_result_text)
                tool_calls.append(
                    AIPlanningToolCall(
                        tool="explore_page",
                        params={"url": login_url},
                        result=login_parsed,
                    )
                )
                logger.info(
                    "Auto-explored login page: %s, found %d elements",
                    login_url, login_parsed.get("element_count", 0) if isinstance(login_parsed, dict) else 0,
                )
            except Exception as exc:
                logger.warning("Auto-explore login page failed for url=%s: %s", login_url, exc)

    return True, tool_calls, internal_links


def _extract_internal_links(
    explore_result: dict[str, Any] | None,
    base_url: str,
    *,
    max_links: int = 20,
) -> list[str]:
    """Extract navigable internal links from an explore_page result.

    Returns absolute URLs on the same domain as *base_url*, deduplicated
    by path.  Skips anchors, javascript: links, and mailto: links.
    No keyword scoring — link selection is left to the LLM.
    """
    if not explore_result or not isinstance(explore_result, dict):
        return []

    elements = explore_result.get("elements", [])
    if not isinstance(elements, list):
        return []

    from urllib.parse import urljoin, urlparse

    base_parsed = urlparse(base_url)
    base_origin = f"{base_parsed.scheme}://{base_parsed.netloc}"
    seen_paths: set[str] = {base_parsed.path or "/"}
    links: list[str] = []

    for elem in elements:
        if not isinstance(elem, dict):
            continue
        tag = elem.get("tag", "")
        if tag != "a":
            continue
        href = elem.get("href") or ""
        if not href or href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
            continue

        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)

        if parsed.netloc != base_parsed.netloc:
            continue

        path = parsed.path or "/"
        if path in seen_paths:
            continue

        seen_paths.add(path)
        clean_url = f"{parsed.scheme}://{parsed.netloc}{path}"
        links.append(clean_url)

    return links[:max_links]


# ---------------------------------------------------------------------------
# Link-aware ReAct helpers (BUG-059)
# These functions implement the link-list presentation / tracking protocol
# that lets the LLM decide which pages to explore instead of relying on a
# hardcoded keyword table.
# ---------------------------------------------------------------------------

# Sentinel prefix injected into system messages to mark link-list presentation.
_LINK_PRESENTATION_SENTINEL = "⟨LINK_LIST⟩"

# Prefix used to serialize presented URLs for later retrieval.
_LINK_URL_PREFIX = "⟨URL⟩"


def _build_link_selection_message(
    links: list[str],
    core_user_flow: str | None,
) -> str:
    """Build a system message presenting navigable links for LLM selection."""
    if not links:
        return "入口页面未发现其他内部导航链接。请基于已采集的入口页面元素生成测试方案，或向用户询问更多页面信息。"

    flow_hint = ""
    if core_user_flow:
        flow_hint = f'用户的核心操作流程是："{core_user_flow}"\n'

    numbered = "\n".join(f"{i+1}. {url}" for i, url in enumerate(links))

    return (
        f"{_LINK_PRESENTATION_SENTINEL}\n"
        + "\n".join(f"{_LINK_URL_PREFIX}{url}" for url in links)
        + f"\n\n系统已采集入口页面的可交互元素，并发现以下内部导航链接：\n\n"
        f"{numbered}\n\n"
        f"{flow_hint}"
        f"请根据用户的核心操作流程，选择需要进一步采集元素和布局信息的页面。\n"
        f'调用 explore_flow 工具，在 steps 参数中传入你选择的步骤列表（建议 2-5 个）。\n'
        f"如果流程涉及登录、筛选、加购等交互，必须在 steps 中包含对应的 actions。\n"
        f"不要在没有探索完所有流程涉及页面的情况下调用 generate_plan。"
    )


def _track_link_presentation(
    conversation: list[dict[str, str]],
    links: list[str],
) -> None:
    """No-op — link data is already embedded in the system message.

    The sentinel prefix in the message text is used by
    _was_link_list_presented / _get_presented_links for retrieval.
    """


def _was_link_list_presented(conversation: list[dict[str, str]]) -> bool:
    """Return True if a link-list message was already injected."""
    for msg in conversation:
        if msg.get("role") == "system" and _LINK_PRESENTATION_SENTINEL in msg.get("content", ""):
            return True
    return False


def _get_presented_links(conversation: list[dict[str, str]]) -> list[str]:
    """Extract the URL list from the most recent link-presentation message."""
    for msg in reversed(conversation):
        if msg.get("role") == "system" and _LINK_PRESENTATION_SENTINEL in msg.get("content", ""):
            import re
            return re.findall(rf"{re.escape(_LINK_URL_PREFIX)}(\S+)", msg["content"])
    return []


def _clear_link_tracking(conversation: list[dict[str, str]]) -> None:
    """Remove the link-presentation sentinel from the last such message."""
    for msg in reversed(conversation):
        if msg.get("role") == "system" and _LINK_PRESENTATION_SENTINEL in msg.get("content", ""):
            msg["content"] = msg["content"].split("\n\n", 1)[-1] if "\n\n" in msg["content"] else msg["content"]
            return


def _build_safety_net_steps(
    urls: list[str],
    core_user_flow: str | None,
) -> list[dict[str, Any]]:
    """Convert a plain URL list into flow steps with descriptive labels.

    Each step gets a ``description`` inferred from the URL path so that
    downstream ``collect_flow_elements`` can assign ``page_state`` markers.
    This ensures the formatted ``page_elements`` uses structured
    ``=== 页面状态 S{n}: {url}（描述）===`` headers that the DSL generator
    can parse and filter by step.
    """
    _URL_LABELS: dict[str, str] = {
        "login": "登录页",
        "signup": "注册页",
        "products": "商品列表页",
        "product_details": "商品详情页",
        "brand_products": "品牌筛选结果页",
        "view_cart": "购物车页",
        "checkout": "结账页",
        "payment": "支付页",
        "contact_us": "联系我们",
        "search": "搜索结果页",
    }
    steps: list[dict[str, Any]] = []
    for url in urls:
        url_clean = url.strip().rstrip("/")
        label = ""
        for keyword, desc in _URL_LABELS.items():
            if keyword in url_clean.lower():
                label = desc
                break
        if not label:
            label = url_clean.rsplit("/", 1)[-1] or url_clean
        step: dict[str, Any] = {"url": url, "description": label}
        steps.append(step)

    if core_user_flow and ("登录" in core_user_flow or "login" in core_user_flow.lower()):
        for step in steps:
            if "login" in step.get("url", "").lower():
                step["description"] = "登录页（含登录表单）"
                break
    if core_user_flow and ("购物车" in core_user_flow or "cart" in core_user_flow.lower()):
        for step in steps:
            if "cart" in step.get("url", "").lower():
                step["description"] = "购物车页（含商品列表和数量）"
                break

    return steps


def _has_internal_links_in_tool_calls(tool_calls: list[AIPlanningToolCall]) -> bool:
    """Check whether any explore_page result contains internal links."""
    return bool(_extract_links_from_tool_calls(tool_calls, None))


def _extract_links_from_tool_calls(
    tool_calls: list[AIPlanningToolCall],
    requirements: AIPlanningRequirements | None,
) -> list[str]:
    """Extract internal links from the last explore_page tool call result."""
    from urllib.parse import urlparse

    for call in reversed(tool_calls):
        if call.tool != "explore_page" or not isinstance(call.result, dict):
            continue
        url = (call.params or {}).get("url", "") or call.result.get("url", "")
        if not url:
            continue
        # Parse base_url from the explore result
        base_url = url
        parsed = urlparse(base_url)
        if not parsed.netloc:
            continue
        return _extract_internal_links(call.result, base_url)
    return []


def _plan_response(
    *,
    requirements: AIPlanningRequirements,
    plan_payload: dict[str, Any] | None,
    assistant_message: str,
    tool_calls: list[AIPlanningToolCall],
    todo_list: list[AIPlanningTodoItem] | None = None,
) -> AIPlanningTurnResponse:
    page_elements = _extract_page_elements(tool_calls)
    plan = (
        _coerce_plan(plan_payload, requirements, page_elements=page_elements)
        if plan_payload
        else _build_plan(requirements, page_elements=page_elements)
    )
    return AIPlanningTurnResponse(
        assistant_message=assistant_message,
        session_status="plan_ready",
        requirements=requirements,
        missing_slots=[],
        suggested_questions=[],
        plan=plan,
        drafts=[],
        next_action="select_scenarios",
        tool_calls=tool_calls,
        todo_list=todo_list or [],
    )


def _coerce_plan(plan_payload: dict[str, Any], requirements: AIPlanningRequirements, *, page_elements: str | None = None) -> AIPlanningPlan:
    candidate = dict(plan_payload)
    if "summary" not in candidate or "scenarios" not in candidate:
        return _build_plan(requirements, page_elements=page_elements)
    try:
        plan = AIPlanningPlan.model_validate(candidate)
        # Fill in missing test_data_requirements and assertions from requirements
        assertions = requirements.main_assertions or ["页面状态符合预期"]
        is_login = _looks_like_login(requirements)
        test_data_reqs = _build_test_data_requirements(requirements, is_login=is_login)
        updated_scenarios = []
        for s in plan.scenarios:
            updates = {}
            if page_elements and s.page_elements is None:
                updates["page_elements"] = page_elements
            if not s.assertions:
                updates["assertions"] = assertions
            if not s.test_data_requirements:
                updates["test_data_requirements"] = test_data_reqs
            if updates:
                s = s.model_copy(update=updates)
            updated_scenarios.append(s)
        plan = plan.model_copy(update={"scenarios": updated_scenarios})
        return plan
    except Exception:
        logger.warning("Planning LLM returned invalid plan payload, fallback to deterministic plan.")
        return _build_plan(requirements, page_elements=page_elements)


def _error_response(
    *,
    requirements: AIPlanningRequirements,
    tool_calls: list[AIPlanningToolCall],
    error_type: str = "unknown",
    error_detail: str = "",
    phase: str = "unknown",
) -> AIPlanningTurnResponse:
    suggestions: dict[str, str] = {
        "llm_call": "请检查 AI 模型配置（API Key、Base URL、Model 名称）是否正确，以及模型服务是否可用。",
        "json_parse": "AI 模型返回了无法解析的内容，请检查模型是否支持 JSON 输出模式（response_format=json_object）。",
        "tool_call": "工具调用执行失败，请查看后端日志获取详细堆栈信息。",
        "timeout": "AI 模型调用超时，请检查网络连接或增大超时时间配置。",
        "connection": "无法连接到 AI 模型服务，请检查 Base URL 是否正确、服务是否在运行。",
    }
    suggestion = suggestions.get(phase, "请查看后端日志获取详细错误信息。")
    detail_parts = [f"阶段: {phase}"]
    if error_type != "unknown":
        detail_parts.append(f"错误类型: {error_type}")
    if error_detail:
        detail_parts.append(f"详细信息: {error_detail}")
    detail_parts.append(f"建议: {suggestion}")
    full_message = "AI 规划过程中遇到错误。\n" + "\n".join(detail_parts)
    logger.error("Planning error: type=%s, phase=%s, detail=%s", error_type, phase, error_detail[:500])
    return AIPlanningTurnResponse(
        assistant_message=full_message,
        session_status="error",
        requirements=requirements,
        missing_slots=_collect_missing_slots(requirements),
        suggested_questions=[],
        plan=None,
        drafts=[],
        next_action="ask_followup",
        tool_calls=tool_calls,
    )


def _default_followup_question(requirements: AIPlanningRequirements) -> str:
    missing_slots = _collect_missing_slots(requirements)
    if not missing_slots:
        return "如果信息足够，我可以直接开始生成测试方案。"
    labels = {
        "app_under_test": "被测系统或业务模块",
        "business_goal": "本次测试的业务目标",
        "entry_url_or_page": "入口页面或 URL",
        "core_user_flow": "核心操作流程",
        "main_assertions": "关键断言",
        "test_data_or_account": "测试数据或账号",
        "scope_limits": "范围限制",
    }
    first_two = [labels[item] for item in missing_slots[:2]]
    return f"还需要你补充 { ' 和 '.join(first_two) }，我再继续规划。"


def _build_analysis_message(analysis: Any) -> str:
    lines = ["执行结果分析：\n"]
    conclusion_labels = {
        "all_passed": "全部通过",
        "partial": "部分通过",
        "all_failed": "全部失败",
    }
    lines.append(f"本轮结论：{conclusion_labels.get(getattr(analysis, 'conclusion', ''), '未知')}")
    for cr in getattr(analysis, "case_results", []):
        status_icon = "✅" if cr.status == "passed" else "❌"
        lines.append(f"  {status_icon} {cr.case_name} — {cr.status} ({cr.passed_steps}/{cr.total_steps}步)")
    for fd in getattr(analysis, "failure_details", []):
        lines.append(f"  ⚠ 失败点：{fd.case_name} 步骤{fd.step_index}({fd.action}) — {fd.suspected_cause}")
    if getattr(analysis, "suspected_root_cause", None):
        lines.append(f"疑似根因：{analysis.suspected_root_cause}")
    if getattr(analysis, "recommended_action", None):
        action_labels = {
            "targeted_retest": "针对性复测",
            "regression": "回归测试",
            "manual": "人工介入",
            "done": "测试完成",
        }
        lines.append(f"建议下一步：{action_labels.get(analysis.recommended_action, analysis.recommended_action)}")
        if getattr(analysis, "recommended_scope", None):
            scope_labels = {"current": "仅当前用例", "adjacent": "相邻流程", "module": "模块级", "core": "核心链路"}
            lines.append(f"回归范围：{scope_labels.get(analysis.recommended_scope, analysis.recommended_scope)}")
    return "\n".join(lines)


def _collect_missing_slots(requirements: AIPlanningRequirements) -> list[str]:
    return [slot for slot in REQUIRED_REQUIREMENT_SLOTS if _slot_is_missing(requirements, slot)]


def _extract_message_content(payload: dict[str, Any]) -> str:
    message = payload.get("choices", [{}])[0].get("message", {})
    content = message.get("content", "")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
        result = "\n".join(text_parts)
        if result.strip():
            return result
    reasoning = message.get("reasoning_content", "")
    if isinstance(reasoning, str) and reasoning.strip():
        logger.warning("LLM produced empty content; using reasoning_content as fallback (len=%d)", len(reasoning))
        return reasoning
    if isinstance(content, str):
        return content
    return ""


def _extract_json_object(response_text: str) -> str:
    stripped = response_text.strip()
    if not stripped:
        return stripped
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            end_fence = stripped.rfind("```", first_newline)
            if end_fence > first_newline:
                stripped = stripped[first_newline + 1 : end_fence].strip()

    in_string = False
    escape_next = False
    depth = 0
    start = -1
    for index, char in enumerate(stripped):
        if escape_next:
            escape_next = False
            continue
        if in_string:
            if char == "\\":
                escape_next = True
            elif char == '"':
                in_string = False
            continue
        if char == '"' and depth > 0:
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start != -1:
                return stripped[start : index + 1]
    return stripped


def _repair_json_text(text: str) -> str:
    """Pre-process AI JSON output before _extract_json_object handles fences and extraction.

    NOTE: The trailing-comma regex does NOT track string boundaries. If a JSON string
    value contains the literal substring `, }` or `, ]`, the comma will be silently
    removed and the string corrupted. This is an accepted trade-off because LLM-generated
    JSON in this codebase uses short keyword-like values where this is exceedingly rare.
    """
    stripped = text.strip()
    # Remove trailing commas before } or ] (most common LLM JSON formatting error)
    stripped = re.sub(r",\s*(\}|\])", r"\1", stripped)
    return stripped


def _safe_parse_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _normalize_json_text(response_text: str) -> str:
    extracted = _extract_json_object(response_text)
    try:
        return json.dumps(json.loads(extracted), ensure_ascii=False)
    except json.JSONDecodeError:
        return response_text.strip()


def _fill_requirements_from_text(requirements: AIPlanningRequirements, text: str) -> None:
    if not requirements.app_under_test:
        requirements.app_under_test = _extract_after_keyword(text, ["被测系统是", "系统是", "应用是"])
    if not requirements.business_goal:
        requirements.business_goal = _extract_after_keyword(text, ["业务目标是", "目标是"])
    if not requirements.entry_url_or_page:
        requirements.entry_url_or_page = _extract_url(text) or _extract_after_keyword(text, ["入口页面是", "入口是", "页面是"])
    if not requirements.core_user_flow:
        requirements.core_user_flow = _extract_after_keyword(text, ["核心流程是", "流程是", "操作流程是"])
    if not requirements.main_assertions:
        assertions = _extract_after_keyword(text, ["主要断言是", "断言是", "预期是"])
        if assertions:
            requirements.main_assertions = _split_items(assertions)
    if not requirements.test_data_or_account:
        requirements.test_data_or_account = _extract_after_keyword(
            text,
            ["测试数据使用", "测试数据是", "测试账号是", "使用管理员账号"],
        )
    if not requirements.scope_limits:
        requirements.scope_limits = _extract_after_keyword(text, ["范围限制是", "限制是", "不覆盖"])


def _extract_after_keyword(text: str, keywords: list[str]) -> str | None:
    for keyword in keywords:
        pattern = re.compile(rf"{re.escape(keyword)}(.+?)(?:[。；;\n]|$)")
        match = pattern.search(text)
        if match:
            value = match.group(1).strip(" ：:")
            if value:
                return value
    return None


def _extract_url(text: str) -> str | None:
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None


def _split_items(value: str) -> list[str]:
    items = re.split(r"[，、]|(?:\s+and\s+)|(?:\s+并且\s+)", value)
    return [item.strip() for item in items if item.strip()]


def _slot_is_missing(requirements: AIPlanningRequirements, slot: str) -> bool:
    value = getattr(requirements, slot)
    if isinstance(value, list):
        return not value
    return not bool(value and str(value).strip())


def _build_plan(requirements: AIPlanningRequirements, *, page_elements: str | None = None) -> AIPlanningPlan:
    assertions = requirements.main_assertions or ["页面状态符合预期"]
    is_login = _looks_like_login(requirements)
    flow_label = "登录" if is_login else "核心流程"

    # Extract flow_steps from core_user_flow for DSL generation
    flow_steps = _extract_flow_steps_from_core_flow(
        requirements.core_user_flow,
        requirements.entry_url_or_page,
    )

    scenarios = [
        # 1. Happy path
        AIPlanningScenario(
            scenario_key="login_success" if is_login else "primary_flow_success",
            title=f"{flow_label}成功",
            goal=requirements.business_goal or "验证主流程可以正常通过",
            preconditions=[
                requirements.entry_url_or_page or "提供有效入口页面",
                requirements.test_data_or_account or "准备可用测试数据",
            ],
            priority="high",
            test_data_requirements=_build_test_data_requirements(requirements, is_login=is_login),
            assertions=assertions,
            draft_prompt=_build_draft_prompt(requirements, scenario_title=f"{flow_label}成功", negative_case=False, page_elements=page_elements),
            page_elements=page_elements,
            flow_steps=flow_steps,
        ),
        # 2. Input validation / exception
        AIPlanningScenario(
            scenario_key="login_error" if is_login else "primary_flow_validation",
            title=f"{flow_label}异常处理",
            goal=f"验证{flow_label}流程在异常输入下的兜底行为",
            preconditions=[requirements.entry_url_or_page or "提供有效入口页面"],
            priority="medium",
            test_data_requirements=_build_test_data_requirements(requirements, is_login=is_login),
            assertions=["错误提示符合预期", *assertions[:1]],
            draft_prompt=_build_draft_prompt(requirements, scenario_title=f"{flow_label}异常处理", negative_case=True, page_elements=page_elements),
            page_elements=page_elements,
            flow_steps=flow_steps,
        ),
        # 3. Data consistency / cross-page verification
        AIPlanningScenario(
            scenario_key="data_consistency",
            title="数据一致性验证",
            goal="验证跨步骤数据传递和状态保持的正确性",
            preconditions=[
                requirements.entry_url_or_page or "提供有效入口页面",
                requirements.test_data_or_account or "准备可用测试数据",
            ],
            priority="medium",
            test_data_requirements=_build_test_data_requirements(requirements, is_login=is_login),
            assertions=["跨页面数据一致", "状态转换符合预期", *assertions[:2]],
            draft_prompt=_build_draft_prompt(requirements, scenario_title="数据一致性验证", negative_case=False, page_elements=page_elements),
            page_elements=page_elements,
            flow_steps=flow_steps,
        ),
        # 4. Boundary / edge case
        AIPlanningScenario(
            scenario_key="boundary_conditions",
            title="边界条件测试",
            goal="验证系统在边界输入下的健壮性",
            preconditions=[requirements.entry_url_or_page or "提供有效入口页面"],
            priority="low",
            test_data_requirements=_build_test_data_requirements(requirements, is_login=is_login),
            assertions=["边界输入处理正确", "无异常崩溃"],
            draft_prompt=_build_draft_prompt(requirements, scenario_title="边界条件测试", negative_case=True, page_elements=page_elements),
            page_elements=page_elements,
            flow_steps=flow_steps,
        ),
    ]
    assumptions = []
    if requirements.entry_url_or_page:
        assumptions.append(f"入口页面使用 {requirements.entry_url_or_page}")
    if requirements.test_data_or_account:
        assumptions.append(f"测试数据以 {requirements.test_data_or_account} 为准")
    if not assumptions:
        assumptions.append("部分上下文缺失，方案基于当前对话做合理假设")
    risks = [requirements.scope_limits] if requirements.scope_limits else ["仍需补充范围限制与边界条件"]
    return AIPlanningPlan(
        summary=f"{requirements.app_under_test or '待补充系统'} - {requirements.business_goal or '测试规划'}",
        assumptions=assumptions,
        risks=risks,
        scenarios=scenarios,
    )


def _extract_flow_steps_from_core_flow(core_user_flow: str | None, entry_url: str | None = None) -> list[dict[str, Any]]:
    """Extract flow_steps from core_user_flow text for fallback plan generation.

    Parses numbered steps like "1. 打开首页点击 Signup / Login" into flow_steps format.
    """
    if not core_user_flow:
        return []

    import re
    steps: list[dict[str, Any]] = []
    # Match numbered steps: "1. text" or "1、text"
    step_pattern = re.compile(r"(\d+)[.、]\s*(.+?)(?=\d+[.、]|$)", re.DOTALL)
    matches = step_pattern.findall(core_user_flow)

    current_page_state = "S0"
    for i, (_, text) in enumerate(matches[:20]):  # Limit to 20 steps
        text = text.strip()
        if not text:
            continue

        # Determine action from text
        action = "click"  # Default action
        target = None
        value = None

        text_lower = text.lower()
        if any(kw in text_lower for kw in ["打开", "进入", "访问", "导航", "goto", "navigate"]):
            action = "goto"
            # Extract URL if present
            url_match = re.search(r'https?://[^\s，。；;]+', text)
            if url_match:
                target = url_match.group(0)
            else:
                target = text
        elif any(kw in text_lower for kw in ["输入", "填写", "输入框", "input", "fill", "type"]):
            action = "input"
            target = text
        elif any(kw in text_lower for kw in ["验证", "检查", "确认", "assert", "verify", "check"]):
            action = "assert_text"
            target = text
        elif any(kw in text_lower for kw in ["等待", "wait"]):
            action = "wait_for"
            target = text
        else:
            action = "click"
            target = text

        steps.append({
            "step_index": i + 1,
            "action": action,
            "target": target,
            "value": value,
            "page_state": current_page_state,
        })

        # Update page state after navigation actions
        if action == "goto":
            current_page_state = f"S{len([s for s in steps if s.get('action') == 'goto'])}"

    return steps


_EMAIL_PATTERN = re.compile(r'\S+@\S+\.\S+')

_LOGIN_URL_KEYWORDS = ("/login", "/signin", "/sign-in", "/sign_in", "/auth")


def _looks_like_login(requirements: AIPlanningRequirements) -> bool:
    haystack = " ".join(filter(None, [requirements.business_goal, requirements.core_user_flow, requirements.entry_url_or_page]))
    lowered = haystack.casefold()
    return "登录" in haystack or "login" in lowered or "signin" in lowered


def _looks_like_login_requirements(requirements: AIPlanningRequirements) -> bool:
    """Check whether requirements imply a login flow — from flow text or credentials in test_data."""
    if _looks_like_login(requirements):
        return True
    test_data = (requirements.test_data_or_account or "").casefold()
    return bool(_EMAIL_PATTERN.search(test_data)) and (
        "password" in test_data or "密码" in test_data or "123456" in test_data
    )


def _is_login_url(url: str) -> bool:
    """Check whether a URL path suggests a login/sign-in page."""
    from urllib.parse import urlparse
    path = urlparse(url).path.casefold()
    return any(kw in path for kw in _LOGIN_URL_KEYWORDS)


def _rank_links_by_flow_relevance(
    links: list[str],
    core_user_flow: str | None,
) -> list[str]:
    """Rank URLs by keyword overlap with core_user_flow.

    Extracts keywords from flow text, then scores URLs whose path segments
    match those keywords.
    """
    if not core_user_flow or not links:
        return list(links)
    from urllib.parse import urlparse
    flow_lowered = core_user_flow.casefold()

    # Path-to-flow keyword mapping: which path keywords indicate relevance
    # for specific flow-intent keywords
    relevance_map: dict[str, list[str]] = {
        "login": ["login", "signin", "sign-in", "登录"],
        "signin": ["login", "signin", "sign-in", "登录"],
        "登录": ["login", "signin", "sign-in", "登录"],
        "product": ["product", "商品", "category"],
        "products": ["product", "商品", "category"],
        "商品": ["product", "商品", "category"],
        "brand": ["brand", "品牌"],
        "品牌": ["brand", "品牌"],
        "cart": ["cart", "购物车"],
        "购物车": ["cart", "购物车"],
        "contact": ["contact", "联系我们"],
        "联系我们": ["contact", "联系我们"],
        "register": ["register", "signup", "注册"],
        "注册": ["register", "signup", "注册"],
        "search": ["search", "搜索"],
        "搜索": ["search", "搜索"],
        "checkout": ["checkout", "结账"],
        "结账": ["checkout", "结账"],
    }

    # Determine which path keywords are relevant based on flow text
    relevant_path_keywords: set[str] = set()
    for flow_kw, path_kws in relevance_map.items():
        if flow_kw in flow_lowered:
            relevant_path_keywords.update(path_kws)

    def _score(url: str) -> int:
        path = urlparse(url).path.casefold()
        score = 0
        for kw in relevant_path_keywords:
            if kw in path:
                score += 1
        return score

    return sorted(links, key=_score, reverse=True)


_ASK_EXPLORABLE_KEYWORDS = (
    "login", "email", "password", "locator", "selector",
    "定位", "登录", "邮箱", "密码", "选择器", "元素",
)


def _is_asking_about_explorable_elements(message: str) -> bool:
    """Check whether the agent is asking about elements that can be discovered by exploration."""
    lowered = message.casefold()
    return any(kw in lowered for kw in _ASK_EXPLORABLE_KEYWORDS)


def _find_unexplored_login_url(
    tool_calls: list[AIPlanningToolCall],
    requirements: AIPlanningRequirements,
) -> str | None:
    """Find a login URL from explore results that hasn't been explored yet."""
    from urllib.parse import urljoin, urlparse

    explored_urls: set[str] = set()
    for call in tool_calls:
        if call.tool == "explore_page" and isinstance(call.params, dict):
            eu = (call.params.get("url") or "").strip().rstrip("/")
            if eu:
                explored_urls.add(eu)

    # Extract the base URL from requirements
    entry_url = requirements.entry_url_or_page or ""
    match = URL_PATTERN.search(entry_url)
    base_url = match.group(0) if match else ""

    # Check already-explored results for internal links containing login URLs
    for call in tool_calls:
        if call.tool != "explore_page" or not isinstance(call.result, dict):
            continue
        # Get the URL that was explored
        explored_url = (call.params or {}).get("url", "") or call.result.get("url", base_url)
        elements = call.result.get("elements", [])
        if not isinstance(elements, list):
            continue
        base_parsed = urlparse(base_url or explored_url)
        base_origin = f"{base_parsed.scheme}://{base_parsed.netloc}"
        for elem in elements:
            if not isinstance(elem, dict):
                continue
            if elem.get("tag") != "a":
                continue
            href = elem.get("href") or ""
            if not href or href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
                continue
            abs_url = urljoin(base_url or explored_url, href)
            if urlparse(abs_url).netloc != base_parsed.netloc:
                continue
            clean_url = abs_url.rstrip("/")
            if _is_login_url(abs_url) and clean_url not in explored_urls:
                return abs_url
    return None


def _auto_explore_entry_and_find_login(
    requirements: AIPlanningRequirements,
    tool_calls: list[AIPlanningToolCall],
    db_session: Session,
    project_id: int,
    *,
    actor_user_id: int = 0,
    planning_session_id: int = 0,
) -> str | None:
    """Auto-explore the entry URL and extract a login URL from its internal links.

    Used by the ask_user interception path when the agent asks about explorable
    elements but no explore_page has been called yet (so _find_unexplored_login_url
    has no data to draw from).
    """
    entry_url = requirements.entry_url_or_page
    if not entry_url or not isinstance(entry_url, str):
        return None
    match = URL_PATTERN.search(entry_url)
    if not match:
        return None
    base_url = match.group(0)

    # Auto-explore the entry page
    logger.info("ask_user intercept: auto-exploring entry URL %s", base_url)
    try:
        result_text = execute_tool(
            tool_name="explore_page",
            params={"url": base_url},
            db_session=db_session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            planning_session_id=planning_session_id,
        )
        parsed = _safe_parse_json(result_text)
    except Exception as exc:
        logger.warning("ask_user intercept: entry explore failed for %s: %s", base_url, exc)
        return None

    tool_calls.append(
        AIPlanningToolCall(
            tool="explore_page",
            params={"url": base_url},
            result=parsed,
        )
    )

    if not isinstance(parsed, dict) or not parsed.get("elements"):
        return None

    # Extract internal links and find login URLs
    internal_links = _extract_internal_links(parsed, base_url)
    login_urls = [url for url in internal_links if _is_login_url(url)]
    if login_urls:
        logger.info("ask_user intercept: found login URL %s from entry page links", login_urls[0])
        return login_urls[0]
    return None


def _build_test_data_requirements(
    requirements: AIPlanningRequirements,
    *,
    is_login: bool,
) -> list[AIPlanningTestDataRequirement]:
    source = requirements.test_data_or_account or "测试数据"
    if is_login:
        return [
            AIPlanningTestDataRequirement(
                key="username",
                label="登录账号",
                value_type="string",
                required=True,
                source_hint=source,
            ),
            AIPlanningTestDataRequirement(
                key="password",
                label="登录密码",
                value_type="string",
                required=True,
                source_hint="secret",
            ),
        ]
    return [
        AIPlanningTestDataRequirement(
            key="input_data",
            label="主流程输入数据",
            value_type="string",
            required=True,
            source_hint=source,
        )
    ]


_VARIABLE_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _extract_undefined_variables(
    steps: list[dict[str, Any]],
    input_contract: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Return variables referenced in steps but not defined in input_contract or capture_text."""
    defined: set[str] = set()
    if input_contract:
        for c in input_contract:
            if isinstance(c, dict) and c.get("context_key"):
                defined.add(c["context_key"])

    # capture_text steps define runtime variables
    for step in steps:
        if isinstance(step, dict) and step.get("action") == "capture_text":
            ck = step.get("context_key")
            if ck:
                defined.add(ck)

    referenced: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        for field in ("value", "target"):
            val = step.get(field, "")
            if isinstance(val, str):
                for match in _VARIABLE_REF_RE.finditer(val):
                    referenced.add(match.group(1))

    return sorted(referenced - defined)


def _build_draft_prompt(
    requirements: AIPlanningRequirements,
    *,
    scenario_title: str,
    negative_case: bool,
    page_elements: str | None = None,
) -> str:
    assertions = "；".join(requirements.main_assertions or ["页面状态符合预期"])
    data_labels = "；".join(
        item.label for item in _build_test_data_requirements(requirements, is_login=_looks_like_login(requirements))
    )
    negative_hint = "需要覆盖异常输入和错误提示。" if negative_case else "请覆盖标准主流程。"
    page_elements_section = ""
    if page_elements:
        page_elements_section = (
            "\n\n注意：页面可交互元素清单已通过 page_elements 字段单独提供。"
            "生成 DSL 时请严格使用其中的 a11y tree 标识（role=\"name\" 格式）作为 target，"
            "标注了 [dynamic] 的元素是交互触发后才出现的动态元素，步骤顺序必须与用户流程一致。"
        )
    # Build test data section with full detail
    test_data = requirements.test_data_or_account
    data_section = ""
    variable_assignments = ""
    if test_data:
        # Parse key:value pairs from the raw test_data text for explicit variable assignment
        import re as _re
        _CN_TO_EN: dict[str, str] = {
            "账号": "email", "邮箱": "email", "邮件": "email", "用户名": "username",
            "密码": "password", "口令": "password",
            "品牌": "brand", "筛选品牌": "brand", "筛选": "filter",
        }
        _parsed_assignments: list[str] = []
        for _entry in _re.split(r"[\n,，;；]+", test_data):
            _entry = _re.sub(r'^\d+\.\s*', '', _entry.strip())
            _m = _re.match(r"(.+?)[：:=]\s*(.+)", _entry) if _entry else None
            if not _m:
                continue
            _label = _m.group(1).strip()
            _value = _m.group(2).strip()
            _en_key = _CN_TO_EN.get(_label, _label)
            _parsed_assignments.append(f"  {_label} → context_key=\"{_en_key}\", value=\"{_value}\"")

        data_section = (
            f"\n\ntest_data_or_account:\n{test_data}\n"
            "注意：上述测试数据中提到的每个字段（如下拉框、日期选择器、复选框）都必须在 steps 中有对应操作。"
            "下拉框用 input action（target 为字段标签，value 为选项文本），复选框用 click action（target 为复选框标签）。"
        )
        if _parsed_assignments:
            variable_assignments = (
                "\n\n【测试数据变量赋值 — input_contract 每个 entry 的 context_key 和 value 必须严格使用以下值】\n"
                "以下是你必须在 input_contract 中使用的确切值。禁止编造或修改测试数据。\n"
                + "\n".join(_parsed_assignments)
                + "\n"
            )

    return (
        f"请基于测试规划生成 DSL 草案。场景：{scenario_title}。"
        f"被测系统：{requirements.app_under_test or '待补充'}。"
        f"目标：{requirements.business_goal or '待补充'}。"
        f"入口：{requirements.entry_url_or_page or '待补充'}。"
        f"流程：{requirements.core_user_flow or '待补充'}。"
        f"断言：{assertions}。"
        f"测试数据需求：{data_labels or '待补充'}。"
        f"范围限制：{requirements.scope_limits or '未说明'}。"
        f"{negative_hint}"
        "【流程-页面导航映射】严格按用户流程一步步生成 DSL："
        "- 每个流程步骤对应一个或多个 DSL 步骤"
        "- 用户说\"打开首页点击登录\" → 必须生成 click/goto 导航到登录页面，不能从首页直接 input 登录字段"
        "- 用户说\"点击某链接\" → 必须生成对应的 click 步骤，不能跳过导航直接 wait_for 目标页面元素"
        "- 每条 wait_for 前必须有对应的 click/goto 把页面带到正确状态"
        "如果已获取到页面元素清单，请严格按照探索数据中元素的实际 role=\"name\" 格式作为 target，不要构造 CSS 选择器格式。step 的 value 字段如涉及测试数据，必须用 ${context_key} 格式引用 input_contract 变量，不要硬编码。"
        "必须为流程和测试数据中提到的每个表单字段生成对应步骤，不得遗漏任何字段（包括下拉框、日期选择器、复选框等）。"
        f"{variable_assignments}"
        f"{data_section}"
        f"{page_elements_section}"
    )
