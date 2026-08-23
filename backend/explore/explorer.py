"""
explorer.py — 探索主循环（R3 拆分自 explore_flow）
  bounded loop：observe → ActionSpace → choose → execute → transition。
  Execute, don't predict：短超时执行，失败进 failed_actions。
"""
from time import perf_counter
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from execution.action_executor import execute_action
from .observation import (
    ExploreState, _observe, _observe_until_stable, _record_page,
)
from .action_space import _build_action_space, _locator_for_element
from .policy import (
    _decide, _detect_auth_failure, _is_repeated_no_progress,
    _validate_completion,
)

MAX_STEPS = 12       # 最多执行 12 个动作（BFC 场景需要 7 个成功动作：
                     # 首页→Products→Polo→加购×2→Continue Shopping→View Cart，
                     # 8 步上限会让购物车页探索不到）
MAX_LLM_CALLS = 16   # 最多 16 次 LLM 决策调用（BFC 实测 8 步探索耗 8-10 次，
                     # 含决策自纠；12 步动作 + 自纠余量）
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
