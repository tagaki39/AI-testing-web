"""
══════════════════════════════════════════════════════════════════════
explore_flow.py — bounded exploration（有限探索）
══════════════════════════════════════════════════════════════════════

【这个文件在项目中的位置】
  生成链路的前置：目标驱动的多页面探索
    用户目标 →【这里：跟随流程探索多页面】→ Planner 生成 DSL → Runner 执行

【核心设计（面试重点）】
  1. bounded 预算：MAX_STEPS=8 步 / MAX_LLM_CALLS=8 次调用
  2. LLM = Planner，Playwright = Executor：LLM 只输出结构化动作
  3. element ref（本版核心）：页面解析成"带编号的元素表"，
     LLM 只能输出 target_ref 引用已有元素——【没有权限创造元素】
  4. exploration_complete（取代 goal_met）：判断标准是
     "是否已收集足够信息生成测试 DSL"，而不是"用户目标是否完成"
     （探索 ≠ 正式测试）
  5. 探索 ≠ 执行：探索发现路径，Runner 用确定性 DSL 重跑验证

【学习路径】
  explore()（主循环）→ _record_page（解析元素表）→ _decide（LLM 决策）
  → _act（ref → locator → 执行）
══════════════════════════════════════════════════════════════════════
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from time import perf_counter
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from resolver import (
    PRICE_RE, _strip_leading_decoration, choose_scope_text,
    LocatorNotFoundError, LocatorAmbiguousError,
)
from runner import _resolve_locator, _substitute

# ── 预算（bounded：探索必须有限）───────────────────────────────────────────────
# R3（评审"Execute, don't predict"）：探索是快速试错——短超时执行，
# 失败即记录；正式 Executor 才用长超时严格等待。
EXPLORE_ACTION_TIMEOUT_MS = 1500
MAX_STEPS = 12       # 最多执行 12 个动作（BFC 场景需要 7 个成功动作：
                     # 首页→Products→Polo→加购×2→Continue Shopping→View Cart，
                     # 8 步上限会让购物车页探索不到）
MAX_LLM_CALLS = 16   # 最多 16 次 LLM 决策调用（BFC 实测 8 步探索耗 8-10 次，
                     # 含决策自纠；12 步动作 + 自纠余量）
_MAX_SNAPSHOT_CHARS = 6000   # 裁剪后快照的最终兜底上限（重组后仍超限才截断）
_MAX_HISTORY = 3     # 决策上下文只看最近 3 步历史
_MAX_TEXT_ELEMENTS = 20      # 文本节点最多注入 20 个（防上下文膨胀）
_MAX_TEXT_LINES = 25         # 智能裁剪：text 行限量
_MAX_OTHER_LINES = 40        # 智能裁剪：非交互语义行（heading/banner 等容器）限量
_MAX_OBSERVATIONS = 12       # 总 observation 上限（防膨胀）
_MAX_OBSERVATIONS_PER_URL = 5   # 同 URL 最多 5 个状态（登录表单 fill 的
                                # value 变化即产生新状态，3 不够：空表单/
                                # 填 email/填 password/登录后回跳都需要槽位）

# 可交互元素角色（element ref 表只收录这些——LLM 只能操作这些）
_INTERACTIVE_ROLES = {
    "button", "link", "textbox", "checkbox", "radio", "combobox",
    "searchbox", "menuitem", "tab", "option",
}

# 解析 aria_snapshot YAML 行的正则
_ELEMENT_RE = re.compile(r'-\s+(\w+)\s+"([^"]*)"')          # - button "Login"
_TEXT_RE = re.compile(r'-\s+text:\s*(.+)')                  # - text: Products

# ── 探索安全保护（第 6 项：代码层二次拦截，不只靠 Prompt）──────────────
# press 允许的按键（枚举，防止 LLM 输出"按下回车"/"return" 等让执行器猜）
_PRESS_KEYS = {"Enter", "Escape", "Tab", "ArrowDown", "ArrowUp"}

# Data Grounding：fill 的 value 必须是 ${key} 占位符（key 白名单见
# state.input_keys）——模型不能输出真实值、不能创造变量名
_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

# 动作能力矩阵（评审 P0-1）：LLM 提议动作，确定性代码决定该动作
# 对该元素是否结构合法。text 元素（无 role）不在矩阵内 → 任何动作
# 都被拒（只做 evidence/context，不可作为动作目标——E2E 暴露模型
# 乱点商品名文本 "Blue Top" 导致 get_by_text 15s 超时）。
ACTION_CAPABILITIES = {
    "button": {"click", "press"},
    "link": {"click", "press"},
    "textbox": {"click", "fill", "press"},
    "searchbox": {"click", "fill", "press"},
    "combobox": {"click", "press"},
    "checkbox": {"click"},
    "radio": {"click"},
    "menuitem": {"click"},
    "tab": {"click"},
    "option": {"click"},
}


def _validate_action_target(action: str, element: dict | None) -> tuple[bool, str | None]:
    """动作-元素结构合法性。返回 (是否合法, 拒绝原因码)。"""
    if element is None or "role" not in element:
        return False, "NON_ACTIONABLE_REF"
    allowed = ACTION_CAPABILITIES.get(element.get("role"), set())
    if action not in allowed:
        return False, "ACTION_NOT_SUPPORTED_BY_ROLE"
    return True, None

# R3（评审"Execute, don't predict"）：主流程不再分类 actionability 失败
# ——ActionSpace 用观察期 actionable 标记过滤（Restrict），执行失败统一
# 进 failed_actions 黑名单。validate_actionability 的分类保留供观察期
# 评估与诊断使用（TARGET_OBSCURED / TARGET_DISABLED / ...）。

# 认证失败信号（精确整句匹配——死胡同要明确停止，不能原地循环）
_AUTH_FAILURE_MARKERS = (
    "your email or password is incorrect",
    "email or password is incorrect",
    "is not registered",
    "invalid email or password",
    "incorrect email or password",
    "email address does not exist",
    "账号或密码错误",
    "邮箱或密码不正确",
)


def _detect_auth_failure(snapshot: str) -> bool:
    """页面是否出现认证失败证据（登录被拒 → 目标无法继续 → 明确停止）。"""
    low = snapshot.lower()
    return any(m in low for m in _AUTH_FAILURE_MARKERS)


def _observe_until_stable(page, timeout_ms: int = 3000) -> str:
    """轮询页面 snapshot 直到状态稳定（等状态证据，不等固定时间）。

    评审 P0-2：点击后固定 300ms 等待不够——模态框/SPA 延迟渲染时
    观察还是旧状态 → 图里记 self-loop，新状态被归到下一次错误动作
    （BFC 场景：Add to cart 后 modal 未渲染，obs3→obs3，modal 状态
    被错误归因到下一次 text 点击的超时窗口）。
    轮询：snapshot hash 变化后连续两次相同 → 认为稳定。
    """
    deadline = perf_counter() + timeout_ms / 1000
    last_hash: str | None = None
    stable_count = 0
    latest = ""
    while perf_counter() < deadline:
        latest = _observe(page)
        h = hashlib.sha256(latest.encode()).hexdigest()[:10]
        if h == last_hash:
            stable_count += 1
            if stable_count >= 2 and last_hash is not None:
                return latest
        else:
            last_hash, stable_count = h, 0
        page.wait_for_timeout(150)
    return latest


def _is_repeated_no_progress(state: "ExploreState", action: str, ref: str) -> bool:
    """同一状态 + 同一动作 + 同一 ref 连续重复且上一次无进展 → True。

    评审收紧：Action 成功 ≠ Business transition 成功——点击 Login
    成功但页面没变（auth 被拒）时，模型可能原地重复点击。这是
    Transition/Progress Validation，不是 locator 问题：
      - 上一次 transition 是 self-loop（from == to，无状态变化）
      - 当前仍处于该 obs
      - 新决策是同一动作 + 同一 ref
    → 代码确定性拒绝，不依赖 LLM 记忆。
    """
    prev = state.transitions[-1] if state.transitions else None
    if prev is None or prev["from"] != prev["to"]:
        return False
    if state.current_obs is None or state.current_obs != prev["to"]:
        return False
    return prev["action"] == action and prev["target_ref"] == ref

# ── GQ：目标动作表（保守 allowlist，人为维护）──────────────────────────
# 用于两处：① 探索完成性校验（goal 要求操作时，0/1 步宣告完成无效）
# ② 生成期目标覆盖检查（ai_agent._check_goal_coverage 引用同表）。
# 只认明确动作动词；goal 不命中任何 pattern → 不检查（fail-open）。
GOAL_ACTION_PATTERNS: dict[str, "re.Pattern"] = {
    "add_to_cart": re.compile(r"(加入购物车|加购|add\s+to\s+cart)", re.IGNORECASE),
    "login": re.compile(r"(登录|login|sign\s*in)", re.IGNORECASE),
    "checkout": re.compile(r"(结算|下单|checkout)", re.IGNORECASE),
}


def goal_requires_actions(goal: str) -> bool:
    """goal 是否要求页面操作（命中动作表任一 pattern）。"""
    return any(p.search(goal) for p in GOAL_ACTION_PATTERNS.values())


def _validate_completion(state: "ExploreState") -> str | None:
    """探索完成宣告的完整性校验（GQ 决策 1，可单测）。

    真实 E2E 踩坑：1 步 fill 后宣告完成 → Planner 只能规划登录，
    用户目标（加购/验证）全部落空。校验规则：
      - goal 不要求操作（如"验证页面含文字"）→ 豁免（单页 0 步合法）
      - 已执行动作 ≥ 2 → 通过
      - 否则 → 返回拒绝原因（由主循环反馈进历史，预算内继续探索）
    """
    if not goal_requires_actions(state.goal):
        return None
    if state.step_count >= 2:
        return None
    return (f"探索不充分：仅执行 {state.step_count} 步就宣告完成"
            "（用户目标要求页面操作），请继续探索目标流程")

# 不可逆/危险操作关键词（点击前拦截：删除/支付/提交订单等）
_DESTRUCTIVE_PATTERNS = (
    "delete", "remove", "pay", "purchase", "submit order",
    "send", "publish", "sign out", "log out", "注销", "删除", "支付",
)


def _within_origin(url: str, entry_url: str) -> bool:
    """origin 限制：探索不得离开入口站点（跨域导航会触发回退）。

    修复：容忍 www/非 www 重定向——入口 saucedemo.com 会 302 到
    www.saucedemo.com，严格 netloc 相等把初始重定向误判为跨域，
    go_back 回到 about:blank 后探索彻底失效（真实 E2E 踩坑）。
    """
    try:
        host_a = urlparse(url).netloc.lower()
        host_b = urlparse(entry_url).netloc.lower()
        if host_a.startswith("www."):
            host_a = host_a[4:]
        if host_b.startswith("www."):
            host_b = host_b[4:]
        return host_a == host_b
    except Exception:
        return False


@dataclass
class ExploreState:
    """探索状态。"""
    goal: str                          # 用户测试目标
    entry_url: str                     # 入口 URL
    current_url: str = ""              # 当前页面
    current_obs: str | None = None     # R3：当前状态唯一事实源（ObservationStore）
                                       # 只由 _record_page 设置——禁止
                                       # observations[-1] 参与状态推导
    snapshot: str = ""                 # 当前页面快照（原始）
    elements: list[dict] = field(default_factory=list)      # 当前页元素表（ref）
    history: list[dict] = field(default_factory=list)       # 操作历史
    observations: list[dict] = field(default_factory=list)  # 页面状态观察（含 state_hash）
    transitions: list[dict] = field(default_factory=list)   # G2：状态转移边（obs3 --click e17--> obs4）
    input_keys: set = field(default_factory=set)   # Data Grounding：允许的 ${key} 白名单
    failed_actions: set = field(default_factory=set)  # R3：{(obs_id, action, ref)} 失败黑名单
    step_count: int = 0                # 已执行动作数
    llm_calls: int = 0                 # 已用 LLM 调用数
    done: bool = False                 # 探索是否完成
    # 计时（Speed v1：定位耗时构成，决定下一刀砍哪）
    # R3 细分：observe（aria 抓取+解析） / action_space（评估过滤） /
    # settle（稳定轮询） / llm / browser_action / fixed_wait
    timings: dict = field(default_factory=lambda: {
        "llm_ms": 0, "browser_action_ms": 0, "fixed_wait_ms": 0,
        "observation_ms": 0, "action_space_ms": 0, "settle_ms": 0,
    })


# ── element ref 解析（aria_snapshot → 带编号的元素表）───────────────────────────
# 核心：把"快照文本"变成"元素清单"，LLM 只能从中引用 ref。

def _parse_elements(snapshot: str) -> list[dict]:
    """解析 aria_snapshot → 可操作元素列表（带 ref 编号）。

    只收录两类：
      - 可交互元素（button/link/textbox...）：LLM 可以点击/填写
      - 文本节点（text: xxx）：可用于定位（span 标题等），限量注入

    例子：
      - button "Add to cart"  → {"ref": "e1", "role": "button", "name": "Add to cart"}
      - text: Products        → {"ref": "e2", "type": "text", "text": "Products"}
    """
    elements: list[dict] = []
    text_count = 0
    for line in snapshot.splitlines():
        line = line.strip()
        m = _ELEMENT_RE.match(line)
        if m:
            role, name = m.group(1), m.group(2)
            if role in _INTERACTIVE_ROLES and name.strip():
                elements.append({
                    "ref": f"e{len(elements) + 1}",
                    "role": role,
                    "name": name.strip(),
                })
            continue
        m = _TEXT_RE.match(line)
        if m:
            text = m.group(1).strip()
            if text and text_count < _MAX_TEXT_ELEMENTS:
                text_count += 1
                elements.append({
                    "ref": f"e{len(elements) + 1}",
                    "type": "text",
                    "text": text[:50],
                })
    return elements


def _elements_to_prompt(elements: list[dict], state: "ExploreState | None" = None) -> str:
    """元素表 → 决策上下文（紧凑格式）。

    E1（评审收紧）：blacklist 后从模型 action space 删除失败 ref——
    比"告诉 LLM 别选它"强：确定性约束缩小输入空间，而不是靠提示词
    让模型记住约束。被黑名单的 ref 直接从元素表消失。
    """
    lines = []
    for e in elements:
        if state is not None and state.current_obs:
            key = (state.current_obs, "click", e["ref"])
            if key in state.failed_actions:
                continue   # 已确定性失败的 ref 不出现在候选表
        if "role" in e:
            lines.append(f'{e["ref"]}: {e["role"]} "{e["name"]}"')
        else:
            lines.append(f'{e["ref"]}: text "{e["text"]}"')
    return "\n".join(lines) if lines else "(当前页面无可操作元素)"


# ── decide：LLM 决策 prompt（ref 引用 + exploration_complete）──────────────────

# 探索专用 system prompt（角色独立——探索是"决策"，不是 DSL 生成）
EXPLORE_SYSTEM_PROMPT = """你是 Web 页面探索决策 Agent。
只负责根据当前页面的元素表和用户目标，选择下一步探索动作。
只从 ref 表引用元素，禁止编造元素或动作。"""

DECIDE_PROMPT = """你是 Web 页面探索器。目标：收集足够信息来生成测试 DSL——不是完成测试，而是发现页面路径和真实元素。

