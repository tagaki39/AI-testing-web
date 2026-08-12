"""
══════════════════════════════════════════════════════════════════════
explore_flow.py — bounded exploration（有限探索）
══════════════════════════════════════════════════════════════════════

【这个文件在项目中的位置】
  生成链路的前置：从"单页快照"升级为"目标驱动的多页面探索"
    用户目标 →【这里：跟随流程探索多页面】→ Planner 生成 DSL → Runner 执行

【为什么需要它（对比单页快照）】
  单页快照只看到入口页——AI 生成"登录后点击 Add to cart"时，
  商品页元素快照里根本没有，只能靠猜（heading=Your Cart 之类）。
  explore_flow 跟着用户目标走：打开登录页 → 填表 → 登录成功
  → 商品页 → 点加购 → 购物车页，每个页面都抓一份 ARIA 快照。

【核心设计（面试重点）】
  1. bounded 预算：探索必须有限，防止烧光 token
       MAX_STEPS=8 步 / MAX_LLM_CALLS=6 次调用
  2. LLM = Planner，Playwright = Executor
       LLM 只输出结构化动作（click/fill/press/wait/back/finish），
       绝不输出代码——可执行性、安全性由此保证
  3. goal_check 内联进 decide：每步判断"页面是否已满足目标所需信息"
       goal_met=true → finish，探索停止（不机械走满流程）
  4. 探索 ≠ 执行：探索发现路径，Runner 用确定性 DSL 重跑验证

【状态机】
  START → observe → decide → act → observe → goal_check(内联)
    └────────────── 未完成，循环 ──────────────┘
    └── 完成/超预算 → END

【学习路径】
  explore()（主循环）→ _observe（抓快照）→ _decide（LLM 决策）
  → _act（Playwright 执行）→ _record_page（收集多页面快照）
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
_MAX_SNAPSHOT_CHARS = 4000   # 每页快照截断长度
_MAX_HISTORY = 3     # 决策上下文只看最近 3 步历史


@dataclass
class ExploreState:
    """探索状态（方案中的 ExploreState）。"""
    goal: str                          # 用户测试目标
    entry_url: str                     # 入口 URL
    current_url: str = ""              # 当前页面
    snapshot: str = ""                 # 当前页面快照
    history: list[dict] = field(default_factory=list)        # 操作历史
    discovered_pages: list[dict] = field(default_factory=list)  # 多页面快照
    step_count: int = 0                # 已执行动作数
    llm_calls: int = 0                 # 已用 LLM 调用数
    done: bool = False                 # 是否完成


# ── decide：LLM 决策 prompt（只输出结构化动作，不输出代码）──────────────────────

DECIDE_PROMPT = """你是 Web 页面探索器。目标：通过执行页面操作，找到完成用户目标所需的页面路径和元素。

当前状态：
- 用户目标: {goal}
- 当前 URL: {url}
- 页面标题: {title}
- 当前页面结构（ARIA snapshot）:
{snapshot}

最近操作历史:
{history}

你的任务：决定下一步动作。只输出 JSON：
{{
  "reason": "为什么这么做",
  "goal_met": false,
  "action": "click | fill | press | wait | back | finish",
  "target": {{"role": "button", "name": "..."}} 或 {{"text": "..."}},
  "value": "fill 要填入的值（用户目标里给出的测试数据，直接用真实值）"
}}

规则：
1. action 只能从上面 6 种选；goal_met=true 时 action 必须是 finish
2. target 必须基于当前页面快照中真实存在的元素
3. 每一步只做一个动作
4. 当页面已具备完成用户目标所需的信息时，goal_met=true 并输出 finish"""


# ── observe：抓当前页面快照 ─────────────────────────────────────────────────────

def _observe(page) -> str:
    """抓取当前页面 ARIA 快照（截断，控制 token）。"""
    try:
        snapshot = page.locator("body").aria_snapshot()
        return (snapshot or "")[:_MAX_SNAPSHOT_CHARS]
    except Exception:
        return ""


def _record_page(state: ExploreState, page) -> None:
    """记录当前页面快照（URL 变化才新增，去重）。"""
    url = page.url
    state.current_url = url
    state.snapshot = _observe(page)
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


# ── decide：LLM 决策（goal_check 内联）─────────────────────────────────────────

def _decide(state: ExploreState, llm_call) -> dict | None:
    """调 LLM 决定下一步动作；返回 None 表示决策失败（停止探索）。"""
    history_text = "\n".join(
        f"- {h.get('action')} {h.get('target')} {h.get('value') or ''} @ {h.get('url')}"
        for h in state.history[-_MAX_HISTORY:]
    ) or "- (无)"

    prompt = DECIDE_PROMPT.format(
        goal=state.goal,
        url=state.current_url,
        title=state.discovered_pages[-1]["title"] if state.discovered_pages else "",
        snapshot=state.snapshot,
        history=history_text,
    )
    try:
        text = llm_call(prompt, system_prompt=None)
        decision = _extract_json(text)
        state.llm_calls += 1

        # 校验动作白名单（Planner 输出的合法性检查）
        action = decision.get("action")
        if action not in {"click", "fill", "press", "wait", "back", "finish"}:
            return None
        return decision
    except Exception:
        return None


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"LLM 输出中找不到 JSON: {text[:200]}")
    return json.loads(match.group(0))


# ── act：Playwright 执行结构化动作（LLM=Planner，这里=Executor）────────────────

def _act(page, decision: dict) -> str:
    """执行 LLM 决策的动作，返回动作名。定位失败/执行失败抛异常。"""
    action = decision.get("action")
    target = decision.get("target")
    value = decision.get("value") or ""

    if action == "click":
        _, locator = _resolve_locator(page, target)
        locator.click()
    elif action == "fill":
        _, locator = _resolve_locator(page, target)
        locator.fill(value)
    elif action == "press":
        _, locator = _resolve_locator(page, target)
        locator.press(value or "Enter")
    elif action == "wait":
        page.wait_for_timeout(int(value) if str(value).isdigit() else 1000)
    elif action == "back":
        page.go_back()
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
        "history": [{url, action, target, value}],  # 探索路径（路径证据）
        "steps_used": int, "llm_calls": int, "done": bool,
      }

    预算耗尽 / 决策失败 / 目标达成 → 停止探索（不中断主链路）。
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
                break   # 决策失败 → 停止（保留已发现页面）

            if decision.get("goal_met") or decision.get("action") == "finish":
                state.done = True
                break

            # 执行动作（失败记录进历史，继续下一轮——不中断探索）
            try:
                _act(page, decision)
                state.history.append({
                    "url": state.current_url,
                    "action": decision["action"],
                    "target": decision.get("target"),
                    "value": decision.get("value"),
                })
            except Exception as exc:
                state.history.append({
                    "url": state.current_url,
                    "action": decision.get("action"),
                    "target": decision.get("target"),
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
