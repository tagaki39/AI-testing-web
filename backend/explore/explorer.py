"""
explorer.py — 探索主循环（R3 拆分自 explore_flow）
  bounded loop：observe → ActionSpace → choose → execute → transition。
  Execute, don't predict：短超时执行，失败进 failed_actions。
"""
from time import perf_counter
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
import json
import re

from execution.action_executor import execute_action
from .observation import (
    ExploreState, _observe, _observe_until_stable, _record_page,
)
from .action_space import _build_action_space, _locator_for_element


import json
import re
from time import perf_counter

from .action_space import _validate_action_target
from .observation import ExploreState   # 类型注解

_MAX_HISTORY = 3     # 决策上下文只看最近 3 步历史
# ── 探索安全保护（第 6 项：代码层二次拦截，不只靠 Prompt）──────────────
# press 允许的按键（枚举，防止 LLM 输出"按下回车"/"return" 等让执行器猜）
_PRESS_KEYS = {"Enter", "Escape", "Tab", "ArrowDown", "ArrowUp"}

# Data Grounding：fill 的 value 必须是 ${key} 占位符（key 白名单见
# state.input_keys）——模型不能输出真实值、不能创造变量名
_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
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


def _detect_error_page(snapshot: str) -> bool:
    """页面是否为明确错误页（404/500 → 目标无法继续 → honest stop）。

    R5（xywhaigc 案例）：登录后跳 404，探索不应继续点"返回首页"
    把无关状态纳入路径。保守匹配防误报：404/500 需要数字 + 错误
    关键词组合，或明确中文短语。
    """
    low = snapshot.lower()
    if "404" in low and any(m in low for m in ("not found", "找不到", "不存在")):
        return True
    if "500" in low and any(m in low for m in ("internal", "服务器错误", "服务异常")):
        return True
    return any(m in low for m in ("页面不存在", "找不到网页", "404错误"))

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

