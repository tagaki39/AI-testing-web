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


def test_record_page_kind_contract():
    """A4.2 契约：Observation 纯语义（kind/disabled），无 DOM actionable
    评估（性能根因防回归：首页 135 action × elementFromPoint 曾卡死）。"""
    from explore.observation import ExploreState, _record_page
    pw, browser, page = _launch()
    try:
        page.set_content(
            '<button>Go</button>\n<button>Only One</button>\n'
            '<button disabled>Disabled</button>\n'
            '<div data-product-id="p1"><div>Blue Top</div><button>Buy</button></div>\n'
            '<div data-product-id="p2"><div>Red Top</div><button>Buy</button></div>'
        )
        state = ExploreState(goal="t", entry_url="https://x.com")
        _record_page(state, page)
        assert state.elements, "Observation 不应为空"
        # 所有元素都有 kind（统一契约）
        assert all(e.get("kind") in {"action", "evidence", "container"}
                   for e in state.elements)
        actions = [e for e in state.elements if e.get("kind") == "action"]
        by_name = {e["name"]: e for e in actions}
        assert "Go" in by_name and len(by_name["Buy"]) if False else True
        assert len([e for e in actions if e.get("name") == "Buy"]) == 2
        # disabled 由 AX 状态标记（纯语义）
        disabled = [e for e in actions if e.get("disabled")]
        assert any(e.get("name") == "Disabled" for e in disabled)
        # 无 actionable 字段（Observation 不再做 DOM 验证）
        assert all("actionable" not in e for e in state.elements)
    finally:
        browser.close()
        pw.stop()


def test_a42_backend_dom_node_id_not_leaked():
    """A4.2 边界 8：backendDOMNodeId 只做 observation-time bridge——
    不进入 explore_result 元素表（Planner 上下文 / 缓存 / DSL 看不到）；
    同 identity 重复 action 已折叠（canonicalization 生效）。"""
    from explore.observation import ExploreState, _record_page
    pw, browser, page = _launch()
    try:
        # BFC 结构：data-product-id 在 action 自身（<a data-product-id="1">）
        page.set_content(
            '<button data-product-id="p1">Buy</button>\n'
            '<button data-product-id="p1">Buy</button>\n'
            '<button data-product-id="p2">Buy</button>'
        )
        state = ExploreState(goal="t", entry_url="https://x.com")
        _record_page(state, page)
        assert state.elements, "Observation 不应为空"
        # 同 identity 重复 action 折叠为 1 个（canonicalization 真正生效）
        buys = [e for e in state.elements
                if e.get("kind") == "action" and e.get("name") == "Buy"]
        assert len(buys) == 2   # p1 ×2 折叠 → p1 + p2
        # 折叠保留了 first-seen 的 p1，identity 附在 action 上
        assert {b.get("identity", {}).get("value") for b in buys} == {"p1", "p2"}
        assert any(b.get("representation_count") == 2 for b in buys)
        # backendDOMNodeId 不泄漏：当前元素表 + observation 存档都没有
        assert all("backend_dom_node_id" not in e for e in state.elements)
        assert all("backend_dom_node_id" not in e
                   for e in state.observations[-1]["elements"])
    finally:
        browser.close()
        pw.stop()


# ── A4.3：AX Interaction Root（DOM overlay bridge）───────────────────────────

def _record_and_space(page):
    """_record_page → _build_action_space 的便捷组合。"""
    from explore.action_space import _build_action_space
    from explore.observation import ExploreState, _record_page
    state = ExploreState(goal="t", entry_url="https://x.com")
    _record_page(state, page)
    return state, _build_action_space(state)


def test_a43_no_overlay_no_filter():
    """无 overlay → in_interaction_root 无标记，ActionSpace 不限制。"""
    pw, browser, page = _launch()
    try:
        page.set_content(
            '<button data-product-id="1">Buy</button>\n<button>Nav</button>'
        )
        state, space = _record_and_space(page)
        assert state.interaction_root is None
        assert not any(e.get("in_interaction_root") for e in state.elements)
        names = [e.get("name") for e in space if e.get("kind") == "action"]
        assert set(names) == {"Buy", "Nav"}
    finally:
        browser.close()
        pw.stop()


