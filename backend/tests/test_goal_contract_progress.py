"""S2 Contract v2：原子 obligation、语义覆盖与进度证据回归。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from explore.observation import ExploreState, TerminationReason  # noqa: E402
from explore.explorer import (  # noqa: E402
    _completion_status,
    _validate_milestone_decision,
)
from explore.progress import derive_milestone_progress  # noqa: E402
from goal_contract import (  # noqa: E402
    GoalContract, GoalContractError, Milestone, build_goal_contract,
    canonicalize_goal_contract, parse_goal_contract,
)


def _contract(*milestones: Milestone) -> GoalContract:
    return GoalContract(milestones=list(milestones))


def _state(goal: str = "test") -> ExploreState:
    state = ExploreState(goal=goal, entry_url="https://x.test")
    state.current_obs = "obs1"
    state.current_url = "https://x.test"
    state.observations = [{
        "id": "obs1", "url": state.current_url, "title": "Home", "elements": [],
    }]
    return state


def _payload(milestones: str) -> str:
    return '{"version":"s2.v2","milestones":[' + milestones + "]}"


def test_schema_removes_ready_and_side_effect() -> None:
    for old_type in ("ready", "side_effect"):
        with pytest.raises(ValueError):
            Milestone(id="m1", type=old_type, intent="旧类型",  # type: ignore[arg-type]
                      target_terms=["按钮"], execution="explorer")


def test_type_specific_atomic_invariants() -> None:
    with pytest.raises(ValueError, match="不允许 field_terms"):
        Milestone(id="m1", type="auth", intent="登录", target_terms=["登录"],
                  field_terms=["账号"], execution="explorer")
    with pytest.raises(ValueError, match="field_terms"):
        Milestone(id="m1", type="input", intent="填写认证字段",
                  field_terms=["账号", "密码"], value_ref="${username}",
                  execution="explorer")
    with pytest.raises(ValueError, match="必须提供一个 value_ref"):
        Milestone(id="m1", type="input", intent="填写账号",
                  field_terms=["账号"], execution="explorer")
    with pytest.raises(ValueError, match="不能是 value_ref"):
        Milestone(id="m1", type="input", intent="填写账号",
                  field_terms=["${username}"], value_ref="${username}",
                  execution="explorer")
    with pytest.raises(ValueError, match="execution=runner"):
        Milestone(id="m1", type="terminal_action", intent="生成图片",
                  target_terms=["生成图片"], execution="explorer")


def test_saucedemo_contract_has_stable_atomic_shape() -> None:
    goal = ("使用账号 ${username} 密码 ${password} 登录后进入 Products，"
            "将第一个商品加入购物车")
    text = _payload(
        '{"id":"m1","type":"input","intent":"填写账号",'
        '"target_terms":[],"field_terms":["账号"],'
        '"value_ref":"${username}","execution":"explorer"},'
        '{"id":"m2","type":"input","intent":"填写密码",'
        '"target_terms":[],"field_terms":["密码"],'
        '"value_ref":"${password}","execution":"explorer"},'
        '{"id":"m3","type":"auth","intent":"登录",'
        '"target_terms":["登录"],"field_terms":[],"value_ref":null,'
        '"execution":"explorer"},'
        '{"id":"m4","type":"navigate","intent":"进入产品页",'
        '"target_terms":["Products"],"field_terms":[],"value_ref":null,'
        '"execution":"explorer"},'
        '{"id":"m5","type":"action","intent":"加入购物车",'
        '"target_terms":["加入购物车"],"field_terms":[],"value_ref":null,'
        '"execution":"explorer"}')
    contract = parse_goal_contract(text, goal, {"username", "password"})
    assert [m.type for m in contract.milestones] == [
        "input", "input", "auth", "navigate", "action"]
    assert [m.value_ref for m in contract.milestones[:2]] == [
        "${username}", "${password}"]


def test_goal_coverage_rejects_missing_or_wrong_add_to_cart() -> None:
    goal = "登录后将商品加入购物车"
    missing = _payload(
        '{"id":"m1","type":"auth","intent":"登录",'
        '"target_terms":["登录"],"field_terms":[],"value_ref":null,'
        '"execution":"explorer"}')
    with pytest.raises(GoalContractError, match="add_to_cart"):
        parse_goal_contract(missing, goal)
    wrong_type = _payload(
        '{"id":"m1","type":"auth","intent":"登录",'
        '"target_terms":["登录"],"field_terms":[],"value_ref":null,'
        '"execution":"explorer"},'
        '{"id":"m2","type":"terminal_action","intent":"加入购物车",'
        '"target_terms":["加入购物车"],"field_terms":[],"value_ref":null,'
        '"execution":"runner"}')
    with pytest.raises(GoalContractError, match="action/explorer"):
        parse_goal_contract(wrong_type, goal)


def test_generate_requires_terminal_action() -> None:
    wrong = _payload(
        '{"id":"m1","type":"action","intent":"生成图片",'
        '"target_terms":["生成图片"],"field_terms":[],"value_ref":null,'
        '"execution":"explorer"}')
    with pytest.raises(GoalContractError, match="terminal_action/runner"):
        parse_goal_contract(wrong, "填写提示词后生成图片")

    poster = wrong.replace("生成图片", "生成海报")
    with pytest.raises(GoalContractError, match="terminal_action/runner"):
        parse_goal_contract(poster, "填写提示词后生成海报")


def test_supported_order_rejects_add_before_navigation() -> None:
    text = _payload(
        '{"id":"m1","type":"action","intent":"加入购物车",'
        '"target_terms":["加入购物车"],"field_terms":[],"value_ref":null,'
        '"execution":"explorer"},'
        '{"id":"m2","type":"navigate","intent":"进入产品页",'
        '"target_terms":["Products"],"field_terms":[],"value_ref":null,'
        '"execution":"explorer"}')
    with pytest.raises(GoalContractError, match="navigate milestone"):
        parse_goal_contract(text, "进入 Products 后加入购物车")


def test_canonicalize_preserves_navigation_and_only_renumbers() -> None:
    contract = _contract(
        Milestone(id="m1", type="navigate", intent="工作台",
                  target_terms=["工作台"], execution="explorer"),
        Milestone(id="m2", type="navigate", intent="图片生成",
                  target_terms=["图片生成"], execution="explorer"))
    normalized = canonicalize_goal_contract(contract, "进入工作台和图片生成页面")
    assert [m.type for m in normalized.milestones] == ["navigate", "navigate"]


def test_navigation_page_name_does_not_imply_terminal_generate() -> None:
    text = _payload(
        '{"id":"m1","type":"navigate","intent":"进入图片生成页面",'
        '"target_terms":["图片生成"],"field_terms":[],"value_ref":null,'
        '"execution":"explorer"}')
    contract = parse_goal_contract(text, "进入图片生成页面")
    assert [m.type for m in contract.milestones] == ["navigate"]


def test_input_progress_requires_frozen_success_provenance() -> None:
    contract = _contract(Milestone(
        id="m1", type="input", intent="填写密码", field_terms=["密码"],
        value_ref="${password}", execution="explorer"))
    state = _state("填写密码 ${password}")
    state.history = [{"milestone_id": "m1", "action": "fill",
                      "target_ref": "obs1:e1", "value": "${password}", "ok": False}]
    assert not derive_milestone_progress(contract, state).complete
    state.history[0]["ok"] = True
    assert derive_milestone_progress(contract, state).complete
    state.history[0]["milestone_id"] = "m2"
    assert not derive_milestone_progress(contract, state).complete


def test_input_obligation_rejects_wrong_field_before_execution() -> None:
    contract = _contract(Milestone(
        id="m1", type="input", intent="填写账号", field_terms=["账号"],
        value_ref="${username}", execution="explorer"))
    state = _state("填写账号 ${username}")
    state.goal_contract = contract
    state.input_keys = {"username"}
    password = {"ref": "obs1:e1", "kind": "action", "role": "textbox",
                "name": "Password"}
    username = {"ref": "obs1:e2", "kind": "action", "role": "textbox",
                "name": "Username"}

    decision = {"action": "fill", "target_ref": "obs1:e1",
                "value": "${username}"}
    assert _validate_milestone_decision(state, decision, password) is not None

    decision["target_ref"] = "obs1:e2"
    assert _validate_milestone_decision(state, decision, username) is None


def test_action_requires_verified_transition() -> None:
    contract = _contract(Milestone(id="m1", type="action", intent="加入购物车",
                                   target_terms=["加入购物车"], execution="explorer"))
    state = _state("加入购物车")
    state.history = [{"milestone_id": "m1", "action": "click", "ok": True,
                      "target": {"name": "加入购物车"}}]
    assert not derive_milestone_progress(contract, state).complete
    state.transitions = [{"from": "obs1", "to": "obs2", "action": "click",
                          "target_name": "Add to cart", "milestone_id": "m2"}]
    assert not derive_milestone_progress(contract, state).complete
    state.transitions[0]["milestone_id"] = "m1"
    assert derive_milestone_progress(contract, state).complete


def test_terminal_action_history_never_completes_runner_obligation() -> None:
    contract = _contract(Milestone(id="m1", type="terminal_action", intent="生成图片",
                                   target_terms=["生成图片"], execution="runner"))
    state = _state("生成图片")
    state.history = [{"milestone_id": "m1", "action": "click", "ok": True,
                      "target": {"name": "生成图片"}}]
    state.goal_contract = contract
    completion = _completion_status(state)
    assert not completion.ready and completion.halt
    assert completion.termination_reason == TerminationReason.MILESTONE_STALLED


def test_visible_terminal_action_is_ready_for_runner() -> None:
    contract = _contract(Milestone(id="m1", type="terminal_action", intent="生成图片",
                                   target_terms=["生成图片"], execution="runner"))
    state = _state("生成图片")
    state.elements = [{"ref": "obs1:e1", "kind": "action", "role": "button",
                       "name": "Generate"}]
    state.observations[0]["elements"] = list(state.elements)
    state.goal_contract = contract
    completion = _completion_status(state)
    assert completion.ready
    assert completion.termination_reason == TerminationReason.READY_FOR_RUNNER


def test_verify_is_runner_readiness_not_success() -> None:
    contract = _contract(Milestone(id="m1", type="verify", intent="验证成功",
                                   target_terms=["成功"], execution="runner"))
    state = _state("验证成功")
    state.goal_contract = contract
    progress = derive_milestone_progress(contract, state)
    assert not progress.complete and progress.ready_for_runner


def test_verify_text_does_not_duplicate_login_obligation() -> None:
    text = _payload(
        '{"id":"m1","type":"auth","intent":"登录",'
        '"target_terms":["登录"],"field_terms":[],"value_ref":null,'
        '"execution":"explorer"},'
        '{"id":"m2","type":"verify","intent":"验证登录成功",'
        '"target_terms":["验证登录成功"],"field_terms":[],"value_ref":null,'
        '"execution":"runner"}')
    contract = parse_goal_contract(text, "登录并验证登录成功")
    assert [m.type for m in contract.milestones] == ["auth", "verify"]


def test_contract_completion_cannot_bypass_quantity_hard_gate() -> None:
    contract = _contract(Milestone(id="m1", type="action", intent="加入购物车",
                                   target_terms=["加入购物车"], execution="explorer"))
    state = _state("将前两个商品加入购物车")
    state.step_count = 2
    state.elements = [{"ref": "obs1:e1", "kind": "action", "role": "button",
                       "name": "加入购物车",
                       "identity": {"attr": "data-product-id", "value": "1"}}]
    state.observations[0]["elements"] = list(state.elements)
    state.history = [{"milestone_id": "m1", "action": "click", "ok": True,
                      "target_ref": "obs1:e1", "target": {"name": "加入购物车"}}]
    state.transitions = [{"from": "obs0", "to": "obs1", "action": "click",
                          "target_ref": "obs1:e1", "target_name": "加入购物车",
                          "milestone_id": "m1"}]
    state.goal_contract = contract
    completion = _completion_status(state)
    assert not completion.ready
    assert "数量目标未完成" in (completion.reason or "")


def test_contract_retry_runs_full_semantic_pipeline() -> None:
    calls: list[str] = []

    def llm(prompt, system_prompt=None, timeout=None):
        calls.append(prompt)
        if len(calls) == 1:
            return _payload(
                '{"id":"m1","type":"auth","intent":"登录",'
                '"target_terms":["登录"],"field_terms":[],"value_ref":null,'
                '"execution":"explorer"}')
        return _payload(
            '{"id":"m1","type":"auth","intent":"登录",'
            '"target_terms":["登录"],"field_terms":[],"value_ref":null,'
            '"execution":"explorer"},'
            '{"id":"m2","type":"action","intent":"加入购物车",'
            '"target_terms":["加入购物车"],"field_terms":[],"value_ref":null,'
            '"execution":"explorer"}')

    contract = build_goal_contract("登录后加入购物车", llm)
    assert len(calls) == 2
    assert [m.type for m in contract.milestones] == ["auth", "action"]
