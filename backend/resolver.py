"""
══════════════════════════════════════════════════════════════════════
resolver.py — Semantic Resolver 语义层（R1：单一事实源）
══════════════════════════════════════════════════════════════════════

【这个文件在项目中的位置】
  定位语义的唯一权威实现。Runner（执行）与 Preflight（生成前校验）此前
  各持一份副本，已发生过真实漂移（修复历史见 commit 注释）——
  本模块抽离后两者共用同一实现，语义不可能再分叉。

【包含什么 / 不包含什么】
  包含：定位语义本身
    - target 解析（字符串/结构化 → ParsedTarget）
    - 候选构建顺序（test_id → role exact → decorated-exact
      → fuzzy（导航名禁止）→ text → css）
    - 导航名 allowlist / 图标前缀容忍 / 业务实体标识
    - 快照文本匹配（Preflight 的 0/1/N 判定，与 DOM 侧同一语义）
    - 定位异常（未找到 / 歧义）
  不包含：编排逻辑（三分法循环、作用域容器爬取、时间预算、
    可见性过滤、同一元素判定）——这些留在 runner._resolve_locator，
    R2 重构解析管线（scoring + confidence gate）时再迁入。

【零依赖设计】
  不 import playwright：locator 对象由调用方传入，本模块只调用其方法
  （get_by_role / get_by_test_id / locator / evaluate）。
  不 import 任何项目模块：依赖 DAG 为 resolver ← runner / ai_agent /
  explore_flow，无环。

【学习路径】
  parse_target（目标统一）→ build_locator_candidates（候选顺序）
  → snapshot_match（快照侧语义）→ 异常
══════════════════════════════════════════════════════════════════════
"""

import re
from dataclasses import dataclass


# ── 导航 target 名（locator 语义共享：导航名禁止 fuzzy substring）────
_NAV_TARGET_NAMES = {
    "cart", "products", "home", "logout", "login", "signup / login",
    "signup/login", "test cases", "api testing", "contact us",
}


def is_navigation_name(role: str | None, name: str | None) -> bool:
    """导航级元素判断（Runner 与 Preflight 共享）。"""
    return (
        role == "link"
        and (name or "").strip().casefold() in _NAV_TARGET_NAMES
    )


# ── accessible name 修饰容忍（Runner 与 Preflight 共享）───────────────
# FontAwesome 私有区字符（U+E000-U+F8FF）会成为 accessible name 的一部分
#（如 "<图标> Cart"）——decorated-exact 容忍图标前缀 + 空白。
# 用 chr() 构造字符范围（避免源码含字面 PUA 字符 → GBK 编码问题）。
_DECORATED_PREFIX = (
    "[" + "".join(chr(c) for c in range(0xE000, 0xF8FF + 1)) + r"\s]*"
)


def decorated_name_pattern(name: str):
    """构建可匹配"图标前缀 + 名称"的正则（Playwright selector 安全）。

    Playwright selector 正则约束（实测）：
      - 裸 "/" 是定界符 → 必须转义 "\\/"（token 内的斜杠）
      - "\\s" 只在字符类内安全（类外 "\\s+" 报 InvalidSelectorError）
      - "\\uE000" 等 Unicode 转义安全
    因此：token 级 re.escape + "/" 手动转义 + 空白用字符类 "[ \\s]*"。
    "Signup / Login" → ^[<PUA 范围>\\s]*Signup[ \\s]*\\/[ \\s]*Login$
    """
    parts = []
    for part in name.strip().split():
        escaped = re.escape(part)
        escaped = escaped.replace("/", "\\/")   # Playwright 正则定界符转义
        parts.append(escaped)
    literal = r"[ \s]*".join(parts)
    return re.compile(rf"^{_DECORATED_PREFIX}{literal}$")


# ── 业务实体标识（Runner 与 Preflight 共享的判定语义）────────────────
# 保守 allowlist：只认 product/item 级别明确业务 id，不泛化 data-id
BUSINESS_ID_ATTRS = ("data-product-id", "data-item-id")


def business_identity(locator) -> str | None:
    """返回 locator 的业务实体标识（"data-product-id=1"）或 None。"""
    try:
        return locator.evaluate(
            """el => {
                for (const a of ['data-product-id', 'data-item-id']) {
                    const v = el.getAttribute(a);
                    if (v) return a + '=' + v;
                }
                return null;
            }"""
        )
    except Exception:
        return None


