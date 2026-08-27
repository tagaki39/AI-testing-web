"""S2 Goal Contract：一次描述完整目标阶段，不生成 locator 或 DSL。"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MilestoneType = Literal[
    "auth", "navigate", "input", "ready", "side_effect", "verify",
]
MilestoneExecution = Literal["explorer", "runner"]

_VALUE_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class GoalContractError(ValueError):
    """Goal Contract 无法安全生成或验证。"""


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Milestone(_ContractModel):
    """一个可由现有事实源判断进度的目标阶段。"""

    id: str = Field(pattern=r"^m[1-9]\d*$")
    type: MilestoneType
    intent: str = Field(min_length=1, max_length=160)
    target_terms: list[str] = Field(default_factory=list, max_length=6)
    field_terms: list[str] = Field(default_factory=list, max_length=6)
    value_ref: str | None = None
    execution: MilestoneExecution = "explorer"

    @model_validator(mode="after")
    def _validate_shape(self) -> "Milestone":
        self.target_terms = _clean_terms(self.target_terms)
        self.field_terms = _clean_terms(self.field_terms)

        if self.type in {"navigate", "ready", "side_effect", "verify"} \
                and not self.target_terms:
            raise ValueError(f"{self.type} milestone 必须提供 target_terms")
        if self.type == "input" and not (self.field_terms or self.value_ref):
            raise ValueError("input milestone 必须提供 field_terms 或 value_ref")
        if self.type == "side_effect" and self.execution != "runner":
            raise ValueError("side_effect 必须声明 execution=runner")
        if self.type != "side_effect" and self.execution == "runner" \
                and self.type != "verify":
            raise ValueError(f"{self.type} milestone 不能声明 execution=runner")
        return self


class GoalContract(_ContractModel):
    """按顺序执行的唯一目标阶段契约。"""

    version: Literal["s2.v1"] = "s2.v1"
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
  "version": "s2.v1",
  "milestones": [
    {{
      "id": "m1",
      "type": "auth|navigate|input|ready|side_effect|verify",
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
2. target_terms/field_terms 只能逐字复制目标中出现的非敏感短语，不得翻译或补写。
3. value_ref 只能引用上面列出的 Runtime Input Keys；没有则为 null。
4. 登录/认证用 auth；进入目标页面用 navigate（target_terms 只填页面名词短语，如"图片生成"，禁止动词开头整句）；填写字段用 input；目标按钮可用用 ready。
5. 生成、发布、支付、删除、提交等终端副作用用 side_effect，execution 必须是 runner。
6. 结果验证用 verify；其余 milestone 的 execution 为 explorer。
7. 禁止输出 target_ref、selector、css、xpath、locator、DSL step、真实凭据或目标外文本。
8. 入口 URL 的打开（goto）不是 milestone——navigate 只描述入口之后的目标页面跳转。
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
    return validate_goal_contract(contract, goal, input_keys)


def build_goal_contract(
    goal: str,
    llm_call,
    input_keys: set[str] | None = None,
    *,
    timeout_s: int = 20,
) -> GoalContract:
    """一次 LLM 调用生成 Goal Contract；失败时不重试、不猜测。"""
    try:
        response = llm_call(
            GOAL_CONTRACT_PROMPT.format(
                goal=goal,
                input_keys=", ".join(sorted(input_keys or set())) or "(无)",
            ),
            system_prompt=GOAL_CONTRACT_SYSTEM_PROMPT,
            timeout=timeout_s,
        )
    except Exception as exc:
        raise GoalContractError(f"Goal Contract 调用失败: {exc}") from exc
    return parse_goal_contract(response, goal, input_keys)

