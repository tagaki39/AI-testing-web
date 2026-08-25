"""
══════════════════════════════════════════════════════════════════════
test_corrections.py — L1 持久化定位覆盖规则测试
══════════════════════════════════════════════════════════════════════

零依赖 plain-assert 脚本，直接运行：

    py backend/tests/test_corrections.py

覆盖：
  1. generalize_url：数字段 → * / query 剥离 / 文本路径保留
  2. target_key 序列化（Locator 各形态 / 字符串 target）
  3. store：upsert 覆盖（保留 verified_count）/ enabled 过滤 /
     连续失败 3 次熔断 / success 清零 / 文件持久化往返
  4. 解析管线（浏览器背书）：correction 唯一命中 → resolved_by=correction；
     过期（0 命中）→ 落回标准候选；歧义（2 命中）→ 中性跳过
══════════════════════════════════════════════════════════════════════
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/

from locator import corrections as store   # noqa: E402
from dsl import Locator, validate_case   # noqa: E402
from locator.resolver import target_key   # noqa: E402


# ── 夹具：把 store 重定向到临时文件 ──────────────────────────────────────────

def _reset_store(tmpdir: str) -> None:
    store.STORE_FILE = Path(tmpdir) / "corrections.json"
    store._memory.clear()
    store._loaded = False


def _tmpdir() -> str:
    return tempfile.mkdtemp(prefix="corr_test_")


# ── 1. generalize_url ────────────────────────────────────────────────────────

def test_generalize_url():
    assert store.generalize_url("https://x.com/products/1") == "x.com/products/*"
    assert store.generalize_url("https://x.com/products/1?ref=ad") == "x.com/products/*"
    assert store.generalize_url("https://x.com/login") == "x.com/login"
    assert store.generalize_url("https://x.com/product_details/42") == "x.com/product_details/*"
    assert store.generalize_url("about:blank") == "/blank"   # 本地测试页统一键


# ── 2. target_key ────────────────────────────────────────────────────────────

def test_target_key_serialization():
    assert target_key({"role": "button", "name": "Login"}) == "role:button:name:Login"
    assert target_key(Locator(role="link", name="Cart")) == "role:link:name:Cart"
    assert target_key({"text": "Products"}) == "text:Products"
    assert target_key({"test_id": "login-button"}) == "test_id:login-button"
    assert target_key({"css": ".btn"}) == "css:.btn"
    assert target_key("button=登录") == "role:button:name:登录"
    assert target_key({"name": "no-role"}) == ""   # 无有效定位字段


# ── 3. store 行为 ────────────────────────────────────────────────────────────

def test_upsert_and_find():
    tmp = _tmpdir()
    _reset_store(tmp)
    loc = Locator(css=".submit-btn")
    store.upsert("https://x.com/login", "role:button:name:Login", loc)
    hit = store.find_enabled("https://x.com/login", "role:button:name:Login")
    assert hit is not None and hit.locator.css == ".submit-btn"
    # query 剥离：同页面不同 query 仍命中
    assert store.find_enabled("https://x.com/login?ref=ad", "role:button:name:Login") is not None
    # 精确模式语义：/login/123 是不同页面（泛化为 login/*）→ miss
    assert store.find_enabled("https://x.com/login/123", "role:button:name:Login") is None
    # 键不匹配 → miss
    assert store.find_enabled("https://x.com/login", "role:button:name:Signup") is None


def test_upsert_preserves_verified_count():
    tmp = _tmpdir()
    _reset_store(tmp)
    store.upsert("https://x.com/login", "k", Locator(css=".a"))
    store.record_success("https://x.com/login", "k")
    store.record_success("https://x.com/login", "k")
    # 同键 upsert：更新 locator、保留 verified_count、重新启用
    store.upsert("https://x.com/login", "k", Locator(css=".b"))
    hit = store.find_enabled("https://x.com/login", "k")
    assert hit.locator.css == ".b"
    assert hit.verified_count == 2
    assert hit.consecutive_failures == 0 and hit.enabled


def test_circuit_breaker_disables_after_three_failures():
    tmp = _tmpdir()
    _reset_store(tmp)
    store.upsert("https://x.com/login", "k", Locator(css=".a"))
    store.record_failure("https://x.com/login", "k")
    store.record_failure("https://x.com/login", "k")
    assert store.find_enabled("https://x.com/login", "k") is not None   # 2 次仍启用
    store.record_failure("https://x.com/login", "k")
    assert store.find_enabled("https://x.com/login", "k") is None       # 第 3 次熔断
    # success 清零并重新启用
    hit = [c for c in store.list_all()][0]
    assert hit.enabled is False and hit.consecutive_failures == 3


def test_persistence_roundtrip():
    tmp = _tmpdir()
    _reset_store(tmp)
    store.upsert("https://x.com/login", "k", Locator(test_id="login-btn"))
    # 模拟进程重启：清内存 + 重新加载
    store._memory.clear()
    store._loaded = False
    hit = store.find_enabled("https://x.com/login", "k")
    assert hit is not None and hit.locator.test_id == "login-btn"


# ── 4. 解析管线集成（浏览器背书，Chromium 不可用时 SKIP）─────────────────────

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


def test_correction_candidate_resolves():
    """correction 唯一命中 → resolved_by=correction（不绕过：仍走统一裁决）。"""
    from execution.runner import _resolve_locator
    tmp = _tmpdir()
    _reset_store(tmp)
    pw, browser, page = _launch()
    try:
        page.set_content('<button class="submit-btn">Go</button>')
        store.upsert(page.url, "role:button:name:Login", Locator(css=".submit-btn"))
        strategy, locator = _resolve_locator(page, {"role": "button", "name": "Login"})
        assert strategy == "correction" and locator.count() == 1
    finally:
        browser.close()
        pw.stop()


def test_correction_stale_falls_back():
    """correction 过期（0 命中）→ 落回标准候选 → NotFound（中性 miss）。"""
    from execution.runner import _resolve_locator
    from locator.resolver import LocatorNotFoundError
    tmp = _tmpdir()
    _reset_store(tmp)
    pw, browser, page = _launch()
    try:
        page.set_content('<button class="other">Other</button>')
        store.upsert(page.url, "role:button:name:Login", Locator(css=".missing"))
        try:
            _resolve_locator(page, {"role": "button", "name": "Login"})
        except LocatorNotFoundError:
            return
        raise AssertionError("过期修正未落回标准候选")
    finally:
        browser.close()
        pw.stop()


def test_correction_ambiguous_is_neutral_miss():
    """correction 歧义（2 命中）→ 自然跳过，报 Ambiguous（与普通歧义一致）。"""
    from execution.runner import _resolve_locator
    from locator.resolver import LocatorAmbiguousError
    tmp = _tmpdir()
    _reset_store(tmp)
    pw, browser, page = _launch()
    try:
        page.set_content('<button class="go">A</button>\n<button class="go">B</button>')
        store.upsert(page.url, "role:button:name:Login", Locator(css=".go"))
        try:
            _resolve_locator(page, {"role": "button", "name": "Login"})
        except LocatorAmbiguousError:
            return
        raise AssertionError("歧义修正未按歧义拒绝")
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