# 语义定位支持的已知角色（白名单，防止把任意文本当角色解析）
# 例如 target="登录=xxx" 时，"登录"不在白名单里 → 按纯文本处理而不是按角色
KNOWN_ROLES = {
    "button", "link", "textbox", "heading", "checkbox", "radio",
    "option", "menuitem", "listitem", "combobox", "tab", "searchbox",
}


# ── 异常类型（定位失败时的两种明确语义）──────────────────────────────────────────
# 自定义异常让调用方能区分"没找到"和"有歧义"，从而给出不同提示。

class LocatorNotFoundError(Exception):
    """0 个匹配：元素不存在或未渲染。"""


class LocatorAmbiguousError(Exception):
    """2+ 个匹配：定位不唯一，必须通过作用域/更精确的 target 消歧。"""


# ── target 解析（字符串 / 结构化 → 统一数据结构）────────────────────────────────
# DSL 里 target 有两种写法（"button=登录" 字符串 或 {"role":...} 结构化），
# 执行器需要把它们统一成一种数据结构 ParsedTarget，后面才好处理。

@dataclass
class ParsedTarget:
    """解析后的统一目标：五选一（或组合）。"""
    role: str | None = None
    name: str | None = None
    text: str | None = None
    test_id: str | None = None
    css: str | None = None


def parse_target(target: str | dict | None) -> ParsedTarget | None:
    """把 DSL target 解析成 ParsedTarget，支持字符串和结构化两种格式。

    注意：DSL 经 Pydantic 解析后，dict 格式的 target 实际是
    Locator 模型实例（不是 dict）——必须先统一转回 dict。

    字符串格式解析规则（从左到右尝试）：
      "css=..."       → CSS 定位
      "test_id=..."   → data-testid 定位
      "text=..."      → 显式文本定位
      "角色=名称"     → 语义定位（角色必须在白名单里）
      其他            → 纯文本定位（兜底）
    """
    if target is None:
        return None

    if not isinstance(target, str):
        # Pydantic 模型实例（Locator）→ 转回 dict
        target = target.model_dump() if hasattr(target, "model_dump") else dict(target)

    if isinstance(target, dict):
        # 结构化格式：直接取出各字段
        return ParsedTarget(
            role=target.get("role"),
            name=target.get("name"),
            text=target.get("text"),
            test_id=target.get("test_id"),
            css=target.get("css"),
        )

    t = target.strip()
    if t.startswith("css="):
        return ParsedTarget(css=t[4:].strip())
    if t.startswith(("test_id=", "testid=")):
        return ParsedTarget(test_id=t.split("=", 1)[1].strip())
    if t.startswith("text="):
        # 显式文本定位（"text" 不是语义角色，必须单独识别）
        return ParsedTarget(text=t[5:].strip())
    if "=" in t:
        # "button=登录" → role="button", name="登录"
        role, _, name = t.partition("=")
        role = role.strip()
        if role in KNOWN_ROLES:           # 只有已知角色才走语义定位
            return ParsedTarget(role=role, name=name.strip())
    return ParsedTarget(text=t)           # 兜底：当作纯文本定位


def build_locator_candidates(container, t: ParsedTarget) -> list[tuple[str, object]]:
    """在 container（page 或 locator）内构建候选定位器，按稳定性排序。

    返回 [(策略名, Playwright locator), ...]：
      test_id → role → text → css（稳定性从高到低）
    执行时逐个试，第一个 count==1 的胜出。

    为什么 role 用模糊匹配（exact=False）？
      真实页面常见：icon 前缀空格（<i></i> Signup / Login）、
      CSS text-transform 大小写变化——accessible name 与可见文本常不一致。
      严格匹配（exact=True）反而会 0 命中。
      歧义仍然安全：模糊匹配到 2+ 个会被三分法拦截。
    """
    candidates: list[tuple[str, object]] = []
    if t.test_id:
        # get_by_test_id 默认只认 data-testid；真实站点常用 data-test/data-qa
        #（saucedemo 用 data-test）→ 附加属性变体
        candidates.append(("test_id", container.get_by_test_id(t.test_id)))
        candidates.append(("test_id_attr", container.locator(
            f'[data-test="{t.test_id}"], [data-qa="{t.test_id}"]',
        )))
    if t.role and t.name:
        # exact-first：避免 "Cart" 模糊命中 "View Cart"（修复 Step 9）
        candidates.append(("role", container.get_by_role(t.role, name=t.name, exact=True)))
        # decorated-exact：容忍 FontAwesome 私有区图标前缀（如 "<图标> Cart"），
        # 排除 "View Cart"/"Add to cart"
        candidates.append(("role_decorated", container.get_by_role(
            t.role, name=decorated_name_pattern(t.name),
        )))
        # 导航级短名（Cart/Home/Products/Login）禁止 fuzzy substring——
        # 否则 "Cart" 会命中 "Add to cart"，再被 business dedup 误聚合成
        # "错误点击却看似成功"（比明确失败更危险）
        if not is_navigation_name(t.role, t.name):
            candidates.append(("role_fuzzy", container.get_by_role(t.role, name=t.name)))
    if t.text:
        candidates.append(("text", container.get_by_text(t.text)))
    if t.css:
        candidates.append(("css", container.locator(t.css)))
    return candidates


