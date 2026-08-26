"""
══════════════════════════════════════════════════════════════════════
test_ax_provider.py — A1：A11y Tree Provider（CDP 结构化观察）
══════════════════════════════════════════════════════════════════════

零依赖 plain-assert 脚本，直接运行：

    py backend/tests/test_ax_provider.py

覆盖：
  1. normalize_cdp_ax_node：CDP raw 节点 → AXNode（role/name/状态/层级）
  2. 属性缺失容错（无 properties / 无 name → 默认值）
  3. 真实 CDP 冒烟（Chromium 可用时）：getFullAXTree → 结构化节点，
     dialog/button 语义容器可识别（SKIP 当浏览器不可用）
══════════════════════════════════════════════════════════════════════
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/

from explore.observation import (   # noqa: E402
    AXNode, CDPAccessibilityProvider, normalize_cdp_ax_node,
)


def _raw_node(**overrides) -> dict:
    base = {
        "nodeId": 42,
        "parentId": 10,
        "childIds": [43, 44],
        "backendDOMNodeId": 1824,
        "ignored": False,
        "role": {"value": "button"},
        "name": {"value": "Continue Shopping"},
        "properties": [
            {"name": "focusable", "value": {"type": "boolean", "value": True}},
            {"name": "disabled", "value": {"type": "boolean", "value": False}},
            {"name": "checked", "value": {"type": "boolean", "value": False}},
            {"name": "level", "value": {"type": "integer", "value": 4}},
        ],
    }
    base.update(overrides)
    return base


def test_normalize_full_node():
    n = normalize_cdp_ax_node(_raw_node())
    assert n.ax_id == "42" and n.role == "button"
    assert n.name == "Continue Shopping"
    assert n.parent_ax_id == "10" and n.child_ax_ids == ["43", "44"]
    assert n.backend_dom_node_id == 1824
    assert n.focusable is True and n.disabled is False
    assert n.checked is False and n.level == 4


def test_normalize_missing_props_defaults():
    """无 properties / 无 name → 默认值（不崩）。"""
    n = normalize_cdp_ax_node({"nodeId": 1, "ignored": True})
    assert n.role is None and n.name == ""
    assert n.focusable is False and n.disabled is False
    assert n.checked is None and n.level == 0
    assert n.ignored is True


def test_normalize_string_values():
    """值可能是字符串（checked="mixed"）→ 原样保留。"""
    n = normalize_cdp_ax_node(_raw_node(properties=[
        {"name": "checked", "value": {"value": "mixed"}},
    ]))
    assert n.checked == "mixed"


# ── 真实 CDP 冒烟（Chromium 不可用时 SKIP）───────────────────────────────────

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


def test_cdp_capture_structured_tree():
    """真实页面：getFullAXTree → 结构化节点，button/dialog 可识别。"""
    pw, browser, page = _launch()
    try:
        page.set_content("""
        <button>Login</button>
        <div role="dialog" aria-label="Added!">
          <button>Continue Shopping</button>
          <a href="#">View Cart</a>
        </div>
        """)
        nodes = CDPAccessibilityProvider().capture(page)
        assert nodes, "CDP 应返回节点"
        roles = {n.role for n in nodes if n.role}
        assert "button" in roles and "dialog" in roles, f"roles: {roles}"
        # 树结构：dialog 应有 children（父子引用完整性）
        dialogs = [n for n in nodes if n.role == "dialog"]
        assert dialogs, "dialog 应存在"
        by_id = {n.ax_id: n for n in nodes}
        children_ok = all(
            c in by_id for d in dialogs for c in d.child_ax_ids
        )
        assert children_ok or not dialogs[0].child_ax_ids, "dialog children 引用应可解析"
    finally:
        browser.close()
        pw.stop()


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
            print(f"SKIP  {name}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{passed} passed, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
