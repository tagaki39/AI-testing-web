"""
══════════════════════════════════════════════════════════════════════
dsl.py — DSL 数据结构定义（整个项目的"规则书"）
══════════════════════════════════════════════════════════════════════

【这个文件在项目中的位置】
  数据流的第一站和最后一站：
    用户输入 → AI 生成 JSON →【这里校验】→ 执行器 → 结果 JSON

【v2 结构化设计（吸收评审）】
  1. target 从字符串升级为结构化 Locator 模型：
       {"role": "button", "name": "登录"}      ← 语义定位
       {"text": "Products"}                    ← 文本定位
       {"test_id": "login-button"}             ← data-testid
       {"css": ".btn"}                         ← CSS 兜底
     不再需要自创 "button=登录 inside=... exact=true" 解析语言
  2. scope 独立成 Scope 模型：role/test_id + has_text 组合
  3. 动作集扩展：goto/click/fill/select/check/wait_for/
     assert_visible/assert_text/assert_url（9 种）
  4. 向后兼容：target/scope 仍接受字符串写法（旧用例不破坏）

【Pydantic 是什么】
  一个第三方库：你声明规则（类型注解），它自动执行校验、类型转换、序列化。

【学习路径】
  从上往下读：Locator（定位目标）→ Scope（消歧容器）
  → DSLStep（单步）→ DSLCase（整个用例）→ validate_case（校验入口）
══════════════════════════════════════════════════════════════════════
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal


class DSLModel(BaseModel):
    """所有 DSL 模型基类：拒绝未知字段（extra=forbid）。

    修复：LLM 输出 {"target": {..., "xpath": "..."}} 时，
    未知字段不再被静默丢弃——AI 以为 xpath 生效，代码其实丢了，
    这种"虚假生效"比报错更危险。
    """
    model_config = ConfigDict(extra="forbid")


class Locator(DSLModel):
    """结构化元素定位（v2：多字段组合，按优先级解析）。

    role + name → 语义定位（最稳，官方推荐，不需要埋点）
    test_id     → data-testid 定位（需要被测系统埋点）
    text        → 文本定位（无语义元素的兜底）
    css         → CSS 兜底（尽量避免）

    为什么结构化而不是字符串 "button=登录"？
      字符串会膨胀成自创解析语言（button=Add to cart inside=Blue Top exact=true）；
      结构化让 Pydantic 校验、Preflight 验证、patch 修复全部简单可靠。
    """

    role: str | None = None      # 语义角色: button / link / textbox / heading ...
    name: str | None = None      # accessible name（与 role 配套）
    test_id: str | None = None   # data-testid 属性值
    text: str | None = None      # 可见文本
    css: str | None = None       # CSS 选择器

    @model_validator(mode="after")
    def _require_one_field(self) -> "Locator":
        """至少一个定位字段（修复：空 Locator 无意义却可通过校验）。"""
        if not any([self.role, self.name, self.text, self.test_id, self.css]):
            raise ValueError("Locator 至少需要 role/name/text/test_id/css 之一")
        return self


class Scope(DSLModel):
    """作用域：先定位"容器"，再在容器内找目标 → 解决同名元素歧义。

    典型场景：页面有 6 个 "Add to cart"，先锁定包含 Blue Top 的容器。
      {"role": "listitem", "has_text": "Blue Top"}
      {"test_id": "product-card", "has_text": "Blue Top"}
      {"has_text": "Blue Top"}（不知道容器角色时——执行器会降级爬父级）
    """

    role: str | None = None
    test_id: str | None = None
    has_text: str | None = None  # 容器必须包含的文本（消歧的锚点）


class DSLStep(DSLModel):
    """一个测试步骤（DSL 的最小单元）。

    action 是 Literal 白名单——只允许这 9 种动作：
      goto / click / fill / select / check / wait_for /
      assert_visible / assert_text / assert_url

    target 支持结构化（Locator，推荐）和字符串（兼容旧用例）：
      结构化:  {"role": "button", "name": "登录"}
      字符串:  "button=登录" / "css=.x" / "登录"（纯文本兜底）

    scope 可选，用于同名元素消歧（见 Scope）。
    """

    action: Literal[
        "goto", "click", "fill", "input", "select", "check",
        "wait_for", "assert_visible", "assert_text", "assert_url",
    ]   # input 是 fill 的兼容别名（旧用例 / AI 偶尔输出）
    target: str | Locator | None = None
    scope: str | Scope | None = None
    value: str | None = None       # 输入值 / 选项文本 / 断言文本 / URL 片段
    timeout_ms: int = Field(default=15000, ge=100, le=60000)   # 单步超时（毫秒）
    observation_ref: str | None = None   # 该步骤基于哪个观察到的页面状态生成
                                          # （grounding provenance + Preflight 验证上下文；
                                          #  不参与 Runner 执行）
    target_ref: str | None = None   # G1：state-scoped 元素引用（"obs3:e17"）——
                                     # Planner 优先引用系统观察到的真实元素；
                                     # 执行时回退到 target 语义（R1 Compiler 接管前）

    @model_validator(mode="after")
    def _check_required_fields(self) -> "DSLStep":
        """action 级必填校验（修复：类型合法但业务语义非法的 DSL 也能通过）。

        例：click 无 target / goto 无 value / assert_url 无 value——
        Pydantic 类型校验管不到，这里按 action 语义强制。

        G3 refs-only：target 与 target_ref 二选一（或都有）——
        Planner 输出只有 target_ref，target 由 Compiler 确定性编译填入；
        两者皆无的步骤在生成链路会被 refs-only 检查/恢复拦截。
        """
        if self.action == "goto" and not self.value:
            raise ValueError("goto 必须提供 value（URL）")
        if self.action in {"click", "check", "wait_for", "assert_visible"} \
                and self.target is None and self.target_ref is None:
            raise ValueError(f"{self.action} 必须提供 target 或 target_ref")
        if self.action in {"fill", "input", "select"} \
                and ((self.target is None and self.target_ref is None)
                     or self.value is None):
            raise ValueError(f"{self.action} 必须提供 target/target_ref 和 value")
        if self.action in {"assert_text", "assert_url"} and not self.value:
            raise ValueError(f"{self.action} 必须提供 value")
        return self


class InputContractItem(DSLModel):
    """变量契约（v2：结构化 schema）。

    关键字段 secret：标记敏感信息（密码/token）——执行器本地注入，
    LLM 上下文中永远只有 ${key} 占位符，没有真实值。
    """

    key: str = Field(min_length=1)
    type: Literal["string", "number", "boolean", "file", "secret"] = "string"
    required: bool = True
    secret: bool = False
    default: str | None = None            # 默认值（可选）
    description: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy(cls, data):
        """兼容旧格式 {"key": "email", "value": "test@x.com"} → default。"""
        if isinstance(data, dict) and "value" in data and "default" not in data:
            data = dict(data)
            data["default"] = data.pop("value")
        return data


class DSLCase(DSLModel):
    """一个完整测试用例 = 元信息 + 步骤列表 + 变量契约。

    input_contract 是"变量声明"：AI 不知道用户的账号密码，
    就在 DSL 里声明 ${email}，执行时由前端/用户填入真实值。
    """

    name: str = Field(min_length=1)              # Field 追加约束：至少 1 个字符
    description: str | None = None
    base_url: str | None = None           # 入口 URL（goto 相对路径时拼接用）
    steps: list[DSLStep] = Field(min_length=1)   # 至少 1 步，空用例直接拒绝
    input_contract: list[InputContractItem] = Field(default_factory=list)  # 明确 default_factory


def validate_case(data: dict) -> DSLCase:
    """校验入口（安全边界）：dict → 校验通过的 DSLCase 对象。

    参数 data: 任意字典（AI 生成的 JSON 或前端传来的 DSL）
    返回: 校验通过的 DSLCase 对象
    抛出: pydantic.ValidationError（字段缺失/类型错/action 不在白名单）

    用 model_validate 而不是 DSLCase(**data) 的原因：
      - model_validate 会递归校验嵌套模型（steps 里每个 DSLStep 都校验）
      - 错误信息精确到具体字段，方便 AI 修复
    """
    return DSLCase.model_validate(data)
