"""
══════════════════════════════════════════════════════════════════════
runner.py — Playwright 执行引擎（整个项目的心脏）
══════════════════════════════════════════════════════════════════════

【这个文件在项目中的位置】
  数据流第三站（真正的"干活"的地方）：
    DSL（已通过校验）→【这里：Playwright 打开真实浏览器逐步骤执行】
    → 每步产出证据（状态/截图/URL）→ 报告返回前端

【核心循环（整个项目的灵魂，面试必讲）】
    for step in case.steps:
        try:  执行动作（定位 → 操作）
        except: 记录失败（不阻断后续步骤）
        每步截图（证据）

【定位器"三分法"（Playwright 官方推荐的心智模型，面试重点）】
    count == 0  → LocatorNotFoundError   未找到，报错
    count == 1  → 直接使用               唯一，继续
    count > 1   → LocatorAmbiguousError  歧义，绝不自动选第一个
    为什么歧义不自动选第一个？
      页面改版后，"第一个"可能已经不是目标元素——宁可靠错误，不可点错元素。

【定位降级顺序（按稳定性）】
    data-testid（最稳，需埋点）→ 语义定位 get_by_role（官方推荐）
    → 文本 get_by_text → CSS 兜底

【学习路径】
  从 execute_case（入口）开始 → 进入 _execute_step（单步）
  → _resolve_locator（三分法核心）→ _parse_target / _build_locators / scope
══════════════════════════════════════════════════════════════════════
"""

import re
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from urllib.parse import urlparse
from uuid import uuid4

from playwright.sync_api import sync_playwright, expect

from dsl import DSLCase, DSLStep

# 执行截图保存目录（项目根/artifacts）
ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "artifacts"

# 变量占位符的正则：匹配 "${email}" 这种写法
# re.compile 预编译一次，后面反复用，比每次 re.search 快
_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# ── 导航 target 名（locator 语义共享：导航名禁止 fuzzy substring）────
_NAV_TARGET_NAMES = {
    "cart", "products", "home", "logout", "login", "signup / login",
    "signup/login", "test cases", "api testing", "contact us",
}


def _is_navigation_name(role: str | None, name: str | None) -> bool:
    """导航级元素判断（Runner 与 ai_agent 共享）。"""
    return (
        role == "link"
        and (name or "").strip().casefold() in _NAV_TARGET_NAMES
    )


# ── accessible name 修饰容忍（Runner 与 Preflight 共享）───────────────
# FontAwesome 私有区字符（U+E000-U+F8FF）会成为 accessible name 的一部分
#（如 " Cart"）——decorated-exact 容忍图标前缀 + 空白。
# 用 ASCII-safe 转义写字符范围（避免源码含字面 PUA 字符 → GBK 编码问题）。
_DECORATED_PREFIX = r"[\uE000-\uF8FF\s]*"


