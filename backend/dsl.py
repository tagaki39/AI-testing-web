"""DSL 结构化测试用例定义（Pydantic 强校验）。

这是整个系统的"安全边界"：
- AI 生成的输出必须通过这里的校验才能执行
- 非法 action、缺少必填字段会直接拒绝，不会进到执行器
"""

from pydantic import BaseModel, Field
from typing import Literal


class DSLStep(BaseModel):
    """一个测试步骤。

    target 支持三种格式（定位策略）：
      "button=登录"      → 语义定位（Playwright getByRole，最稳，官方推荐）
      "textbox=邮箱"     → 同上
      "css=.login-btn"   → CSS 兜底（尽量避免）
      纯文本 "欢迎回来"    → 文本定位（getByText）
    """

    action: Literal["goto", "click", "input", "wait_for", "assert_text"]
    target: str | None = None      # 元素定位（goto 不需要）
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
