"""AI-assisted DSL generation — single-call thinking-model path."""

from __future__ import annotations

import json
import logging
import re
import socket
import time as _time
from dataclasses import dataclass
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from pydantic import TypeAdapter, ValidationError

from app.core.config import get_settings
from app.core.structured_logging import get_structured_logger
from app.schemas.dsl import (
    AssertTextStep,
    AssertUrlContainsStep,
    CaptureTextStep,
    ClickStep,
    DSLCase,
    DSLCaseInputContract,
    DSLCaseOutputContract,
    DSLStep,
    GenerateDslBaseUrlSource,
    DslGenerationContextProfile,
    DslGenerationPromptVariant,
    DslGenerationRejectionReasonCode,
    DslGenerationRiskFlag,
    GenerateDslMeta,
    GenerateDslMode,
    GenerateDslRequest,
    GotoStep,
    InputStep,
    WaitForStep,
)


logger = logging.getLogger(__name__)
slog = get_structured_logger(__name__)


# ── Exceptions ─────────────────────────────────────────────────────────────────

class DslGenerationError(RuntimeError):
    """Raised when the model response cannot be converted into a valid DSL case."""


class DslGenerationConfigError(DslGenerationError):
    """Raised when AI DSL generation is disabled or missing required configuration."""


class DslGenerationNetworkError(DslGenerationError):
    """Raised when the LLM HTTP endpoint is unreachable (DNS/TCP/connection timeout)."""


# ── Network / HTTP helpers ─────────────────────────────────────────────────────

def _is_transient_network_error(exc: BaseException) -> bool:
    if isinstance(exc, socket.timeout):
        return True
    if isinstance(exc, HTTPError):
        return exc.code == 429 or 500 <= exc.code < 600
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, (socket.timeout, ConnectionError, TimeoutError, OSError)):
            return True
        return True
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return False


def _urlopen_with_retry(
    http_request: "request.Request",
    *,
    timeout_seconds: float,
    max_retries: int = 2,
    initial_backoff: float = 1.0,
):
    attempt = 0
    while True:
        try:
            return request.urlopen(http_request, timeout=timeout_seconds)
        except Exception as exc:
            if not _is_transient_network_error(exc) or attempt >= max_retries:
                raise
            wait = initial_backoff * (2 ** attempt)
            logger.warning(
                "LLM call attempt %d failed (%s: %s); retrying in %.1fs",
                attempt + 1, type(exc).__name__, exc, wait,
            )
            _time.sleep(wait)
            attempt += 1


# ── Action / field alias maps ──────────────────────────────────────────────────

_STEP_ADAPTER = TypeAdapter(DSLStep)
_INPUT_CONTRACT_ADAPTER = TypeAdapter(DSLCaseInputContract)
_OUTPUT_CONTRACT_ADAPTER = TypeAdapter(DSLCaseOutputContract)

_ACTION_ALIASES: dict[str, str] = {
    "open": "goto", "navigate": "goto", "visit": "goto",
    "tap": "click", "press": "click",
    "fill": "input", "enter": "input",
    "wait": "wait_for", "wait_for_element": "wait_for",
    "assert_contains_text": "assert_text", "assert_text_contains": "assert_text",
    "assert_url": "assert_url_contains", "assert_url_has": "assert_url_contains",
    "assert_path_contains": "assert_url_contains",
    "extract_text": "capture_text", "get_text": "capture_text",
    "save_text": "capture_text", "store_text": "capture_text",
}

_STEP_MODELS: dict[str, Any] = {
    "goto": GotoStep, "click": ClickStep, "input": InputStep,
    "wait_for": WaitForStep, "assert_text": AssertTextStep,
    "assert_url_contains": AssertUrlContainsStep, "capture_text": CaptureTextStep,
}

_URL_VALUE_ACTIONS = frozenset({"goto", "assert_url_contains"})
_ASSERT_TEXT_FALLBACK_TARGET = "body"

_STEP_TARGET_ALIASES: dict[str, tuple[str, ...]] = {
    "click": ("target", "element", "label", "selector", "locator", "description"),
    "input": ("target", "element", "label", "selector", "locator", "description"),
    "wait_for": ("target", "element", "label", "selector", "locator", "description"),
    "assert_text": ("target", "element", "label", "selector", "locator", "description"),
}

_STEP_VALUE_ALIASES: dict[str, tuple[str, ...]] = {
    "goto": ("value", "url", "path", "href", "target"),
    "input": ("value", "text", "input", "content"),
    "assert_text": ("value", "expected", "expected_text", "expectedText", "text"),
    "assert_url_contains": ("value", "expected", "url", "path", "contains", "target"),
}

_STEP_TIMEOUT_ALIASES = ("timeout_ms", "timeoutMs", "timeout")

_VALUE_TYPE_ALIASES: dict[str, str] = {
    "str": "string", "string": "string", "text": "string",
    "int": "number", "integer": "number", "float": "number",
    "double": "number", "number": "number",
    "bool": "boolean", "boolean": "boolean",
    "dict": "object", "map": "object", "json": "object", "object": "object",
    "list": "array", "array": "array",
}

