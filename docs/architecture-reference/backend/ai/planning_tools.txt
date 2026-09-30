"""Planning tool registry — each tool is a callable the ReAct agent can invoke."""

from __future__ import annotations

import json
import logging
import re as _re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.ai.page_explorer import (
    capture_browser_session,
    collect_a11y_nodes,
    is_storage_state_stale,
    load_storage_state_meta,
)
from app.core.structured_logging import get_structured_logger

logger = logging.getLogger(__name__)
slog = get_structured_logger(__name__)


@dataclass(frozen=True)
class PlanningTool:
    """Declarative descriptor for a planning tool."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON-Schema style


def list_available_tools() -> list[PlanningTool]:
    """Return all registered tools."""
    return list(_TOOL_REGISTRY.values())


def get_tool_descriptions_for_prompt() -> str:
    """Build a text block describing all tools for the LLM system prompt."""
    lines: list[str] = []
    for tool in _TOOL_REGISTRY.values():
        lines.append(f"### {tool.name}")
        lines.append(tool.description)
        params = tool.parameters
        if params.get("properties"):
            lines.append("参数:")
            for pname, pinfo in params["properties"].items():
                required = pname in params.get("required", [])
                req_mark = " (必填)" if required else ""
                lines.append(f"  - {pname}{req_mark}: {pinfo.get('description', '')}")
        lines.append("")
    return "\n".join(lines)


def execute_tool(
    *,
    tool_name: str,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
    actor_user_id: int = 0,
    planning_session_id: int = 0,
) -> str:
    """Execute a tool by name and return the result as a JSON string."""
    # project_id == 0 means no project linked to session
    _NO_PROJECT_MSG = json.dumps(
        {"info": "当前会话未关联项目，请先创建或关联项目后再使用此功能。"},
        ensure_ascii=False,
    )

    _PROJECT_REQUIRED_TOOLS = {
        "list_test_cases",
        "get_case_detail",
        "list_recent_executions",
        "get_case_stats",
        "get_execution_detail",
        "get_project_test_status",
        "get_failure_analysis",
        "get_recommended_retest",
        "get_project_insights",
        "update_insights",
        "explore_page",
        "capture_page_session",
        "explore_flow",
    }

    tool_def = _TOOL_REGISTRY.get(tool_name)
    if tool_def is None:
        return json.dumps({"error": f"工具 '{tool_name}' 不存在"}, ensure_ascii=False)

    handler = _TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return json.dumps({"error": f"工具 '{tool_name}' 未注册处理函数"}, ensure_ascii=False)

    if not project_id and tool_name in _PROJECT_REQUIRED_TOOLS:
        return _NO_PROJECT_MSG

    try:
        # Inject planning_session_id for explore tools so they share one browser
        _EXPLORE_TOOLS = {"explore_page", "explore_flow", "capture_page_session"}
        if planning_session_id and tool_name in _EXPLORE_TOOLS:
            params = {**params, "planning_session_id": planning_session_id}

        slog.tool_call("tool_start", data={
            "tool_name": tool_name,
            "params": {k: str(v)[:200] for k, v in params.items()},
            "session_id": planning_session_id,
        }, session_id=planning_session_id)
        tool_timer = time.perf_counter()

        if tool_name == "create_project":
            result = handler(
                params=params, db_session=db_session, project_id=project_id,
                actor_user_id=actor_user_id, planning_session_id=planning_session_id,
            )
        else:
            result = handler(params=params, db_session=db_session, project_id=project_id)

        duration_ms = int((time.perf_counter() - tool_timer) * 1000)
        result_str = json.dumps(result, ensure_ascii=False, default=str)
        slog.tool_call("tool_complete", data={
            "tool_name": tool_name,
            "duration_ms": duration_ms,
            "result_size_bytes": len(result_str),
            "result_summary": result_str[:300],
            "session_id": planning_session_id,
        }, session_id=planning_session_id)
        return result_str
    except Exception as exc:
        duration_ms = int((time.perf_counter() - tool_timer) * 1000)
        logger.warning("Tool %s execution failed: %s", tool_name, exc)
        slog.tool_call("tool_error", data={
            "tool_name": tool_name,
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:500],
            "duration_ms": duration_ms,
            "session_id": planning_session_id,
        }, session_id=planning_session_id, level=logging.WARNING)
        db_session.rollback()
        return json.dumps({"error": f"工具执行失败: {exc!s}"}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Individual tool implementations
# ---------------------------------------------------------------------------

def _handle_get_project_info(
    *,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
) -> dict[str, Any]:
    from app.services.project_management import get_project
    project = get_project(db_session, project_id)
    if project is None:
        return {"error": f"项目 {project_id} 不存在"}
    return {"id": project.id, "name": project.name, "description": project.description}


def _handle_list_test_cases(
    *,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
) -> dict[str, Any]:
    from sqlalchemy import select
    from app.models import TestCase
    statement = select(TestCase).where(TestCase.project_id == project_id).order_by(TestCase.created_at.desc())
    records = db_session.scalars(statement).all()

    results = [
        {"id": r.id, "name": r.name, "description": r.description}
        for r in records
    ]

    search = params.get("search")
    if search:
        keyword = search.lower()
        results = [
            c for c in results
            if keyword in (c.get("name") or "").lower() or keyword in (c.get("description") or "").lower()
        ]
    limit = min(int(params.get("limit", 10)), 20)
    return {"cases": results[:limit], "total": len(results)}


def _handle_get_case_detail(
    *,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
) -> dict[str, Any]:
    from app.services.cases import get_case
    case_id = int(params.get("case_id", 0))
    if not case_id:
        return {"error": "必须提供 case_id 参数"}
    case = get_case(db_session, case_id)
    if case is None:
        return {"error": f"用例 {case_id} 不存在"}
    return {
        "id": case.id,
        "name": case.name,
        "description": case.description,
        "base_url": getattr(case, "base_url", None),
        "steps": [
            s.model_dump(mode="json") if hasattr(s, "model_dump") else s
            for s in getattr(case, "steps", [])
        ],
        "input_contract": [
            c.model_dump(mode="json") if hasattr(c, "model_dump") else c
            for c in getattr(case, "input_contract", [])
        ],
        "output_contract": [
            c.model_dump(mode="json") if hasattr(c, "model_dump") else c
            for c in getattr(case, "output_contract", [])
        ],
    }


def _handle_list_recent_executions(
    *,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
) -> dict[str, Any]:
    from app.services.executions import list_executions
    limit = min(int(params.get("limit", 5)), 10)
    executions = list_executions(db_session, project_id=project_id, limit=limit)
    results = []
    for ex in executions:
        results.append({
            "id": ex.id,
            "case_name": ex.case_name,
            "status": ex.status,
            "started_at": str(ex.started_at),
        })
    return {"executions": results}


def _handle_get_case_stats(
    *,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
) -> dict[str, Any]:
    from app.services.cases import get_project_test_case_stats
    stats = get_project_test_case_stats(db_session, project_id)
    return stats if isinstance(stats, dict) else {"stats": str(stats)}


def _handle_get_execution_detail(
    *,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
) -> dict[str, Any]:
    from app.services.executions import get_case_execution
    run_id = int(params.get("run_id", 0))
    if not run_id:
        return {"error": "必须提供 run_id 参数"}
    detail = get_case_execution(db_session, run_id)
    if detail is None:
        return {"error": f"执行记录 {run_id} 不存在"}
    steps_summary = []
    if detail.report and detail.report.steps:
        for step in detail.report.steps:
            s: dict[str, Any] = {
                "step_index": step.step_index,
                "action": step.action,
                "status": step.status,
            }
            if step.target is not None:
                s["target"] = step.target
            if step.value is not None:
                s["value"] = step.value
            if step.error_message is not None:
                s["error_message"] = step.error_message
            if step.resolved_by is not None:
                s["resolved_by"] = step.resolved_by
            if step.url is not None:
                s["url"] = step.url
            if step.duration_ms is not None:
                s["duration_ms"] = step.duration_ms
            if step.console_events:
                errors = [e for e in step.console_events if isinstance(e, dict) and e.get("level") == "error"]
                if errors:
                    s["console_errors"] = [e.get("text", "") for e in errors]
            if step.network_events:
                failures = [e for e in step.network_events if isinstance(e, dict) and (e.get("status") or 0) >= 400]
                if failures:
                    s["network_errors"] = [{"url": e.get("url", ""), "status": e.get("status")} for e in failures]
            steps_summary.append(s)
    return {
        "id": detail.id,
        "case_id": detail.case_id,
        "case_name": detail.case_name,
        "status": detail.status,
        "total_steps": detail.total_steps,
        "failed_step_index": detail.failed_step_index,
        "error_message": detail.error_message,
        "duration_ms": detail.duration_ms,
        "steps": steps_summary,
    }


def _handle_get_project_test_status(
    *,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
) -> dict[str, Any]:
    from sqlalchemy import select
    from app.models import TestCase, TestCaseRun

    cases = db_session.scalars(
        select(TestCase).where(TestCase.project_id == project_id).order_by(TestCase.id)
    ).all()

    case_statuses: list[dict[str, Any]] = []
    for case in cases:
        latest_run = db_session.scalars(
            select(TestCaseRun)
            .where(TestCaseRun.case_id == case.id)
            .order_by(TestCaseRun.started_at.desc())
            .limit(1)
        ).first()

        if latest_run is None:
            case_statuses.append({
                "case_id": case.id,
                "case_name": case.name,
                "latest_status": "no_runs",
                "latest_run_id": None,
            })
        else:
            report = latest_run.report or {}
            steps = report.get("steps", [])
            passed = sum(1 for s in steps if isinstance(s, dict) and s.get("status") == "passed")
            total = len(steps)
            case_statuses.append({
                "case_id": case.id,
                "case_name": case.name,
                "latest_status": latest_run.status,
                "latest_run_id": latest_run.id,
                "passed_steps": passed,
                "total_steps": total,
                "error_message": latest_run.error_message,
            })

    if not case_statuses:
        conclusion = "no_runs"
    elif all(c["latest_status"] == "passed" for c in case_statuses):
        conclusion = "all_passed"
    elif all(c["latest_status"] in ("failed", "needs_intervention") for c in case_statuses):
        conclusion = "all_failed"
    else:
        conclusion = "partial"

    return {"conclusion": conclusion, "cases": case_statuses, "total_cases": len(case_statuses)}


def _handle_get_failure_analysis(
    *,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
) -> dict[str, Any]:
    from sqlalchemy import select
    from app.models import TestCase, TestCaseRun

    limit = min(int(params.get("limit", 5)), 10)
    case_id_filter = params.get("case_id")
    if case_id_filter:
        case_id_filter = int(case_id_filter)

    query = select(TestCaseRun).where(TestCaseRun.project_id == project_id)
    if case_id_filter:
        query = query.where(TestCaseRun.case_id == case_id_filter)
    query = query.order_by(TestCaseRun.started_at.desc()).limit(limit * 3)

    runs = db_session.scalars(query).all()

    case_map: dict[int, list[TestCaseRun]] = {}
    for run in runs:
        case_map.setdefault(run.case_id, []).append(run)

    patterns: list[dict[str, Any]] = []
    for cid, case_runs in case_map.items():
        case_record = db_session.get(TestCase, cid)
        case_name = case_record.name if case_record else f"Case {cid}"

        statuses = [r.status for r in case_runs]
        failed_runs = [r for r in case_runs if r.status in ("failed", "needs_intervention")]
        passed_runs = [r for r in case_runs if r.status == "passed"]

        if not failed_runs:
            continue

        consecutive_failures = 0
        for s in statuses:
            if s in ("failed", "needs_intervention"):
                consecutive_failures += 1
            else:
                break

        # Flaky detection with confidence scoring (sliding window)
        flaky_score = 0.0
        if len(statuses) >= 3:
            window = statuses[:min(len(statuses), 6)]
            switches = sum(1 for i in range(1, len(window)) if window[i] != window[i - 1])
            pass_count_window = sum(1 for s in window if s == "passed")
            fail_count_window = sum(1 for s in window if s in ("failed", "needs_intervention"))
            if pass_count_window > 0 and fail_count_window > 0:
                # Score based on: (switch ratio) * (balance between pass/fail)
                switch_ratio = switches / max(len(window) - 1, 1)
                balance = 1.0 - abs(pass_count_window - fail_count_window) / len(window)
                flaky_score = round(switch_ratio * balance, 2)
        suspected_flaky = flaky_score >= 0.4

        last_error = None
        if failed_runs:
            last_error = failed_runs[0].error_message

        patterns.append({
            "case_id": cid,
            "case_name": case_name,
            "total_runs": len(case_runs),
            "passed_count": len(passed_runs),
            "failed_count": len(failed_runs),
            "consecutive_failures": consecutive_failures,
            "last_error_message": last_error,
            "suspected_flaky": suspected_flaky,
            "flaky_score": flaky_score,
        })

    return {"failure_patterns": patterns}


def _handle_get_recommended_retest(
    *,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
) -> dict[str, Any]:
    """Analyze failures and recommend a concrete retest scope."""
    from sqlalchemy import select
    from app.models import TestCase, TestCaseRun

    cases = db_session.scalars(
        select(TestCase).where(TestCase.project_id == project_id).order_by(TestCase.id)
    ).all()

    failed_cases: list[dict[str, Any]] = []
    passed_case_ids: list[int] = []

    for case in cases:
        latest_run = db_session.scalars(
            select(TestCaseRun)
            .where(TestCaseRun.case_id == case.id)
            .order_by(TestCaseRun.started_at.desc())
            .limit(1)
        ).first()

        if latest_run is None or latest_run.status == "passed":
            if latest_run is not None:
                passed_case_ids.append(case.id)
            continue

        report = latest_run.report or {}
        steps = report.get("steps", [])
        failed_step_info = []
        for s in steps:
            if isinstance(s, dict) and s.get("status") in ("failed", "needs_intervention"):
                failed_step_info.append({
                    "step_index": s.get("step_index"),
                    "action": s.get("action"),
                    "target": s.get("target"),
                    "error_message": s.get("error_message"),
                })

        # Check for consecutive failures
        recent_runs = db_session.scalars(
            select(TestCaseRun)
            .where(TestCaseRun.case_id == case.id)
            .order_by(TestCaseRun.started_at.desc())
            .limit(3)
        ).all()
        consecutive = sum(1 for r in recent_runs if r.status in ("failed", "needs_intervention"))

        failed_cases.append({
            "case_id": case.id,
            "case_name": case.name,
            "latest_status": latest_run.status,
            "failed_steps": failed_step_info,
            "consecutive_failures": consecutive,
            "error_message": latest_run.error_message,
        })

    if not failed_cases:
        return {
            "recommendation": "no_retest_needed",
            "reason": "当前项目所有用例均已通过，无需复测。",
            "retest_cases": [],
            "regression_scope": None,
        }

    # Determine regression scope
    total_cases = len(cases)
    failed_ratio = len(failed_cases) / max(total_cases, 1)

    if len(failed_cases) == 1 and failed_ratio < 0.3:
        scope = "current"
        reason = f"仅 {failed_cases[0]['case_name']} 失败，失败局限在单一功能点。"
    elif failed_ratio < 0.5:
        scope = "adjacent"
        reason = f"{len(failed_cases)}/{total_cases} 用例失败，失败可能影响相邻流程。"
    elif failed_ratio < 0.8:
        scope = "module"
        reason = f"{len(failed_cases)}/{total_cases} 用例失败，建议模块级回归。"
    else:
        scope = "core"
        reason = f"{len(failed_cases)}/{total_cases} 用例失败，建议核心链路回归。"

    # Check for flaky indicators
    flaky_cases = [c for c in failed_cases if c["consecutive_failures"] == 1 and len(recent_runs) > 1]
    if flaky_cases:
        scope = "current"
        reason += f" 其中 {len(flaky_cases)} 个用例为首次失败，疑似偶发问题，建议先针对性复测。"

    retest_case_ids = [c["case_id"] for c in failed_cases]

    return {
        "recommendation": "targeted_retest" if scope == "current" else "regression",
        "reason": reason,
        "retest_cases": [
            {"case_id": c["case_id"], "case_name": c["case_name"], "failed_steps": c["failed_steps"]}
            for c in failed_cases
        ],
        "retest_case_ids": retest_case_ids,
        "regression_scope": scope,
        "passed_case_count": len(passed_case_ids),
        "failed_case_count": len(failed_cases),
    }


def _handle_get_project_insights(
    *,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
) -> dict[str, Any]:
    """Read cross-session insights for a project."""
    from sqlalchemy import select
    from app.models import TestPointInsight

    insight = db_session.scalar(
        select(TestPointInsight).where(TestPointInsight.project_id == project_id)
    )
    if insight is None:
        return {"has_insights": False, "message": "该项目暂无历史洞察数据。"}

    return {
        "has_insights": True,
        "flaky_case_ids": insight.flaky_case_ids or [],
        "failure_patterns": insight.failure_patterns or {},
        "regression_risk": insight.regression_risk,
        "last_analysis_summary": insight.last_analysis_summary,
        "updated_at": str(insight.updated_at) if insight.updated_at else None,
    }


def _handle_update_insights(
    *,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
) -> dict[str, Any]:
    """Persist cross-session insights for a project after analysis."""
    from sqlalchemy import select as sa_select
    from app.models import TestPointInsight

    flaky_case_ids = params.get("flaky_case_ids")
    failure_patterns = params.get("failure_patterns")
    regression_risk = params.get("regression_risk")
    summary = params.get("summary")

    insight = db_session.scalar(
        sa_select(TestPointInsight).where(TestPointInsight.project_id == project_id)
    )
    if insight is None:
        insight = TestPointInsight(project_id=project_id)
        db_session.add(insight)
        db_session.flush()

    if isinstance(flaky_case_ids, list):
        insight.flaky_case_ids = flaky_case_ids
    if isinstance(failure_patterns, dict):
        existing = insight.failure_patterns or {}
        insight.failure_patterns = {**existing, **failure_patterns}
    if isinstance(regression_risk, str):
        insight.regression_risk = regression_risk
    if isinstance(summary, str):
        insight.last_analysis_summary = summary[:2000]

    db_session.flush()
    return {
        "status": "updated",
        "project_id": project_id,
        "flaky_count": len(insight.flaky_case_ids or []),
        "regression_risk": insight.regression_risk,
    }


def _resolve_storage_state_dir() -> Path:
    """Resolve storage state directory from app config."""
    from app.core.config import get_settings
    return Path(get_settings().storage_state_dir)


def _handle_explore_page(
    *,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
) -> dict[str, Any]:
    url = params.get("url")
    if not url or not isinstance(url, str) or not url.strip():
        return {"error": "必须提供 url 参数"}

    planning_session_id = int(params.get("planning_session_id", 0))
    logger.info("explore_page: url=%s, project_id=%d, session_id=%d", url.strip(), project_id, planning_session_id)

    # Resolve relative URLs against session's entry_url_or_page
    resolved_url = url.strip()
    if not resolved_url.startswith(("http://", "https://")):
        from app.models import AIPlanningSession
        from urllib.parse import urlparse, urljoin
        session_obj = db_session.get(AIPlanningSession, planning_session_id) if planning_session_id else None
        if session_obj and session_obj.requirements_json:
            entry = session_obj.requirements_json.get("entry_url_or_page", "")
            if entry and entry.startswith("http"):
                parsed = urlparse(entry)
                base = f"{parsed.scheme}://{parsed.netloc}"
                resolved_url = urljoin(base, resolved_url.lstrip("/"))
                logger.info("explore_page: resolved relative URL to %s", resolved_url)

    storage_dir = _resolve_storage_state_dir()
    storage_path = str(storage_dir / f"{project_id}.json") if (storage_dir / f"{project_id}.json").exists() else None

    # Cache lookup
    import hashlib
    from app.services.ai_planning import _lookup_tool_cache, _normalize_cache_url
    state_hash = hashlib.md5(
        open(storage_path, "rb").read() if storage_path else b""
    ).hexdigest()[:12]
    normalized = _normalize_cache_url(resolved_url)
    cached = _lookup_tool_cache(
        db_session,
        ("explore_page", planning_session_id, normalized, 1280, 720, state_hash),
        ttl_hours=4,
    )
    if cached is not None:
        logger.info("Cache hit: explore_page %s", resolved_url)
        return cached

    # Use BrowserSessionManager for shared browser
    from app.ai.page_explorer import BrowserSessionManager
    ctx, page = BrowserSessionManager.get_or_create_context(
        planning_session_id, storage_state_path=storage_path,
    )
    logger.info("explore_page: navigating to %s", resolved_url)
    page.goto(resolved_url, timeout=30000, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass

    # Check if page actually loaded
    if page.url == "about:blank":
        logger.warning("explore_page: page stuck on about:blank after goto %s", resolved_url)
        return {
            "url": resolved_url,
            "a11y_nodes": [],
            "element_count": 0,
            "error": "页面未能加载（停留在 about:blank），可能是网站无法访问或被反爬虫机制阻止",
        }

    flow_text = params.get("core_user_flow_text")

    nodes = collect_a11y_nodes(page, page_state="S0", core_user_flow_text=flow_text)
    logger.info("explore_page: found %d a11y nodes from %s", len(nodes), resolved_url)

    result: dict[str, Any] = {
        "url": resolved_url,
        "a11y_nodes": nodes,
        "element_count": len(nodes),
    }

    if not nodes:
        result["warning"] = "页面未发现可用 A11y 交互元素"

    meta = load_storage_state_meta(storage_dir, project_id=project_id)
    if meta and is_storage_state_stale(meta):
        result["warning"] = "会话状态超过24小时未更新"

    return result


def _handle_capture_page_session(
    *,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
) -> dict[str, Any]:
    url = params.get("url")
    if not url or not isinstance(url, str) or not url.strip():
        return {"error": "必须提供 url 参数"}

    steps = params.get("steps")
    if not isinstance(steps, list):
        steps = []
    planning_session_id = int(params.get("planning_session_id", 0))

    logger.info("capture_page_session: url=%s, steps=%d, project_id=%d, session_id=%d", url.strip(), len(steps), project_id, planning_session_id)
    storage_dir = _resolve_storage_state_dir()
    result = capture_browser_session(
        url=url.strip(),
        steps=steps,
        storage_dir=storage_dir,
        project_id=project_id,
        session_id=planning_session_id,
    )
    logger.info("capture_page_session: status=%s, cookie_count=%s", result.get("status"), result.get("cookie_count"))
    return result


def _handle_explore_flow(
    *,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
) -> dict[str, Any]:
    flow_steps = params.get("steps")
    urls = params.get("urls")
    flow_description = params.get("flow_description")
    planning_session_id = int(params.get("planning_session_id", 0))
    from app.models import AIPlanningSession, AIPlanningMessage

    # Resolve base_url: 1) from params, 2) from session requirements, 3) from messages
    resolved_base_url = params.get("base_url") or ""
    if not resolved_base_url and planning_session_id:
        from sqlalchemy import select, desc
        session_obj = db_session.get(AIPlanningSession, planning_session_id)
        if session_obj and session_obj.requirements_json:
            entry = session_obj.requirements_json.get("entry_url_or_page", "")
            if entry and entry.startswith("http"):
                from urllib.parse import urlparse
                parsed = urlparse(entry)
                resolved_base_url = f"{parsed.scheme}://{parsed.netloc}"
        if not resolved_base_url:
            import re
            msg = db_session.execute(
                select(AIPlanningMessage.content)
                .where(AIPlanningMessage.session_id == planning_session_id)
                .where(AIPlanningMessage.role == "user")
                .order_by(desc(AIPlanningMessage.id))
                .limit(1)
            ).scalar_one_or_none()
            if msg:
                url_match = re.search(r'https?://[^\s]+', msg)
                if url_match:
                    from urllib.parse import urlparse
                    parsed = urlparse(url_match.group(0))
                    resolved_base_url = f"{parsed.scheme}://{parsed.netloc}"
    logger.info("explore_flow: resolved_base_url=%s", resolved_base_url)

    if isinstance(flow_steps, list) and flow_steps:
        from app.ai.page_explorer import _collect_flow_a11y

        logger.info("explore_flow: %d steps, flow=%s, project_id=%d, session_id=%d",
                    len(flow_steps), flow_description or "-", project_id, planning_session_id)

        if not resolved_base_url:
            from urllib.parse import urlparse
            for step in flow_steps:
                if isinstance(step, dict):
                    step_url = step.get("url", "")
                    if isinstance(step_url, str) and step_url.startswith("http"):
                        parsed = urlparse(step_url)
                        resolved_base_url = f"{parsed.scheme}://{parsed.netloc}"
                        break

        storage_dir = _resolve_storage_state_dir()
        storage_path = str(storage_dir / f"{project_id}.json") if (storage_dir / f"{project_id}.json").exists() else None

        # Extract core_user_flow text from session for keyword-driven expand
        flow_text = None
        if planning_session_id:
            session_obj = db_session.get(AIPlanningSession, planning_session_id)
            if session_obj and session_obj.requirements_json:
                flow_text = session_obj.requirements_json.get("core_user_flow")

        page_results = _collect_flow_a11y(
            flow_steps,
            base_url=resolved_base_url or None,
            storage_state_path=storage_path,
            session_id=planning_session_id,
            core_user_flow_text=flow_text,
        )
        total_elements = sum(pr.get("element_count", 0) for pr in page_results)
        total_pages = len(page_results)

        skipped_count = sum(1 for pr in page_results if pr.get("page_state") == "SKIPPED")
        base_url_warning = None
        if skipped_count > 0:
            base_url_warning = (
                f"{skipped_count} 个步骤使用了相对 URL 但缺少 base_url 无法解析。"
                "请提供完整 URL（如 https://example.com/login）或通过 base_url 参数指定网站基础地址。"
            )

        result: dict[str, Any] = {
            "pages": page_results,
            "total_pages": total_pages,
            "total_elements": total_elements,
        }
        if base_url_warning:
            result["warning"] = base_url_warning
        return result

    # --- Legacy URL-based exploration ---
    if not isinstance(urls, list) or not urls:
        return {"error": "必须提供 steps 或 urls 参数（非空列表）"}

    valid_urls = [u for u in urls if isinstance(u, str) and u.strip()]
    if not valid_urls:
        return {"error": "urls 列表中没有有效的 URL"}

    logger.info("explore_flow: %d urls=%s, flow=%s, project_id=%d, session_id=%d",
                len(valid_urls), valid_urls[:3], flow_description or "-", project_id, planning_session_id)

    storage_dir = _resolve_storage_state_dir()
    storage_path = str(storage_dir / f"{project_id}.json") if (storage_dir / f"{project_id}.json").exists() else None

    from app.ai.page_explorer import BrowserSessionManager
    ctx, page = BrowserSessionManager.get_or_create_context(
        planning_session_id, storage_state_path=storage_path,
    )

    page_results = []
    for idx, url in enumerate(valid_urls):
        resolved = resolved_base_url.rstrip("/") + "/" + url.lstrip("/") if not url.startswith("http") and resolved_base_url else url
        try:
            page.goto(resolved, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception as exc:
            page_results.append({"url": url, "page_state": f"S{idx}", "a11y_nodes": [], "element_count": 0, "error": str(exc)})
            continue
        nodes = collect_a11y_nodes(page, page_state=f"S{idx}")
        page_results.append({"url": page.url, "page_state": f"S{idx}", "a11y_nodes": nodes, "element_count": len(nodes)})

    total_elements = sum(pr.get("element_count", 0) for pr in page_results)
    return {
        "pages": page_results,
        "total_pages": len(page_results),
        "total_elements": total_elements,
    }


def _handle_create_project(
    *,
    params: dict[str, Any],
    db_session: Session,
    project_id: int,
    actor_user_id: int,
    planning_session_id: int,
) -> dict[str, Any]:
    from sqlalchemy import select as sa_select
    from app.models import Project, SessionProject
    from app.schemas.projects import ProjectCreate
    from app.services.project_management import create_project

    name = params.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        return {"error": "必须提供项目名称（name 参数）"}

    description = params.get("description")
    if description and not isinstance(description, str):
        description = None

    if not actor_user_id:
        return {"error": "无法创建项目：未识别当前用户，请先关联项目或通过 UI 创建。"}

    base_name = name.strip()
    final_name = base_name
    logger.info("create_project: name=%s, planning_session_id=%d", base_name, planning_session_id)

    # Idempotent: if name already exists, append (2), (3), ...
    if db_session.scalar(sa_select(Project.id).where(Project.name == base_name)):
        _suffix_re = _re.compile(rf"^{_re.escape(base_name)} \((\d+)\)$")
        _like = base_name.replace("%", "\\%").replace("_", "\\_")
        candidates = db_session.scalars(
            sa_select(Project.name).where(Project.name.like(f"{_like} (%)", escape="\\"))
        ).all()
        used = {int(m.group(1)) for c in candidates if (m := _suffix_re.match(c))}
        n = 2
        while n in used:
            n += 1
        final_name = f"{base_name} ({n})"

    try:
        project = create_project(
            db_session,
            ProjectCreate(name=final_name, description=description.strip() if description else None),
            owner_user_id=actor_user_id,
        )
    except Exception as exc:
        db_session.rollback()
        return {"error": f"创建项目失败：{exc}"}

    if planning_session_id:
        # Check if session already has a default project (auto-created with session)
        # If so, replace it with the new project instead of adding a second one
        default_link = db_session.scalar(
            sa_select(SessionProject).join(Project, Project.id == SessionProject.project_id).where(
                SessionProject.session_id == planning_session_id,
                Project.is_default == True,  # noqa: E712
            )
        )
        if default_link:
            logger.info(
                "create_project: replacing default project %d with new project %d for session %d",
                default_link.project_id, project.id, planning_session_id,
            )
            default_link.project_id = project.id
            db_session.flush()
        else:
            existing = db_session.scalar(
                sa_select(SessionProject).where(
                    SessionProject.session_id == planning_session_id,
                    SessionProject.project_id == project.id,
                )
            )
            if not existing:
                db_session.add(SessionProject(
                    session_id=planning_session_id,
                    project_id=project.id,
                ))
                db_session.flush()

    result: dict[str, Any] = {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "auto_linked_to_session": bool(planning_session_id),
    }
    if final_name != base_name:
        result["name_deduplicated"] = True
        result["original_name"] = base_name
    return result


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_TOOL_REGISTRY: dict[str, PlanningTool] = {
    "get_project_info": PlanningTool(
        name="get_project_info",
        description="获取当前项目的基本信息，包括项目名称和描述。",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    "create_project": PlanningTool(
        name="create_project",
        description="创建一个新项目并自动关联到当前会话。当用户需要测试但还没有项目时使用此工具。创建成功后，后续工具（explore_page、list_test_cases 等）即可正常使用。",
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "项目名称，建议使用被测系统名称",
                },
                "description": {
                    "type": "string",
                    "description": "项目描述（可选），简要说明测试范围",
                },
            },
            "required": ["name"],
        },
    ),
    "list_test_cases": PlanningTool(
        name="list_test_cases",
        description="列出当前项目下的测试用例概要（id、名称、描述），可选按关键词搜索。",
        parameters={
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "按关键词过滤用例名称或描述",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回数量上限，默认10，最大20",
                },
            },
            "required": [],
        },
    ),
    "get_case_detail": PlanningTool(
        name="get_case_detail",
        description="查看指定测试用例的完整详情，包括步骤、输入输出契约。",
        parameters={
            "type": "object",
            "properties": {
                "case_id": {
                    "type": "integer",
                    "description": "要查看的测试用例 ID",
                },
            },
            "required": ["case_id"],
        },
    ),
    "list_recent_executions": PlanningTool(
        name="list_recent_executions",
        description="查看项目最近的测试执行记录，包括状态和执行时间。",
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "返回数量上限，默认5，最大10",
                },
            },
            "required": [],
        },
    ),
    "get_case_stats": PlanningTool(
        name="get_case_stats",
        description="获取项目下测试用例的统计信息。",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    "get_execution_detail": PlanningTool(
        name="get_execution_detail",
        description="查看指定测试执行的完整详情，包括每一步的状态、错误信息、定位器解析结果、控制台和网络错误。用于分析失败原因。",
        parameters={
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "integer",
                    "description": "要查看的测试执行记录 ID",
                },
            },
            "required": ["run_id"],
        },
    ),
    "get_project_test_status": PlanningTool(
        name="get_project_test_status",
        description="查看项目下所有测试用例的最新执行状态，包括通过率、失败摘要和整体结论（全部通过/部分通过/全部失败/无执行记录）。",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    "get_failure_analysis": PlanningTool(
        name="get_failure_analysis",
        description="分析项目或指定用例的失败模式，包括连续失败次数、疑似 flaky 标记、最近错误信息。用于根因分析和回归策略决策。",
        parameters={
            "type": "object",
            "properties": {
                "case_id": {
                    "type": "integer",
                    "description": "指定分析某个用例（可选，不填则分析项目全部用例）",
                },
                "limit": {
                    "type": "integer",
                    "description": "分析的最近执行记录数量，默认5，最大10",
                },
            },
            "required": [],
        },
    ),
    "get_recommended_retest": PlanningTool(
        name="get_recommended_retest",
        description="分析项目当前的失败模式，输出具体的复测推荐：哪些用例需要重跑、回归范围、推荐理由。用于辅助复测和回归策略决策。",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    "get_project_insights": PlanningTool(
        name="get_project_insights",
        description="获取项目跨会话的历史洞察数据，包括已知 flaky 用例、历史失败模式、回归风险等级。新会话开始时调用以获取上下文。",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    "update_insights": PlanningTool(
        name="update_insights",
        description="将分析结果持久化为项目级洞察，包括标记 flaky 用例、记录失败模式、更新回归风险。在执行分析完成后调用。",
        parameters={
            "type": "object",
            "properties": {
                "flaky_case_ids": {
                    "type": "array",
                    "description": "被标记为 flaky 的用例 ID 列表",
                    "items": {"type": "integer"},
                },
                "failure_patterns": {
                    "type": "object",
                    "description": "检测到的失败模式，如 {locator_stale: {count: 3, cases: [1,2]}}",
                },
                "regression_risk": {
                    "type": "string",
                    "description": "回归风险等级：low / medium / high / critical",
                },
                "summary": {
                    "type": "string",
                    "description": "本次分析的摘要，用于下次会话快速回顾",
                },
            },
            "required": [],
        },
    ),
    "explore_page": PlanningTool(
        name="explore_page",
        description="访问指定 URL 页面，采集页面上所有可交互元素（按钮、输入框、链接等），返回元素的 id、label、placeholder 等定位属性。如果项目已保存浏览器会话状态，会自动复用登录态。",
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要采集的目标页面 URL",
                },
            },
            "required": ["url"],
        },
    ),
    "capture_page_session": PlanningTool(
        name="capture_page_session",
        description="仅保存浏览器会话状态（cookie），不采集页面元素。如果需要登录后采集页面元素，请改用 explore_flow 并在 steps 中包含登录 actions。仅在需要跨 session 复用登录态且不需要采集登录页元素时使用此工具。",
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "登录页面的 URL",
                },
                "steps": {
                    "type": "array",
                    "description": "登录操作的步骤列表，每步包含 action、target 和 value",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["input", "click"]},
                            "target": {"type": "string"},
                            "value": {"type": "string"},
                        },
                    },
                },
            },
            "required": ["url"],
        },
    ),
    "explore_flow": PlanningTool(
        name="explore_flow",
        description="沿用户测试流程探索多个页面，在每个页面执行交互动作后采集可交互元素和视觉布局。优先使用 steps 参数（动作式探索），仅在纯静态页面浏览场景使用 urls 参数。steps 模式可执行 click/input/wait_for 动作，确保采集到的元素反映真实页面状态（如登录后、加购后）。会复用浏览器会话。探索阶段 target 可用泛化文本（如 'Add to cart'、'Continue Shopping'），探索器自动匹配第一个可交互元素——目的是把页面带到正确状态。当你还不知道页面上有哪些商品时，可以先用泛化 target 探索，拿到商品名后再用于 DSL 生成。",
        parameters={
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "description": "[简单模式] 需要依次访问和采集的页面 URL 列表，按流程顺序排列。与 steps 二选一。",
                    "items": {"type": "string"},
                },
                "steps": {
                    "type": "array",
                    "description": "[动作模式] 流探索步骤列表。每步可选填 url（导航目标）和 actions（要执行的动作）。与 urls 二选一。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "导航目标 URL（可选；跳过则留在当前页面）"},
                            "description": {"type": "string", "description": "这一步的人类可读描述（如\"点击登录按钮后进入的登录页\"）"},
                            "actions": {
                                "type": "array",
                                "description": "导航后要执行的动作列表，按顺序执行",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "action": {"type": "string", "enum": ["click", "input", "wait_for"], "description": "动作类型"},
                                        "target": {"type": "string", "description": "目标元素的可见文本、label 或 placeholder"},
                                        "value": {"type": "string", "description": "输入值（仅 input 动作需要）"},
                                    },
                                    "required": ["action", "target"],
                                },
                            },
                        },
                    },
                },
                "flow_description": {
                    "type": "string",
                    "description": "可选。对该流程的语义描述（如\"首页→登录→商品搜索→加入购物车→结算\"），帮助探索器理解页面间的导航意图。",
                },
                "base_url": {
                    "type": "string",
                    "description": "可选。网站基础 URL（如 https://example.com）。如果 steps/urls 中使用相对路径（如 /login、/products），必须提供 base_url 以便正确解析。可从用户提供的入口 URL 中提取 scheme://host 部分。",
                },
            },
            "required": [],
        },
    ),
}

_TOOL_HANDLERS: dict[str, Any] = {
    "create_project": _handle_create_project,
    "get_project_info": _handle_get_project_info,
    "list_test_cases": _handle_list_test_cases,
    "get_case_detail": _handle_get_case_detail,
    "list_recent_executions": _handle_list_recent_executions,
    "get_case_stats": _handle_get_case_stats,
    "get_execution_detail": _handle_get_execution_detail,
    "get_project_test_status": _handle_get_project_test_status,
    "get_failure_analysis": _handle_get_failure_analysis,
    "get_recommended_retest": _handle_get_recommended_retest,
    "get_project_insights": _handle_get_project_insights,
    "update_insights": _handle_update_insights,
    "explore_page": _handle_explore_page,
    "capture_page_session": _handle_capture_page_session,
    "explore_flow": _handle_explore_flow,
}
