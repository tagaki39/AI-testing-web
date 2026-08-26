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
    AXNode, CDPAccessibilityProvider, build_observation_elements,
    normalize_cdp_ax_node, semantic_state_signature,
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


# ── A2：Structured Observation（kind / context / 树结构）─────────────────────

def _ax(id_, role, name="", parent=None, ignored=False, **kw):
    return AXNode(ax_id=id_, role=role, name=name, parent_ax_id=parent,
                  child_ax_ids=[], backend_dom_node_id=None, ignored=ignored, **kw)


def test_a2_kind_classification():
    """button → action / dialog → container / 有名字的 text → evidence。"""
    nodes = [
        _ax("1", "button", "Continue Shopping"),
        _ax("2", "dialog", "Added!"),
        _ax("3", "text", "登录成功"),
    ]
    els = build_observation_elements(nodes)
    kinds = {e.role: e.kind for e in els}
    assert kinds["button"] == "action"
    assert kinds["dialog"] == "container"
    assert kinds["text"] == "evidence"   # 断言用文本证据保留


def test_a2_semantic_context_dialog():
    """dialog 内的 button → context_role=dialog, context_name=Added!。"""
    nodes = [
        _ax("1", "dialog", "Added!"),
        _ax("2", "button", "Continue Shopping", parent="1"),
        _ax("3", "button", "View Cart", parent="1"),
        _ax("4", "button", "Add to cart"),   # dialog 外
    ]
    els = build_observation_elements(nodes)
    by_name = {e.name: e for e in els}
    assert by_name["Continue Shopping"].context_role == "dialog"
    assert by_name["Continue Shopping"].context_name == "Added!"
    assert by_name["Add to cart"].context_role is None   # 容器外无 context


def test_a2_parent_ref_reconstruction():
    """父引用重建：child 的 parent_ref 指向父元素 ref。"""
    nodes = [
        _ax("1", "listitem", "Blue Top"),
        _ax("2", "button", "Add to cart", parent="1"),
    ]
    els = build_observation_elements(nodes)
    child = next(e for e in els if e.role == "button")
    parent = next(e for e in els if e.role == "listitem")
    assert child.parent_ref == parent.ref


def test_a2_ignored_nodes_skipped():
    """ignored 节点不进元素列表。"""
    nodes = [
        _ax("1", "button", "Login"),
        _ax("2", "text", "decor", ignored=True),
    ]
    els = build_observation_elements(nodes)
    assert len(els) == 1 and els[0].role == "button"


# ── A4：semantic state signature（相同业务状态 → 相同 hash）──────────────────

def test_a4_same_semantic_tree_same_hash():
    """相同 action/container 集合（顺序不同）→ 相同 hash。"""
    a = [
        {"ref": "e1", "role": "button", "name": "Login"},
        {"ref": "e2", "role": "textbox", "name": "Username"},
    ]
    b = [
        {"ref": "e9", "role": "textbox", "name": "Username"},
        {"ref": "e7", "role": "button", "name": "Login"},
    ]
    assert semantic_state_signature(a) == semantic_state_signature(b)


def test_a4_text_change_no_new_state():
    """文本/输入值变化不影响语义签名（fill 不产生 phantom obs）。"""
    base = [{"ref": "e1", "role": "button", "name": "Login"}]
    with_value = [
        {"ref": "e1", "role": "button", "name": "Login"},
        {"ref": "e2", "type": "text", "text": "welcome test1"},
    ]
    assert semantic_state_signature(base) == semantic_state_signature(with_value)


def test_a4_dialog_state_changes_hash():
    """dialog 开/关（context_role 变化）→ 不同 hash。"""
    no_dialog = [
        {"ref": "e1", "role": "button", "name": "Add to cart"},
    ]
    dialog_open = [
        {"ref": "e1", "role": "button", "name": "Add to cart"},
        {"ref": "e2", "role": "button", "name": "Continue Shopping",
         "context_role": "dialog", "context_name": "Added!"},
    ]
    assert semantic_state_signature(no_dialog) != semantic_state_signature(dialog_open)


def test_a4_no_role_elements_none():
    """无 role 元素（纯文本页）→ None（回落全文 hash）。"""
    assert semantic_state_signature([{"ref": "e1", "type": "text", "text": "hello"}]) is None
    assert semantic_state_signature([]) is None


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