_OUTPUT_SOURCE_ALIASES: dict[str, str] = {
    "url": "latest_url", "page_url": "latest_url", "current_url": "latest_url",
    "latest_url": "latest_url", "error_message": "error_message", "status": "status",
    "step_url": "last_step_url", "last_step_url": "last_step_url",
    "page_title": "last_step_page_title", "last_step_page_title": "last_step_page_title",
    "step_target": "last_step_target", "last_step_target": "last_step_target",
    "step_value": "last_step_value", "last_step_value": "last_step_value",
    "step_error_message": "last_step_error_message",
    "last_step_error_message": "last_step_error_message",
}

_CONTRACT_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "label", "title"),
    "context_key": ("context_key", "contextKey", "key"),
    "value_type": ("value_type", "valueType", "type"),
    "required": ("required", "is_required", "isRequired"),
    "source": ("source", "value_from", "valueFrom", "extract_from", "extractFrom", "from"),
    "description": ("description", "desc", "notes"),
}

_CASE_WRAPPER_KEYS = ("case", "data", "result", "response", "draft")
_CASE_STEPS_ALIASES = ("step", "step_list", "stepList", "actions")
_STEP_ACTION_KEYS = ("action", "type", "command", "step_action", "stepAction")
_STEP_COLLECTION_KEYS = ("steps", "items", "list", "value", "data")

_GENERIC_CASE_NAMES = {"ai 生成用例", "ai生成用例", "generated test case", "test case", "测试用例"}
_GENERIC_CASE_DESCRIPTIONS = {
    "ai 自动生成测试用例", "自动生成测试用例", "自动生成",
    "generated by ai", "ai generated test case",
}
_GENERIC_CONTRACT_NAMES = {
    "input", "output", "value", "values", "data", "result",
    "field", "item", "param", "params",
    "输入", "输出", "值", "数据", "结果", "字段", "参数",
}

SUPPORTED_DSL_ACTIONS = [
    "goto", "click", "input", "wait_for",
    "assert_text", "assert_url_contains", "capture_text",
]

AI_DSL_PROMPT_VERSION = "2026-05-30.product-card-context-v1"


# ── Utility functions ──────────────────────────────────────────────────────────

def _promote_first_alias(step: dict[str, Any], canonical_key: str, aliases: tuple[str, ...]) -> None:
    if step.get(canonical_key):
        return
    for alias in aliases:
        if alias == canonical_key:
            continue
        if alias in step and step[alias] not in (None, ""):
            step[canonical_key] = step.pop(alias)
            return


def _clean_icon_chars(text: str) -> str:
    """Remove Font Awesome / icon font Unicode PUA characters from text."""
    return re.sub(r'[-\U000f0000-\U000ffffd]', '', text).strip()


def _normalize_step(step: dict[str, Any]) -> dict[str, Any] | None:
    """Minimal step normalization: action aliases + URL target→value swap.

    The thinking model rarely makes mistakes, but a lightweight safety net
    catches the most common slip-ups without the complexity of the old
    _normalize_llm_step / _repair_target_format patch chain.
    """
    if not isinstance(step, dict):
        return None
    act_raw = (step.get("action") or "").strip().lower()
    if not act_raw:
        return None

    canonical = _ACTION_ALIASES.get(act_raw, act_raw)
    if canonical != act_raw:
        step["action"] = canonical

    # Promote alias field names to canonical
    target_aliases = _STEP_TARGET_ALIASES.get(canonical)
    if target_aliases:
        _promote_first_alias(step, "target", target_aliases)
    value_aliases = _STEP_VALUE_ALIASES.get(canonical)
    if value_aliases:
        _promote_first_alias(step, "value", value_aliases)
    _promote_first_alias(step, "timeout_ms", _STEP_TIMEOUT_ALIASES)

    # goto / assert_url_contains: URL goes in value, not target
    if canonical in _URL_VALUE_ACTIONS:
        if not step.get("value") and step.get("target"):
            step["value"] = step.pop("target")

    # assert_text: if only target is given, move it to value and use body fallback
    if canonical == "assert_text":
        if not step.get("value") and step.get("target"):
            step["value"] = step["target"]
            step["target"] = _ASSERT_TEXT_FALLBACK_TARGET

    # Drop steps that can't be validated
    if canonical == "input" and (not step.get("value") or not step.get("target")):
        return None
    if canonical in ("click", "wait_for") and not step.get("target"):
        return None
    if canonical == "capture_text" and (not step.get("target") or not step.get("context_key")):
        return None

    return step


_VAR_PATTERN = re.compile(r"\$\{[\w]+\}")
_PRICE_TEXT_RE = re.compile(r"^(?:Rs\.|₹|\$|€|£)\s*[\d,]+(?:\.\d+)?$", re.IGNORECASE)
_GENERIC_PRODUCT_ACTIONS = {"add to cart", "view product", "continue shopping", "view cart"}


