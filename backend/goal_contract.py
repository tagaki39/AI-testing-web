"""S2 Goal Contract：一次描述完整目标阶段，不生成 locator 或 DSL。"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from goal_semantics import (
    CONTRACT_OBLIGATIONS,
    required_semantics,
    semantic_labels,
)


MilestoneType = Literal[
    "auth", "navigate", "input", "action", "terminal_action", "verify",
]
MilestoneExecution = Literal["explorer", "runner"]

_VALUE_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

_VERIFY_GOAL_RE = re.compile(r"(验证|校验|断言|verify|assert)", re.IGNORECASE)
_CREDENTIAL_INPUT_KEYS = {"username", "email", "password"}


class GoalContractError(ValueError):
    """Goal Contract 无法安全生成或验证。"""


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Milestone(_ContractModel):
    """一个可由现有事实源判断进度的目标阶段。"""

    id: str = Field(pattern=r"^m[1-9]\d*$")
    type: MilestoneType
    intent: str = Field(min_length=1, max_length=160)
    target_terms: list[str] = Field(default_factory=list, max_length=1)
    field_terms: list[str] = Field(default_factory=list, max_length=1)
    value_ref: str | None = None
    execution: MilestoneExecution = "explorer"

    @model_validator(mode="after")
    def _validate_shape(self) -> "Milestone":
        self.target_terms = _clean_terms(self.target_terms)
        self.field_terms = _clean_terms(self.field_terms)

        if self.type == "input":
            if self.target_terms:
                raise ValueError("input milestone 不允许 target_terms")
            if len(self.field_terms) != 1:
                raise ValueError("input milestone 必须且只能提供一个 field_terms")
            if not self.value_ref:
                raise ValueError("input milestone 必须提供一个 value_ref")
            if self.execution != "explorer":
                raise ValueError("input milestone 必须 execution=explorer")
            if _VALUE_REF_RE.fullmatch(self.field_terms[0]):
                raise ValueError("input field_terms 必须是字段名，不能是 value_ref")
            return self

        if len(self.target_terms) != 1:
            raise ValueError(f"{self.type} milestone 必须且只能提供一个 target_terms")
        if self.field_terms:
            raise ValueError(f"{self.type} milestone 不允许 field_terms")
        if self.value_ref is not None:
            raise ValueError(f"{self.type} milestone 不允许 value_ref")

        expected_execution = (
            "runner" if self.type in {"terminal_action", "verify"}
            else "explorer"
        )
        if self.execution != expected_execution:
            raise ValueError(
                f"{self.type} milestone 必须 execution={expected_execution}")
        return self


class GoalContract(_ContractModel):
    """按顺序执行的唯一目标阶段契约。"""

    version: Literal["s2.v2"] = "s2.v2"
    milestones: list[Milestone] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _validate_order(self) -> "GoalContract":
        expected = [f"m{i}" for i in range(1, len(self.milestones) + 1)]
        actual = [m.id for m in self.milestones]
        if actual != expected:
            raise ValueError(
                "milestone id 必须按 m1..mN 连续排列且不得重复"
            )
        runner_seen = False
        for milestone in self.milestones:
            if milestone.execution == "runner":
                runner_seen = True
            elif runner_seen:
                raise ValueError("runner milestone 之后不能再出现 explorer milestone")
        return self


GOAL_CONTRACT_SYSTEM_PROMPT = """你是 Web 测试目标分解器。
只输出严格 JSON。你只描述目标阶段，不生成 selector、ref、CSS、XPath、URL 或 DSL。"""


GOAL_CONTRACT_PROMPT = """把下面的已脱敏 Web 测试目标拆成顺序 milestones。

目标：{goal}
可用 Runtime Input Keys：{input_keys}

只输出以下 JSON 结构：
{{
      "version": "s2.v2",
  "milestones": [
    {{
      "id": "m1",
      "type": "auth|navigate|input|action|terminal_action|verify",
      "intent": "简短阶段意图",
      "target_terms": ["从目标原文逐字复制的短语"],
      "field_terms": ["从目标原文逐字复制的字段短语"],
      "value_ref": "${{runtime_key}} 或 null",
      "execution": "explorer|runner"
    }}
  ]
}}