# 动作 label → 探索 history 中必须出现的关键词（target name，casefold 匹配）
# 真实网站验证（xywhaigc 登录页按钮是中文"登录"）：必须覆盖中英文。
_ACTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "add_to_cart": ("add to cart", "加入购物车", "加入購物車"),
    "login": ("login", "sign in", "登录", "登陆", "登入"),
    "checkout": ("checkout", "结算", "结账", "去结算"),
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
    if state.step_count < 2:
        return (f"探索不充分：仅执行 {state.step_count} 步就宣告完成"
                "（用户目标要求页面操作），请继续探索目标流程")
    # R3（BFC 实测）：目标要求的动作类型必须已探索过——模型 3 步
    # （Products/Polo）就宣告完成，加购/购物车流程全没探索，Planner
    # 无从生成完整 DSL。goal 命中动作表 → 必须存在对应 click 的证据。
    for label, pattern in GOAL_ACTION_PATTERNS.items():
        if not pattern.search(state.goal):
            continue
        keywords = _ACTION_KEYWORDS[label]
        covered = any(
            h.get("action") == "click" and h.get("target")
            # 两边去空白后匹配（真实网站验证：登录按钮 name 是 "登 录"、
            # "Add to cart" 关键词自身含空格——只去一侧会漏配）
            and any(k.replace(" ", "")
                    in str(h.get("target", {})).lower().replace(" ", "")
                    for k in keywords)
            for h in state.history
        )
        if not covered:
            return (f"探索不充分：目标要求 {label} 动作，但探索未执行过"
                    f"（history 无 {keywords[0]} 的 click）——请继续探索该流程")
    return None

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
            # 消歧信息（A4.2/A4.1 观察期采集）：商品名/容器锚点优先
            #（LLM 靠它理解"前两个商品"），无则用 identity（data-product-id）。
            # 没有它们，6 个同名 "Add to cart" 无法区分——LLM 只会选第一个。
            line = f'{e["ref"]}: {e["role"]} "{e["name"]}"'
            anchor = e.get("scope_has_text")
            ident = e.get("identity") or {}
            if anchor:
                line += f' [{anchor}]'
            elif ident.get("value"):
                line += f' [id={ident["value"]}]'
            lines.append(line)
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
7. 不得离开入口站点（跨域导航会被回退）
8. 数量完成度（目标里的数量词是硬要求）：
   - 目标说"前两个商品/2 个/每个"等数量时，必须逐一完成对应的成功动作
     （如"前两个商品加入购物车"= 需要 2 次不同商品的加购点击，且每次
     都触发加购成功），全部完成前不得 exploration_complete
   - 出现模态框/对话框时（可操作元素表会骤减）：目标未完成时优先选择
     能继续完成目标的动作（如"继续购物/关闭"类）回到业务页面，
     不要急于进入收尾页面（如购物车）——只有最后一个商品加购后的
     弹窗才应该选择进入购物车验证"""


# ── Policy：目标约束（R6——从 StateGraph 派生，不存第二状态源）───────────────
# 职责分层：
#   ActionSpace = 结构合法性（当前页面物理上能做什么）
#   Policy      = 目标约束（根据用户目标，现在应该允许选什么）
#   LLM         = 在允许集合里做语义选择
# "能 derive 的状态不要 store"——完成度直接从成功 transitions 推导，
# 不引入 required_count/completed_adds 等第二事实源；未来"添加三个角色"
# "上传四张图片"只是同一个 Policy 规则，不需要新的 guard。

_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_QTY_PATTERNS = (
    re.compile(r"前([一二两三四五六七八九十\d]+)个"),
    re.compile(r"([\d一二两三四五六七八九十]+)\s*个(?:商品|产品|物品)"),
    re.compile(r"first\s+(\d+)\s+(?:products|items)", re.I),
    re.compile(r"(\d+)\s+(?:products|items)", re.I),
)

# 终态动作模式（"进入收尾页面"的信号，通用非站点特判）：
# View Cart / Checkout / Submit / 结账 / 支付等；"Add to cart" 不误伤
#（lookbehind 排除 "to cart" 前导词）。
_TERMINAL_ACTION_RE = re.compile(
    r"(?<![a-z] )((view\s*)?cart|checkout|submit|place\s*order|pay)\b"
    r"|下单|结账|支付|结算",
    re.IGNORECASE,
)


def _extract_required_count(goal: str) -> int | None:
    """目标数量词 → int（"前两个商品" → 2）；无数量词 → None。"""
    for pat in _QTY_PATTERNS:
        m = pat.search(goal or "")
        if m:
            raw = m.group(1).strip()
            if raw.isdigit():
                return int(raw)
            if raw in _CN_NUM:
                return _CN_NUM[raw]
    return None


def _derive_completed_entities(state: "ExploreState") -> set[str]:
    """从成功转移边派生已完成业务实体（identity 可证才计）。

    成功边 = from≠to（产生真实状态变化的动作；失败/self-loop 排除）。
    target_ref → 元素表查 identity（data-product-id 等）——不同商品弹
    相同 modal（无内容区分），只有 identity 能区分实体。
    """
    ref_map = {
        e["ref"]: e
        for o in state.observations
        for e in o.get("elements", [])
    }
    completed: set[str] = set()
    for t in state.transitions:
        if not (t.get("from") and t.get("to") and t.get("from") != t.get("to")):
            continue
        ident = (ref_map.get(t.get("target_ref") or "") or {}).get("identity") or {}
        if ident.get("attr") and ident.get("value"):
            completed.add(f"{ident['attr']}={ident['value']}")
    return completed


def _apply_goal_constraints(goal: str, state: "ExploreState",
                            action_space: list[dict]) -> list[dict]:
    """Policy：目标数量未完成时，interaction root 内终态动作不暴露。

    只作用于 interaction root 打开时（modal/overlay）；数量完成或
    无数量要求 → 原样返回。完成度从 StateGraph 派生（_derive_completed_entities）。
    """
    required = _extract_required_count(goal)
    if required is None:
        return action_space
    if len(_derive_completed_entities(state)) >= required:
        return action_space
    return [
        e for e in action_space
        if e.get("kind") != "action"
        or not _TERMINAL_ACTION_RE.search(e.get("name") or "")
    ]


# ── observe / record ───────────────────────────────────────────────────────────

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
        text = llm_call(prompt, system_prompt=EXPLORE_SYSTEM_PROMPT,
                        timeout=EXPLORE_LLM_TIMEOUT_S)   # P0：单次决策 20s 上限
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



MAX_STEPS = 10       # 最多执行 10 个动作（BFC 正常 7 个：Products→Polo→
                     # 加购×2→Continue Shopping→View Cart→Cart）
MAX_LLM_CALLS = 10   # 最多 10 次 LLM 决策调用（10 次还完不成 = 诚实失败）
MAX_EXPLORE_SECONDS = 60   # P0 硬预算：整个 explore 的 wall-clock deadline
                           #（BFC 300s 黑洞：LLM 挂起时无上限等待——
                           # 任何模型抽风都不会无限增长）
EXPLORE_LLM_TIMEOUT_S = 20   # 探索单次决策 LLM 超时（非长文生成，
                             # 20s 足够；Planner 保留 60s）
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

        started_at = perf_counter()
        deadline = started_at + MAX_EXPLORE_SECONDS   # P0 硬预算
        while (not state.done
               and state.step_count < MAX_STEPS
               and state.llm_calls < MAX_LLM_CALLS
               and perf_counter() < deadline):
            # R3：ActionSpace——LLM 只能从"当前可操作"的候选中选
            #（模态框遮挡的 Add to cart 不进入候选，模型没权限选错）
            action_space = _build_action_space(state)
            # R6：Policy——目标约束叠加（数量未完成时 interaction root
            # 内终态动作不暴露；从 StateGraph 派生，不存第二状态源）
            if state.interaction_root:
                action_space = _apply_goal_constraints(
                    state.goal, state, action_space)
            # P0 诊断：每次决策打点（卡在 LLM vs 浏览器一目了然）
            # A4.3：interaction root source（ax dialog / dom_overlay）；
            # 可点 action 与 evidence 分开计数（evidence 不是 selectable）
            iroot = (state.interaction_root or {}).get("source")
            n_act = sum(1 for e in action_space if e.get("kind") == "action")
            n_ev = len(action_space) - n_act
            print(f"[EXPLORE] step={state.step_count} llm={state.llm_calls} "
                  f"interaction_root={iroot} "
                  f"DECIDE_START selectable_actions={n_act} evidence={n_ev}",
                  flush=True)
            decision, decision_error = _decide(state, llm_call, elements=action_space)
            print(f"[EXPLORE] step={state.step_count} llm={state.llm_calls} "
                  f"DECIDE_DONE action={(decision or {}).get('action')} "
                  f"ref={(decision or {}).get('target_ref')} "
                  f"err={(decision_error or '')[:60]}", flush=True)
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

            # R3.1：Browser Action Executor——locator 由调用方（Resolver）
            # 解析，执行层返回结构化 ToolResult（不抛异常）；
            # 失败统一黑名单化（同状态同 ref 不再重试撞墙）。
            t0 = perf_counter()
            locator = None
            action_done = None
            if element is not None:
                try:
                    _, _, locator = _locator_for_element(page, element)
                    # I1：身份证据前移——探索时 count==1 命中即标 verified
                    for e in state.elements:
                        if e["ref"] == ref:
                            e["verified"] = True
                            break
                except Exception as exc:
                    # 定位解析失败与执行失败同口径：黑名单 + 历史记录
                    if from_obs:
                        state.failed_actions.add((from_obs, decision.get("action"), ref))
                    state.history.append({
                        "url": state.current_url,
                        "action": decision.get("action"),
                        "target_ref": ref,
                        "target": target,
                        "error": f"LOCATOR_FAILED: {str(exc)[:100]}",
                    })
            if decision.get("action") == "back" or locator is not None:
                result = execute_action(
                    page, action=decision["action"], locator=locator,
                    value=decision.get("value"),
                    element_name=(element or {}).get("name", "") if element else "",
                    runtime_inputs=runtime_inputs or {},
                )
                if result.ok:
                    action_done = decision["action"]
                    state.history.append({
                        "url": state.current_url,
                        "action": decision["action"],
                        "target_ref": ref,
                        "target": target,          # 解析后的 target（Planner 可读）
                        "value": decision.get("value"),
                    })
                else:
                    if from_obs:
                        state.failed_actions.add((from_obs, decision.get("action"), ref))
                    state.history.append({
                        "url": state.current_url,
                        "action": decision.get("action"),
                        "target_ref": ref,
                        "target": target,
                        "error": (f"{result.code}: {result.message}"
                                  if result.code else (result.message or "")),
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

            # 错误页 honest stop（R5：xywhaigc 案例——登录后 404，探索
            # 不应继续点"返回首页"把无关状态纳入探索路径，最后靠 G3 兜底。
            # 明确错误页 = 用户目标无法继续 → 诚实停止并记录）。
            if _detect_error_page(state.snapshot):
                state.history.append({
                    "url": state.current_url,
                    "action": "error_page",
                    "error": "页面为错误页（404/500）——测试目标无法继续，"
                             "停止探索（GOAL_NOT_REACHED）",
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