def _fix_variable_misuse(step: dict[str, Any], warnings: list[str]) -> dict[str, Any] | None:
    """Detect and fix ${var} misuse in target fields.

    Variables can only appear in value fields.  If a target contains ${var},
    the step is either repaired (assert_text) or dropped with a warning.
    """
    target = step.get("target", "")
    if not target or not _VAR_PATTERN.search(target):
        return step

    logger.info("_fix_variable_misuse: detected ${{var}} in target=%r action=%s", target, step.get("action"))

    action = step.get("action", "")

    # assert_text: target has ${var} but should be a real locator.
    # Always set target to body fallback. If value is empty, use the variable there.
    if action == "assert_text":
        if not step.get("value"):
            step["value"] = target
        step["target"] = _ASSERT_TEXT_FALLBACK_TARGET
        warnings.append(
            f"assert_text target contained ${{var}} '{target}', "
            f"target set to '{_ASSERT_TEXT_FALLBACK_TARGET}'"
        )
        return step

    # wait_for: target has ${var} — drop the step, can't wait for a variable
    if action == "wait_for":
        warnings.append(
            f"wait_for target contained ${{var}} '{target}', step dropped"
        )
        return None

    # click/input: target has ${var} — drop the step
    warnings.append(
        f"{action} target contained ${{var}} '{target}', step dropped"
    )
    return None


def _clean_element_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return _clean_icon_chars(value).strip()


def _is_price_text(value: str) -> bool:
    return bool(_PRICE_TEXT_RE.match(value.strip()))


def _is_generic_product_action(value: str) -> bool:
    return value.strip().casefold() in _GENERIC_PRODUCT_ACTIONS


def _selector_candidates_for_step(
    verified_selectors: list[dict[str, Any]],
    *,
    semantic_value: str,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for selector_info in verified_selectors:
        strategy = selector_info.get("strategy", "")
        selector = selector_info.get("selector", "")
        if not strategy or not selector:
            continue
        candidate_strategy = f"verified_{strategy}"
        key = (candidate_strategy, selector)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "strategy": candidate_strategy,
            "selector": selector,
            "semantic_value": semantic_value,
            "pre_score": 1.0,
            "pre_features": {
                "verified": True,
                "source": selector_info.get("source") or "a11y_product_card",
            },
        })
    return candidates


# ── JSON extraction ────────────────────────────────────────────────────────────

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


def _format_validation_error(exc: ValidationError) -> str:
    first_error = exc.errors(include_url=False)[0]
    location = ".".join(str(item) for item in first_error.get("loc", ()))
    message = first_error.get("msg", "unknown validation error")
    return f"AI 返回的 DSL 不符合当前 schema：{location} {message}".strip()


def _looks_like_html_response(response_text: str) -> bool:
    normalized = response_text.lstrip().casefold()
    return normalized.startswith("<!doctype html") or normalized.startswith("<html")


def _build_non_json_response_error(
    *, endpoint: str, base_url: str, content_type: str, response_text: str,
) -> str:
    normalized_preview = re.sub(r"\s+", " ", response_text).strip()[:160]
    hint = ""
    if _looks_like_html_response(response_text):
        hint = " 响应看起来像 HTML 页面，请检查 AI_DSL_BASE_URL 是否指向了真正的 OpenAI 兼容 API 根路径。"
        if not base_url.rstrip("/").endswith("/v1"):
            hint += " 当前 base_url 末尾不包含 /v1。"
    return (
        "AI DSL 生成接口返回了无法解析的非 JSON 响应。"
        f" endpoint={endpoint}"
        f" content_type={content_type or 'unknown'}"
        f" preview={normalized_preview or '<empty>'}.{hint}"
    )


# ── LLM call ───────────────────────────────────────────────────────────────────

def _should_enable_thinking_mode(*, base_url: str, model: str) -> bool:
    normalized_base_url = base_url.strip().casefold()
    normalized_model = model.strip().casefold()
    return (
        "open.bigmodel.cn" in normalized_base_url
        or normalized_model.startswith("glm-")
        or "deepseek" in normalized_model
    )


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
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        logger.warning("LLM returned empty content, falling back to reasoning_content (%d chars)", len(reasoning))
        return reasoning
    if isinstance(content, str):
        return content
    return ""


def _log_dsl_cache_usage(raw_payload: dict[str, Any]) -> None:
    if not isinstance(raw_payload, dict):
        return
    usage = raw_payload.get("usage", {}) or {}
    if not isinstance(usage, dict):
        return
    hit = usage.get("prompt_cache_hit_tokens", 0) or 0
    miss = usage.get("prompt_cache_miss_tokens", 0) or 0
    if hit or miss:
        ratio = hit / (hit + miss) * 100 if (hit + miss) > 0 else 0
        logger.info(
            "DSL cache: hit=%d miss=%d ratio=%.0f%% total=%d completion=%d",
            hit, miss, ratio,
            usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
        )


