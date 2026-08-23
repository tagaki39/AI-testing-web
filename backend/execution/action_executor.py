"""
══════════════════════════════════════════════════════════════════════
action_executor.py — Browser Action Executor（R3.1：统一浏览器动作执行）
══════════════════════════════════════════════════════════════════════

【这个文件在项目中的位置】
  Tool-driven 架构的执行层（docs/优化.txt 最终架构）：

    Agent（探索/规划）
      ↓
    Browser Action Tool ←【这里】
      ↓
    click_preprocessor → Playwright 动作

【核心原则（评审：Execute, don't predict）】
  不预测元素能不能点——scroll_into_view + Playwright 自动等待 +
  短超时直接执行，失败返回结构化 ToolResult（不抛异常）。
  探索循环据此做黑名单化；可见性/遮挡等证据由观察期 Page Explorer
  输出（explore/action_space.py 的 actionable 标记），不属于本层。

【与 Runner 的边界】
  本层是"动作执行"（点击/填充/按键/返回），不是"定位解析"——
  locator 由调用方经 Resolver 构建传入。探索用短超时快速试错，
  Runner 未来复用本层时按需传更严格 timeout（R3.4 拆包时评估）。

【学习路径】
  ToolResult（结构化结果）→ execute_action（唯一执行入口）
══════════════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass

from runner import _substitute

# 探索动作短超时（快速试错：失败即黑名单化，不长时间等待）
EXPLORE_ACTION_TIMEOUT_MS = 1500

# 不可逆/危险操作关键词（执行层安全闸口：点击前拦截，不只靠 Prompt）
_DESTRUCTIVE_PATTERNS = (
    "delete", "remove", "pay", "purchase", "submit order",
    "send", "publish", "sign out", "log out", "注销", "删除", "支付",
)


@dataclass
class ToolResult:
    """统一工具返回结构（评审：每个工具自己保证确定性 + 结构化结果）。

    ok=False 时 code 区分失败类别（黑名单化/反馈依据）：
      ACTION_FAILED        执行失败（超时/不可见/被遮挡等）
      DESTRUCTIVE_BLOCKED  危险操作拦截（安全闸口）
      UNKNOWN_ACTION       不支持的动作
    """
    ok: bool
    code: str | None = None
    message: str | None = None


def execute_action(
    page, *, action: str, locator, value: str = "",
    element_name: str = "", runtime_inputs: dict | None = None,
    timeout_ms: int = EXPLORE_ACTION_TIMEOUT_MS,
) -> ToolResult:
    """执行单个浏览器动作，返回 ToolResult（任何执行失败都不抛异常）。

    参数:
      page:          Playwright page
      action:        click / fill / press / back
      locator:       已解析的 Playwright locator（调用方经 Resolver 构建）
      value:         动作值（fill 支持 ${var}，执行时本地注入）
      element_name:  元素名称（危险操作拦截依据）
      runtime_inputs: ${key} → 真实值（敏感信息不进 LLM，本地注入）
      timeout_ms:    动作超时（探索短超时；正式执行可传更长）
    """
    if action == "back":
        try:
            page.go_back()
            return ToolResult(ok=True)
        except Exception as exc:
            return ToolResult(ok=False, code="ACTION_FAILED", message=str(exc)[:100])

    if action == "click":
        # 危险操作二次拦截（代码层，不只靠 Prompt）
        name = (element_name or "").lower()
        if any(p in name for p in _DESTRUCTIVE_PATTERNS):
            return ToolResult(
                ok=False, code="DESTRUCTIVE_BLOCKED",
                message=f"危险操作被拦截: {element_name!r}",
            )
        try:
            # click_preprocessor：滚动到视口（Playwright 自动等待可操作性）
            locator.scroll_into_view_if_needed(timeout=timeout_ms)
            locator.click(timeout=timeout_ms)
            return ToolResult(ok=True)
        except Exception as exc:
            return ToolResult(ok=False, code="ACTION_FAILED", message=str(exc)[:100])

    if action == "fill":
        try:
            locator.fill(
                _substitute(value, runtime_inputs or {}) or "",
                timeout=timeout_ms,
            )
            return ToolResult(ok=True)
        except Exception as exc:
            return ToolResult(ok=False, code="ACTION_FAILED", message=str(exc)[:100])

    if action == "press":
        try:
            locator.press(
                _substitute(value, runtime_inputs or {}) or "Enter",
                timeout=timeout_ms,
            )
            return ToolResult(ok=True)
        except Exception as exc:
            return ToolResult(ok=False, code="ACTION_FAILED", message=str(exc)[:100])

    return ToolResult(ok=False, code="UNKNOWN_ACTION",
                      message=f"不支持的浏览器动作: {action!r}")