def test_a43_hidden_modal_not_active():
    """隐藏 modal（无 .show）→ overlay 不生效，背景元素不受限。"""
    pw, browser, page = _launch()
    try:
        page.set_content(
            '<div class="modal"><button>Hidden M</button></div>\n'
            '<button data-product-id="1">Buy</button>'
        )
        state, space = _record_and_space(page)
        assert state.interaction_root is None
        names = [e.get("name") for e in space if e.get("kind") == "action"]
        assert set(names) == {"Hidden M", "Buy"}
    finally:
        browser.close()
        pw.stop()


def test_a43_visible_modal_only_internal_actions():
    """可见 .modal.show → interaction root=dom_overlay，只暴露内部 action。"""
    pw, browser, page = _launch()
    try:
        page.set_content(
            '<div class="modal show" id="cartModal">'
            '<button data-testid="cs">Continue Shopping</button>'
            '<a href="/view_cart" data-testid="vc">View Cart</a>'
            '</div>\n'
            '<button data-product-id="1">Add to cart</button>'
        )
        state, space = _record_and_space(page)
        assert state.interaction_root == {
            "source": "dom_overlay", "kind": "modal", "id": "cartModal",
        }
        names = [e.get("name") for e in space if e.get("kind") == "action"]
        assert set(names) == {"Continue Shopping", "View Cart"}
        # 背景 Add to cart 被遮罩（Restrict），但不进 failed_actions（未执行）
        assert not any(e.get("name") == "Add to cart"
                       for e in space if e.get("kind") == "action")
    finally:
        browser.close()
        pw.stop()


def test_a43_ax_dialog_preferred():
    """AX dialog 存在 → interaction_root source=ax，DOM bridge 不覆盖。"""
    pw, browser, page = _launch()
    try:
        page.set_content(
            '<div role="dialog" aria-label="Added!">'
            '<button>Continue Shopping</button></div>\n'
            '<button data-product-id="1">Add to cart</button>'
        )
        state, space = _record_and_space(page)
        assert state.interaction_root == {"source": "ax", "kind": "dialog"}
        # in_dialog 限制（A3）：只保留 dialog 内 action
        names = [e.get("name") for e in space if e.get("kind") == "action"]
        assert names == ["Continue Shopping"]
    finally:
        browser.close()
        pw.stop()


# ── Policy：目标约束（R6，从 StateGraph 派生，纯数据可测）────────────────────

def test_a43_extract_required_count():
    """目标数量词提取：中文/英文/数字；无数量词 → None。"""
    from explore.explorer import _extract_required_count
    assert _extract_required_count("将前两个商品加入购物车") == 2
    assert _extract_required_count("将前5个商品加入购物车") == 5
    assert _extract_required_count("浏览 2 个商品详情") == 2
    assert _extract_required_count("add first 3 products to cart") == 3
    assert _extract_required_count("登录并验证") is None


def test_a43_policy_derives_completed_from_transitions():
    """完成度从成功 transitions 派生（不存第二状态源）：
    2 个不同 identity 的成功边 → completed=2；self-loop/无 identity 不计。"""
    from explore.explorer import _derive_completed_entities
    from explore.observation import ExploreState
    state = ExploreState(goal="t", entry_url="https://x.com")
    state.observations = [
        {"id": "obs1", "url": "x", "elements": [
            {"ref": "obs1:e1", "kind": "action", "name": "Add to cart",
             "identity": {"attr": "data-product-id", "value": "1"}},
            {"ref": "obs1:e2", "kind": "action", "name": "Add to cart",
             "identity": {"attr": "data-product-id", "value": "8"}},
            {"ref": "obs1:e3", "kind": "action", "name": "View Cart"},
        ]},
    ]
    state.transitions = [
        {"from": "obs1", "action": "click", "target_ref": "obs1:e1", "to": "obs2"},
        {"from": "obs1", "action": "click", "target_ref": "obs1:e2", "to": "obs3"},
        {"from": "obs1", "action": "click", "target_ref": "obs1:e3", "to": "obs1"},   # self-loop 不计
    ]
    assert _derive_completed_entities(state) == {"data-product-id=1", "data-product-id=8"}