def _decorated_name_pattern(name: str):
    """构建可匹配"图标前缀 + 名称"的正则（Playwright selector 安全）。

    Playwright selector 正则约束（实测）：
      - 裸 "/" 是定界符 → 必须转义 "\\/"（token 内的斜杠）
      - "\\s" 只在字符类内安全（类外 "\\s+" 报 InvalidSelectorError）
      - "\\uE000" 等 Unicode 转义安全
    因此：token 级 re.escape + "/" 手动转义 + 空白用字符类 "[ \\s]*"。
    "Signup / Login" → ^[\\uE000-\\uF8FF\\s]*Signup[ \\s]*\\/[ \\s]*Login$
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
_BUSINESS_ID_ATTRS = ("data-product-id", "data-item-id")


def _business_identity(locator) -> str | None:
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


# ── goto URL 安全校验（第 6 项：SSRF/内网探测基础防护）───────────────
# 平台允许用户手改 DSL——上线后 goto 可被用来探测内网/云 metadata。
# 只允许公网 http/https；localhost/内网 IP/私有网段直接拒绝。
_PRIVATE_IP_PREFIXES = (
    "127.", "10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
    "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.",
)


def _validate_goto_url(url: str) -> bool:
    """goto URL 白名单校验：公网 http/https，非内网地址。"""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        if host in ("localhost", "0.0.0.0"):
            return False
        if host.startswith(_PRIVATE_IP_PREFIXES):
            return False
        return True
    except Exception:
        return False


# 语义定位支持的已知角色（白名单，防止把任意文本当角色解析）
# 例如 target="登录=xxx" 时，"登录"不在白名单里 → 按纯文本处理而不是按角色
_KNOWN_ROLES = {
    "button", "link", "textbox", "heading", "checkbox", "radio",
    "option", "menuitem", "listitem", "combobox", "tab", "searchbox",
}


# ── 异常类型（定位失败时的三种明确语义）──────────────────────────────────────────
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


def _parse_target(target: str | dict | None) -> ParsedTarget | None:
    """把 DSL target 解析成 ParsedTarget，支持字符串和结构化两种格式。

    注意：DSL 经 Pydantic 解析后，dict 格式的 target 实际是
    LocatorTarget 模型实例（不是 dict）——必须先统一转回 dict。

    字符串格式解析规则（从左到右尝试）：
      "css=..."       → CSS 定位
      "test_id=..."   → data-testid 定位
      "角色=名称"     → 语义定位（角色必须在白名单里）
      其他            → 纯文本定位（兜底）
    """
    if target is None:
        return None

    if not isinstance(target, str):
        # Pydantic 模型实例（LocatorTarget）→ 转回 dict
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
        if role in _KNOWN_ROLES:          # 只有已知角色才走语义定位
            return ParsedTarget(role=role, name=name.strip())
    return ParsedTarget(text=t)           # 兜底：当作纯文本定位


def _build_locators(container, t: ParsedTarget) -> list[tuple[str, object]]:
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
        candidates.append(("test_id", container.get_by_test_id(t.test_id)))
    if t.role and t.name:
        # exact-first：避免 "Cart" 模糊命中 "View Cart"（修复 Step 9）
        candidates.append(("role", container.get_by_role(t.role, name=t.name, exact=True)))
        # decorated-exact：容忍 FontAwesome 私有区图标前缀（如 " Cart"），
        # 排除 "View Cart"/"Add to cart"
        candidates.append(("role_decorated", container.get_by_role(
            t.role, name=_decorated_name_pattern(t.name),
        )))
        # 导航级短名（Cart/Home/Products/Login）禁止 fuzzy substring——
        # 否则 "Cart" 会命中 "Add to cart"，再被 business dedup 误聚合成
        # "错误点击却看似成功"（比明确失败更危险）
        if not _is_navigation_name(t.role, t.name):
            candidates.append(("role_fuzzy", container.get_by_role(t.role, name=t.name)))
    if t.text:
        candidates.append(("text", container.get_by_text(t.text)))
    if t.css:
        candidates.append(("css", container.locator(t.css)))
    return candidates


# ── 作用域解析（消歧：先锁定容器，再在容器内找目标）──────────────────────────────
# 解决"页面有 6 个 Add to cart"这类同名歧义：
# 先找到包含目标文本的容器，再在容器内查找。

def _text_scope_containers(page, text: str) -> list[object]:
    """文本作用域：所有匹配文本的元素向上爬最多 3 层父级作为候选容器。

    ⚠️ 修复：不再 .first 自动选第一个——文本匹配多处时（如"Blue Top"
    出现在商品列表和购物车推荐），每个匹配都生成候选容器，
    由 _resolve_locator 的 scope 联合三分法判断哪个容器内 target 唯一。
    """
    anchors = page.get_by_text(text)
    count = anchors.count()
    if count == 0:
        return []
    containers: list[object] = []
    for i in range(min(count, 5)):   # 限制候选数，防容器膨胀
        base = anchors.nth(i)
        containers.append(base)
        current = base
        for _ in range(3):
            current = current.locator("xpath=..")   # xpath=.. 表示"父元素"
            containers.append(current)
    return containers


def _resolve_scope_containers(page, scope) -> list[object]:
    """返回候选"容器"列表；无作用域时返回 [page]（全页面查找）。

    降级链（与原项目"product role → 文本爬父级"同思路）：
      1. 结构化 role/test_id + has_text（最精确，先尝试）
      2. 结构化容器 0 匹配时 → 自动追加"has_text 文本爬父级"容器
         （很多页面容器是无角色 div，AI 只知道文本不知道角色）
      3. 只有 has_text / 字符串 scope → 直接文本爬父级

    字符串 scope（兼容 "inside Blue Top" 格式）也走第 3 级。
    """
    if scope is None:
        return [page]

    if not isinstance(scope, str):
        # Pydantic 模型实例（LocatorScope）→ 转回 dict（同 _parse_target 处理）
        scope = scope.model_dump() if hasattr(scope, "model_dump") else dict(scope)

    if isinstance(scope, dict):
        containers = []
        if scope.get("role"):
            c = page.get_by_role(scope["role"])
            if scope.get("has_text"):
                c = c.filter(has_text=scope["has_text"])   # 容器必须包含该文本
            containers.append(c)
        if scope.get("test_id"):
            c = page.get_by_test_id(scope["test_id"])
            if scope.get("has_text"):
                c = c.filter(has_text=scope["has_text"])
            containers.append(c)

        # 降级：AI 只给了 has_text（不知道容器角色），或角色容器不存在
        # → 追加文本爬父级容器作为兜底（放在最后，精确的优先尝试）
        if scope.get("has_text"):
            containers.extend(_text_scope_containers(page, scope["has_text"]))

        if not containers:
            raise LocatorNotFoundError(f"scope 无效: {scope}")
        return containers

    # 字符串 scope：文本爬父级
    return _text_scope_containers(page, scope)


# ── 定位器解析（三分法入口）──────────────────────────────────────────────────────

def _resolve_locator(page, target, scope=None, *, allow_lazy: bool = False, timeout_ms: int = 15000):
    """核心定位函数：target + 可选 scope → (命中策略名, 唯一 Playwright locator)。

    处理流程：
      1. 解析 target → ParsedTarget
      2. 解析 scope → 候选容器列表（无 scope 就是全页面）
      3. 遍历容器 × 遍历定位策略（test_id→role→text→css）
      4. 每个 locator 数匹配数（count()）：
           == 1 → 就是它，返回（附带策略名，供统计定位分布）
           >  1 → AmbiguousError（歧义，提示用 scope）
           == 0 → 试下一个策略（降级）

    返回值带策略名是"量化设计"：每次执行自动记录定位策略命中分布，
    积累后产出"语义定位命中率 83%"这类面试数据。

    allow_lazy=True（wait_for 步骤专用）：
      元素可能正在渲染（页面异步加载），count()==0 不代表不存在。
      此时用 locator.wait_for() 等待它出现（Playwright 内部自动轮询）。
      其他动作（click/input）要求元素已存在，不做等待。
    """
    t = _parse_target(target)
    if t is None or (t.role is None and t.text is None and t.test_id is None and t.css is None):
        raise LocatorNotFoundError(f"target 无法解析: {target!r}")

    # fast path（scope 最小化原则）：全页面 target 唯一 → scope 不参与。
    # "scope can reduce ambiguity, but can never introduce ambiguity into
    #  an already unique target"——冗余 scope（如 has_text="Login"）不得
    # 让本来唯一的 locator 变得歧义。
    if scope is not None:
        for strategy, locator in _build_locators(page, t):
            try:
                if locator.count() == 1:
                    return strategy, locator
            except Exception:
                pass

    containers = _resolve_scope_containers(page, scope)

    # 两阶段 Resolver + global deadline（修复执行慢：per-candidate 等待
    # 5s × N 容器 × M 策略可叠加成 60-110s）：
    #   Phase A：立即扫描（只 count()，不等待）——已有候选就直接处理
    #   Phase B：全部 count==0 时，全局预算（≤5s）内轮询 rescan
    # 整个 _resolve_locator 的时间上界 ≈ 5s，不再随候选数线性增长。
    resolve_deadline = perf_counter() + min(timeout_ms, 5000) / 1000.0

    def scan() -> tuple[list, list[str], bool]:
        """Phase A：只 count()。返回 (唯一命中列表, 策略错误, 是否存在>0 匹配)。"""
        matches: list[tuple[str, object]] = []
        errors: list[str] = []
        has_positive = False
        for container in containers:
            for strategy, locator in _build_locators(container, t):
                try:
                    count = locator.count()
                except Exception as exc:
                    # strategy-local failure：单策略异常不炸链，记录继续
                    errors.append(f"{strategy}: {type(exc).__name__}: {str(exc)[:120]}")
                    continue
                if count == 1:
                    matches.append((strategy, locator))
                    break   # 该容器内唯一命中，下一容器
                if count > 1:
                    has_positive = True
                    continue   # 容器太宽 → 尝试更精确的容器/策略
                # count == 0：留给 Phase B 判定（全 0 才等待）
        return matches, errors, has_positive

    matches: list[tuple[str, object]] = []
    strategy_errors: list[str] = []
    while True:
        matches, strategy_errors, has_positive = scan()
        if matches:
            break   # 唯一命中已存在
        # 无唯一命中：歧义（>1）存在时不值得等待；全 0 且预算内 → 轮询
        if has_positive or perf_counter() >= resolve_deadline:
            break
        page.wait_for_timeout(150)
        # allow_lazy（wait_for/assert_visible）：visible 等待由执行器
        #（定位唯一后 locator.wait_for(visible)）负责，这里只保证"出现且唯一"

    if len(matches) == 1:
        return matches[0][0], matches[0][1]
    if len(matches) > 1:
        # ① 可见性过滤：同一商品 normal+overlay 双 render 时，隐藏的
        #    Add to cart 不计入（overlay 通常不可见）——distinct actionable
        visible: list[tuple[str, object]] = []
        for m in matches:
            try:
                if m[1].is_visible():
                    visible.append(m)
            except Exception:
                visible.append(m)   # 无法判断 → 保留
        candidates = visible if visible else matches

        # ② 同一元素判定：多个容器指向同一个 DOM 元素（容器嵌套冗余）
        #    → 消歧成功，不是真歧义
        try:
            first = candidates[0][1].element_handle(timeout=2000)
            all_same = all(
                loc.evaluate("(el, ref) => el === ref", first)
                for _, loc in candidates[1:]
            )
            if all_same:
                return candidates[0][0], candidates[0][1]
        except Exception:
            pass

        # ③ 业务实体聚类（严格版）：多个 DOM 若【全部】共享同一个明确业务
        # 标识（data-product-id / data-item-id），是同一商品的重复表示
        # （normal+overlay）→ equivalence-class representative selection
        # （不是 arbitrary first——语义：已证明同一业务实体才选一个）。
        # 保守 allowlist：不泛化 data-id（语义太弱，可能是 row/component id）。
        identities = [_business_identity(loc) for _, loc in candidates]
        if (identities
                and all(x is not None for x in identities)
                and len(set(identities)) == 1):
            return candidates[0]

        raise LocatorAmbiguousError(
            f"scope 下存在多个候选容器，target 在 {len(candidates)} 处唯一命中（可见、不同元素、不同业务实体）: {target}"
        )
    hint = f"，请用 scope 消歧（如 scope={'{'}\"role\":\"listitem\",\"has_text\":\"...\"{'}'}）" if scope is None else ""
    error_detail = f"；strategy_errors={strategy_errors}" if strategy_errors else ""
    raise LocatorNotFoundError(
        f"所有定位策略均未命中: {target}{hint}{error_detail}"
    )


# ── 变量替换 ────────────────────────────────────────────────────────────────────
# DSL 里 ${email} 是占位符，执行前替换成真实值。
# 缺失的变量直接报错（而不是静默留下 ${email} 让执行失败得莫名其妙）。

def _substitute(value: str | None, variables: dict[str, str]) -> str | None:
    """把 ${email} 之类的变量替换成真实值；缺失变量明确报错。

    re.sub 的用法：pattern.sub(替换函数, 文本)
      - 每匹配到一处 ${xxx}，就调用 replace(match) 得到替换值
      - match.group(1) 是正则里括号捕获的部分（变量名）
    """
    if not value:
        return value

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in variables:
            raise KeyError(f"运行时变量缺失: ${{{key}}}")
        return variables[key]

    return _VAR_PATTERN.sub(replace, value)


# ── 单步执行 ────────────────────────────────────────────────────────────────────

def _execute_step(page, step: DSLStep, variables: dict[str, str], step_dir: Path, index: int) -> dict:
    """执行单步，返回证据字典。失败不抛出——记录在结果里，继续执行下一步。

    关键设计（面试点）：
      - 每步独立 try/except：一步失败不阻断后续步骤
      - 成功/失败都截图：失败截图是排查问题的最重要证据

    动作分发（action 白名单的 5 种）：
      goto        → 跳转页面
      click       → 点击元素（先定位再点击）
      input       → 在输入框填入文本
      wait_for    → 等待元素出现（allow_lazy）
      assert_text → 断言文本（有 target 断言元素内文本，无 target 断言整页）
    """
    # 先构造"证据骨架"（默认全绿，失败时改 status/error）
    # duration_ms / resolved_by 是量化字段：每步耗时 + 定位策略命中
    step_started_at = perf_counter()
    evidence = {
        "step_index": index,
        "action": step.action,
        "target": step.target if isinstance(step.target, str) else (step.target.model_dump() if step.target else None),
        "scope": step.scope if isinstance(step.scope, str) else (step.scope.model_dump() if step.scope else None),
        "value": step.value,
        "status": "passed",
        "error": None,
        "url": None,
        "screenshot": None,
        "duration_ms": 0,
        "resolved_by": None,     # 定位策略：test_id / role / text / css / None(goto/整页断言)
        "resolve_ms": 0,         # 定位解析耗时（区分 resolve vs action）
    }
    try:
        if step.action == "goto":
            # goto 不需要定位，直接跳转；相对路径会拼 base_url（简化版直接传完整 URL）
            # SSRF 防护：只允许公网 http/https（第 6 项）
            url = _substitute(step.value, variables) or ""
            if not _validate_goto_url(url):
                raise ValueError(f"goto URL 不合法（仅允许公网 http/https）: {url[:60]}")
            page.goto(url, wait_until="domcontentloaded", timeout=step.timeout_ms)

        elif step.action == "assert_url":
            # URL 断言不需要定位：验证 URL 包含期望片段（登录跳转最实用）。
            # 用 expect().to_have_url 自动重试——点击后页面导航中立即断言
            # 会 false failure（修复：之前是立即判断 page.url）
            expected = _substitute(step.value, variables) or ""
            expect(page).to_have_url(
                re.compile(re.escape(expected)),
                timeout=step.timeout_ms,
            )

        elif step.action == "assert_text" and not step.target:
            # 无 target 的断言 → 验证整个页面包含文本
            text = _substitute(step.value, variables) or ""
            expect(page.locator("body")).to_contain_text(text, timeout=step.timeout_ms)

        else:
            # 先定位（三分法），再执行动作
            # wait_for / assert_visible 允许"等待出现"（元素可能渲染中）
            allow_lazy = step.action in ("wait_for", "assert_visible")
            resolve_started = perf_counter()
            try:
                resolved_by, locator = _resolve_locator(
                    page, step.target, step.scope,
                    allow_lazy=allow_lazy,
                    timeout_ms=step.timeout_ms,
                )
            finally:
                # 调用方计时：成功/NotFound/Ambiguous 都有真实 resolve_ms
                evidence["resolve_ms"] = int((perf_counter() - resolve_started) * 1000)
            evidence["resolved_by"] = resolved_by   # 记录定位策略命中

            if step.action == "click":
                locator.click(timeout=step.timeout_ms)
            elif step.action in ("fill", "input"):
                # input 是旧版别名，统一走 fill（Playwright 语义）
                locator.fill(_substitute(step.value, variables) or "", timeout=step.timeout_ms)
            elif step.action == "select":
                # 下拉框：按可见文本选选项
                locator.select_option(label=_substitute(step.value, variables) or "", timeout=step.timeout_ms)
            elif step.action == "check":
                # 复选框：勾选（已勾选则跳过）
                locator.check(timeout=step.timeout_ms)
            elif step.action == "wait_for":
                locator.wait_for(state="visible", timeout=step.timeout_ms)
            elif step.action == "assert_visible":
                expect(locator).to_be_visible(timeout=step.timeout_ms)
            elif step.action == "assert_text":
                text = _substitute(step.value, variables) or ""
                expect(locator).to_contain_text(text, timeout=step.timeout_ms)

        # 每步截图作为证据（full_page=True 截整页，不只是视口）
        shot = step_dir / f"step-{index:02d}.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
            evidence["screenshot"] = f"/artifacts/{step_dir.name}/step-{index:02d}.png"
        except Exception:
            pass

        evidence["url"] = page.url

    except Exception as exc:
        # 记录失败：保留异常类型名 + 前 300 字符的错误信息
        evidence["status"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        # 失败也截图，方便排查
        try:
            shot = step_dir / f"step-{index:02d}.png"
            page.screenshot(path=str(shot))
            evidence["screenshot"] = f"/artifacts/{step_dir.name}/step-{index:02d}.png"
        except Exception:
            pass

    # 量化字段：本步耗时（毫秒）
    evidence["duration_ms"] = max(0, int((perf_counter() - step_started_at) * 1000))
    return evidence


# ── 执行入口 ────────────────────────────────────────────────────────────────────

def execute_case(
    case: DSLCase,
    variables: dict[str, str] | None = None,
    continue_on_failure: bool = False,
) -> dict:
    """执行整个用例，返回报告。

    流程：
      1. 合并 input_contract 的默认值到变量表
      2. 创建本轮执行独立目录（uuid，修复并发冲突）
      3. 启动 Playwright → 打开 Chromium（headless 无头模式）
      4. 逐步骤执行（核心循环）
      5. 统计通过数 → 返回报告 dict

    continue_on_failure（修复 #18：默认 fail-fast）：
      False → 某步失败后剩余步骤标记 skipped（级联失败不产生噪音报告）
      True  → 每步独立成败，继续执行（旧行为）

    sync_playwright() 上下文管理器：自动管理浏览器生命周期。
    headless=True：无头模式（不弹窗口），服务器环境必须用这个。
    """
    variables = dict(variables or {})
    # 把 input_contract 里的默认值合并进来（DSL 声明的变量默认值）
    # 兼容：模型实例（v2 schema，default 字段）与旧 dict（value 字段）
    for contract in case.input_contract:
        c = contract.model_dump() if hasattr(contract, "model_dump") else contract
        key = c.get("key") or c.get("context_key")
        default = c.get("default")
        if default is None:
            default = c.get("value")   # 旧格式兼容
        if key and default is not None:
            variables.setdefault(key, default)

    # 每轮执行独立目录（修复 #7：uuid 替代 len(glob)+1——
    # 并发请求同时进来会算出相同 run_id 写同一目录）
    run_id = uuid4().hex[:12]
    run_dir = ARTIFACTS_DIR / f"run-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    latest_url = None
    failed = False

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.set_default_timeout(15000)   # 全局默认超时 15 秒

        # 核心循环：逐步骤执行（fail-fast 默认：失败后剩余步骤 skipped）
        for index, step in enumerate(case.steps, start=1):
            if failed and not continue_on_failure:
                results.append({
                    "step_index": index,
                    "action": step.action,
                    "status": "skipped",
                    "error": "前序步骤失败，已跳过（fail-fast）",
                    "duration_ms": 0,
                })
                continue
            evidence = _execute_step(page, step, variables, run_dir, index)
            results.append(evidence)
            if evidence["status"] == "failed" and not continue_on_failure:
                failed = True
            if evidence["url"]:
                latest_url = evidence["url"]

        browser.close()

    # 统计：全过才算通过（skipped 不算失败但也不算通过）
    passed = sum(1 for r in results if r["status"] == "passed")

    # ── 量化汇总（面试数据来源）─────────────────────────────
    total_ms = sum(r.get("duration_ms") or 0 for r in results)
    locator_stats: dict[str, int] = {}
    for r in results:
        strategy = r.get("resolved_by")
        if strategy:
            locator_stats[strategy] = locator_stats.get(strategy, 0) + 1

    return {
        "run_id": run_id,
        "case_name": case.name,
        "total_steps": len(results),
        "passed_steps": passed,
        "status": "passed" if passed == len(results) else "failed",
        "latest_url": latest_url,
        # 量化数据：总耗时 / 平均每步耗时 / 定位策略分布
        "total_duration_ms": total_ms,
        "avg_step_ms": total_ms // len(results) if results else 0,
        "locator_stats": locator_stats,
        "results": results,
    }
