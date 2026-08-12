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

import json
import re
from dataclasses import dataclass, field

from playwright.sync_api import sync_playwright

from runner import _resolve_locator

# ── 预算（bounded：探索必须有限）───────────────────────────────────────────────
MAX_STEPS = 8        # 最多执行 8 个动作
MAX_LLM_CALLS = 8    # 最多 8 次 LLM 决策调用（登录→商品→购物车流程约需 7-8 步）
_MAX_SNAPSHOT_CHARS = 4000   # 每页快照截断长度（保留给 element 解析用）
_MAX_HISTORY = 3     # 决策上下文只看最近 3 步历史
_MAX_TEXT_ELEMENTS = 20      # 文本节点最多注入 20 个（防上下文膨胀）

# 可交互元素角色（element ref 表只收录这些——LLM 只能操作这些）
_INTERACTIVE_ROLES = {
    "button", "link", "textbox", "checkbox", "radio", "combobox",
    "searchbox", "menuitem", "tab", "option",
}

# 解析 aria_snapshot YAML 行的正则
_ELEMENT_RE = re.compile(r'-\s+(\w+)\s+"([^"]*)"')          # - button "Login"
_TEXT_RE = re.compile(r'-\s+text:\s*(.+)')                  # - text: Products


@dataclass
class ExploreState:
    """探索状态。"""
    goal: str                          # 用户测试目标
    entry_url: str                     # 入口 URL
    current_url: str = ""              # 当前页面
    snapshot: str = ""                 # 当前页面快照（原始）
    elements: list[dict] = field(default_factory=list)      # 当前页元素表（ref）
    history: list[dict] = field(default_factory=list)       # 操作历史
    discovered_pages: list[dict] = field(default_factory=list)  # 多页面快照
    step_count: int = 0                # 已执行动作数
    llm_calls: int = 0                 # 已用 LLM 调用数
    done: bool = False                 # 探索是否完成


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
  "value": "fill 要填入的值（用户目标里给出的测试数据，用真实值）"
}}

规则：
1. target_ref 必须来自上面的元素表，不得编造
2. action 只能从上面 5 种选（wait 已移除，点击后执行器自动等待页面加载）
3. 每一步只做一个动作
4. 当已收集到生成测试 DSL 所需的全部页面路径和元素时，exploration_complete=true 并输出 finish
5. 探索阶段禁止执行删除、支付、提交订单等不可逆操作"""


# ── observe / record ───────────────────────────────────────────────────────────

def _observe(page) -> str:
    """抓取当前页面 ARIA 快照（截断，控制 token）。"""
    try:
        snapshot = page.locator("body").aria_snapshot()
        return (snapshot or "")[:_MAX_SNAPSHOT_CHARS]
    except Exception:
        return ""


def _record_page(state: ExploreState, page) -> None:
    """记录当前页面：快照 + 解析出元素表（URL 变化才新增页面）。"""
    url = page.url
    state.current_url = url
    state.snapshot = _observe(page)
    state.elements = _parse_elements(state.snapshot)   # ← ref 表
    if not any(p["url"] == url for p in state.discovered_pages):
        state.discovered_pages.append({
            "url": url,
            "title": _safe_title(page),
            "snapshot": state.snapshot,
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
        text = llm_call(prompt, system_prompt=None)
        decision = json.loads(re.search(r"\{.*\}", text, re.DOTALL).group(0))
        state.llm_calls += 1

        # 强校验：action 白名单 + target_ref 必须存在于当前元素表
        action = decision.get("action")
        if action not in {"click", "fill", "press", "back", "finish"}:
            return None
        if action != "finish":
            ref = decision.get("target_ref")
            if ref is None or not any(e["ref"] == ref for e in state.elements):
                return None   # 编造 ref → 决策无效
        return decision
    except Exception:
        return None


# ── act：ref → locator → 执行（LLM=Planner，这里=Executor）────────────────────

def _act(page, decision: dict, elements: list[dict]) -> str:
    """执行 LLM 决策的动作，返回动作名。定位失败/执行失败抛异常。"""
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
        locator.click()
    elif action == "fill":
        locator.fill(value)
    elif action == "press":
        locator.press(value or "Enter")
    else:
        raise ValueError(f"不支持的探索动作: {action}")
    return action


# ── 主入口 ──────────────────────────────────────────────────────────────────────

def explore(goal: str, entry_url: str, llm_call) -> dict:
    """bounded exploration 主循环。

    参数:
      goal:      用户测试目标（自然语言）
      entry_url: 入口 URL
      llm_call:  LLM 调用函数（由 ai_agent 注入，避免循环依赖）
                 签名: llm_call(prompt, system_prompt) -> str

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
        page.goto(entry_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
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
            try:
                _act(page, decision, state.elements)
                state.history.append({
                    "url": state.current_url,
                    "action": decision["action"],
                    "target_ref": ref,
                    "target": target,          # 解析后的 target（Planner 可读）
                    "value": decision.get("value"),
                })
            except Exception as exc:
                state.history.append({
                    "url": state.current_url,
                    "action": decision.get("action"),
                    "target_ref": ref,
                    "target": target,
                    "error": str(exc)[:100],
                })

            state.step_count += 1
            page.wait_for_timeout(800)   # 等页面渲染（SPA 异步）
            _record_page(state, page)    # observe 新页面状态

        browser.close()

    return {
        "pages": state.discovered_pages,
        "history": state.history,
        "steps_used": state.step_count,
        "llm_calls": state.llm_calls,
        "done": state.done,
    }