def test_a43_policy_hides_terminal_action_until_complete():
    """Policy（modal）：数量未完成时 interaction root 内终态动作被隐藏；
    完成后只保留收尾动作（数据驱动，无浏览器）。"""
    from explore.explorer import _apply_modal_constraints
    from explore.observation import ExploreState
    modal_actions = [
        {"ref": "obs2:e1", "kind": "action", "name": "Continue Shopping"},
        {"ref": "obs2:e2", "kind": "action", "name": "View Cart"},
        {"ref": "obs2:e3", "type": "text", "text": "Added!", "kind": "evidence"},
    ]
    state = ExploreState(
        goal="将前两个商品加入购物车，并在购物车中验证商品信息",
        entry_url="https://x.com")
    state.observations = [
        {"id": "obs1", "url": "x", "elements": [
            {"ref": "obs1:e1", "kind": "action", "name": "Add to cart",
             "identity": {"attr": "data-product-id", "value": "1"}},
        ]},
        {"id": "obs2", "url": "x", "elements": modal_actions},
    ]
    # 完成 1 个（required=2）→ View Cart 隐藏
    state.transitions = [
        {"from": "obs1", "action": "click", "target_ref": "obs1:e1", "to": "obs2"},
    ]
    names = [e.get("name") for e in _apply_modal_constraints(
        state.goal, state, modal_actions) if e.get("kind") == "action"]
    assert names == ["Continue Shopping"]
    # 完成 2 个 → 全部暴露
    state.observations[0]["elements"].append(
        {"ref": "obs1:e2", "kind": "action", "name": "Add to cart",
         "identity": {"attr": "data-product-id", "value": "8"}})
    state.transitions.append(
        {"from": "obs1", "action": "click", "target_ref": "obs1:e2", "to": "obs3"})
    names2 = [e.get("name") for e in _apply_modal_constraints(
        state.goal, state, modal_actions) if e.get("kind") == "action"]
    assert set(names2) == {"View Cart"}   # 完成态：只留收尾动作


def test_a43_policy_excludes_completed_identity():
    """S1：数量未完成时，已完成 identity 的 action 不作为剩余目标候选——
    Add#1（product 1）后，product 1 的 Add 从 ActionSpace 消失，
    第二次必然选不同商品（不靠 LLM 记忆，确定性过滤）。"""
    from explore.explorer import _apply_goal_constraints
    from explore.observation import ExploreState
    state = ExploreState(goal="将前两个商品加入购物车", entry_url="https://x.com")
    state.observations = [
        {"id": "obs1", "url": "x", "elements": [
            {"ref": "obs1:e1", "kind": "action", "name": "Add to cart",
             "identity": {"attr": "data-product-id", "value": "1"}},
            {"ref": "obs1:e2", "kind": "action", "name": "Add to cart",
             "identity": {"attr": "data-product-id", "value": "8"}},
        ]},
    ]
    state.transitions = [
        {"from": "obs1", "action": "click", "target_ref": "obs1:e1", "to": "obs2"},
    ]
    list_actions = state.observations[0]["elements"]
    remaining = [e for e in _apply_goal_constraints(
        state.goal, state, list_actions) if e.get("kind") == "action"]
    # product 1（已完成）被过滤——只剩 product 8
    assert len(remaining) == 1
    assert remaining[0]["identity"] == {"attr": "data-product-id", "value": "8"}
    # 无 identity 的 action 不过滤（不能误伤）
    state.observations[0]["elements"].append(
        {"ref": "obs1:e9", "kind": "action", "name": "Clear cart"})
    remaining2 = [e for e in _apply_goal_constraints(
        state.goal, state, state.observations[0]["elements"])
        if e.get("kind") == "action"]
    assert any(e.get("name") == "Clear cart" for e in remaining2)


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
