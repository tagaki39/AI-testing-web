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
    ParsedTarget, build_locator_candidates, build_locator_exact_first,
    build_locator_for_count, decorated_name_pattern, is_navigation_name,
    parse_target, snapshot_match,
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
