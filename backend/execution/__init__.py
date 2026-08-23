"""
execution — 执行层（R3：Tool-driven 架构）
  Browser Action Executor：统一浏览器动作执行 + 结构化 ToolResult。
"""
from .action_executor import (
    EXPLORE_ACTION_TIMEOUT_MS, ToolResult, execute_action,
)

__all__ = ["EXPLORE_ACTION_TIMEOUT_MS", "ToolResult", "execute_action"]
