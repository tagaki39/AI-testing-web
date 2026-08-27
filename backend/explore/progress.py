"""S2 Milestone Progress：只从 Observation/history/StateGraph 推导进度。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from goal_contract import GoalContract, Milestone


MilestoneStatus = Literal["completed", "current", "pending"]

_AUTH_TERMS = ("login", "sign in", "登录", "登陆", "登入")


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

    def as_dict(self) -> dict:
        return {
            "complete": self.complete,
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
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _contains_term(value: object, terms: list[str] | tuple[str, ...]) -> bool:
    haystack = _norm(value)
    return any(_norm(term) in haystack for term in terms if _norm(term))


def _verified_transitions(state) -> list[dict]:
    return [
        t for t in state.transitions
        if t.get("from") and t.get("to") and t.get("from") != t.get("to")
    ]


def _ref_elements(state) -> dict[str, dict]:
    return {
        element.get("ref", ""): element
        for observation in state.observations
        for element in observation.get("elements", [])
        if element.get("ref")
    }


def _current_observation(state) -> dict:
    return next((
        observation for observation in state.observations
        if observation.get("id") == state.current_obs
    ), {})


def _observation_evidence_text(observation: dict) -> str:
    parts = [observation.get("url", ""), observation.get("title", "")]
    for element in observation.get("elements", []):
        if element.get("kind") != "action":
            parts.extend([
                element.get("name", ""), element.get("text", ""),
                element.get("context_name", ""),
            ])
    return "\n".join(str(part) for part in parts if part)


def _action_text(history_item: dict, ref_map: dict[str, dict]) -> str:
    element = ref_map.get(history_item.get("target_ref") or "", {})
    target = history_item.get("target") or {}
    if isinstance(target, dict):
        target_text = " ".join(str(value) for value in target.values())
    else:
        target_text = str(target)
    return " ".join(filter(None, [
        str(element.get("name") or ""),
        str(element.get("context_name") or ""),
        target_text,
    ]))


def _derive_fact(milestone: Milestone, state) -> tuple[bool, tuple[str, ...]]:
    transitions = _verified_transitions(state)
    ref_map = _ref_elements(state)
    current = _current_observation(state)

    if milestone.type == "auth":
        terms = tuple(milestone.target_terms) or _AUTH_TERMS
        matched = next((
            transition for transition in transitions
            if _contains_term(transition.get("target_name"), terms + _AUTH_TERMS)
        ), None)
        return (matched is not None, (f"transition:{matched.get('id', '?')}",)
                if matched else ())

    if milestone.type == "navigate":
        current_text = _observation_evidence_text(current)
        if _contains_term(current_text, milestone.target_terms):
            return True, (f"observation:{state.current_obs}",)
        matched = next((
            transition for transition in transitions
            if transition.get("to") == state.current_obs
            and _contains_term(transition.get("target_name"), milestone.target_terms)
        ), None)
        return (matched is not None, (f"transition:{matched.get('id', '?')}",)
                if matched else ())

    if milestone.type == "input":
        for index, item in enumerate(state.history):
            if item.get("action") != "fill" or item.get("error"):
                continue
            field_matches = not milestone.field_terms or _contains_term(
                _action_text(item, ref_map), milestone.field_terms)
            value_matches = not milestone.value_ref or \
                item.get("value") == milestone.value_ref
            if field_matches and value_matches:
                return True, (f"history:{index}",)
        return False, ()

    if milestone.type == "ready":
        matched = next((
            element for element in state.elements
            if element.get("kind") == "action" and not element.get("disabled")
            and _contains_term(
                " ".join(filter(None, [
                    str(element.get("name") or ""),
                    str(element.get("context_name") or ""),
                ])),
                milestone.target_terms,
            )
        ), None)
        return (matched is not None, (f"element:{matched.get('ref')}",)
                if matched else ())

    if milestone.type == "side_effect":
        # P1 兼容桥：契约已经声明 runner-only；P4 接管前，现有 Explorer
        # 仍可能执行该动作。这里只从成功 history 推导，不触发副作用。
        for index, item in enumerate(state.history):
            if item.get("error") or item.get("action") not in {
                "click", "check", "select", "press",
            }:
                continue
            if _contains_term(_action_text(item, ref_map), milestone.target_terms):
                return True, (f"history:{index}",)
        return False, ()

    if milestone.type == "verify":
        text = _observation_evidence_text(current)
        if _contains_term(text, milestone.target_terms):
            return True, (f"observation:{state.current_obs}",)
        return False, ()

    return False, ()


def derive_milestone_progress(contract: GoalContract, state) -> GoalProgress:
    """按顺序从现有事实推导进度；不修改 state，不新增第二状态源。"""
    progress: list[MilestoneProgress] = []
    current: Milestone | None = None
    blocked = False
    for milestone in contract.milestones:
        complete, evidence = _derive_fact(milestone, state)
        if not blocked and complete:
            status: MilestoneStatus = "completed"
        elif not blocked:
            status = "current"
            current = milestone
            blocked = True
        else:
            status = "pending"
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
    )

