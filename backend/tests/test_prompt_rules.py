"""
══════════════════════════════════════════════════════════════════════
test_prompt_rules.py — prompt 规则防回归（Wait after state-changing actions）
══════════════════════════════════════════════════════════════════════

零依赖 plain-assert 脚本，直接运行：

    py backend/tests/test_prompt_rules.py

背景：E2E 暴露"加购后立即进购物车 → 购物车为空"——动作后异步
状态未确认就执行下一步。对齐参考项目（dsl_generator prompt 规则
5 Wait after actions / 7 Modify-then-assert）。

关键语义（评审收紧后）：wait_for 必须表达上一修改动作的 observable
postcondition（新状态元素），不是机械地在每个 click 前插 wait_for。

覆盖：
  1. legacy prompt（SYSTEM_PROMPT）包含 postcondition 等待规则
  2. refs-only prompt（SYSTEM_PROMPT_REFS_ONLY）包含同规则
  3. wait_for 在 action 白名单内且 DSLStep 校验可用
  4. 规则含反例语义（"wait_for Cart 不能证明加购完成"）
  5. detect_missing_wait_for 真检测器：确定性模式检出 + 不误报
  6. 探索决策 prompt 的状态感知
══════════════════════════════════════════════════════════════════════
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/

from pydantic import ValidationError   # noqa: E402

from ai_agent import (
    SYSTEM_PROMPT, SYSTEM_PROMPT_REFS_ONLY, detect_missing_wait_for,
)   # noqa: E402
from dsl import DSLCase, DSLStep   # noqa: E402
from explore_flow import DECIDE_PROMPT   # noqa: E402

LEGACY_KEYWORDS = [
    "Wait after state-changing actions",
    "Modify-then-assert",
    "postcondition",
    "不能证明加购完成",
    "不得在修改生效前断言新值",
]

REFS_ONLY_KEYWORDS = [
    "Wait after state-changing actions",
    "Modify-then-assert",
    "postcondition",
    "不能证明加购完成",
    "不得在修改生效前断言新值",
]


def test_legacy_prompt_has_wait_rules() -> None:
    for kw in LEGACY_KEYWORDS:
        assert kw in SYSTEM_PROMPT, f"SYSTEM_PROMPT 缺少规则: {kw!r}"


def test_refs_only_prompt_has_wait_rules() -> None:
    for kw in REFS_ONLY_KEYWORDS:
        assert kw in SYSTEM_PROMPT_REFS_ONLY, f"SYSTEM_PROMPT_REFS_ONLY 缺少规则: {kw!r}"


def test_wait_for_action_valid() -> None:
    """wait_for 是合法 action：有 target 时校验通过，无 target 时拒绝。"""
    ok = DSLStep(action="wait_for", target={"role": "button", "name": "确认"})
    assert ok.action == "wait_for"
    try:
        DSLStep(action="wait_for")   # 无 target → 业务校验拒绝
        raise AssertionError("wait_for 无 target 应被拒绝")
    except ValidationError:
        pass


def test_wait_rule_not_legacy_only() -> None:
    """等待规则必须同时存在于两个模式（legacy 与 refs-only 走不同 prompt）。"""
    for prompt in (SYSTEM_PROMPT, SYSTEM_PROMPT_REFS_ONLY):
        assert "Wait after state-changing actions" in prompt
        assert "Modify-then-assert" in prompt
        # 规则必须是 postcondition 语义：允许"等 Remove"，禁止"等 Cart"
        assert "Remove" in prompt
        assert "不能证明加购完成" in prompt
        # 禁止机械 wait-before-action
        assert "机械" in prompt


# ── detect_missing_wait_for 真检测器（确定性模式，非文档）──────────────────────

def _case(steps: list[dict]) -> DSLCase:
    return DSLCase(name="t", steps=[DSLStep(**s) for s in steps])


def test_detect_modify_then_assert_without_wait() -> None:
    """加购（非导航 click）后立即断言 → 检出。"""
    c = _case([
        {"action": "click", "target": {"role": "button", "name": "Add to cart"}},
        {"action": "assert_text", "value": "Blue Top"},
    ])
    issues = detect_missing_wait_for(c)
    assert len(issues) == 1
    assert "postcondition" in issues[0]


def test_detect_fill_then_assert() -> None:
    """fill 后立即断言 → 检出。"""
    c = _case([
        {"action": "fill", "target": {"role": "textbox", "name": "Email"},
         "value": "${email}"},
        {"action": "assert_text", "value": "welcome"},
    ])
    assert len(detect_missing_wait_for(c)) == 1


def test_no_detect_navigation_click_then_assert_url() -> None:
    """导航 click 后断言 URL → 不报（Playwright 自动等待页面加载）。"""
    c = _case([
        {"action": "click", "target": {"role": "link", "name": "Login"}},
        {"action": "assert_url", "value": "/login"},
    ])
    assert detect_missing_wait_for(c) == []


def test_no_detect_wait_between_modify_and_assert() -> None:
    """modify → wait_for（postcondition）→ 断言 → 不报。"""
    c = _case([
        {"action": "click", "target": {"role": "button", "name": "Add to cart"}},
        {"action": "wait_for", "target": {"role": "button", "name": "Remove"}},
        {"action": "assert_text", "value": "Blue Top"},
    ])
    assert detect_missing_wait_for(c) == []


def test_no_detect_goto_then_assert() -> None:
    """goto → assert_url 天然安全 → 不报。"""
    c = _case([
        {"action": "goto", "value": "https://x.com"},
        {"action": "assert_url", "value": "x.com"},
    ])
    assert detect_missing_wait_for(c) == []


def test_detect_assert_after_multi_modify_chain() -> None:
    """加购 → 进购物车 → 断言：链尾断言前一步是 click Cart（导航）
    → 不报；但链中 click 非导航（Add to cart）不接断言 → 不报。
    全链安全模式不误报。"""
    c = _case([
        {"action": "click", "target": {"role": "button", "name": "Add to cart"}},
        {"action": "click", "target": {"role": "link", "name": "Cart"}},
        {"action": "assert_text", "value": "Blue Top"},
    ])
    assert detect_missing_wait_for(c) == []


def test_decide_prompt_has_state_awareness() -> None:
    """探索决策 prompt 必须标注当前状态并禁止沿用旧状态 ref。

    背景：E2E 中模型在登录后 8 连拒——持续引用旧状态（obs1）的 ref。
    """
    assert "{current_obs}" in DECIDE_PROMPT   # 当前状态占位（_decide 注入）
    assert "当前页面状态" in DECIDE_PROMPT     # 状态标注行
    assert "已失效" in DECIDE_PROMPT         # 旧状态 ref 失效声明


def test_decide_history_carries_error() -> None:
    """history 渲染必须带失败原因（error），否则模型无法自纠。"""
    import re
    # 从 explore_flow 取 history 渲染逻辑做轻量断言：render 里含 "失败"
    src = open(Path(__file__).resolve().parents[1] / "explore_flow.py",
               encoding="utf-8").read()
    assert "失败: {h['error'][:80]}" in src


def main() -> int:
    tests = [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
