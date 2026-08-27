"""S2 Milestone Progress：只从 Observation/history/StateGraph 推导进度。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from goal_contract import GoalContract, Milestone
from goal_semantics import ACTION_ALIASES


MilestoneStatus = Literal[
    "completed", "current", "ready_for_runner", "pending",
]

_AUTH_TERMS = ("login", "sign in", "登录", "登陆", "登入")
_ACTION_TERM_GROUPS = tuple(ACTION_ALIASES.values())


@dataclass(frozen=True)
class MilestoneProgress:
    milestone_id: str
    type: str
    status: MilestoneStatus
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class GoalProgress:
    milestones: tuple[MilestoneProgress, ...]
    current_milestone: Milestone | None
    complete: bool
    ready_for_runner: bool = False

    def as_dict(self) -> dict:
        return {
            "complete": self.complete,
            "ready_for_runner": self.ready_for_runner,
            "current_milestone": (
                self.current_milestone.id if self.current_milestone else None
            ),
            "milestones": [
                {
                    "id": item.milestone_id,
                    "type": item.type,
                    "status": item.status,
                    "evidence": list(item.evidence),
                }
                for item in self.milestones
            ],
        }


def _norm(value: object) -> str:
    """归一化：NFKC + casefold + 去全部空白（"登 录" 与 "登录" 等价）。"""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", "", text)


def _contains_term(value: object, terms: list[str] | tuple[str, ...]) -> bool:
    haystack = _norm(value)
    return any(_norm(term) in haystack for term in terms if _norm(term))


def _matches_action_terms(value: object,
                          terms: list[str] | tuple[str, ...]) -> bool:
    """动作名受控同义匹配；只用于当前可操作元素，不扩散到页面证据。"""
    if _contains_term(value, terms):
        return True
    normalized_terms = tuple(_norm(term) for term in terms if _norm(term))
    haystack = _norm(value)
    for group in _ACTION_TERM_GROUPS:
        aliases = tuple(_norm(alias) for alias in group)
        goal_in_group = any(
            alias in term or term in alias
            for term in normalized_terms
            for alias in aliases
        )
        if goal_in_group and any(alias in haystack for alias in aliases):
            return True
    return False


def _verified_transitions(state) -> list[dict]:
    return [
        t for t in state.transitions
        if t.get("from") and t.get("to") and t.get("from") != t.get("to")
    ]


def _current_observation(state) -> dict:
    return next((
        observation for observation in state.observations
        if observation.get("id") == state.current_obs
    ), {})


def _derive_fact(milestone: Milestone, state) -> tuple[bool, tuple[str, ...]]:
    transitions = _verified_transitions(state)

    if milestone.type == "auth":
        terms = tuple(milestone.target_terms) or _AUTH_TERMS
        matched = next((
            transition for transition in transitions
            if transition.get("milestone_id") == milestone.id
            and _contains_term(transition.get("target_name"), terms + _AUTH_TERMS)
        ), None)
        return (matched is not None, (f"transition:{matched.get('id', '?')}",)
                if matched else ())

    if milestone.type == "navigate":
        # typed evidence：navigate 完成 = 已真实到达目标页（URL/title
        # 页面身份证据），且只认入口 obs 或 verified transition.to 的 obs
        #（侧边栏菜单词一直存在 ≠ 到达——不匹配任意元素文本）。
        for observation in state.observations:
            is_entry = observation["id"] == (state.observations[0].get("id") if state.observations else None)
            is_reached = any(
                t.get("to") == observation["id"] and t.get("from") != t.get("to")
                for t in transitions
            )
            if not (is_entry or is_reached):
                continue
            text = f"{observation.get('url', '')}\n{observation.get('title', '')}"
            if _contains_term(text, milestone.target_terms):
                return True, (f"observation:{observation['id']}",)
        # verified 导航 transition（点击导航项含目标词 → to 即目标页）
        matched = next((
            transition for transition in transitions
            if transition.get("from") != transition.get("to")
            and transition.get("milestone_id") == milestone.id
            and _contains_term(transition.get("target_name"),
                               milestone.target_terms)
        ), None)
        return (matched is not None, (f"transition:{matched.get('id', '?')}",)
                if matched else ())

    if milestone.type == "input":
        for index, item in enumerate(state.history):
            # provenance 在动作执行成功时固化；不从字段同义词反推归属。
            if item.get("milestone_id") == milestone.id \
                    and item.get("action") == "fill" \
                    and item.get("value") == milestone.value_ref \
                    and item.get("ok") is True:
                return True, (f"history:{index}",)
        return False, ()

    if milestone.type == "action":
        matched = next((
            transition for transition in transitions
            if transition.get("milestone_id") == milestone.id
            and _matches_action_terms(
                transition.get("target_name"), milestone.target_terms)
        ), None)
        return (matched is not None, (f"transition:{matched.get('id', '?')}",)
                if matched else ())

    if milestone.type == "terminal_action":
        # Runner-only 里程碑永远不能由 Explorer history 标记完成。
        return False, ()

    if milestone.type == "verify":
        # 验证结果只能由 Runner postcondition 产生，Explorer 不宣告通过。
        return False, ()

    return False, ()


def _runner_readiness(milestone: Milestone, state) -> tuple[bool, tuple[str, ...]]:
    """判断 Runner 是否已有足够输入接管，不把 readiness 冒充完成事实。"""
    current = _current_observation(state)
    if milestone.type == "verify":
        # Planner 可以从当前 Observation 编译 postcondition；这不代表断言已通过。
        return (bool(current), (f"observation:{state.current_obs}",)
                if current else ())
    if milestone.type == "terminal_action":
        matched = next((
            element for element in state.elements
            if element.get("kind") == "action" and not element.get("disabled")
            and _matches_action_terms(
                " ".join(filter(None, [
                    str(element.get("name") or ""),
                    str(element.get("context_name") or ""),
                ])),
                milestone.target_terms,
            )
        ), None)
        return (matched is not None, (f"element:{matched.get('ref')}",)
                if matched else ())
    return False, ()


def derive_milestone_progress(contract: GoalContract, state) -> GoalProgress:
    """按顺序从现有事实推导进度；不修改 state，不新增第二状态源。"""
    progress: list[MilestoneProgress] = []
    current: Milestone | None = None
    blocked = False
    ready_for_runner = False
    for milestone in contract.milestones:
        if blocked:
            progress.append(MilestoneProgress(
                milestone_id=milestone.id,
                type=milestone.type,
                status="pending",
            ))
            continue
        if milestone.execution == "runner":
            ready, evidence = _runner_readiness(milestone, state)
            status = "ready_for_runner" if ready else "current"
            current = milestone
            ready_for_runner = ready
            blocked = True
            progress.append(MilestoneProgress(
                milestone_id=milestone.id,
                type=milestone.type,
                status=status,
                evidence=evidence if ready else (),
            ))
            continue
        complete, evidence = _derive_fact(milestone, state)
        if complete:
            status: MilestoneStatus = "completed"
        else:
            status = "current"
            current = milestone
            blocked = True
        progress.append(MilestoneProgress(
            milestone_id=milestone.id,
            type=milestone.type,
            status=status,
            evidence=evidence if status == "completed" else (),
        ))
    return GoalProgress(
        milestones=tuple(progress),
        current_milestone=current,
        complete=current is None,
        ready_for_runner=ready_for_runner,
    )