当前状态：
- 用户目标: {goal}
- 当前页面状态: {current_obs}（元素表属于此状态）
- 当前 URL: {url}
- 可用 Runtime Input Keys: {input_keys}
- 当前页面可操作元素（ref 表）:
{elements}

最近操作历史:
{history}

你的任务：决定下一步动作。只输出 JSON：
{{
  "reason": "为什么这么做",
  "exploration_complete": false,
  "action": "click | fill | press | back | finish",
  "target_ref": "obs1:e1",
  "value": "fill 的 value 必须是 ${{input_key}} 占位符，input_key 严格取自『可用 Runtime Input Keys』；禁止创造不存在的变量名，禁止输出任何真实值"
}}

规则：
1. target_ref 必须来自【当前】元素表（表头 {current_obs} 标明了当前状态），不得编造
   - ref 带 obs 前缀（如 obs1:e1）——照抄表里完整格式，不要省略 obs 前缀
   - 页面状态变化后元素表会换新——历史中的旧 ref 属于旧状态，当前表中不存在即已失效，
     禁止沿用旧 ref；先用当前表的新 ref 重新执行动作
2. action 只能从上面 5 种选（wait 已移除，点击后执行器自动等待页面加载）
3. press 的 value 只能是: Enter, Escape, Tab, ArrowDown, ArrowUp
4. 每一步只做一个动作
5. 当已收集到生成测试 DSL 所需的全部页面路径和元素时，exploration_complete=true 并输出 finish
6. 探索阶段禁止执行删除、支付、提交订单、注销等不可逆操作（执行器会二次拦截）
7. 不得离开入口站点（跨域导航会被回退）"""


# ── observe / record ───────────────────────────────────────────────────────────

def _observe(page) -> str:
    """抓取当前页面 ARIA 快照并智能裁剪（第 7 项：不再粗暴截断）。

    修复：简单 [:4000] 截断会丢后半段元素（重要按钮在截断外时
    Preflight 误判"不存在"）。结构化裁剪按优先级保留：
      1. 可交互元素行（button/link/textbox...）——全部保留，永不丢
      2. heading 等语义行——限量
      3. text 行——限量
      4. 其他（容器/装饰）——限量
    层级缩进保留（只过滤不重排）；重组后仍超限才最终截断。
    """
    try:
        snapshot = page.locator("body").aria_snapshot() or ""
    except Exception:
        return ""
    return _smart_truncate(snapshot)


def _smart_truncate(snapshot: str) -> str:
    """结构化裁剪：按元素优先级过滤行（见 _observe 说明）。"""
    lines = snapshot.splitlines()
    kept: list[str] = []
    text_count = 0
    other_count = 0
    for line in lines:
        stripped = line.strip()
        m = _ELEMENT_RE.match(stripped)
        if m:
            kept.append(line)          # 可交互元素：全保留
            continue
        m = _TEXT_RE.match(stripped)
        if m:
            if text_count < _MAX_TEXT_LINES:
                kept.append(line)
                text_count += 1
            continue
        if stripped.startswith("-"):
            if other_count < _MAX_OTHER_LINES:   # 容器/heading 等语义行：限量
                kept.append(line)
                other_count += 1
            continue
        kept.append(line)              # 缩进/空行
    result = "\n".join(kept)
    return result[:_MAX_SNAPSHOT_CHARS] if len(result) > _MAX_SNAPSHOT_CHARS else result


def _record_page(state: ExploreState, page, snapshot: str | None = None) -> None:
    """记录当前页面状态为 observation（升级：URL + state_hash 去重）。

    Observation = URL + 页面状态 + ARIA 证据——
    同 URL 不同状态（如 Add to cart 点击后按钮变 Remove）也保存，
    解决 SPA 状态丢失（此前只按 URL 去重）。

    snapshot 可外部传入（P0-2：_observe_until_stable 的稳定快照），
    避免重复抓取 aria_snapshot。
    """
    url = page.url
    state.current_url = url
    if snapshot is None:
        snapshot = _observe(page)
    elements = _parse_elements(snapshot)   # ← ref 表（页面级 e1/e2，局部变量）

    # 状态哈希：snapshot 变化 = 页面状态变化（即使 URL 相同）
    state_hash = hashlib.sha256(snapshot.encode()).hexdigest()[:10]

    # 当前 snapshot 命中已有 observation → 恢复该状态的 state-scoped
    # 元素表。修复：此前已存在路径会先污染 state.elements（无 obs 前缀
    # 新表）——决策校验拿 obs2:e10 对表校验全被拒（8 连拒）。
    # 注意必须是"命中哪个 obs 就恢复哪个"——A→B→A 场景恢复 obs1
    # 的元素表，而不是上一次的 state.elements（可能是 obs2）。
    matched = next((
        o for o in state.observations
        if o["url"] == url and o.get("state_hash") == state_hash
    ), None)
    if matched is not None:
        state.elements = matched["elements"]
        state.current_obs = matched["id"]   # R3：current_obs 唯一事实源
        return matched["id"]   # E1：transition 的 to 用实际所在状态

    same_url_count = sum(1 for o in state.observations if o["url"] == url)
    if (len(state.observations) >= _MAX_OBSERVATIONS
            or same_url_count >= _MAX_OBSERVATIONS_PER_URL):
        # 观察预算满：不给当前状态一个"裸元素表"（无 state owner）。
        # 停止探索（主循环检测 done），比带着无主元素继续决策安全。
        state.history.append({
            "url": url,
            "action": "observation_cap",
            "error": "观察预算已满（total/per-url 上限），停止探索",
        })
        state.done = True
        return None

    state.snapshot = snapshot
    state.elements = elements
    obs_id = f"obs{len(state.observations) + 1}"
    state.current_obs = obs_id   # R3：current_obs 唯一事实源
    # G1：state-scoped ref——元素 ref 从页面级 "e1" 升级为状态级 "obs3:e1"。
    # Planner 引用 obs3:e17 时，系统知道 belongs_to=obs3（state identity）。
    for element in state.elements:
        element["ref"] = f"{obs_id}:{element['ref']}"

    # I1：同名重复元素采集容器文本锚点（只处理重复，非重复零开销）——
    # 先 enrich 再持久化，元素表与 observation 共享同一对象
    _attach_scope_context(state, page)

    # R3：观察期可操作性评估（Page Explorer 输出 actionable 标记——
    # 参考项目 page_explorer 的 verified 标记模式）。模态框打开时
    # 被遮挡的 Add to cart 标记 actionable=False → ActionSpace 直接
    # 过滤，模型看不到它（Restrict, don't repair）。
    # 性能边界（评审）：这层是 cheap/synchronous/best-effort——
    # 只做 elementFromPoint 毫秒级判断，绝不 trial（全量 trial 会
    # 被遮挡元素拖到秒级）。允许 false positive（执行失败再删 candidate）。
    t_as = perf_counter()
    for e in state.elements:
        if "role" in e:
            try:
                _, _, loc = _locator_for_element(page, e)
                e["actionable"], _ = validate_actionability(page, loc, "click")
            except Exception:
                e["actionable"] = False
    state.timings["action_space_ms"] += int((perf_counter() - t_as) * 1000)

    state.observations.append({
        "id": obs_id,
        "url": url,
        "title": _safe_title(page),
        "state_hash": state_hash,
        "snapshot": state.snapshot,
        "elements": state.elements,   # G1：observations 携带 state-scoped refs
    })

    # invariant：state.elements 的每个 ref 都必须有明确 state identity
    #（obsN:eM 格式）。裸 e1/e2 意味着状态所有权丢失——决策校验将失效。
    assert all(":" in e["ref"] for e in state.elements), (
        f"_record_page 后存在无 state 前缀的 ref: {state.elements[:5]}")
    return obs_id


def _safe_title(page) -> str:
    try:
        return page.title() or ""
    except Exception:
        return ""


def _pick_anchor_text(lines: list[str], node_text: str) -> str | None:
    """从容器文本行选锚点（跳过价格/短行/元素自身文本）。

    I1：与 Preflight 的 choose_scope_text 同族启发式，但需排除节点
    自身文本（Preflight 侧调用方已排除，这里统一处理）。
    """
    for line in lines:
        line = line.strip()
        if not line or len(line) < 2:
            continue
        if line == node_text:
            continue
        if PRICE_RE.fullmatch(line):
            continue
        return line[:60]
    return None


def _attach_scope_context(state: ExploreState, page) -> None:
    """I1：为 observation 内同名重复的元素采集容器文本锚点（scope_has_text）。

    只处理重复的元素（role+name 或 text 键）——非重复零开销（决策 3
    性能上界）。锚点来自 DOM 祖先链（li/article/@data-testid/
    @data-product-id/@data-item-id）——比 a11y 树 parent 更贴近真实
    业务容器结构。文本节点（无 role 的 <a> 等）同样处理：图标前缀先
    剥掉再匹配（PUA 在 CSS 伪元素里，DOM 文本不含）。
    采集失败静默（无锚点 → Compiler 不附加 scope → 运行时诚实拒绝）。
    """
    name_counts: dict[tuple[str, str], int] = {}
    text_counts: dict[str, int] = {}
    for e in state.elements:
        if "role" in e and e.get("name"):
            key = (e["role"], e["name"])
            name_counts[key] = name_counts.get(key, 0) + 1
        elif e.get("text"):
            text_counts[e["text"]] = text_counts.get(e["text"], 0) + 1
    duplicates = {k for k, c in name_counts.items() if c > 1}
    dup_texts = {t for t, c in text_counts.items() if c > 1}
    if not duplicates and not dup_texts:
        return

    seen: dict[tuple[str, str], int] = {}
    text_seen: dict[str, int] = {}
    for e in state.elements:
        anchor = None
        try:
            if "role" in e and e.get("name"):
                key = (e["role"], e["name"])
                if key not in duplicates or e.get("scope_has_text"):
                    continue
                i = seen.get(key, 0)
                seen[key] = i + 1
                node = page.get_by_role(e["role"], name=e["name"], exact=True).nth(i)
            elif e.get("text") and e["text"] in dup_texts and not e.get("scope_has_text"):
                i = text_seen.get(e["text"], 0)
                text_seen[e["text"]] = i + 1
                # 图标前缀在 CSS 伪元素中 → 剥掉装饰后按 DOM 文本精确匹配
                node = page.get_by_text(
                    _strip_leading_decoration(e["text"]), exact=True,
                ).nth(i)
            else:
                continue
            container = node.locator(
                "xpath=ancestor::*[self::li or self::article or @data-testid"
                " or @data-product-id or @data-item-id][1]"
            )
            container_count = container.count()
            if container_count == 0:
                container = node.locator("xpath=../..")
                container_count = container.count()
            raw = container.inner_text().strip() if container_count > 0 else ""
            node_text = node.inner_text().strip()
            anchor = _pick_anchor_text(
                [ln.strip() for ln in raw.splitlines() if ln.strip()], node_text,
            )
        except Exception:
            continue
        if anchor:
            e["scope_has_text"] = anchor


# ── decide：LLM 决策（ref 强校验 + exploration_complete）──────────────────────

def _build_action_space(state: ExploreState) -> list[dict]:
    """R3：ActionSpace——当前状态下 LLM 真正能选择的动作候选。

    评审核心："模型没权限选择错误动作，比告诉模型不要选错误动作更简单。"
    对元素表逐个做执行前可操作性检查（trial，短超时）——被模态框遮挡
    的 Add to cart 直接从候选消失；模型只能在 [View Cart, Continue
    Shopping, ...] 里选。这取代 modal hint / 复杂拒绝反馈等补丁。

    过滤规则：
      - 黑名单 ref（确定性失败过）剔除
      - 可操作性检查失败（不可见/不可用/被遮挡）剔除
    返回可操作元素列表（供 prompt 与决策校验共用）。
    """
    if not state.current_obs:
        return list(state.elements)
    obs_id = state.current_obs
    usable: list[dict] = []
    for e in state.elements:
        if (obs_id, "click", e["ref"]) in state.failed_actions:
            continue   # 确定性失败过
        if "role" not in e:
            usable.append(e)   # 文本元素保留（wait_for/定位参考用）
            continue
        # 观察期已评估的 actionable 标记（R3：不预测，用 Page Explorer 输出）；
        # 无标记的元素保守剔除（防御：观察期评估失败 = 不可操作）
        if e.get("actionable"):
            usable.append(e)
    return usable


def _decide(state: ExploreState, llm_call, elements: list[dict] | None = None) -> tuple[dict | None, str | None]:
    """调 LLM 决定下一步，返回 (决策, 校验错误)。

    ref 不在元素表 → 决策无效——这是"LLM 没有权限创造元素"的代码保证：
      prompt 只给元素表，输出必须引用 ref，代码校验 ref 存在。
    elements 可传入 ActionSpace 过滤后的候选（R3：LLM 只能选可操作的）。
    错误信息返回给调用方（预算内反馈进历史让 LLM 自纠，不直接夭折）。
    """
    elements = elements if elements is not None else state.elements
    history_text = "\n".join(
        f"- {h.get('action')} {h.get('target_ref')} {h.get('value') or ''} @ {h.get('url')}"
        + (f" → 失败: {h['error'][:80]}" if h.get("error") else "")
        for h in state.history[-_MAX_HISTORY:]
    ) or "- (无)"

    prompt = DECIDE_PROMPT.format(
        goal=state.goal,
        current_obs=state.current_obs or "?",
        url=state.current_url,
        input_keys=", ".join(sorted(state.input_keys)) if state.input_keys else "(无)",
        elements=_elements_to_prompt(elements, state),
        history=history_text,
    )
    try:
        t0 = perf_counter()
        text = llm_call(prompt, system_prompt=EXPLORE_SYSTEM_PROMPT)
        state.timings["llm_ms"] += int((perf_counter() - t0) * 1000)
        state.llm_calls += 1   # 每次决策尝试都计（预算护栏：坏决策也消耗预算）
        decision = json.loads(re.search(r"\{.*\}", text, re.DOTALL).group(0))

        # 强校验：action 白名单 + target_ref 必须存在于当前元素表
        action = decision.get("action")
        if action not in {"click", "fill", "press", "back", "finish"}:
            return None, f"非法 action {action!r}（白名单: click/fill/press/back/finish）"
        if action == "press":
            # press 按键枚举（第 6 项：不允许 LLM 自由输出按键）
            if (decision.get("value") or "") not in _PRESS_KEYS:
                return None, f"press 的 value 必须是: {'/'.join(sorted(_PRESS_KEYS))}"
        if action != "finish":
            ref = decision.get("target_ref")
            if ref is None or not any(e["ref"] == ref for e in elements):
                return None, (f"target_ref {ref!r} 不在当前元素表——"
                              "ref 带 obs 前缀，照抄表内完整格式（如 obs1:e1）")
            # no-progress guard：同一状态同一动作同一 ref 重复且上次无进展
            if _is_repeated_no_progress(state, action, ref):
                return None, ("NO_PROGRESS: 同一元素上的同一动作上一次执行"
                              "未产生状态变化（self-loop）——禁止原地重复，"
                              "请选择其他动作或输出 finish 宣告失败")
            # 动作-元素结构合法性（评审 P0-1：text 元素不可点击等）。
            # 确定性拒绝 → 反馈历史让模型自纠（llm+1，step+0）。
            el = next((e for e in elements if e["ref"] == ref), None)
            ok, reason = _validate_action_target(action, el)
            if not ok:
                desc = el.get("text", "")[:30] if el else ""
                label = f"text {desc!r}" if el and "role" not in el \
                    else (el.get("role") if el else "?")
                return None, (f"{reason}: 目标 {ref!r}（{label}）不支持动作"
                              f" {action!r}——text 元素仅作证据不可点击，"
                              "请选当前表中的可交互控件 ref")
            # R3（评审瘦身）：failed-actions blacklist 不再需要显式拒绝——
            # ActionSpace 已把黑名单 ref 从候选表删除（模型看不到它），
            # 若模型仍输出（防御兜底）则 ref 校验自然拒绝（不在表内）。
        if action == "fill":
            # Data Grounding 强校验（评审收紧：不只靠 prompt）：
            # fill 的 value 必须是 ${key} 占位符，且 key 必须在
            # Runtime Input Keys 白名单内——模型不能创造变量名，
            # 更不能直接输出真实值（如 test123@example.com）。
            value = decision.get("value") or ""
            m = _PLACEHOLDER_RE.fullmatch(value.strip())
            if not m:
                return None, ("fill 的 value 必须是 ${key} 占位符形式"
                              "（禁止输出真实值）")
            key = m.group(1)
            if key not in state.input_keys:
                return None, (f"未知 runtime input key {key!r}——"
                              f"允许的 keys: {', '.join(sorted(state.input_keys)) or '(无)'}")
        return decision, None
    except Exception as exc:
        return None, f"决策解析失败: {str(exc)[:120]}"


# ── act：ref → locator → 执行（LLM=Planner，这里=Executor）────────────────────

def _locator_for_element(page, element: dict) -> tuple[dict, dict | None, object]:
    """element → (target, scope, locator)。

    E1：_act 与 validate_actionability 共用的定位构建——
    I1 同名重复元素带 scope_has_text 锚点消歧。
    """
    target = {"role": element["role"], "name": element["name"]} if "role" in element \
        else {"text": element["text"]}
    scope = {"has_text": element["scope_has_text"]} if element.get("scope_has_text") else None
    _, locator = _resolve_locator(page, target, scope=scope)
    return target, scope, locator


def validate_actionability(page, locator, action: str) -> tuple[bool, str]:
    """R3：可操作性评估（观察期 Page Explorer 输出 actionable 标记用）。

    性能关键：观察期对全部可交互元素评估（60-70 个/页）——
    不能用 click(trial=True)（被遮挡元素要等满 3s 超时，模态框场景
    10+ 个被挡元素 = 30s/观察）。改 elementFromPoint 同步检测（毫秒级）：
      - 可见性 / 可用性（is_visible / is_enabled）
      - 遮挡：命中点最上层元素必须是 target 或其内部，或 target
        是它的祖先（el.contains(target)——修复：命中点落在父容器
        空白区时 el 是祖先，原判定误报遮挡）

    返回 (是否可操作, 拒绝原因码)。bounded：全部同步调用。
    """
    try:
        if not locator.is_visible():
            return False, "TARGET_NOT_VISIBLE"
        if not locator.is_enabled():
            return False, "TARGET_DISABLED"
        if action == "click":
            box = locator.bounding_box()
            if not box:
                return False, "TARGET_NOT_VISIBLE"
            handle = locator.element_handle()
            obscured = page.evaluate(
                """([x, y, target]) => {
                    const el = document.elementFromPoint(x, y);
                    if (!el) return true;
                    return !(el === target || target.contains(el) || el.contains(target));
                }""",
                [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, handle],
            )
            if obscured:
                return False, "TARGET_OBSCURED"
        return True, ""
    except Exception:
        return False, "ACTIONABILITY_CHECK_ERROR"

def _act(page, decision: dict, elements: list[dict], runtime_inputs: dict) -> str:
    """执行 LLM 决策的动作，返回动作名。定位失败/执行失败抛异常。

    fill 的值支持 ${var} 占位：LLM 上下文里只有占位符，
    真实值由 runtime_inputs 在本地注入（敏感信息不进 LLM）。
    """
    action = decision.get("action")
    value = decision.get("value") or ""

    if action == "back":
        page.go_back()
        return action

    # ref → 元素信息 → 语义定位器
    ref = decision.get("target_ref")
    element = next((e for e in elements if e["ref"] == ref), None)
    if element is None:
        raise ValueError(f"未知 target_ref: {ref}")
    target, scope, locator = _locator_for_element(page, element)
    # I1：身份证据前移——探索时 count==1 命中即标 verified
    #（证据不是豁免，运行时仍过三分法 + 评分；供 Compiler/metrics 使用）
    for e in elements:
        if e["ref"] == ref:
            e["verified"] = True
            break

    if action == "click":
        # 危险操作二次拦截（第 6 项：代码层，不只靠 Prompt）——
        # 目标名称含删除/支付/提交订单等关键词 → 拒绝执行
        name = element.get("name", "") if element else ""
        if any(p in name.lower() for p in _DESTRUCTIVE_PATTERNS):
            raise ValueError(f"危险操作被拦截: {name!r}")
        locator.click(timeout=EXPLORE_ACTION_TIMEOUT_MS)   # 探索短超时快速试错
    elif action == "fill":
        locator.fill(_substitute(value, runtime_inputs) or "")
    elif action == "press":
        locator.press(_substitute(value, runtime_inputs) or "Enter",
                      timeout=EXPLORE_ACTION_TIMEOUT_MS)
    else:
        raise ValueError(f"不支持的探索动作: {action}")
    return action


# ── 主入口 ──────────────────────────────────────────────────────────────────────

def explore(goal: str, entry_url: str, llm_call, runtime_inputs: dict | None = None) -> dict:
    """bounded exploration 主循环。

    参数:
      goal:          用户测试目标（已脱敏，LLM 看到的只有 ${var} 占位符）
      entry_url:     入口 URL
      llm_call:      LLM 调用函数（由 ai_agent 注入，避免循环依赖）
                     签名: llm_call(prompt, system_prompt) -> str
      runtime_inputs: 本地运行时值（账号密码等，fill 时注入，不进 LLM）

    返回:
      {
        "pages": [{url, title, snapshot}],   # 多页面快照（喂给 Planner）
        "history": [{url, action, target_ref, value}],  # 探索路径
        "steps_used": int, "llm_calls": int, "done": bool,
      }

    预算耗尽 / 决策失败 / exploration_complete → 停止探索。
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(15000)

        state = ExploreState(
            goal=goal,
            entry_url=entry_url,
            # Data Grounding：模型只能引用这些 ${key}（keys 不含真实值）
            input_keys=set(runtime_inputs) if runtime_inputs else set(),
        )
        t0 = perf_counter()
        page.goto(entry_url, wait_until="domcontentloaded")
        state.timings["browser_action_ms"] += int((perf_counter() - t0) * 1000)
        # 初始渲染等待：domcontentloaded 后 SPA 内容可能异步出现，
        # 短等一次（之前固定 1200ms——改为按需，先保留 500ms 底线）
        wait_start = perf_counter()
        page.wait_for_timeout(500)
        state.timings["fixed_wait_ms"] += int((perf_counter() - wait_start) * 1000)
        _record_page(state, page)   # 初始 observe：入口页

        while (not state.done
               and state.step_count < MAX_STEPS
               and state.llm_calls < MAX_LLM_CALLS):
            # R3：ActionSpace——LLM 只能从"当前可操作"的候选中选
            #（模态框遮挡的 Add to cart 不进入候选，模型没权限选错）
            action_space = _build_action_space(state)
            decision, decision_error = _decide(state, llm_call, elements=action_space)
            if decision is None:
                # 决策被校验拒绝：把错误反馈进历史，预算内让 LLM 自纠。
                # 修复：单次坏决策直接夭折整个探索——真实 E2E 中 fill 之后
                # 模型沿用历史里的旧 ref 被拒，探索止步登录页。
                # 预算护栏在 _decide 内（每次尝试都计 llm_calls），不会死循环。
                state.history.append({
                    "url": state.current_url,
                    "action": "decision_rejected",
                    "error": decision_error,
                })
                # R3（评审瘦身）：拒绝不设单独 stalled 机制——统一
                # SafetyController 语义：llm_calls 预算耗尽即停（bounded）。
                # 候选表已过滤黑名单 ref，模型有足够空间转向合法动作。
                continue

            if decision.get("exploration_complete") or decision.get("action") == "finish":
                # GQ：完成宣告完整性校验——动作过少的宣告无效，
                # 拒绝原因反馈进历史、预算内继续（复用 decision_rejected
                # 自纠机制；goal 无操作要求时豁免，见 _validate_completion）
                completion_error = _validate_completion(state)
                if completion_error is not None:
                    state.history.append({
                        "url": state.current_url,
                        "action": "decision_rejected",
                        "error": completion_error,
                    })
                    continue
                state.done = True
                break

            # 执行动作（失败记录进历史，继续下一轮）
            ref = decision.get("target_ref")
            element = next((e for e in state.elements if e["ref"] == ref), None) if ref else None
            target = {"role": element["role"], "name": element["name"]} if element and "role" in element \
                else ({"text": element["text"]} if element else None)
            # G2：动作前状态（transition 的 from）
            from_obs = state.current_obs

            # R3（评审 "Execute, don't predict"）：不做执行前可操作性
            # 预测——ActionSpace 已在决策候选层用观察期 actionable 标记
            # 过滤（Restrict）。这里直接短超时执行，失败即记录黑名单。
            t0 = perf_counter()
            try:
                action_done = _act(page, decision, state.elements, runtime_inputs or {})
                state.history.append({
                    "url": state.current_url,
                    "action": decision["action"],
                    "target_ref": ref,
                    "target": target,          # 解析后的 target（Planner 可读）
                    "value": decision.get("value"),
                })
            except Exception as exc:
                action_done = None
                # E1：执行失败也进黑名单（同状态同 ref 不再重试撞墙）
                if from_obs:
                    state.failed_actions.add((from_obs, decision.get("action"), ref))
                state.history.append({
                    "url": state.current_url,
                    "action": decision.get("action"),
                    "target_ref": ref,
                    "target": target,
                    "error": str(exc)[:100],
                })
            state.timings["browser_action_ms"] += int((perf_counter() - t0) * 1000)

            state.step_count += 1
            # 按动作类型决定等待（修复：固定 800ms 浪费——fill 基本无需等待，
            # 点击/导航需要渲染时间）：
            wait_start = perf_counter()
            if action_done in {"click", "press", "back"}:
                page.wait_for_timeout(300)
            # fill → 不等待
            state.timings["fixed_wait_ms"] += int((perf_counter() - wait_start) * 1000)

            t0 = perf_counter()
            if action_done in {"click", "press", "back"}:
                # P0-2：等状态证据而非固定时间——点击后模态框/SPA 延迟
                # 渲染时，固定 300ms 观察会错位（旧状态 → self-loop 归因错）
                snapshot = _observe_until_stable(page)
                state.timings["settle_ms"] += int((perf_counter() - t0) * 1000)
            else:
                snapshot = _observe(page)
                state.timings["observation_ms"] += int((perf_counter() - t0) * 1000)

            if action_done != "fill":
                # observe 新页面状态。fill 不创建 persistent observation：
                # 表单填充只改 input value（ephemeral），不产生新 graph node
                #（评审收紧：fill 一次 = 一个 obs 会让登录页吃掉 4 个槽位）。
                # 返回动作后实际所处 observation id（matched 或新建）——
                # E1：transition 的 to 必须用真实状态，不能是 observations[-1]
                #（Continue Shopping 关闭模态框回到旧状态 → to=旧 obs）。
                to_obs = _record_page(state, page, snapshot=snapshot)
            else:
                state.current_url = page.url   # fill 不改页面结构，只同步 URL
                to_obs = state.current_obs

            # 认证失败 evidence（死胡同要明确停止，不能原地循环）：
            # 页面出现 "email or password is incorrect" 等信号 → 目标无法
            # 继续 → 记录原因并停止（done），比重复点击 Login 诚实。
            if _detect_auth_failure(state.snapshot):
                state.history.append({
                    "url": state.current_url,
                    "action": "auth_rejected",
                    "error": "页面出现认证失败提示，测试目标无法继续——停止探索",
                })
                state.done = True
                break

            # G2：记录状态转移边（obs3 --click e17--> obs4）。
            # 动作成功（action_done 非 None）才记录；to = 动作后实际所处
            # observation（_record_page 返回值——matched 或新建）。
            # E1：不能取 observations[-1]——Continue Shopping 关闭模态框
            # 回到旧状态时，[-1] 还是模态框 obs，会记成错误 self-loop。
            if action_done is not None and from_obs and to_obs:
                state.transitions.append({
                    "from": from_obs,
                    "action": decision["action"],
                    "target_ref": ref,
                    "to": to_obs,
                })

            # origin 守卫（第 6 项）：点击跨域链接（文档/GitHub/外部认证）
            # → 记录并回退，探索不离开被测站点
            if not _within_origin(state.current_url, state.entry_url):
                state.history.append({
                    "url": state.current_url,
                    "action": "origin_guard",
                    "error": "跨域导航被拦截，已回退",
                })
                page.go_back()
                page.wait_for_timeout(300)
                _record_page(state, page)

        browser.close()

    state.timings["explore_total_ms"] = (
        state.timings["llm_ms"] + state.timings["browser_action_ms"]
        + state.timings["fixed_wait_ms"] + state.timings["observation_ms"]
    )

    return {
        "observations": state.observations,
        "transitions": state.transitions,   # G2：状态转移边
        "history": state.history,
        "steps_used": state.step_count,
        "llm_calls": state.llm_calls,
        "done": state.done,
        "timings": state.timings,
    }