def _call_llm(
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
    thinking_enabled = _should_enable_thinking_mode(base_url=base_url, model=model)
    logger.info("DSL _call_llm: model=%s, thinking=%s, base_url=%s", model, thinking_enabled, base_url)
    if thinking_enabled:
        payload["thinking"] = {"type": "enabled"}
        payload["max_tokens"] = 65536
        payload["temperature"] = 0.0
    else:
        payload["temperature"] = 0.0
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
    try:
        response = _urlopen_with_retry(http_request, timeout_seconds=timeout_seconds)
    except (URLError, socket.timeout, ConnectionError, TimeoutError) as exc:
        logger.error("DSL LLM call failed (network): %s", exc)
        raise DslGenerationNetworkError(
            f"AI DSL 生成失败：无法连接到 LLM API。"
            f"错误：{type(exc).__name__}: {exc}。"
            f"请检查网络连通性、DNS 解析或代理设置。"
        ) from exc
    with response:
        raw_body = response.read()
        response_text = raw_body.decode("utf-8", errors="surrogateescape")
        response_text = response_text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        content_type = ""
        if hasattr(response, "headers") and response.headers is not None:
            content_type = response.headers.get("Content-Type", "")
        try:
            raw_payload = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise DslGenerationError(
                _build_non_json_response_error(
                    endpoint=endpoint, base_url=base_url,
                    content_type=content_type, response_text=response_text,
                )
            ) from exc

    _log_dsl_cache_usage(raw_payload)
    return _extract_message_content(raw_payload)


# ── Prompt building ────────────────────────────────────────────────────────────

def _format_elements_flat(a11y_nodes_by_state: dict[str, list[dict[str, Any]]]) -> str:
    """Format a11y nodes as a flat list grouped by page state.

    Containers are shown with their children indented beneath,
    so the AI can see which elements belong to which container.
    The container type is not hardcoded — AI determines the scope name from context.

    Note: Data is grouped by page -> action. The same page may appear multiple times
    with different actions, showing how elements change after each action.
    """
    if not a11y_nodes_by_state:
        return "(no elements available)"

    lines: list[str] = []

    for state in sorted(a11y_nodes_by_state.keys()):
        nodes = a11y_nodes_by_state[state]
        if not nodes:
            continue
        lines.append(f"\n## Page state: {state}")

        # Build parent→children index for all nodes
        node_by_id: dict[str, dict[str, Any]] = {}
        children_of: dict[str, list[dict[str, Any]]] = {}
        for n in nodes:
            nid = n.get("node_id", "")
            if nid:
                node_by_id[nid] = n
            pid = n.get("parent_id")
            if pid:
                children_of.setdefault(pid, []).append(n)

        # Count duplicates across ALL nodes (for dedup labels)
        name_counts: dict[tuple[str, str], int] = {}
        for n in nodes:
            role = n.get("role", "unknown")
            name = _clean_element_name(n.get("name", "") or "")
            if name:
                key = (str(role), name.casefold())
                name_counts[key] = name_counts.get(key, 0) + 1

        seen_counts: dict[tuple[str, str], int] = {}

        def _fmt_node(n: dict[str, Any], indent: str = "") -> str:
            role = n.get("role", "unknown")
            name = _clean_element_name(n.get("name", "") or "")
            disabled = " [DISABLED]" if n.get("disabled") else ""
            if name:
                key = (str(role), name.casefold())
                duplicate_count = name_counts.get(key, 0)
                duplicate_part = ""
                if duplicate_count > 1:
                    seen_counts[key] = seen_counts.get(key, 0) + 1
                    duplicate_part = f" [duplicate {seen_counts[key]}/{duplicate_count}]"
                vs_count = len(n.get("verified_selectors") or [])
                verified_part = f" [verified={vs_count}]" if vs_count > 0 else ""
                return f"{indent}- {role}=\"{name}\"{disabled}{duplicate_part}{verified_part}"
            return f"{indent}- {role}{disabled}"

        # Find all container nodes (nodes with children)
        container_ids = set(children_of.keys())
        containers = [node_by_id[nid] for nid in container_ids if nid in node_by_id]
        container_node_ids = {n.get("node_id") for n in containers}

        # Top-level nodes: not a container and not a child of any container
        top_level = [n for n in nodes
                     if n.get("node_id") not in container_node_ids
                     and n.get("parent_id") not in container_ids]

        # Render containers with children
        for container in containers:
            cid = container.get("node_id", "")
            container_role = container.get("role", "unknown")

            # Derive container name from child paragraph/heading
            container_name = ""
            for child in children_of.get(cid, []):
                child_role = (child.get("role") or "").lower()
                child_name = _clean_element_name(child.get("name", "") or "")
                if child_role in ("paragraph", "heading") and child_name:
                    container_name = child_name
                    break

            # Format header: use container name with [container] prefix to distinguish from child elements
            if container_name:
                header = f"- [container] {container_name}"
            else:
                header = f"- [container] {container_role}"

            vs_count = len(container.get("verified_selectors") or [])
            if vs_count > 0:
                header += f" [verified={vs_count}]"
            lines.append(header)

            # Render children
            for child in children_of.get(cid, []):
                lines.append(_fmt_node(child, indent="  "))

        # Render remaining top-level nodes (not containers, not children)
        for n in top_level:
            lines.append(_fmt_node(n))

    return "\n".join(lines) if lines else "(no elements available)"


def _parse_test_data_from_prompt(prompt_text: str) -> dict[str, str]:
    """Extract key:value pairs from the test_data_or_account section of a prompt.

    Handles formats like:
      - 账号：Xjy13302412005@outlook.com，密码：123456
      - email: test@example.com, password: 123456
      - 1. 账号：xxx，密码：yyy
    """
    result: dict[str, str] = {}
    # Find the test_data section.  Negative lookahead after 测试数据 excludes
    # "测试数据需求" and "测试数据变量赋值" which appear in planning-agent draft
    # prompts — those are NOT the actual test data values.
    td_match = re.search(
        r'(?:test_data_or_account|test data|测试数据(?!\s*(?:需求|变量)))[：:\s]*\n?'
        r'(.+?)(?=\n\s*(?:scope_limits|范围限制|main_assertions|$)|\Z)',
        prompt_text, re.DOTALL | re.IGNORECASE,
    )
    if not td_match:
        return result

    raw = td_match.group(1).strip()
    # Split into entries: newlines, Chinese/English commas/semicolons, numbered items
    entries = re.split(r"[\n,，;；]+", raw)
    for entry in entries:
        entry = re.sub(r'^\d+\.\s*', '', entry.strip())
        if not entry:
            continue
        m = re.match(r"(.+?)[：:=]\s*(.+)", entry)
        if m:
            key = m.group(1).strip()
            value = m.group(2).strip()
            result[key] = value

    return result


def _match_test_data_to_contracts(
    test_data: dict[str, str],
    context_keys: list[str],
) -> dict[str, str]:
    """Match parsed test data labels to input_contract context_keys.

    Uses Chinese→English key mapping and heuristic matching so that
    "账号" → "email", "密码" → "password", etc.
    """
    _CHINESE_KEY_MAP: dict[str, list[str]] = {
        "账号": ["email", "username", "login", "user", "account"],
        "邮箱": ["email", "mail", "username"],
        "邮件": ["email", "mail"],
        "用户名": ["username", "user", "login", "email"],
        "密码": ["password", "pass", "pwd"],
        "口令": ["password", "pass", "pwd"],
        "url": ["url", "link", "href"],
        "网址": ["url", "link"],
        "链接": ["url", "link"],
        "品牌": ["brand"],
        "筛选": ["filter"],
    }

    result: dict[str, str] = {}
    if not test_data or not context_keys:
        return result

    for ck in context_keys:
        ck_lower = ck.lower()

        for label, value in test_data.items():
            label_lower = label.lower()

            # Direct match: context_key appears in or contains label
            if ck_lower in label_lower or label_lower in ck_lower:
                result[ck] = value
                break

            # Chinese key mapping
            for cn_key, en_keys in _CHINESE_KEY_MAP.items():
                if cn_key in label_lower and ck_lower in en_keys:
                    result[ck] = value
                    break
            else:
                continue
            break
        else:
            # Heuristic fallbacks
            if ("email" in ck_lower or "mail" in ck_lower):
                for v in test_data.values():
                    if "@" in v:
                        result[ck] = v
                        break
            elif ("password" in ck_lower or "pass" in ck_lower or "pwd" in ck_lower):
                for v in test_data.values():
                    if "@" not in v and len(v) >= 4:
                        result[ck] = v
                        break

    return result


# ── Main entry point ───────────────────────────────────────────────────────────

def generate_case_draft(
    *,
    payload: "GenerateDslRequest",
    flow_steps: list[dict[str, Any]] | None = None,  # kept for backwards compat, unused
    a11y_nodes_by_state: dict[str, list[dict[str, Any]]] | None = None,
    scenario_variables: list[dict[str, Any]] | None = None,
    db_session: Any | None = None,  # 用于查询 anti-patterns
) -> "tuple[DSLCase, list[str], list[str], GenerateDslMeta]":
    """Generate a DSL case with a single thinking-model call.

    Replaces the old segmented flash-model pipeline.  One call with
    thinking enabled produces higher quality DSLs, and the semantic
    locator handles target resolution — no post-hoc repair needed.
    """
    settings = get_settings()
    if not settings.enable_ai_dsl_generate:
        raise DslGenerationConfigError(
            "AI DSL 生成功能未开启。请设置 ENABLE_AI_DSL_GENERATE=true。"
        )

    api_key = settings.ai_dsl_api_key or ""
    model = settings.ai_dsl_model or ""
    base_url = settings.ai_dsl_base_url
    if not api_key:
        raise DslGenerationConfigError(
            "AI DSL 生成失败：未配置 API Key。请设置 AI_DSL_API_KEY 环境变量。"
        )
    if not model:
        raise DslGenerationConfigError(
            "AI DSL 生成失败：未配置模型。请设置 AI_DSL_MODEL 环境变量。"
        )

    logger.info(
        "DSL generation start: prompt_len=%d, base_url=%s, a11y_states=%d",
        len(payload.prompt), payload.base_url,
        len(a11y_nodes_by_state) if a11y_nodes_by_state else 0,
    )

    case_base_url = payload.base_url or None
    all_warnings: list[str] = []
    all_notes: list[str] = []

    # ── Build element list ──
    elements_text = _format_elements_flat(a11y_nodes_by_state or {})

    # ── Build test data section ──
    test_data_raw = _parse_test_data_from_prompt(payload.prompt)
    test_data_lines = ""
    if test_data_raw:
        test_data_lines = "\n".join(f"  {k}: {v}" for k, v in test_data_raw.items())
    test_data_section = f"\n## Test data\n{test_data_lines}\n" if test_data_lines else ""

    # ── Build scenario variables section ──
    variables_section = ""
    if scenario_variables:
        var_lines: list[str] = []
        for v in scenario_variables:
            if not isinstance(v, dict):
                continue
            ck = v.get("context_key", "")
            desc = v.get("description", "")
            if ck:
                var_lines.append(f"  - ${{{ck}}}: {desc}")
        if var_lines:
            variables_section = "\n## Scenario variables\n" + "\n".join(var_lines) + "\n"

    # ── Build concise system prompt ──
    system_prompt = """You generate web testing DSL in JSON. Return {"name","description","base_url","input_contract","output_contract","steps"}.
No markdown, no explanation — JSON only.

## Data format

The Available elements are grouped by page -> action:
- Each page section shows the URL and page state
- Under each page, actions are listed with the elements that appeared AFTER that action
- The same page may appear multiple times with different actions, showing how elements change
- This is normal and expected — use the most recent state of each element

## Rules (in priority order)

1. **Targets**: Copy the EXACT role="name" format from the Available elements section.
   Include the role prefix — this enables precise locator resolution.
   FORBIDDEN: CSS selectors (#id, .class, [attr]), XPath (//, /html), tag names (div, span),
   data-testid, or ANY DOM-derived selector. The system resolves locators from a11y role+name only.

   **IMPORTANT**: Use the role from the element that HAS the name, NOT from its parent.
   Example: If you see:
   ```
   - [container] Blue Top
     - paragraph="Blue Top"
     - heading="Rs. 500"
     - link="Add to cart"
   ```
   Use: `paragraph "Blue Top"` or role from the indented child element.
   The `[container]` prefix indicates a container element — NEVER use it as a target.

   **Element disambiguation**: When multiple elements have the same role and name (e.g. multiple
   "Add to cart" buttons), you MUST use the scoped format:
   target=<role> "<name>" inside "<container_identifier>"
   The scope name comes from the parent container's identifying text (product name, row label, etc.).
   Look at the page structure: elements are grouped in containers (product cards, table rows, forms, etc.).
   Use the container's unique identifying text as the scope.
   Never target a bare price like "Rs. 500".

   **CRITICAL**: When using `inside`, use the CHILD element's role, NOT the container's role.
   Example:
   - To capture price: `capture_text heading "Rs. 500" inside "Blue Top"` ✓
   - WRONG: `capture_text paragraph "Blue Top" inside "Blue Top"` ✗

2. **Page structure understanding**: The Available elements use indentation to show parent-child relationships:
   - Indented elements are children of the element above them
   - Example: `- [container] Blue Top\n  - paragraph="Blue Top"\n  - heading="Rs. 500"\n  - link="Add to cart"`
     means paragraph, heading, and link are children of the Blue Top container
   - Use the parent container's identifying text as the scope name for `inside`
   - Example: To click "Add to cart" inside "Blue Top" product card, use: target=link "Add to cart" inside "Blue Top"

   **Correct DSL examples**:
   - click link "Products" → target=link "Products"
   - click link "Add to cart" inside "Blue Top" → target=link "Add to cart" inside "Blue Top"
   - capture_text heading "Rs. 500" inside "Blue Top" → target=heading "Rs. 500" inside "Blue Top"
   - capture_text paragraph "Blue Top" → target=paragraph "Blue Top"
   - assert_text link "Blue Top" → target=link "Blue Top", value="Blue Top"

   **WRONG examples** (NEVER do this):
   - capture_text paragraph "Blue Top" inside "Blue Top" → WRONG! use container text as scope, not as child target
   - click paragraph "Blue Top" → OK only for capture_text; for clicking prefer link/button roles

3. **Navigation**: You MUST click/goto to reach a page BEFORE interacting with elements on it.
   The first step after goto / is a navigation click, not a form input.

4. **Login**: The DSL must be self-contained. Include all login steps (input email + password + click Login).
   Do NOT assume the user is already logged in. Use ${var} for credentials.

5. **Wait after actions**: After navigation clicks or form submits, add wait_for for a confirmation element.

6. **Input trigger**: When changing a value that requires keyboard activation (quantity, search),
   add trigger="Enter" on the input step. The executor handles the keypress.

7. **Modify-then-assert**: When changing a value, input → wait_for update → assert.
   Do NOT assert a new value without first inputting it.

8. **Capture-then-assert**: capture_text stores element text into a variable
   (use context_key as the variable name). Later assert_text steps can reference
   this variable via ${context_key} in the VALUE field to verify the captured
   text appears on a different page (e.g. cart page).

9. **Form coverage**: Generate a step for EVERY form field mentioned in the flow.
   Dropdown: input action. Checkbox/radio: click action.

10. **Field rules**:
    - goto / assert_url_contains: value=URL, NO target
    - click / wait_for: target only, NO value
    - input / assert_text: BOTH target AND value required
    - capture_text: target + context_key (snake_case variable name)
    - ${var} placeholders can ONLY be used in the VALUE field of input/assert_text, NEVER as a target.

11. **input_contract**: Define every ${var} used in steps. Include context_key AND value.
    CRITICAL: The "value" field MUST be copied VERBATIM from the "## Test data" section.
    NEVER invent, guess, or modify test data values.

Return ONLY the JSON object."""

    # ── Build user prompt ──
    user_prompt_parts = [
        f"Generate a complete, executable web test DSL for this scenario:\n\n{payload.prompt.strip()}\n",
        f"## Available elements\n{elements_text}",
    ]

    # 注入 user_context（用户上下文信息）
    if payload.user_context:
        user_prompt_parts.append(f"\n## User Context\n{payload.user_context}\n")

    # 注入 anti-patterns（负例 few-shot）
    if db_session is not None and payload.project_id is not None:
        try:
            from app.services.anti_patterns import retrieve_relevant_anti_patterns, format_anti_patterns_for_prompt
            anti_patterns = retrieve_relevant_anti_patterns(
                db_session,
                project_id=payload.project_id,
                prompt_text=payload.prompt,
                retry_reason_code=payload.retry_reason_code,
                limit=3,
            )
            if anti_patterns:
                user_prompt_parts.append(format_anti_patterns_for_prompt(anti_patterns))
                # 记录结构化日志
                pattern_categories = [p.error_category for p in anti_patterns]
                slog.ai_thinking(
                    "dsl_anti_pattern_injection",
                    message=f"Injected {len(anti_patterns)} anti-patterns into DSL prompt",
                    data={
                        "project_id": payload.project_id,
                        "pattern_count": len(anti_patterns),
                        "pattern_categories": pattern_categories,
                        "retry_reason_code": payload.retry_reason_code,
                    },
                )
                logger.info("Injected %d anti-patterns into DSL prompt", len(anti_patterns))
        except Exception:
            logger.warning("Failed to inject anti-patterns into DSL prompt", exc_info=True)

    if test_data_section:
        user_prompt_parts.append(test_data_section)
    if variables_section:
        user_prompt_parts.append(variables_section)
    user_prompt_parts.append(
        f"\nBase URL: {case_base_url or '(use full URLs in goto steps)'}\n"
        "\nGenerate the complete DSL now. Include ALL steps — navigation, login, interactions, assertions."
    )

    user_prompt = "\n".join(user_prompt_parts)

    # ── Call LLM ──
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    logger.info("DSL prompt lengths: system=%d, user=%d", len(system_prompt), len(user_prompt))

    try:
        response_text = _call_llm(
            messages=messages,
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_seconds=max(60.0, getattr(settings, "ai_dsl_timeout_ms", 180000) / 1000),
        )
    except DslGenerationError:
        raise
    except Exception as exc:
        logger.error("DSL LLM call failed: %s", exc)
        raise DslGenerationNetworkError(
            f"AI DSL 生成失败：无法连接到 LLM API。错误：{type(exc).__name__}: {exc}。"
        ) from exc

    cleaned = _extract_json_object(response_text)
    logger.debug("DSL response length: %d", len(cleaned))

    try:
        raw = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise DslGenerationError(
            f"AI 返回了非法的 JSON。preview: {cleaned[:300]}"
        ) from exc

    if not isinstance(raw, dict):
        raise DslGenerationError("AI 返回的不是 JSON 对象")

    # Unwrap common wrapper keys (case/data/result/draft)
    for wrapper_key in _CASE_WRAPPER_KEYS:
        if wrapper_key in raw and isinstance(raw[wrapper_key], dict):
            raw = raw[wrapper_key]
            break

    # Extract steps from various possible locations
    steps_result: list[dict[str, Any]] = []
    for key in ("steps", "items", "list"):
        if key in raw and isinstance(raw[key], list):
            steps_result = raw[key]
            break
    if not steps_result and isinstance(raw.get("value"), list):
        steps_result = raw["value"]
    if not steps_result:
        # Try to find any array value at top level
        for val in raw.values():
            if isinstance(val, list) and val and isinstance(val[0], dict) and "action" in val[0]:
                steps_result = val
                break

    # ── Minimal normalization ──
    logger.info("Starting normalization: %d raw steps", len(steps_result))
    normalized_steps: list[dict[str, Any]] = []
    for s in steps_result:
        normalized = _normalize_step(s)
        if normalized is not None:
            fixed = _fix_variable_misuse(normalized, all_warnings)
            if fixed is not None:
                normalized_steps.append(fixed)
    logger.info("Normalization complete: %d steps, %d warnings", len(normalized_steps), len(all_warnings))

    if len(normalized_steps) != len(steps_result):
        logger.info("Normalization dropped %d malformed steps", len(steps_result) - len(normalized_steps))

    # ── Rewrite step_index ──
    for i, s in enumerate(normalized_steps):
        s["step_index"] = i + 1

    # ── Process input_contract ──
    input_contract_raw = raw.get("input_contract", []) or []
    if not isinstance(input_contract_raw, list):
        input_contract_raw = []

    # Extract context_keys from the contract to match test data
    context_keys_from_contract = [
        c.get("context_key", "") for c in input_contract_raw
        if isinstance(c, dict) and c.get("context_key")
    ]

    # Match test data to contract context_keys
    matched_values = _match_test_data_to_contracts(test_data_raw, context_keys_from_contract)

    # Build input_contract with values filled in
    input_contract: list[dict[str, Any]] = []
    for c in input_contract_raw:
        if not isinstance(c, dict):
            continue
        ck = c.get("context_key") or c.get("contextKey") or ""
        if not ck:
            continue
        entry: dict[str, Any] = {
            "name": c.get("name") or c.get("label") or c.get("title") or ck,
            "context_key": ck,
            "value_type": _VALUE_TYPE_ALIASES.get(
                (c.get("value_type") or c.get("type") or "string").lower(), "string",
            ),
            "required": c.get("required", c.get("is_required", c.get("isRequired", True))),
            "description": c.get("description") or c.get("desc") or None,
        }
        # Fill value from matched test data, or keep the LLM-provided value
        if ck in matched_values:
            entry["value"] = matched_values[ck]
        elif c.get("value"):
            entry["value"] = str(c["value"])
        input_contract.append(entry)

    # If LLM didn't generate contracts but steps reference ${vars}, auto-create them
    if not input_contract:
        import re as _re
        var_pattern = _re.compile(r"\$\{(\w+)\}")
        seen_vars: set[str] = set()
        for s in normalized_steps:
            for field in ("value", "target"):
                text = s.get(field) or ""
                if isinstance(text, str):
                    for match in var_pattern.finditer(text):
                        seen_vars.add(match.group(1))
        for var in sorted(seen_vars):
            val = matched_values.get(var, "")
            input_contract.append({
                "name": var,
                "context_key": var,
                "value_type": "string",
                "required": True,
                "value": val,
            })
            if not val:
                logger.warning("Variable '%s' has no resolved value; ${%s} will not be substituted at runtime", var, var)

    # ── Process output_contract ──
    output_contract_raw = raw.get("output_contract", []) or []
    if not isinstance(output_contract_raw, list):
        output_contract_raw = []

    output_contract: list[dict[str, Any]] = []
    for c in output_contract_raw:
        if not isinstance(c, dict):
            continue
        ck = c.get("context_key") or c.get("contextKey") or c.get("key") or ""
        if not ck:
            continue
        entry: dict[str, Any] = {
            "name": c.get("name") or c.get("label") or c.get("title") or ck,
            "context_key": ck,
            "value_type": _VALUE_TYPE_ALIASES.get(
                (c.get("value_type") or c.get("valueType") or c.get("type") or "string").lower(), "string",
            ),
            "source": _OUTPUT_SOURCE_ALIASES.get(
                (c.get("source") or c.get("value_from") or c.get("valueFrom") or "").lower(), None,
            ),
            "description": c.get("description") or c.get("desc") or None,
        }
        output_contract.append(entry)

    # If LLM didn't generate output contracts but steps use capture_text, auto-create them
    if not output_contract:
        for s in normalized_steps:
            if s.get("action") == "capture_text" and s.get("context_key"):
                output_contract.append({
                    "name": s["context_key"],
                    "context_key": s["context_key"],
                    "value_type": "string",
                    "description": None,
                })

    # ── Build case ──
    normalized_case: dict[str, Any] = {
        "name": raw.get("name") or payload.prompt.strip()[:200] or "AI 生成用例",
        "description": raw.get("description") or payload.prompt.strip()[:500],
        "base_url": raw.get("base_url") or case_base_url,
        "input_contract": input_contract,
        "output_contract": output_contract,
        "steps": normalized_steps,
    }

    if not normalized_case.get("base_url"):
        raise DslGenerationError(
            "DSL 生成失败：缺少入口 URL（base_url 为空）。"
            "请确认 AI 已从测试需求中提取到 entry_url_or_page 字段。"
        )
    if not normalized_case["steps"]:
        raise DslGenerationError(
            "DSL 生成失败：未生成任何步骤。请检查页面元素采集是否正常，或入口 URL 是否可达。"
        )

    case = DSLCase.model_validate(normalized_case)

    generation_meta = GenerateDslMeta(
        model=model,
        generation_mode="draft",
        import_mode=payload.import_mode,
        prompt_variant="baseline_draft",
        context_profile="blank_request",
        active_governance_focus_reasons=["context_mismatch", "bad_contracts"],
        risk_flags=[],
        base_url_source="ai_output" if case.base_url else "request",
        base_url_backfilled=False,
        repaired_invalid_actions=0,
        removed_invalid_steps=0,
        removed_invalid_contracts=0,
        preserve_contracts_applied=False,
        used_current_case_context=False,
        used_current_steps_context=False,
    )

    all_notes.append(f"单次生成：{len(normalized_steps)} 步，{len(input_contract)} 个输入变量")

    logger.info("DSL generation complete: %d steps, %d input_contracts", len(normalized_steps), len(input_contract))
    return case, all_warnings, all_notes, generation_meta


# ── Public API (compatibility wrappers) ────────────────────────────────────────

def resolve_prompt_version(payload: GenerateDslRequest) -> str:
    if payload.retry_reason_code is None:
        return AI_DSL_PROMPT_VERSION
    return f"{AI_DSL_PROMPT_VERSION}+retry.{payload.retry_reason_code}"


def resolve_generation_mode(
    request_generation_mode: GenerateDslMode | None,
    *,
    settings=None,
) -> GenerateDslMode:
    if request_generation_mode is not None:
        return request_generation_mode
    active_settings = settings or get_settings()
    return "strict_steps_only" if active_settings.ai_dsl_strict_mode else "draft"


def resolve_generation_profile(
    *,
    payload: GenerateDslRequest,
    generation_mode: GenerateDslMode,
) -> tuple[DslGenerationPromptVariant, DslGenerationContextProfile]:
    if payload.import_mode == "contracts_only":
        return "contracts_focus", "contracts_focus"
    if generation_mode == "strict_steps_only" and payload.current_steps:
        return "repair_steps", "repair_steps"
    if payload.current_case is not None:
        return "rewrite_from_case", "rewrite_from_case"
    return "baseline_draft", "blank_request"


# ── Governance constants ───────────────────────────────────────────────────────

DEFAULT_GOVERNANCE_REJECTION_REASONS: tuple = ("context_mismatch", "bad_contracts")
SETTLED_GOVERNANCE_REJECTION_REASONS: tuple = ("wrong_actions", "invalid_structure")


# ── Backwards compatibility alias ──────────────────────────────────────────────
# Old callers import generate_segmented_case_draft; redirect to new function.

generate_segmented_case_draft = generate_case_draft