规则：
1. milestone id 必须是连续的 m1..mN，最多 8 个。
2. 一个 milestone 只描述一个 obligation。target_terms/field_terms 都最多一个，且只能逐字复制目标中的非敏感短语。
3. 每个 Runtime Input Key 必须有独立 input：field_terms 恰好一个字段名，value_ref 恰好一个 ${{key}}；input 的 target_terms 为空。
4. auth 只表示 Login/Submit 产生的认证状态迁移，不携带 field_terms/value_ref；账号和密码必须拆成独立 input。
5. navigate 表示入口之后的页面跳转；action 表示 Explorer 可安全真实执行的动作（如 Add to cart）。二者 execution=explorer。
6. 生成、发布、支付、删除、提交、checkout 等终端副作用用 terminal_action，execution=runner。
7. 结果验证用 verify，execution=runner。READY_FOR_RUNNER 是系统派生状态，禁止输出 ready milestone。
8. 非 input milestone 的 field_terms 必须为空且 value_ref=null。禁止输出 target_ref、selector、css、xpath、locator、DSL step 或真实凭据。
9. 入口 URL 的打开（goto）不是 milestone。
"""


def _clean_terms(terms: list[str]) -> list[str]:
    cleaned: list[str] = []
    for term in terms:
        value = " ".join(str(term).split()).strip()
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(normalized.split())


def _is_goal_fragment(term: str, goal: str) -> bool:
    return _normalize_text(term) in _normalize_text(goal)


def validate_goal_contract(
    contract: GoalContract,
    goal: str,
    input_keys: set[str] | None = None,
) -> GoalContract:
    """验证所有自由文本和值引用均受原目标与 runtime key 约束。"""
    keys = input_keys or set()
    for milestone in contract.milestones:
        for term in milestone.target_terms + milestone.field_terms:
            if not _is_goal_fragment(term, goal):
                raise GoalContractError(
                    f"milestone {milestone.id} 包含目标外短语: {term!r}"
                )
        if milestone.value_ref:
            match = _VALUE_REF_RE.fullmatch(milestone.value_ref)
            if match is None or match.group(1) not in keys:
                raise GoalContractError(
                    f"milestone {milestone.id} 引用了未声明的 runtime input"
                )
    return contract


def validate_goal_coverage(
    contract: GoalContract,
    goal: str,
    input_keys: set[str] | None = None,
) -> GoalContract:
    """薄语义校验：支持族必须有且只有一个正确类型的原子 obligation。"""
    milestones = contract.milestones

    # 每个出现在脱敏目标中的 runtime placeholder 必须有唯一 input obligation。
    required_keys = {
        key for key in (input_keys or set()) if f"${{{key}}}" in (goal or "")
    }
    for key in required_keys:
        matches = [m for m in milestones if m.value_ref == f"${{{key}}}"]
        if len(matches) != 1 or matches[0].type != "input":
            raise GoalContractError(
                f"runtime input {key!r} 必须对应唯一 input milestone")

    # 一个动作 milestone 不能同时承担两个受支持语义（例如“下单并支付”）。
    milestone_semantics: dict[str, tuple[str, ...]] = {}
    for milestone in milestones:
        if milestone.type not in {"auth", "action", "terminal_action"}:
            continue
        labels = semantic_labels(milestone.target_terms[0])
        if len(labels) > 1:
            raise GoalContractError(
                f"milestone {milestone.id} 同时包含多个动作语义: {labels}")
        milestone_semantics[milestone.id] = labels

    required = required_semantics(goal)
    for label in required:
        expected_type, expected_execution = CONTRACT_OBLIGATIONS[label]
        matches = [
            m for m in milestones
            if label in milestone_semantics.get(m.id, ())
        ]
        if len(matches) != 1:
            raise GoalContractError(
                f"目标要求 {label}，契约必须包含唯一对应 milestone")
        milestone = matches[0]
        if milestone.type != expected_type \
                or milestone.execution != expected_execution:
            raise GoalContractError(
                f"目标 {label} 必须映射为 {expected_type}/"
                f"{expected_execution}，实际为 {milestone.type}/"
                f"{milestone.execution}")

    if _VERIFY_GOAL_RE.search(goal or ""):
        verifies = [m for m in milestones if m.type == "verify"]
        if len(verifies) != 1:
            raise GoalContractError("目标要求验证，契约必须包含唯一 verify milestone")

    # 只约束已有确定性支持族的顺序，不建立万能自然语言排序器。
    index_by_id = {m.id: i for i, m in enumerate(milestones)}
    auth = next((m for m in milestones if m.type == "auth"), None)
    if auth is not None:
        auth_index = index_by_id[auth.id]
        credential_inputs = [
            m for m in milestones
            if m.type == "input"
            and m.value_ref
            and _VALUE_REF_RE.fullmatch(m.value_ref).group(1)
            in _CREDENTIAL_INPUT_KEYS
        ]
        if any(index_by_id[m.id] > auth_index for m in credential_inputs):
            raise GoalContractError("认证字段 input 必须位于 auth 之前")
        add_actions = [
            m for m in milestones
            if "add_to_cart" in milestone_semantics.get(m.id, ())
        ]
        if any(index_by_id[m.id] < auth_index for m in add_actions):
            raise GoalContractError("add_to_cart action 不能位于 auth 之前")

    add_action = next((
        m for m in milestones
        if "add_to_cart" in milestone_semantics.get(m.id, ())
    ), None)
    if add_action is not None:
        add_index = index_by_id[add_action.id]
        if any(index_by_id[m.id] > add_index
               for m in milestones if m.type == "navigate"):
            raise GoalContractError("navigate milestone 不能位于 add_to_cart action 之后")

    return contract


def parse_goal_contract(
    text: str,
    goal: str,
    input_keys: set[str] | None = None,
) -> GoalContract:
    """从 LLM 文本中提取并严格验证唯一 JSON 对象。"""
    start = (text or "").find("{")
    end = (text or "").rfind("}")
    if start < 0 or end <= start:
        raise GoalContractError("Goal Contract 响应不包含 JSON 对象")
    try:
        payload = json.loads(text[start:end + 1])
        contract = GoalContract.model_validate(payload)
    except Exception as exc:
        raise GoalContractError(f"Goal Contract schema 无效: {exc}") from exc
    contract = canonicalize_goal_contract(contract, goal)
    contract = validate_goal_contract(contract, goal, input_keys)
    return validate_goal_coverage(contract, goal, input_keys)


def canonicalize_goal_contract(contract: GoalContract, goal: str) -> GoalContract:
    """只做无语义猜测的规范化；类型错误必须由语义校验拒绝。"""
    if goal is None:
        raise GoalContractError("canonicalize_goal_contract 必须显式传入 goal")
    milestones = [
        m.model_copy(update={"id": f"m{i}"})
        for i, m in enumerate(contract.milestones, start=1)
    ]
    return GoalContract(milestones=milestones)


def build_goal_contract(
    goal: str,
    llm_call,
    input_keys: set[str] | None = None,
    *,
    timeout_s: int = 20,
) -> GoalContract:
    """生成 Goal Contract；最多一次 constrained retry（带上次错误摘要），
    仍不合法 → GoalContractError（fail closed，不静默降级旧模式）。"""
    prompt = GOAL_CONTRACT_PROMPT.format(
        goal=goal,
        input_keys=", ".join(sorted(input_keys or set())) or "(无)",
    )
    try:
        response = llm_call(
            prompt,
            system_prompt=GOAL_CONTRACT_SYSTEM_PROMPT,
            timeout=timeout_s,
        )
        return parse_goal_contract(response, goal, input_keys)
    except GoalContractError as exc:
        # constrained retry ×1：精简错误摘要 + 明确 JSON 骨架（不嵌入坏输出
        # 全文——错误细节会污染输出格式，实测 LLM 据此输出 {"goal":...} 走偏）
        retry_prompt = (
            "上一次输出未通过校验（错误摘要："
            f"{str(exc)[:200]}）。\n\n"
            "严格按此 JSON 骨架输出，只替换内容、不要改动结构：\n"
            '{"version": "s2.v2", "milestones": ['
            '{"id": "m1", "type": "auth|navigate|input|action|terminal_action|verify", '
            '"intent": "简短阶段意图", "target_terms": ["目标原文短语"], '
            '"field_terms": ["目标原文字段短语"], "value_ref": "${key}" 或 null, '
            '"execution": "explorer|runner"}]}\n\n'
            f"目标：{goal}\n"
            f"可用 Runtime Input Keys：{', '.join(sorted(input_keys or set())) or '(无)'}"
        )
        try:
            response = llm_call(
                retry_prompt,
                system_prompt=GOAL_CONTRACT_SYSTEM_PROMPT,
                timeout=timeout_s,
            )
            return parse_goal_contract(response, goal, input_keys)
        except GoalContractError as retry_exc:
            raise GoalContractError(
                f"Goal Contract 两次生成均不合法: {retry_exc}") from retry_exc
