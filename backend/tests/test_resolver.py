"""
══════════════════════════════════════════════════════════════════════
test_resolver.py — Semantic Resolver（R1）语义锁定测试
══════════════════════════════════════════════════════════════════════

零依赖 plain-assert 脚本，直接运行：

    py backend/tests/test_resolver.py

目的：R1 抽离后锁定单一事实源语义（此前 Runner 与 Preflight 各持副本、
发生过漂移）。覆盖：
  1. parse_target：字符串各形态 + 结构化 + 未知角色兜底
  2. is_navigation_name：导航 allowlist 判定
  3. decorated_name_pattern：图标前缀（PUA）容忍、不误吞扩展名
  4. snapshot_match：exact → decorated → fuzzy 次序；导航名禁止 fuzzy
  5. build_locator_candidates：候选顺序 + 导航名无 fuzzy 候选（真实 DOM）
  6. build_locator_exact_first / build_locator_for_count（真实 DOM）

浏览器背书部分（5/6）需要 Playwright Chromium：未安装时 SKIP，不判失败。
══════════════════════════════════════════════════════════════════════
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/

from resolver import (   # noqa: E402
    LowConfidenceError, ParsedTarget, build_locator_candidates,
    build_locator_exact_first, build_locator_for_count,
    decorated_name_pattern, is_navigation_name, parse_target, snapshot_match,
)

# FontAwesome 购物车图标（U+F07A，PUA 区）：用 chr() 构造，避免源码含字面 PUA
_PUA_CART = chr(0xF07A) + " Cart"


# ── 1. parse_target ──────────────────────────────────────────────────────────

def test_parse_target_string_forms():
    """字符串各形态：css=/test_id=/testid=/text=/角色=名称/纯文本兜底。"""
    assert parse_target("css=.btn").css == ".btn"
    assert parse_target("test_id=login-button").test_id == "login-button"
    assert parse_target("testid=login-button").test_id == "login-button"
    assert parse_target("text=Products").text == "Products"
    p = parse_target("button=登录")
    assert p.role == "button" and p.name == "登录"
    assert parse_target("登录").text == "登录"          # 无 = → 纯文本


def test_parse_target_unknown_role_falls_back_to_text():
    """未知角色（不在白名单）→ 按纯文本处理，不按角色解析。"""
    p = parse_target("widget=xxx")
    assert p.role is None and p.text == "widget=xxx"


def test_parse_target_structured_and_none():
    """结构化 dict / Locator 模型 / None。"""
    p = parse_target({"role": "button", "name": "Add to cart"})
    assert p.role == "button" and p.name == "Add to cart"
    assert parse_target(None) is None
    from dsl import Locator
    p = parse_target(Locator(role="link", name="Buy"))
    assert p.role == "link" and p.name == "Buy"


# ── 2. is_navigation_name ────────────────────────────────────────────────────

def test_is_navigation_name():
    """导航 allowlist：大小写不敏感；非 link / 扩展名不算导航。"""
    assert is_navigation_name("link", "Cart")
    assert is_navigation_name("link", "  cart ")           # strip + casefold
    assert is_navigation_name("link", "Signup / Login")
    assert not is_navigation_name("link", "Add to cart")   # 扩展名不是导航短名
    assert not is_navigation_name("button", "Cart")        # 只看 link
    assert not is_navigation_name("link", None)


# ── 3. decorated_name_pattern ────────────────────────────────────────────────

def test_decorated_pattern_tolerates_icon_prefix():
    """容忍 PUA 图标前缀 + 空白；不吞掉扩展名（"View Cart"/"Add to cart"）。"""
    pat = decorated_name_pattern("Cart")
    assert pat.fullmatch(_PUA_CART) is not None            # 图标前缀 + 名称
    assert pat.fullmatch(" Cart") is not None              # 纯空白前缀
    assert pat.fullmatch("Cart") is not None               # 无前缀
    assert pat.fullmatch("View Cart") is None              # 不匹配扩展名
    assert pat.fullmatch("Add to cart") is None


def test_decorated_pattern_slash_escape():
    """含 "/" 的导航名（Signup / Login）可构建且精确匹配。"""
    pat = decorated_name_pattern("Signup / Login")
    assert pat.fullmatch("Signup / Login") is not None
    assert pat.fullmatch("Signup Login") is None


# ── 4. snapshot_match ────────────────────────────────────────────────────────

def test_snapshot_match_exact_and_counts():
    """exact 命中 + 计数（0/1/N）。"""
    snap = '- button "Add to cart"\n- button "Add to cart"'
    found, count = snapshot_match(snap, "button", "Add to cart")
    assert (found, count) == (True, 2)
    assert snapshot_match(snap, "button", "Buy") == (False, 0)


def test_snapshot_match_decorated():
    """decorated-exact：图标前缀名称命中且只计 1 次。"""
    snap = '- link "' + _PUA_CART + '"\n- link "View Cart"'
    found, count = snapshot_match(snap, "link", "Cart")
    assert (found, count) == (True, 1)    # 只有图标版命中，View Cart 不算


def test_snapshot_match_nav_name_forbids_fuzzy():
    """导航名禁止 fuzzy："Cart" 不得把 "Add to cart"/"View Cart" 计入。"""
    snap = '- link "Add to cart"\n- link "View Cart"'
    assert snapshot_match(snap, "link", "Cart") == (False, 0)


def test_snapshot_match_non_nav_fuzzy():
    """非导航名允许 fuzzy 兜底——只在 exact/decorated 全空时生效（并计数）。"""
    snap = '- button "Add to cart"\n- button "Add to cart now"'
    # exact 全空（没有按钮恰好叫 "Add to"）→ 落到 fuzzy，两条都命中
    found, count = snapshot_match(snap, "button", "Add to")
    assert found and count == 2
    # exact 命中时短路（"Add to cart now" 不是 exact）
    found, count = snapshot_match(snap, "button", "Add to cart")
    assert (found, count) == (True, 1)


def test_snapshot_match_plain_text():
    """无 role → 整快照纯文本匹配。"""
    snap = '- text: Products\n- text: Your Cart'
    assert snapshot_match(snap, None, "Products") == (True, 1)
    assert snapshot_match(snap, None, "products") == (True, 1)   # 大小写不敏感
    assert snapshot_match(snap, None, "Nope") == (False, 0)


# ── 浏览器背书（Chromium 未安装时 SKIP）───────────────────────────────────────

class _BrowserUnavailable(Exception):
    """Playwright 浏览器不可用 → 测试 SKIP（不判失败）。"""


def _launch():
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        return pw, browser, page
    except Exception as exc:
        raise _BrowserUnavailable(str(exc)[:80])


_TEST_HTML = """
<a href="#">Cart</a>
<button data-testid="add-1">Add to cart</button>
<a href="#">View Cart</a>
<button>Add to cart</button>
"""


def test_candidates_order_and_nav_guard():
    """候选顺序：test_id 优先；role exact → decorated；导航名无 fuzzy 候选。"""
    pw, browser, page = _launch()
    try:
        page.set_content(_TEST_HTML)

        # test_id 候选排第一
        strategies = [s for s, _ in build_locator_candidates(
            page, ParsedTarget(test_id="add-1"))]
        assert strategies[0] == "test_id"

        # 非导航 button：exact → decorated → fuzzy 全都有，且 exact 在前
        strategies = [s for s, _ in build_locator_candidates(
            page, ParsedTarget(role="button", name="Add to cart"))]
        assert strategies == ["role", "role_decorated", "role_fuzzy"]

        # 导航 link（Cart）：禁止 fuzzy 候选（否则会命中 Add to cart / View Cart）
        strategies = [s for s, _ in build_locator_candidates(
            page, ParsedTarget(role="link", name="Cart"))]
        assert strategies == ["role", "role_decorated"]

        # exact 候选只命中 "Cart" 本身（1 个）；fuzzy 本会命中 2 个
        exact_loc = build_locator_candidates(
            page, ParsedTarget(role="link", name="Cart"))[0][1]
        assert exact_loc.count() == 1
    finally:
        browser.close()
        pw.stop()


def test_exact_first_and_for_count_on_dom():
    """exact-first（不跳脏 fuzzy）与 for-count（绕过三分法）的真实 DOM 语义。"""
    pw, browser, page = _launch()
    try:
        page.set_content(_TEST_HTML)

        # exact-first：nav "Cart" 无 fuzzy 兜底 → 只有 exact 命中（count 1）
        loc = build_locator_exact_first(page, {"role": "link", "name": "Cart"})
        assert loc is not None and loc.count() == 1

        # exact-first：decorated 容忍图标前缀（构造含 PUA 图标的链接）
        page.set_content('<a href="#">' + _PUA_CART + '</a>')
        loc = build_locator_exact_first(page, {"role": "link", "name": "Cart"})
        assert loc is not None and loc.count() == 1

        # for-count：绕过三分法允许 count>1（候选提取语义）
        page.set_content(_TEST_HTML)
        loc = build_locator_for_count(page, {"role": "link", "name": "Cart"})
        assert loc.count() == 2   # fuzzy：Cart + View Cart

        # for-count：test_id 缺失时回退 data-test 属性变体
        page.set_content('<div data-test="login-box">x</div>')
        loc = build_locator_for_count(page, {"test_id": "login-box"})
        assert loc.count() == 1
    finally:
        browser.close()
        pw.stop()


def test_resolve_locator_ambiguous_not_notfound():
    """多匹配无唯一命中必须报 Ambiguous 而不是 NotFound。

    真实 E2E：products 页 N 个 View Product 链接，Runner 报
    "所有定位策略均未命中"——元素明明存在，错误语义误导排查。
    """
    from runner import _resolve_locator
    from resolver import LocatorAmbiguousError
    pw, browser, page = _launch()
    try:
        page.set_content('<button>Add to cart</button>\n<button>Add to cart</button>')
        try:
            _resolve_locator(page, {"role": "button", "name": "Add to cart"})
        except LocatorAmbiguousError:
            pass
        else:
            raise AssertionError("多匹配未报 LocatorAmbiguousError")
        # 唯一匹配仍正常解析
        page.set_content('<button>Only one</button>')
        strategy, locator = _resolve_locator(page, {"role": "button", "name": "Only one"})
        assert strategy == "role" and locator.count() == 1
    finally:
        browser.close()
        pw.stop()


# ── R2：评分 + 置信度门槛（真实 DOM 背书）─────────────────────────────────────

def test_scoring_stronger_strategy_wins():
    """test_id(100) 命中 + text(60) 命中不同元素 → margin 40 → 接受 test_id。"""
    from runner import _resolve_locator
    pw, browser, page = _launch()
    try:
        page.set_content(
            '<button data-testid="add-1">Different</button>\n<button>Add to cart</button>'
        )
        strategy, locator = _resolve_locator(
            page, {"test_id": "add-1", "text": "Add to cart"},
        )
        assert strategy == "test_id"
        assert "Different" in locator.inner_text()
    finally:
        browser.close()
        pw.stop()


def test_margin_gate_rejects():
    """test_id + test_id_attr 各命中不同元素 → margin 5 < 20 → LowConfidence。"""
    from runner import _resolve_locator
    pw, browser, page = _launch()
    try:
        page.set_content(
            '<button data-testid="add-1">A</button>\n<button data-test="add-1">B</button>'
        )
        try:
            _resolve_locator(page, {"test_id": "add-1"})
        except LowConfidenceError as exc:
            assert "低于门槛" in str(exc)
            return
        raise AssertionError("margin 不足未拒绝")
    finally:
        browser.close()
        pw.stop()


def test_weak_winner_rejected():
    """css(30) 命中 + text(60) ×2 → 弱胜强证据存在 → LowConfidence。"""
    from runner import _resolve_locator
    pw, browser, page = _launch()
    try:
        page.set_content(
            '<button class="cart-btn">C</button>\n'
            '<button>Add to cart</button>\n<button>Add to cart</button>'
        )
        try:
            _resolve_locator(page, {"css": ".cart-btn", "text": "Add to cart"})
        except LowConfidenceError:
            return
        raise AssertionError("弱策略胜出未被拒绝")
    finally:
        browser.close()
        pw.stop()


def test_fuzzy_commonality_does_not_block_exact():
    """role(90) 命中 + role_fuzzy(50) ×2 → margin 40 → 接受 exact。"""
    from runner import _resolve_locator
    pw, browser, page = _launch()
    try:
        page.set_content(
            '<button>Add to cart</button>\n'
            '<button>Add to cart now</button>\n<button>Add to cart now</button>'
        )
        strategy, locator = _resolve_locator(
            page, {"role": "button", "name": "Add to cart"},
        )
        assert strategy == "role" and locator.count() == 1
    finally:
        browser.close()
        pw.stop()


def test_same_element_dedup_preserved():
    """scope 多容器同策略命中 → 仍走 dedup 消歧（R2 不破坏既有行为）。"""
    from runner import _resolve_locator
    pw, browser, page = _launch()
    try:
        page.set_content(
            '<div data-product-id="p1">Blue Top<button>Buy</button></div>\n'
            '<div data-product-id="p2">Red Top<button>Buy</button></div>'
        )
        # scope 锁定 Blue Top 容器；同策略（role）在多容器中的 count>1
        # 是 scope 消歧的正常形态，不得触发 margin 拒绝
        strategy, locator = _resolve_locator(
            page, {"role": "button", "name": "Buy"},
            scope={"has_text": "Blue Top"},
        )
        assert strategy == "role" and locator.count() == 1
    finally:
        browser.close()
        pw.stop()


def test_low_confidence_is_ambiguous_subclass():
    """LowConfidenceError 继承 LocatorAmbiguousError（既有 catch 兼容）。"""
    from resolver import LocatorAmbiguousError
    assert issubclass(LowConfidenceError, LocatorAmbiguousError)


# ── decorated 修复（名称本身以图标开头）────────────────────────────────────────

def test_decorated_pattern_leading_icon_name():
    """名称以图标开头时 decorated pattern 仍可匹配（真实 E2E bug 修复）。"""
    name = chr(0xF0FE) + " View Product"
    pat = decorated_name_pattern(name)
    assert pat.fullmatch(name) is not None            # 图标 + 名称
    assert pat.fullmatch(" " + name) is not None      # 空白 + 图标 + 名称
    assert pat.fullmatch("View Product") is not None  # 纯名称（零装饰）


def test_snapshot_match_decorated_leading_icon():
    """快照中名称带图标前缀时，decorated 分支命中（导航名禁 fuzzy，
    命中只可能来自 decorated——修复前该场景为 False）。"""
    snap = '- link "' + chr(0xF0FE) + ' Contact Us"'
    found, count = snapshot_match(snap, "link", "Contact Us")
    assert (found, count) == (True, 1)


def test_attach_scope_context_duplicates_only():
    """I1 采集：同名重复按钮获容器锚点（跳过价格行）；唯一元素零采集。"""
    from explore import ExploreState, _attach_scope_context, _observe, _parse_elements
    pw, browser, page = _launch()
    try:
        page.set_content(
            '<div data-product-id="p1"><div>Blue Top</div><button>Buy</button></div>\n'
            '<div data-product-id="p2"><div>Rs. 700</div><div>Red Top</div>'
            '<button>Buy</button></div>\n'
            '<button>Only One</button>'
        )
        state = ExploreState(goal="t", entry_url="https://x.com")
        state.snapshot = _observe(page)
        state.elements = _parse_elements(state.snapshot)
        _attach_scope_context(state, page)

        buys = [e for e in state.elements if e.get("name") == "Buy"]
        assert len(buys) == 2
        anchors = {e.get("scope_has_text") for e in buys}
        assert anchors == {"Blue Top", "Red Top"}   # 价格行 Rs. 700 被跳过

        only = [e for e in state.elements if e.get("name") == "Only One"][0]
        assert only.get("scope_has_text") is None   # 唯一元素零采集
    finally:
        browser.close()
        pw.stop()


def test_text_candidates_strip_icon_prefix():
    """图标前缀文本：候选含 text_clean 变体（PUA 在 CSS 伪元素，DOM 无）。"""
    from resolver import RELAXATION_GROUP_OF, STRATEGY_SCORES
    pw, browser, page = _launch()
    try:
        page.set_content('<a class="add">Add to cart</a>\n<a class="add">Add to cart</a>')
        candidates = build_locator_candidates(
            page, ParsedTarget(text=chr(0xF07A) + " Add to cart"),
        )
        strategies = [s for s, _ in candidates]
        assert strategies == ["text", "text_clean"]           # 原样 + 剥装饰
        assert STRATEGY_SCORES["text_clean"] == 55
        assert RELAXATION_GROUP_OF["text_clean"] == "text"    # 同族不互相竞争
        # 原样 0 命中；text_clean 命中 2（歧义由裁决层处理）
        assert candidates[0][1].count() == 0
        assert candidates[1][1].count() == 2
    finally:
        browser.close()
        pw.stop()


def test_text_node_scope_resolution():
    """文本节点 + 编译 scope：图标前缀文本在容器内唯一命中（I1 完整闭环）。"""
    from runner import _resolve_locator
    pw, browser, page = _launch()
    try:
        page.set_content(
            '<div data-product-id="p1"><div>Blue Top</div>'
            '<a class="add">Add to cart</a></div>\n'
            '<div data-product-id="p2"><div>Red Top</div>'
            '<a class="add">Add to cart</a></div>'
        )
        strategy, locator = _resolve_locator(
            page, {"text": chr(0xF07A) + " Add to cart"},
            scope={"has_text": "Blue Top"},
        )
        assert strategy == "text_clean" and locator.count() == 1
        assert "Blue Top" in locator.evaluate(
            "el => el.parentElement ? el.parentElement.innerText : ''",
        )
    finally:
        browser.close()
        pw.stop()


def test_capture_anchors_text_nodes():
    """采集：重复文本节点（无 role）同样获得容器锚点。"""
    from explore import ExploreState, _attach_scope_context
    pw, browser, page = _launch()
    try:
        page.set_content(
            '<div data-product-id="p1"><div>Blue Top</div>'
            '<a class="add">Add to cart</a></div>\n'
            '<div data-product-id="p2"><div>Red Top</div>'
            '<a class="add">Add to cart</a></div>'
        )
        state = ExploreState(goal="t", entry_url="https://x.com")
        state.elements = [
            {"ref": "e1", "type": "text", "text": chr(0xF07A) + " Add to cart"},
            {"ref": "e2", "type": "text", "text": chr(0xF07A) + " Add to cart"},
        ]
        _attach_scope_context(state, page)
        anchors = {e.get("scope_has_text") for e in state.elements}
        assert anchors == {"Blue Top", "Red Top"}
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
