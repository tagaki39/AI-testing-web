"""
explore/ — bounded exploration 包
  职责分离（R3 拆分，R4 合并 policy 回 explorer）：
    observation.py   状态观察（ARIA → canonical obs id）
    action_space.py  可操作性候选过滤（Restrict）
    explorer.py      主循环 + LLM 决策策略（observe → choose → execute → transition）
"""
from .observation import (
    ExploreState, _parse_elements, _record_page, _observe, _safe_title,
    _attach_legacy_dom_scope, _observe_until_stable,
)
from .action_space import (
    ACTION_CAPABILITIES, _build_action_space, _locator_for_element,
    validate_actionability, _validate_action_target,
)
from .explorer import (
    explore, _within_origin,   # R4：policy 已合并回 explorer；_act 已由 execution/action_executor 取代
    GOAL_ACTION_PATTERNS, _ACTION_KEYWORDS, goal_requires_actions,
    _decide, _detect_auth_failure, _detect_error_page,
    _is_repeated_no_progress, _validate_completion, DECIDE_PROMPT,
    missing_verified_goal_actions,
    EXPLORE_SYSTEM_PROMPT, _elements_to_prompt,
)
# R3.1：执行超时常量已迁入执行层（Browser Action Executor）——
# 保留此重导出以免破坏既有引用
from execution.action_executor import EXPLORE_ACTION_TIMEOUT_MS

__all__ = [
    "ExploreState", "explore", "_decide", "_record_page", "_observe",
    "_build_action_space", "validate_actionability", "_validate_action_target",
    "_validate_completion", "_detect_auth_failure", "_is_repeated_no_progress",
    "GOAL_ACTION_PATTERNS", "_ACTION_KEYWORDS", "DECIDE_PROMPT",
]
