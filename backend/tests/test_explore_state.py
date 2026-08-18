"""
══════════════════════════════════════════════════════════════════════
test_explore_state.py — 探索状态所有权与 Data Grounding 回归
══════════════════════════════════════════════════════════════════════

零依赖 plain-assert 脚本，直接运行：

    py backend/tests/test_explore_state.py

背景（E2E 暴露）：fill 失败后页面状态未变，_record_page 的 already
路径把 state.elements 替换成无 obs 前缀的新表——决策校验拿 obs2:e10
对表校验 8 连拒，探索止步登录页。

评审收紧后的 invariant：
  1. _record_page 返回后，state.elements 的每个 ref 必须有 state
     identity（obsN:eM）——裸 e1/e2 意味着状态所有权丢失
  2. 当前 snapshot 命中哪个 observation，就恢复哪个的 elements
     （A→B→A 必须恢复 obs1，不是"上一次元素表"= obs2）
  3. observation cap 满 → 停止探索，不带无主元素继续决策
  4. Data Grounding：fill 的 value 必须是 ${key} 占位符且 key 在
     Runtime Input Keys 白名单内——模型不能创造变量名、
     不能直接输出真实值

覆盖：
  A. 同状态重复 observe → 元素表保持 obs 前缀
  B. A→B→A → 恢复 obs1 的元素表（不是 obs2）
  C. 未知 runtime key（${username}）→ 决策被拒
  D. 真实值 literal（test123@example.com）→ 决策被拒
  E. observation per-url cap → 停止探索并记录原因
══════════════════════════════════════════════════════════════════════
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/

from explore_flow import (   # noqa: E402
    ExploreState, _decide, _detect_auth_failure,
    _is_repeated_no_progress, _record_page, _validate_action_target,
    validate_actionability,
)


# ── page mock（_record_page 只依赖 url / title / locator("body").aria_snapshot）──

class _MockLocator:
    def __init__(self, snapshot: str):
        self._snapshot = snapshot

    def aria_snapshot(self) -> str:
        return self._snapshot


class _MockPage:
    def __init__(self, url: str, snapshot: str):
        self._url = url
        self._snapshot = snapshot

    @property
    def url(self) -> str:
        return self._url

    def title(self) -> str:
        return "mock title"

    def locator(self, selector: str):
        assert selector == "body"
        return _MockLocator(self._snapshot)


# ── A：同状态重复 observe ────────────────────────────────────────────────────

def test_a_repeated_observe_keeps_scoped_refs() -> None:
    """fill 失败后页面未变，再次 observe 命中 obs1——元素表必须保持
    obs1: 前缀（此前 bug：替换成裸 e1/e2，决策校验全拒）。"""
    snap = '- button "Login"\n- link "Products"\n'
    page = _MockPage("https://x.com", snap)
    state = ExploreState(goal="login", entry_url="https://x.com")
    _record_page(state, page)          # obs1 创建
    assert state.elements[0]["ref"].startswith("obs1:")
    _record_page(state, page)          # 同 snapshot → 命中 obs1（不新增）
    assert len(state.observations) == 1
    assert all(":" in e["ref"] for e in state.elements), (
        f"重复 observe 后出现裸 ref: {state.elements[:3]}")
    assert state.elements[0]["ref"].startswith("obs1:")


# ── B：A→B→A 恢复正确 owner ──────────────────────────────────────────────────

def test_b_back_to_prev_state_restores_that_owner() -> None:
    """A→B→A：回到 A 时必须恢复 obs1 的元素表，而不是 obs2 的
    （"恢复上一次 elements"只在连续重复场景安全——评审收紧点）。"""
    snap_a = '- button "Login"\n'
    snap_b = '- link "Products"\n'
    state = ExploreState(goal="login", entry_url="https://x.com")
    _record_page(state, _MockPage("https://x.com", snap_a))      # obs1
    _record_page(state, _MockPage("https://x.com/login", snap_b))  # obs2
    assert state.elements[0]["ref"].startswith("obs2:")
    _record_page(state, _MockPage("https://x.com", snap_a))      # 回到 A
    assert state.elements[0]["ref"].startswith("obs1:"), (
        f"A→B→A 后元素表 owner 错误: {state.elements[0]['ref']}")


# ── C/D：Data Grounding（_decide 强校验）─────────────────────────────────────

def _decide_state() -> ExploreState:
    state = ExploreState(
        goal="login", entry_url="https://x.com",
        input_keys={"email", "password"},
    )
    state.elements = [{"ref": "obs2:e10", "role": "textbox", "name": "Email"}]
    state.observations = [{
        "id": "obs2", "url": "https://x.com/login",
        "state_hash": "h", "elements": state.elements,
    }]
    return state


def test_c_unknown_runtime_key_rejected() -> None:
    """${username} 不在白名单（{email, password}）→ 决策拒绝。"""
    def llm(prompt, system_prompt=None):
        return '{"action": "fill", "target_ref": "obs2:e10", "value": "${username}"}'
    decision, err = _decide(_decide_state(), llm)
    assert decision is None, "未知 key 必须被拒"
    assert "未知 runtime input key" in (err or "")


def test_d_literal_value_rejected() -> None:
    """真实值（test123@example.com）→ 决策拒绝（模型不得输出真实值）。"""
    def llm(prompt, system_prompt=None):
        return ('{"action": "fill", "target_ref": "obs2:e10", '
                '"value": "test123@example.com"}')
    decision, err = _decide(_decide_state(), llm)
    assert decision is None, "真实值 literal 必须被拒"
    assert "占位符" in (err or "")


def test_c2_known_key_passes() -> None:
    """${email} 在白名单 → 决策通过（对照：校验不放跑真占位符）。"""
    def llm(prompt, system_prompt=None):
        return '{"action": "fill", "target_ref": "obs2:e10", "value": "${email}"}'
    decision, err = _decide(_decide_state(), llm)
    assert decision is not None, f"合法占位符被误拒: {err}"
    assert decision["value"] == "${email}"


# ── H：动作能力矩阵（P0-1：LLM 提议，代码裁决结构合法性）─────────────────────

def test_h_text_element_not_clickable() -> None:
    """text 元素（无 role）不可作为 click 目标（E2E：模型乱点 Blue Top 文本）。"""
    ok, reason = _validate_action_target("click", {"type": "text", "text": "Blue Top"})
    assert not ok and reason == "NON_ACTIONABLE_REF"


def test_h2_role_action_matrix() -> None:
    """role 能力矩阵：button 可 click 不可 fill；textbox 可 fill 可 click。"""
    assert _validate_action_target("click", {"role": "button", "name": "Login"}) == (True, None)
    assert _validate_action_target("fill", {"role": "button", "name": "Login"})[0] is False
    assert _validate_action_target("fill", {"role": "textbox", "name": "Email"}) == (True, None)
    assert _validate_action_target("click", {"role": "textbox", "name": "Email"}) == (True, None)
    assert _validate_action_target("press", {"role": "link", "name": "Cart"}) == (True, None)


def test_h3_text_click_rejected_in_decide() -> None:
    """完整路径：模型决策 click text 元素 → _decide 拒绝（确定性）。"""
    state = ExploreState(goal="buy", entry_url="https://x.com")
    state.elements = [
        {"ref": "obs1:e1", "type": "text", "text": "Blue Top"},
        {"ref": "obs1:e2", "role": "button", "name": "Add to cart"},
    ]
    state.observations = [{
        "id": "obs1", "url": "https://x.com", "state_hash": "h",
        "elements": state.elements,
    }]
    def llm(prompt, system_prompt=None):
        return '{"action": "click", "target_ref": "obs1:e1"}'
    decision, err = _decide(state, llm)
    assert decision is None, "click text 必须被拒"
    assert "NON_ACTIONABLE_REF" in (err or "")


def test_h4_button_click_passes() -> None:
    """点击真按钮 → 通过（对照）。"""
    state = ExploreState(goal="buy", entry_url="https://x.com")
    state.elements = [{"ref": "obs1:e2", "role": "button", "name": "Add to cart"}]
    state.observations = [{
        "id": "obs1", "url": "https://x.com", "state_hash": "h",
        "elements": state.elements,
    }]
    def llm(prompt, system_prompt=None):
        return '{"action": "click", "target_ref": "obs1:e2"}'
    decision, err = _decide(state, llm)
    assert decision is not None, f"合法点击被误拒: {err}"


# ── I：E1 Runtime Actionability Guard（A11y 存在 ≠ 当前可操作）───────────────

class _MockLocator2:
    """可操作性校验用 locator mock（elementFromPoint 判定可控）。"""
    def __init__(self, visible=True, enabled=True, obscured=False, box=None):
        self._visible = visible
        self._enabled = enabled
        self._obscured = obscured
        self._box = box or {"x": 0, "y": 0, "width": 100, "height": 20}
        self.click_calls = 0

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return self._enabled

    def bounding_box(self):
        return self._box

    def element_handle(self):
        return object()


class _MockPage3:
    def __init__(self, obscured):
        self._obscured = obscured

    def evaluate(self, js, arg):
        return self._obscured


def test_i1_obscured_link_rejected() -> None:
    """模态框打开后底层 Add to cart：可见/可用但被遮挡 → TARGET_OBSCURED。"""
    loc = _MockLocator2(visible=True, enabled=True, obscured=True)
    ok, reason = validate_actionability(_MockPage3(True), loc, "click")
    assert not ok and reason == "TARGET_OBSCURED"


def test_i2_actionable_button_passes() -> None:
    """Continue Shopping（无遮挡）→ 通过。"""
    loc = _MockLocator2(visible=True, enabled=True, obscured=False)
    ok, reason = validate_actionability(_MockPage3(False), loc, "click")
    assert ok and reason == ""


def test_i3_not_visible_rejected() -> None:
    """不可见 → TARGET_NOT_VISIBLE。"""
    loc = _MockLocator2(visible=False)
    ok, reason = validate_actionability(_MockPage3(False), loc, "click")
    assert not ok and reason == "TARGET_NOT_VISIBLE"


def test_i4_blacklist_removed_from_action_space() -> None:
    """R3（评审瘦身）：失败 ref 从 ActionSpace 候选消失（Restrict——
    模型没权限选择错误动作），而非"选后再拒"。"""
    from explore_flow import _build_action_space
    state = ExploreState(goal="buy", entry_url="https://x.com")
    state.elements = [
        {"ref": "obs4:e25", "role": "link", "name": "Add to cart", "actionable": True},
        {"ref": "obs4:e24", "role": "button", "name": "Continue Shopping", "actionable": True},
    ]
    state.observations = [{
        "id": "obs4", "url": "https://x.com", "state_hash": "h",
        "elements": state.elements,
    }]
    state.failed_actions.add(("obs4", "click", "obs4:e25"))
    state.current_obs = "obs4"
    space = _build_action_space(state)
    refs = [e["ref"] for e in space]
    assert "obs4:e25" not in refs, "黑名单 ref 必须从候选消失"
    assert "obs4:e24" in refs, "未黑名单 ref 保留"


def test_i5_blacklisted_ref_rejected_by_validator() -> None:
    """防御兜底：模型仍输出黑名单 ref → ref 校验拒绝（不在候选表内）。"""
    state = ExploreState(goal="buy", entry_url="https://x.com")
    state.elements = [
        {"ref": "obs4:e25", "role": "link", "name": "Add to cart", "actionable": True},
        {"ref": "obs4:e24", "role": "button", "name": "Continue Shopping", "actionable": True},
    ]
    state.observations = [{
        "id": "obs4", "url": "https://x.com", "state_hash": "h",
        "elements": state.elements,
    }]
    state.failed_actions.add(("obs4", "click", "obs4:e25"))
    state.current_obs = "obs4"
    def llm(prompt, system_prompt=None):
        return '{"action": "click", "target_ref": "obs4:e25"}'
    decision, err = _decide(state, llm, elements=[
        e for e in state.elements if (state.current_obs, "click", e["ref"])
        not in state.failed_actions])
    assert decision is None, "黑名单 ref 必须被拒"
    assert "不在当前元素表" in (err or "")


# ── F/G：no-progress guard + auth failure（Transition/Progress Validation）────

def _progress_state() -> ExploreState:
    state = ExploreState(goal="login", entry_url="https://x.com",
                         input_keys={"email", "password"})
    state.elements = [{"ref": "obs2:e12", "role": "button", "name": "Login"}]
    state.observations = [{
        "id": "obs2", "url": "https://x.com/login",
        "state_hash": "h", "elements": state.elements,
    }]
    state.current_obs = "obs2"
    return state


def test_f_self_loop_same_action_rejected() -> None:
    """上一次 transition 是 self-loop（click Login 无状态变化）且新决策
    是同一动作 + 同一 ref → no-progress 拒（Action 成功 ≠ transition 成功）。"""
    state = _progress_state()
    state.transitions.append({
        "from": "obs2", "action": "click", "target_ref": "obs2:e12", "to": "obs2",
    })
    def llm(prompt, system_prompt=None):
        return '{"action": "click", "target_ref": "obs2:e12"}'
    decision, err = _decide(state, llm)
    assert decision is None, "self-loop 重复必须被拒"
    assert "NO_PROGRESS" in (err or "")


def test_f2_self_loop_different_action_passes() -> None:
    """self-loop 后换动作（fill）→ 不被 no-progress 拒。"""
    state = _progress_state()
    state.transitions.append({
        "from": "obs2", "action": "click", "target_ref": "obs2:e12", "to": "obs2",
    })
    state.elements.append({"ref": "obs2:e10", "role": "textbox", "name": "Email"})
    def llm(prompt, system_prompt=None):
        return '{"action": "fill", "target_ref": "obs2:e10", "value": "${email}"}'
    decision, err = _decide(state, llm)
    assert decision is not None, f"换动作被误拒: {err}"


def test_f3_progress_transition_not_rejected() -> None:
    """上一次 transition 产生了新状态（obs2→obs3）→ 同动作同 ref 不拒。"""
    state = _progress_state()
    state.transitions.append({
        "from": "obs2", "action": "click", "target_ref": "obs2:e12", "to": "obs3",
    })
    def llm(prompt, system_prompt=None):
        return '{"action": "click", "target_ref": "obs2:e12"}'
    decision, err = _decide(state, llm)
    assert decision is not None, f"有进展的重复被误拒: {err}"


def test_g_auth_failure_detected() -> None:
    """页面出现认证失败整句 → 识别（死胡同明确停止）。"""
    assert _detect_auth_failure("Your email or password is incorrect!")
    assert _detect_auth_failure("The email address does not exist. Register first.")
    assert _detect_auth_failure("账号或密码错误，请重试")


def test_g2_auth_failure_false_positive_avoided() -> None:
    """普通文本不含认证失败整句 → 不误报。"""
    assert not _detect_auth_failure("Products list with prices and Add to cart buttons")
    assert not _detect_auth_failure("email subscription form")
    assert not _detect_auth_failure("")

def test_e_observation_cap_stops_exploration() -> None:
    """per-url cap=5：第 6 个不同状态 → done=True + 记录原因，
    不再继续带无主元素决策。"""
    state = ExploreState(goal="login", entry_url="https://x.com")
    for i in range(6):
        _record_page(state, _MockPage("https://x.com", f'- button "B{i}"\n'))
    assert state.done, "观察预算满必须停止探索"
    assert any(h.get("action") == "observation_cap" for h in state.history)


def main() -> int:
    tests = [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
