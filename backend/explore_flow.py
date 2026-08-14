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

from playwright.sync_api import sync_playwright

from runner import _resolve_locator, _substitute

# ── 预算（bounded：探索必须有限）───────────────────────────────────────────────
MAX_STEPS = 8        # 最多执行 8 个动作
MAX_LLM_CALLS = 8    # 最多 8 次 LLM 决策调用（登录→商品→购物车流程约需 7-8 步）
_MAX_SNAPSHOT_CHARS = 6000   # 裁剪后快照的最终兜底上限（重组后仍超限才截断）
_MAX_HISTORY = 3     # 决策上下文只看最近 3 步历史
_MAX_TEXT_ELEMENTS = 20      # 文本节点最多注入 20 个（防上下文膨胀）
_MAX_TEXT_LINES = 25         # 智能裁剪：text 行限量
_MAX_OTHER_LINES = 40        # 智能裁剪：非交互语义行（heading/banner 等容器）限量
_MAX_OBSERVATIONS = 12       # 总 observation 上限（防膨胀）
_MAX_OBSERVATIONS_PER_URL = 3   # 同 URL 最多 3 个状态（SPA 状态变化）

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

# 不可逆/危险操作关键词（点击前拦截：删除/支付/提交订单等）
_DESTRUCTIVE_PATTERNS = (
    "delete", "remove", "pay", "purchase", "submit order",
    "send", "publish", "sign out", "log out", "注销", "删除", "支付",
)


def _within_origin(url: str, entry_url: str) -> bool:
    """origin 限制：探索不得离开入口站点（跨域导航会触发回退）。"""
    try:
        return urlparse(url).netloc == urlparse(entry_url).netloc
    except Exception:
        return False