# ── 快照文本匹配（Preflight 的 0/1/N 判定，与 DOM 侧同一语义）────────────────────

def snapshot_match(snapshot: str, role: str | None, name: str) -> tuple[bool, int]:
    """在 ARIA 快照文本中查找 role+name 或纯文本，返回 (是否找到, 出现次数)。

    匹配语义与 DOM 侧 build_locator_candidates 对齐（exact → decorated →
    非导航才 fuzzy），导航短名（Cart 等）禁止 fuzzy substring——否则
    "Cart" 会把 "Add to cart"/"View Cart" 计入，制造 false AMBIGUOUS。

    快照格式（aria_snapshot 输出）：
      - button "Add to cart"        ← role+name 格式
      - text: Your Cart             ← 纯文本格式
    """
    if role:
        pattern = re.compile(rf'\b{re.escape(role)}\s+"([^"]*)"')
        quotes = pattern.findall(snapshot)
        if not quotes:
            return False, 0
        # exact
        exact = [q for q in quotes if q.strip() == name]
        if exact:
            return True, len(exact)
        # decorated-exact（图标前缀 + 名称）
        decorated = [q for q in quotes if decorated_name_pattern(name).fullmatch(q.strip())]
        if decorated:
            return True, len(decorated)
        # 非导航才 fuzzy substring
        if not is_navigation_name(role, name):
            fuzzy = [q for q in quotes if name.lower() in q.lower()]
            return bool(fuzzy), len(fuzzy)
        return False, 0
    # 纯文本：直接在快照里找
    return name.lower() in snapshot.lower(), snapshot.lower().count(name.lower())


# ── 定位器构建（Preflight / 候选提取用的两个入口，同一候选顺序）──────────────────

def is_navigation_target(target) -> bool:
    """target 是否属于导航级元素（link 且名称在 allowlist）。"""
    parsed = parse_target(target)
    if parsed is None:
        return False
    return is_navigation_name(parsed.role, parsed.name)


def build_locator_exact_first(page, target: dict):
    """构建定位器（与 Runner 共享语义）：exact → decorated-exact →
    fuzzy（导航名禁止 fuzzy）。

    返回 count>0 的第一个候选；全部为 0 → None。
    修复历史：FontAwesome 图标字符进 accessible name（"<图标> Cart"）——
    exact 失败后尝试 decorated-exact，而不是直接跳脏 fuzzy。
    """
    parsed = parse_target(target)
    if parsed is None:
        return None
    if parsed.test_id:
        loc = page.get_by_test_id(parsed.test_id)
        if loc.count() == 0:
            loc = page.locator(f'[data-test="{parsed.test_id}"], [data-qa="{parsed.test_id}"]')
        return loc
    if parsed.role and parsed.name:
        loc = page.get_by_role(parsed.role, name=parsed.name, exact=True)
        if loc.count() == 0:
            loc = page.get_by_role(
                parsed.role, name=decorated_name_pattern(parsed.name),
            )
        if loc.count() == 0 and not is_navigation_name(parsed.role, parsed.name):
            loc = page.get_by_role(parsed.role, name=parsed.name)
        return loc
    if parsed.text:
        return page.get_by_text(parsed.text, exact=True)
    if parsed.css:
        return page.locator(parsed.css)
    return None


def build_locator_for_count(page, target: dict):
    """直接构建定位器（绕过三分法，允许 count>1）——候选提取专用。"""
    parsed = parse_target(target)
    if parsed is None:
        return None
    if parsed.test_id:
        loc = page.get_by_test_id(parsed.test_id)
        if loc.count() == 0:
            loc = page.locator(f'[data-test="{parsed.test_id}"], [data-qa="{parsed.test_id}"]')
        return loc
    if parsed.role and parsed.name:
        return page.get_by_role(parsed.role, name=parsed.name)
    if parsed.text:
        return page.get_by_text(parsed.text)
    if parsed.css:
        return page.locator(parsed.css)
    return None
