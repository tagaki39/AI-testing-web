"""
══════════════════════════════════════════════════════════════════════
snapshot.py — 页面结构快照（ARIA snapshot 抓取）
══════════════════════════════════════════════════════════════════════

【这个文件在项目中的位置】
  新增的一站：AI 生成 DSL 之前
    用户需求 →【这里：打开页面抓语义结构】→ 注入 prompt → AI 生成

【为什么需要它（面试重点）】
  没有快照时，AI 是"盲猜"页面结构——它可能生成 target="heading=Home"
  但页面上根本没有这个元素（你之前踩过的坑）。
  有了快照，AI 看到真实页面："button 登录 / link 首页 / textbox 邮箱"，
  生成的定位器基于真实元素，准确率大幅提升。

【ARIA snapshot 是什么】
  Playwright 提供 locator.aria_snapshot()：把页面可访问性结构
  输出成 YAML 文本。它是无障碍树的人类可读视图：

    button "Signup / Login"
    link "Products"
    textbox "Email Address"

【降级原则（与原项目一致）】
  快照抓取失败（URL 无效 / 页面超时 / 网络不通）→ 返回 None，
  上层降级为"无快照直接生成"——绝不中断主链路。
══════════════════════════════════════════════════════════════════════
"""

from playwright.sync_api import sync_playwright

# 快照最大长度（字符）。真实页面 A11y 树可能很长，
# 全部塞进 prompt 会超上下文/浪费 token——截断保留关键结构。
_MAX_SNAPSHOT_CHARS = 6000


def capture_page_snapshot(url: str) -> str | None:
    """打开 URL，抓取页面 ARIA snapshot，返回 YAML 文本。

    参数 url: 被测网站入口（已由 AI 从用户需求中提取）
    返回: 快照文本；任何失败返回 None（调用方降级处理）

    为什么失败返回 None 而不是抛异常？
      生成链路的主线是"生成 DSL"，快照只是增强上下文。
      页面打不开时，用户还能用无快照模式生成（原项目同样降级）。
    """
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(30000)
            # domcontentloaded：不等图片等资源加载完，更快；重页面也不会卡死
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)   # 给 JS 渲染留时间（单页应用可能异步出内容）

            # aria_snapshot() 返回无障碍树的 YAML 文本（含 role、name、结构层级）
            snapshot = page.locator("body").aria_snapshot()

            browser.close()

        if not snapshot or not snapshot.strip():
            return None
        # 截断：保留开头（顶层结构最有价值）
        return snapshot[: _MAX_SNAPSHOT_CHARS]
    except Exception:
        # 任何失败（URL 无效/超时/页面拒绝连接）→ 降级
        return None
