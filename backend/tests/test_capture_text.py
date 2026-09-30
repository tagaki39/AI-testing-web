"""
══════════════════════════════════════════════════════════════════════
test_capture_text.py — capture_text 运行时变量捕获测试
══════════════════════════════════════════════════════════════════════

零依赖 plain-assert 脚本，直接运行：

    py backend/tests/test_capture_text.py

覆盖：
  1. dsl 校验：capture_text 缺 target/context_key → ValidationError；合法通过
  2. _execute_step 直驱（set_content 绕过 goto 公网限制）：
     capture 价格 → 后续 assert_text ${price} 通过
  3. 未捕获引用 ${key} → 明确失败（缺变量 KeyError）
  4. 二次捕获同名 key 覆盖
══════════════════════════════════════════════════════════════════════
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/

from pydantic import ValidationError   # noqa: E402
from dsl import validate_case   # noqa: E402

# ── 1. dsl 校验 ───────────────────────────────────────────────────────────────

def test_capture_text_schema():
    """合法 capture_text 通过；缺 target / 缺 context_key 拒绝。"""
    case = validate_case({"name": "t", "steps": [
        {"action": "goto", "value": "https://x.com"},
        {"action": "capture_text", "target": {"text": "Rs. 500"},
         "context_key": "price"},
        {"action": "assert_text", "value": "${price}"},
    ]})
    assert case.steps[1].action == "capture_text"
    assert case.steps[1].context_key == "price"

    for steps in (
        [{"action": "goto", "value": "https://x.com"},
         {"action": "capture_text", "context_key": "price"}],
        [{"action": "goto", "value": "https://x.com"},
         {"action": "capture_text", "target": {"text": "x"}}],
    ):
        try:
            validate_case({"name": "t", "steps": steps})
        except ValidationError:
            continue
        raise AssertionError(f"未拒绝: {steps}")


# ── 2-4. _execute_step 直驱（浏览器背书，Chromium 不可用 SKIP）───────────────

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


def _run_steps_on(page, steps: list[dict], variables: dict):
    """直接驱动 runner._execute_step（页面已 set_content，绕过 goto 限制）。"""
    from execution.runner import _execute_step
    case = validate_case({"name": "t", "steps": steps})
    step_dir = Path(tempfile.mkdtemp(prefix="capture_"))
    results = []
    for i, step in enumerate(case.steps, start=1):
        results.append(_execute_step(page, step, variables, step_dir, i))
    return results


def test_capture_then_assert():
    """capture 价格 → 后续 assert_text ${price}（同一元素/页面）通过。"""
    pw, browser, page = _launch()
    try:
        page.set_content('<div><span class="price">Rs. 500</span></div>')
        variables = {}
        results = _run_steps_on(page, [
            {"action": "capture_text",
             "target": {"css": ".price"}, "context_key": "price"},
            {"action": "assert_text",
             "target": {"css": ".price"}, "value": "${price}"},
        ], variables)
        assert results[0]["status"] == "passed", results[0]["error"]
        assert results[1]["status"] == "passed", results[1]["error"]
        assert variables["price"] == "Rs. 500"
    finally:
        browser.close()
        pw.stop()


def test_unreferenced_capture_fails_clearly():
    """引用未捕获的 ${key} → 明确失败（缺变量）。"""
    pw, browser, page = _launch()
    try:
        page.set_content('<div><span>Hi</span></div>')
        variables = {}
        results = _run_steps_on(page, [
            {"action": "assert_text", "value": "${missing}"},
        ], variables)
        assert results[0]["status"] == "failed"
        assert "missing" in (results[0]["error"] or "")
    finally:
        browser.close()
        pw.stop()


def test_recapture_overwrites():
    """同一 context_key 二次捕获 → 覆盖为最新值。"""
    pw, browser, page = _launch()
    try:
        page.set_content('<div><span class="a">first</span>'
                         '<span class="b">second</span></div>')
        variables = {}
        results = _run_steps_on(page, [
            {"action": "capture_text", "target": {"css": ".a"},
             "context_key": "val"},
            {"action": "capture_text", "target": {"css": ".b"},
             "context_key": "val"},
        ], variables)
        assert results[0]["status"] == "passed"
        assert results[1]["status"] == "passed"
        assert variables["val"] == "second"
    finally:
        browser.close()
        pw.stop()


# ── 运行入口 ──────────────────────────────────────────────────────────────────

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
        except _BrowserUnavailable as exc:
            print(f"SKIP  {name}（浏览器不可用: {exc}）")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
