"""Service layer for DSL anti-pattern management.

Anti-patterns are stored examples of wrong DSL generation, used as few-shot
negative examples in the DSL generator prompt to help the LLM avoid repeating
the same mistakes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models.dsl_anti_pattern import DSLAntiPattern

logger = logging.getLogger(__name__)

# Maximum anti-patterns to inject per generation request
MAX_INJECT = 3

# Error category constants
MISSING_NAVIGATION = "missing_navigation"
MISSING_WAIT_FOR = "missing_wait_for"
MISSING_INPUT_BEFORE_ASSERT = "missing_input_before_assert"
MISSING_CAPTURE_TEXT = "missing_capture_text"
TARGET_NOT_FOUND = "target_not_found"
MISSING_STEP = "missing_step"
WRONG_PAGE_STATE = "wrong_page_state"


def _fingerprint(snippet: dict[str, Any]) -> str:
    """Generate a stable fingerprint for a step snippet to detect duplicates."""
    # Normalize: sort keys, strip whitespace
    normalized = json.dumps(snippet, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def record_anti_pattern(
    db_session: Session,
    *,
    error_category: str,
    wrong_snippet: dict[str, Any],
    context_note: str | None = None,
    rule_violated: str | None = None,
    source: str = "auto",
    project_id: int | None = None,
) -> DSLAntiPattern:
    """Record an anti-pattern, incrementing frequency if a duplicate exists.

    Returns the created or updated DSLAntiPattern.
    """
    fingerprint = _fingerprint(wrong_snippet)

    # Check for existing anti-pattern with same category and similar snippet
    existing = db_session.execute(
        select(DSLAntiPattern).where(
            DSLAntiPattern.error_category == error_category,
            DSLAntiPattern.project_id == project_id,
        )
    ).scalars().all()

    for anti in existing:
        if _fingerprint(anti.wrong_snippet) == fingerprint:
            anti.frequency += 1
            db_session.flush()
            logger.info(
                "Anti-pattern frequency incremented: category=%s, id=%d, freq=%d",
                error_category, anti.id, anti.frequency,
            )
            return anti

    # Create new anti-pattern
    anti = DSLAntiPattern(
        project_id=project_id,
        error_category=error_category,
        wrong_snippet=wrong_snippet,
        context_note=context_note,
        rule_violated=rule_violated,
        source=source,
        frequency=1,
    )
    db_session.add(anti)
    db_session.flush()
    logger.info(
        "Anti-pattern recorded: category=%s, source=%s, project_id=%s",
        error_category, source, project_id,
    )
    return anti


def retrieve_relevant_anti_patterns(
    db_session: Session,
    *,
    project_id: int | None = None,
    prompt_text: str = "",
    page_elements: str = "",
    retry_reason_code: str | None = None,
    limit: int = MAX_INJECT,
) -> list[DSLAntiPattern]:
    """Retrieve anti-patterns relevant to the current generation request.

    Selection strategy:
    1. Same project first, then global (project_id IS NULL)
    2. Match retry_reason_code to error_category
    3. Keyword matching from prompt_text
    4. Frequency-based ordering as tiebreaker
    """
    if limit <= 0:
        return []

    # Build candidate set: same project + global
    candidates: list[DSLAntiPattern] = []
    if project_id is not None:
        candidates.extend(
            db_session.execute(
                select(DSLAntiPattern).where(
                    DSLAntiPattern.project_id == project_id,
                )
            ).scalars().all()
        )
    # Always include global patterns
    global_patterns = db_session.execute(
        select(DSLAntiPattern).where(
            DSLAntiPattern.project_id.is_(None),
        )
    ).scalars().all()
    candidates.extend(global_patterns)

    if not candidates:
        return []

    # Score each candidate
    scored: list[tuple[float, DSLAntiPattern]] = []
    prompt_lower = prompt_text.lower() if prompt_text else ""
    elements_lower = page_elements.lower() if page_elements else ""

    # Map retry reason codes to error categories
    _retry_category_map = {
        "wrong_actions": (MISSING_NAVIGATION, MISSING_WAIT_FOR, MISSING_STEP),
        "invalid_structure": (MISSING_STEP, WRONG_PAGE_STATE),
        "context_mismatch": (TARGET_NOT_FOUND, WRONG_PAGE_STATE),
        "bad_contracts": (MISSING_CAPTURE_TEXT,),
    }

    for anti in candidates:
        score = 0.0

        # 1. Retry reason match (highest priority)
        if retry_reason_code and retry_reason_code in _retry_category_map:
            if anti.error_category in _retry_category_map[retry_reason_code]:
                score += 5.0

        # 2. Keyword matching from prompt
        if prompt_lower:
            snippet_str = json.dumps(anti.wrong_snippet, ensure_ascii=False).lower()
            # Extract meaningful keywords from prompt
            for keyword in ("login", "登录", "cart", "购物车", "brand", "品牌",
                            "form", "表单", "search", "搜索", "assert", "click", "input"):
                if keyword in prompt_lower and keyword in snippet_str:
                    score += 1.0

        # 3. Page structure matching
        if elements_lower:
            snippet_str = json.dumps(anti.wrong_snippet, ensure_ascii=False).lower()
            if "<table" in elements_lower and "assert_text" in snippet_str:
                score += 1.0
            if "<input" in elements_lower and "input" in snippet_str:
                score += 0.5

        # 4. Source priority: execution evidence > preflight > auto
        if anti.source == "execution":
            score += 3.0
        elif anti.source == "preflight":
            score += 1.5

        # 5. Frequency bonus (capped)
        score += min(anti.frequency / 10, 2.0)

        scored.append((score, anti))

    # Sort by score descending, then frequency descending
    scored.sort(key=lambda x: (-x[0], -x[1].frequency))

    # Take top N with score > 0
    result = [anti for score, anti in scored[:limit] if score > 0]
    return result


def format_anti_patterns_for_prompt(anti_patterns: list[DSLAntiPattern]) -> str:
    """Format anti-patterns as few-shot negative examples for the DSL generator prompt."""
    if not anti_patterns:
        return ""

    lines = ["", "━━━ 常见错误示例（请避免重复以下模式）━━━", ""]

    for i, anti in enumerate(anti_patterns, 1):
        snippet_json = json.dumps(anti.wrong_snippet, ensure_ascii=False, indent=2)
        lines.append(f"❌ 错误 #{i} [{anti.error_category}]:")
        lines.append(f"步骤: {snippet_json}")
        if anti.context_note:
            lines.append(f"问题: {anti.context_note}")
        if anti.rule_violated:
            lines.append(f"规则: {anti.rule_violated}")
        lines.append("")

    return "\n".join(lines)
