"""DSL 结构化测试用例定义（Pydantic 强校验）。

这是整个系统的"安全边界"：
- AI 生成的输出必须通过这里的校验才能执行
- 非法 action、缺少必填字段会直接拒绝，不会进到执行器
"""

from pydantic import BaseModel, Field
from typing import Literal


class LocatorTarget(BaseModel):
    """结构化元素定位（精确、无歧义，比字符串格式更稳）。

    四种定位方式任选其一：
      {"role": "button", "name": "登录"}   → 语义定位（最稳，官方推荐）
      {"text": "登录"}                     → 文本定位
      {"test_id": "login-button"}          → data-testid（开发埋点后最稳）
      {"css": ".login-btn"}                → CSS 兜底（尽量避免）
    """

    role: str | None = None      # 语义角色: button / link / textbox / heading ...
    name: str | None = None      # accessible name（必须与 role 配套使用）
    text: str | None = None      # 可见文本
    test_id: str | None = None   # data-testid 属性值
    css: str | None = None       # CSS 选择器


class LocatorScope(BaseModel):
    """作用域：先定位"容器"，再在容器内找目标 → 解决同名元素歧义。

    典型场景：页面有 6 个 "Add to cart" 按钮，需要先锁定
    包含 "Blue Top" 的容器，再在里面找按钮。

      {"role": "listitem", "has_text": "Blue Top"}   → 找包含 Blue Top 的列表项
      {"test_id": "product-card", "has_text": "Blue Top"}  → 找对应商品卡片
    """

    role: str | None = None
    test_id: str | None = None
    has_text: str | None = None  # 容器必须包含的文本


class DSLStep(BaseModel):
    """一个测试步骤。

    target 支持两种格式：
      字符串（兼容 AI 生成）: "button=登录" / "css=.x" / "登录"（纯文本）
      结构化（更精确）: {"role": "button", "name": "登录"}

    scope 可选，用于消歧：字符串 "Blue Top" 或结构化 {"role": ..., "has_text": ...}
    """

    action: Literal["goto", "click", "input", "wait_for", "assert_text"]
    target: str | LocatorTarget | None = None
    scope: str | LocatorScope | None = None
    value: str | None = None       # 输入值 / 断言文本 / 目标 URL
    timeout_ms: int = 15000        # 单步超时


class DSLCase(BaseModel):
    """一个完整测试用例。"""

    name: str = Field(min_length=1)
    description: str | None = None
    base_url: str | None = None           # 入口 URL
    steps: list[DSLStep] = Field(min_length=1)
    input_contract: list[dict] = []       # 变量定义，如 [{"key":"email","value":"test@x.com"}]


def validate_case(data: dict) -> DSLCase:
    """校验 AI 生成的 DSL，非法直接抛异常。"""
    return DSLCase.model_validate(data)
