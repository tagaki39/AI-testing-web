"""
══════════════════════════════════════════════════════════════════════
test_action_executor.py — Browser Action Executor（R3.1）测试
══════════════════════════════════════════════════════════════════════

零依赖 plain-assert 脚本，直接运行：

    py backend/tests/test_action_executor.py

覆盖（浏览器背书，Chromium 不可用 SKIP）：
  1. click 成功 → ToolResult(ok=True)
  2. click 失败（元素不存在/超时）→ ok=False + ACTION_FAILED，不抛异常
  3. 危险操作拦截 → DESTRUCTIVE_BLOCKED（执行层安全闸口）
  4. fill ${var} 本地注入；缺变量 → ACTION_FAILED
  5. press / back 成功；未知动作 → UNKNOWN_ACTION
══════════════════════════════════════════════════════════════════════
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/

from execution.action_executor import ToolResult, execute_action   # noqa: E402


class _BrowserUnavailable(Exception):
    pass


def _launch():
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        return pw, browser, page
    except Exception as exc:
        raise _BrowserUnavailable(str(exc)[:80])


def test_click_success():
    """click 成功 → ok=True（含 scroll_into_view preprocessor）。"""
    pw, browser, page = _launch()
    try:
        page.set_content('<div style="height:2000px"></div><button id="b">Go</button>')
        locator = page.locator("#b")
        result = execute_action(page, action="click", locator=locator)
        assert isinstance(result, ToolResult) and result.ok is True
        assert result.code is None and result.message is None
    finally:
        browser.close()
        pw.stop()


def test_click_failure_no_exception():
    """click 失败（不存在元素）→ ok=False + ACTION_FAILED，绝不抛异常。"""
    pw, browser, page = _launch()
    try:
        page.set_content("<div>x</div>")
        result = execute_action(
            page, action="click", locator=page.locator("#missing"),
            timeout_ms=300,
        )
        assert result.ok is False and result.code == "ACTION_FAILED"
        assert "missing" in (result.message or "") or result.message
    finally:
        browser.close()
        pw.stop()


def test_destructive_blocked():
    """危险操作拦截：执行层安全闸口（Delete/Remove 类名称拒绝执行）。"""
    pw, browser, page = _launch()
    try:
        page.set_content('<button id="b">Delete Account</button>')
        result = execute_action(
            page, action="click", locator=page.locator("#b"),
            element_name="Delete Account",
        )
        assert result.ok is False and result.code == "DESTRUCTIVE_BLOCKED"
        assert "拦截" in (result.message or "")
    finally:
        browser.close()
        pw.stop()


def test_fill_with_substitution():
    """fill ${var} 本地注入；缺变量 → ACTION_FAILED（不抛异常）。"""
    pw, browser, page = _launch()
    try:
        page.set_content('<input id="u">')
        result = execute_action(
            page, action="fill", locator=page.locator("#u"),
            value="${username}", runtime_inputs={"username": "standard_user"},
        )
        assert result.ok is True
        assert page.locator("#u").input_value() == "standard_user"

        result = execute_action(
            page, action="fill", locator=page.locator("#u"),
            value="${missing}", runtime_inputs={},
        )
        assert result.ok is False and result.code == "ACTION_FAILED"
        assert "missing" in (result.message or "")
    finally:
        browser.close()
        pw.stop()


def test_press_back_and_unknown_action():
    """press / back 成功；未知动作 → UNKNOWN_ACTION。"""
    pw, browser, page = _launch()
    try:
        page.set_content('<input id="u">')
        result = execute_action(page, action="press", locator=page.locator("#u"),
                                value="Enter")
        assert result.ok is True

        page.set_content("<a href='#x'>x</a>")
        page.evaluate("window.scrollTo(0, 0)")
        result = execute_action(page, action="back", locator=None)
        assert result.ok is True

        result = execute_action(page, action="hover", locator=page.locator("body"))
        assert result.ok is False and result.code == "UNKNOWN_ACTION"
    finally:
        browser.close()
        pw.stop()


def test_record_page_marks_actionable():
    """观察期 actionable 标记真实生效（防回归：拆分时漏 import 导致
    NameError 被静默吞掉，全部元素误标 False → 探索残废）。"""
    from explore.observation import ExploreState, _record_page
    pw, browser, page = _launch()
    try:
        page.set_content(
            '<button>Go</button>\n<button>Only One</button>\n'
            '<div data-product-id="p1"><div>Blue Top</div><button>Buy</button></div>\n'
            '<div data-product-id="p2"><div>Red Top</div><button>Buy</button></div>'
        )
        state = ExploreState(goal="t", entry_url="https://x.com")
        _record_page(state, page)
        by_name = {e["name"]: e for e in state.elements if "role" in e}
        assert by_name["Go"].get("actionable") is True      # 唯一元素可操作
        # 同名重复 + I1 锚点 → 也能被标记（scope 消歧解析成功）
        buys = [e for e in state.elements if e.get("name") == "Buy"]
        assert len(buys) == 2
        assert all(e.get("actionable") is True for e in buys)
    finally:
        browser.close()
        pw.stop()


# ── 运行入口 ──────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    passed = skipped = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"PASS  {name}")
        except _BrowserUnavailable as exc:
            skipped += 1
            print(f"SKIP  {name}（浏览器不可用: {exc}）")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{passed}/{len(tests)} passed" + (f", {skipped} skipped" if skipped else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