@dataclass
class ExploreState:
    """探索状态。"""
    goal: str                          # 用户测试目标
    entry_url: str                     # 入口 URL
    current_url: str = ""              # 当前页面
    snapshot: str = ""                 # 当前页面快照（原始）
    elements: list[dict] = field(default_factory=list)      # 当前页元素表（ref）
    history: list[dict] = field(default_factory=list)       # 操作历史
    observations: list[dict] = field(default_factory=list)  # 页面状态观察（含 state_hash）
    step_count: int = 0                # 已执行动作数
    llm_calls: int = 0                 # 已用 LLM 调用数
    done: bool = False                 # 探索是否完成
    # 计时（Speed v1：定位耗时构成，决定下一刀砍哪）
    timings: dict = field(default_factory=lambda: {
        "llm_ms": 0, "browser_action_ms": 0, "fixed_wait_ms": 0, "observation_ms": 0,
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


def _elements_to_prompt(elements: list[dict]) -> str:
    """元素表 → 决策上下文（紧凑格式）。"""
    lines = []
    for e in elements:
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
- 当前 URL: {url}
- 当前页面可操作元素（ref 表）:
{elements}

最近操作历史:
{history}

你的任务：决定下一步动作。只输出 JSON：
{{
  "reason": "为什么这么做",
  "exploration_complete": false,
  "action": "click | fill | press | back | finish",
  "target_ref": "e1",
  "value": "fill 的 value 必须使用 ${{var}} 占位符（如 ${{username}}）；不得输出真实敏感值，真实值由执行器本地注入"
}}

规则：
1. target_ref 必须来自上面的元素表，不得编造
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


def _record_page(state: ExploreState, page) -> None:
    """记录当前页面状态为 observation（升级：URL + state_hash 去重）。

    Observation = URL + 页面状态 + ARIA 证据——
    同 URL 不同状态（如 Add to cart 点击后按钮变 Remove）也保存，
    解决 SPA 状态丢失（此前只按 URL 去重）。
    """
    url = page.url
    state.current_url = url
    state.snapshot = _observe(page)
    state.elements = _parse_elements(state.snapshot)   # ← ref 表（页面级 e1/e2）

    # 状态哈希：snapshot 变化 = 页面状态变化（即使 URL 相同）
    state_hash = hashlib.sha256(state.snapshot.encode()).hexdigest()[:10]
    same_url_count = sum(1 for o in state.observations if o["url"] == url)

    already = any(
        o["url"] == url and o.get("state_hash") == state_hash
        for o in state.observations
    )
    if already:
        return
    if len(state.observations) >= _MAX_OBSERVATIONS:
        return
    if same_url_count >= _MAX_OBSERVATIONS_PER_URL:
        return

    obs_id = f"obs{len(state.observations) + 1}"
    # G1：state-scoped ref——元素 ref 从页面级 "e1" 升级为状态级 "obs3:e1"。
    # Planner 引用 obs3:e17 时，系统知道 belongs_to=obs3（state identity）。
    for element in state.elements:
        element["ref"] = f"{obs_id}:{element['ref']}"

    state.observations.append({
        "id": obs_id,
        "url": url,
        "title": _safe_title(page),
        "state_hash": state_hash,
        "snapshot": state.snapshot,
        "elements": state.elements,   # G1：observations 携带 state-scoped refs
    })


def _safe_title(page) -> str:
    try:
        return page.title() or ""
    except Exception:
        return ""


# ── decide：LLM 决策（ref 强校验 + exploration_complete）──────────────────────

def _decide(state: ExploreState, llm_call) -> dict | None:
    """调 LLM 决定下一步；ref 不在元素表 → 决策无效（返回 None 停止）。

    这是"LLM 没有权限创造元素"的代码保证：
      prompt 只给元素表，输出必须引用 ref，代码校验 ref 存在。
    """
    history_text = "\n".join(
        f"- {h.get('action')} {h.get('target_ref')} {h.get('value') or ''} @ {h.get('url')}"
        for h in state.history[-_MAX_HISTORY:]
    ) or "- (无)"

    prompt = DECIDE_PROMPT.format(
        goal=state.goal,
        url=state.current_url,
        elements=_elements_to_prompt(state.elements),
        history=history_text,
    )
    try:
        t0 = perf_counter()
        text = llm_call(prompt, system_prompt=EXPLORE_SYSTEM_PROMPT)
        state.timings["llm_ms"] += int((perf_counter() - t0) * 1000)
        decision = json.loads(re.search(r"\{.*\}", text, re.DOTALL).group(0))
        state.llm_calls += 1

        # 强校验：action 白名单 + target_ref 必须存在于当前元素表
        action = decision.get("action")
        if action not in {"click", "fill", "press", "back", "finish"}:
            return None
        if action == "press":
            # press 按键枚举（第 6 项：不允许 LLM 自由输出按键）
            if (decision.get("value") or "") not in _PRESS_KEYS:
                return None
        if action != "finish":
            ref = decision.get("target_ref")
            if ref is None or not any(e["ref"] == ref for e in state.elements):
                return None   # 编造 ref → 决策无效
        return decision
    except Exception:
        return None


# ── act：ref → locator → 执行（LLM=Planner，这里=Executor）────────────────────

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
    target = {"role": element["role"], "name": element["name"]} if "role" in element \
        else {"text": element["text"]}
    _, locator = _resolve_locator(page, target)

    if action == "click":
        # 危险操作二次拦截（第 6 项：代码层，不只靠 Prompt）——
        # 目标名称含删除/支付/提交订单等关键词 → 拒绝执行
        name = element.get("name", "") if element else ""
        if any(p in name.lower() for p in _DESTRUCTIVE_PATTERNS):
            raise ValueError(f"危险操作被拦截: {name!r}")
        locator.click()
    elif action == "fill":
        locator.fill(_substitute(value, runtime_inputs) or "")
    elif action == "press":
        locator.press(_substitute(value, runtime_inputs) or "Enter")
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

        state = ExploreState(goal=goal, entry_url=entry_url)
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
            decision = _decide(state, llm_call)
            if decision is None:
                break   # 决策失败（编造 ref / 非法动作）→ 停止

            if decision.get("exploration_complete") or decision.get("action") == "finish":
                state.done = True
                break

            # 执行动作（失败记录进历史，继续下一轮）
            ref = decision.get("target_ref")
            element = next((e for e in state.elements if e["ref"] == ref), None) if ref else None
            target = {"role": element["role"], "name": element["name"]} if element and "role" in element \
                else ({"text": element["text"]} if element else None)
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
            _record_page(state, page)    # observe 新页面状态
            state.timings["observation_ms"] += int((perf_counter() - t0) * 1000)

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
        "history": state.history,
        "steps_used": state.step_count,
        "llm_calls": state.llm_calls,
        "done": state.done,
        "timings": state.timings,
    }
