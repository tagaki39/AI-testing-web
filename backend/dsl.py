"""
══════════════════════════════════════════════════════════════════════
dsl.py — DSL 数据结构定义（整个项目的"规则书"）
══════════════════════════════════════════════════════════════════════

【这个文件在项目中的位置】
  数据流的第一站和最后一站：
    用户输入 → AI 生成 JSON →【这里校验】→ 执行器 → 结果 JSON

【核心思想：安全边界】
  - AI 的输出是不可信的（可能格式错、字段错、凭空造 action）
  - 本文件用 Pydantic 声明"合法 DSL 长什么样"
  - 任何不符合规则的输入，在进入执行器之前就被拒绝
  - 这个文件只声明规则，不写任何执行逻辑

【Pydantic 是什么】
  一个第三方库：你声明规则（类型注解），它自动执行校验、类型转换、序列化。
  例如 action: Literal["goto", ...] 声明后，写 "hover" 就直接报错。

【学习路径】
  从上往下读：LocatorTarget（定位目标）→ LocatorScope（消歧容器）
  → DSLStep（单步）→ DSLCase（整个用例）→ validate_case（校验入口）
══════════════════════════════════════════════════════════════════════
"""

from pydantic import BaseModel, Field
from typing import Literal


class LocatorTarget(BaseModel):
    """结构化元素定位（比字符串格式更精确、无歧义）。

    四种定位方式任选其一，对应 Playwright 四种定位 API：
      {"role": "button", "name": "登录"}   → 语义定位（最稳，官方推荐，不需要埋点）
      {"text": "登录"}                     → 文本定位（无语义元素的兜底）
      {"test_id": "login-button"}          → data-testid（开发埋点后最稳，但需要对方配合）
      {"css": ".login-btn"}                → CSS 兜底（尽量避免，改版就坏）

    为什么需要它？字符串 "button=登录" 也能表达同样意思（见 DSLStep），
    但结构化对象让"解析"和"校验"都更可靠——字段名明确，不会拼错。
    """

    role: str | None = None      # 语义角色: button / link / textbox / heading ...
    name: str | None = None      # accessible name（元素在无障碍树里的名字，必须与 role 配套）
    text: str | None = None      # 可见文本（页面显示的字）
    test_id: str | None = None   # data-testid 属性值
    css: str | None = None       # CSS 选择器


class LocatorScope(BaseModel):
    """作用域：先定位"容器"，再在容器内找目标 → 解决同名元素歧义。

    典型场景：页面有 6 个 "Add to cart" 按钮，直接按名字找会歧义。
    解法：先锁定"包含 Blue Top 的容器"，再在容器内部找按钮。

      {"role": "listitem", "has_text": "Blue Top"}
        → page.get_by_role("listitem").filter(has_text="Blue Top")
        → 在返回的容器内继续找目标

      {"test_id": "product-card", "has_text": "Blue Top"}
        → 页面有 data-testid 时更精确

    这是 Playwright 推荐的 locator chaining（链式定位）思路：
    目标 = 容器.filter(...).get_by_role(...)
    """

    role: str | None = None
    test_id: str | None = None
    has_text: str | None = None  # 容器必须包含的文本（消歧的锚点）


class DSLStep(BaseModel):
    """一个测试步骤（DSL 的最小单元）。

    action 是 Literal 白名单——只允许这 5 种动作，
    AI 生成 "hover"、"scroll" 等不在名单里的动作会被 Pydantic 直接拒绝。

    target 支持两种格式（兼容 AI 生成的字符串 + 人工编辑的结构化对象）：
      字符串:  "button=登录" / "css=.x" / "登录"（纯文本兜底）
      结构化:  {"role": "button", "name": "登录"}

    scope 可选，用于同名元素消歧（见 LocatorScope）。
    """

    action: Literal["goto", "click", "input", "wait_for", "assert_text"]
    target: str | LocatorTarget | None = None
    scope: str | LocatorScope | None = None
    value: str | None = None       # 输入值 / 断言文本 / 目标 URL
    timeout_ms: int = 15000        # 单步超时（毫秒）


class DSLCase(BaseModel):
    """一个完整测试用例 = 元信息 + 步骤列表 + 变量契约。

    input_contract 是"变量声明"：AI 不知道用户的账号密码，
    就在 DSL 里声明 ${email}，执行时由前端/用户填入真实值。
    """

    name: str = Field(min_length=1)              # Field 追加约束：至少 1 个字符
    description: str | None = None
    base_url: str | None = None           # 入口 URL（goto 相对路径时拼接用）
    steps: list[DSLStep] = Field(min_length=1)   # 至少 1 步，空用例直接拒绝
    input_contract: list[dict] = []       # 变量定义，如 [{"key":"email","value":"test@x.com"}]


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
