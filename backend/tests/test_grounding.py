"""
══════════════════════════════════════════════════════════════════════
test_grounding.py — State Grounding Validator（G3）回归测试
══════════════════════════════════════════════════════════════════════

零依赖 plain-assert 脚本（项目无 pytest），直接运行：

    py backend/tests/test_grounding.py

覆盖（对应 backend/regressions/ 的两个 grounding regression）：
  1. SauceDemo：inventory --click 商品名--> detail 后引用 inventory ref → 拒绝
  2. AutomationExercise：list --View Product--> detail 后引用 list ref → 拒绝
  3. 正向：refs 正确跟随转移边 → 通过
  4. 编造 ref → UnknownTargetRefError
  5. observation_ref 与 target_ref 同步骤矛盾 → 拒绝
  6. 无图 / 空图 → no-op（降级路径不受影响）
  7. 不可追踪 click → current=None，后续不误拒（锁定 fail-open 行为）
  8. goto URL 尾部斜杠匹配
  9. 无 ref 步骤的 observation_ref 跨状态 → 拒绝
  10. 多转移边歧义 → fail-open
  11. from_explore_result 工厂往返（探索结果 dict → 图）+ extra=forbid
══════════════════════════════════════════════════════════════════════
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/

from grounding import (   # noqa: E402
    UnreachableObservationError,
    GraphElement, GraphObservation, GraphTransition, StateGraph,
    StateGroundingMismatchError, UnknownTargetRefError,
    validate_state_grounding,
)
from dsl import validate_case   # noqa: E402


# ── 夹具 ──────────────────────────────────────────────────────────────────────

# Regression 1 — SauceDemo：inventory（obs3）→ detail（obs4）
SAUCEDEMO_GRAPH = StateGraph(
    observations=[
        GraphObservation(
            id="obs3", url="https://www.saucedemo.com/inventory.html",
            state_hash="h3",
            elements=[
                GraphElement(ref="obs3:e17", role="link", name="Sauce Labs Backpack"),
                GraphElement(ref="obs3:e18", role="button", name="Add to cart"),
            ],
        ),
        GraphObservation(
            id="obs4", url="https://www.saucedemo.com/inventory-item.html?id=4",
            state_hash="h4",
            elements=[GraphElement(ref="obs4:e1", role="button", name="Add to cart")],
        ),
    ],
    transitions=[
        GraphTransition(from_="obs3", action="click", target_ref="obs3:e17", to="obs4"),
    ],
)

# Regression 2 — AutomationExercise：list（obs1）→ detail（obs2）
AUTOMATIONEXERCISE_GRAPH = StateGraph(
    observations=[
        GraphObservation(
            id="obs1", url="https://automationexercise.com/products",
            state_hash="h1",
            elements=[
                GraphElement(ref="obs1:e1", role="link", name="View Product"),
                GraphElement(ref="obs1:e2", role="link", name="Add to cart"),
            ],
        ),
        GraphObservation(
            id="obs2", url="https://automationexercise.com/product_details/1",
            state_hash="h2",
            elements=[GraphElement(ref="obs2:e1", role="button", name="Add to cart")],
        ),
    ],
    transitions=[
        GraphTransition(from_="obs1", action="click", target_ref="obs1:e1", to="obs2"),
    ],
)


def _case(steps: list[dict]):
    return validate_case({"name": "t", "steps": steps})


def _expect_mismatch(case, graph, step_index, expected, actual):
    try:
        validate_state_grounding(case, graph)
    except StateGroundingMismatchError as exc:
        assert exc.step_index == step_index, f"step_index={exc.step_index} != {step_index}"
        assert exc.expected == expected, f"expected={exc.expected} != {expected}"
        assert exc.actual == actual, f"actual={exc.actual} != {actual}"
        return
    raise AssertionError("未抛出 StateGroundingMismatchError")


def _expect_unknown_ref(case, graph, step_index, ref):
    try:
        validate_state_grounding(case, graph)
    except UnknownTargetRefError as exc:
        assert exc.step_index == step_index, f"step_index={exc.step_index} != {step_index}"
        assert exc.ref == ref, f"ref={exc.ref} != {ref}"
        return
    raise AssertionError("未抛出 UnknownTargetRefError")


# ── 回归测试 ──────────────────────────────────────────────────────────────────

def test_saucedemo_regression_rejected():
    """Regression 1：detail 后引用 inventory 的 add-to-cart ref → 执行前拒绝。"""
    case = _case([
        {"action": "goto", "value": "https://www.saucedemo.com/inventory.html"},
        {"action": "click", "target_ref": "obs3:e17",
         "target": {"role": "link", "name": "Sauce Labs Backpack"}},
        {"action": "click", "target_ref": "obs3:e18",
         "target": {"role": "button", "name": "Add to cart"}},
    ])
    _expect_mismatch(case, SAUCEDEMO_GRAPH, step_index=3, expected="obs4", actual="obs3")


def test_automationexercise_regression_rejected():
    """Regression 2：detail 后引用 list 的 Add to cart → 执行前拒绝。"""
    case = _case([
        {"action": "goto", "value": "https://automationexercise.com/products"},
        {"action": "click", "target_ref": "obs1:e1",
         "target": {"role": "link", "name": "View Product"}},
        {"action": "click", "target_ref": "obs1:e2",
         "target": {"role": "link", "name": "Add to cart"}},
    ])
    _expect_mismatch(case, AUTOMATIONEXERCISE_GRAPH, step_index=3, expected="obs2", actual="obs1")


def test_happy_path_refs_follow_edges():
    """正向：refs 正确跟随转移边（inventory → detail → detail 内操作）→ 通过。"""
    case = _case([
        {"action": "goto", "value": "https://www.saucedemo.com/inventory.html"},
        {"action": "click", "target_ref": "obs3:e17",
         "target": {"role": "link", "name": "Sauce Labs Backpack"}},
        {"action": "click", "target_ref": "obs4:e1",
         "target": {"role": "button", "name": "Add to cart"}},
        {"action": "assert_url", "value": "/cart.html"},
    ])
    validate_state_grounding(case, SAUCEDEMO_GRAPH)   # 不抛异常即通过


def test_fabricated_ref_rejected():
    """编造 ref（不存在于图）→ UnknownTargetRefError（硬拒绝，不清空）。"""
    case = _case([
        {"action": "goto", "value": "https://www.saucedemo.com/inventory.html"},
        {"action": "click", "target_ref": "obs3:e99",
         "target": {"role": "button", "name": "Add to cart"}},
    ])
    _expect_unknown_ref(case, SAUCEDEMO_GRAPH, step_index=2, ref="obs3:e99")


def test_observation_ref_contradiction_rejected():
    """同一步骤 grounding 证据矛盾（target_ref=obs3、observation_ref=obs4）→ 拒绝。"""
    case = _case([
        {"action": "goto", "value": "https://www.saucedemo.com/inventory.html"},
        {"action": "click", "target_ref": "obs3:e17", "observation_ref": "obs4",
         "target": {"role": "link", "name": "Sauce Labs Backpack"}},
    ])
    # expected = 当前推导状态（obs3）；actual = ref 所属（obs3），矛盾在 observation_ref=obs4
    _expect_mismatch(case, SAUCEDEMO_GRAPH, step_index=2, expected="obs3", actual="obs3")


def test_no_graph_is_noop():
    """graph=None / 空图 → no-op（无探索降级生成路径不受影响）。"""
    bad_case = _case([
        {"action": "goto", "value": "https://www.saucedemo.com/inventory.html"},
        {"action": "click", "target_ref": "obs3:e99",
         "target": {"role": "button", "name": "Add to cart"}},
    ])
    validate_state_grounding(bad_case, None)                       # 不抛
    validate_state_grounding(bad_case, StateGraph())               # 空图不抛
    no_ref_case = _case([
        {"action": "goto", "value": "https://www.saucedemo.com/inventory.html"},
        {"action": "click", "target": {"role": "link", "name": "Sauce Labs Backpack"}},
    ])
    validate_state_grounding(no_ref_case, SAUCEDEMO_GRAPH)         # 无 ref 不抛


def test_untraceable_click_fail_open():
    """不可追踪 click（无转移边）→ current=None → 后续不误拒（fail-open）。"""
    graph_without_edges = StateGraph(
        observations=SAUCEDEMO_GRAPH.observations, transitions=[],
    )
    case = _case([
        {"action": "goto", "value": "https://www.saucedemo.com/inventory.html"},
        {"action": "click", "target_ref": "obs3:e17",
         "target": {"role": "link", "name": "Sauce Labs Backpack"}},   # 无转移边
        {"action": "click", "target_ref": "obs3:e18",
         "target": {"role": "button", "name": "Add to cart"}},         # 不可证伪 → 放行
    ])
    validate_state_grounding(case, graph_without_edges)


def test_click_without_ref_fail_open():
    """无 ref 的 click 不可追踪 → 后续 ref 校验放行（fail-open）。"""
    case = _case([
        {"action": "goto", "value": "https://www.saucedemo.com/inventory.html"},
        {"action": "click", "target": {"role": "link", "name": "Sauce Labs Backpack"}},
        {"action": "click", "target_ref": "obs3:e18",
         "target": {"role": "button", "name": "Add to cart"}},
    ])
    validate_state_grounding(case, SAUCEDEMO_GRAPH)


def test_goto_url_trailing_slash_matches():
    """goto URL 尾部斜杠归一化匹配 → current=obs3（用跨状态错误证明匹配成功）。"""
    case = _case([
        {"action": "goto", "value": "https://www.saucedemo.com/inventory.html/"},
        {"action": "click", "target_ref": "obs4:e1",
         "target": {"role": "button", "name": "Add to cart"}},
    ])
    _expect_mismatch(case, SAUCEDEMO_GRAPH, step_index=2, expected="obs3", actual="obs4")


def test_no_ref_step_observation_ref_mismatch():
    """无 ref 步骤声明了跨状态 observation_ref → 拒绝（同一错位模式）。"""
    case = _case([
        {"action": "goto", "value": "https://www.saucedemo.com/inventory.html"},
        {"action": "click", "target_ref": "obs3:e17",
         "target": {"role": "link", "name": "Sauce Labs Backpack"}},   # → obs4
        {"action": "assert_text", "value": "Sauce Labs Backpack", "observation_ref": "obs3"},
    ])
    _expect_mismatch(case, SAUCEDEMO_GRAPH, step_index=3, expected="obs4", actual="obs3")


def test_ambiguous_edges_fail_open():
    """同一 (from, ref) 多条边指向不同 to → 不可追踪，后续不误拒。"""
    graph = StateGraph(
        observations=SAUCEDEMO_GRAPH.observations,
        transitions=[
            GraphTransition(from_="obs3", action="click", target_ref="obs3:e17", to="obs4"),
            GraphTransition(from_="obs3", action="click", target_ref="obs3:e17", to="obs5"),
        ],
    )
    case = _case([
        {"action": "goto", "value": "https://www.saucedemo.com/inventory.html"},
        {"action": "click", "target_ref": "obs3:e17",
         "target": {"role": "link", "name": "Sauce Labs Backpack"}},
        {"action": "click", "target_ref": "obs3:e18",
         "target": {"role": "button", "name": "Add to cart"}},
    ])
    validate_state_grounding(case, graph)


def test_from_explore_result_roundtrip():
    """explore_result dict → StateGraph 工厂：text 节点 type 丢弃、from 别名、往返校验。"""
    explore_result = {
        "observations": [
            {
                "id": "obs1", "url": "https://x.com/list", "state_hash": "abc",
                "title": "列表页",                    # 多余字段应被工厂丢弃
                "snapshot": "- button \"Buy\"",       # 不进图
                "elements": [
                    {"ref": "obs1:e1", "role": "link", "name": "Buy"},
                    {"ref": "obs1:e2", "type": "text", "text": "Products"},
                ],
            },
        ],
        "transitions": [
            {"from": "obs1", "action": "click", "target_ref": "obs1:e1", "to": "obs2"},
        ],
    }
    graph = StateGraph.from_explore_result(explore_result)
    assert len(graph.observations) == 1 and len(graph.transitions) == 1
    assert graph.observations[0].elements[0].ref == "obs1:e1"
    assert graph.observations[0].elements[1].text == "Products"   # type 字段已丢弃
    assert graph.transitions[0].from_ == "obs1" and graph.transitions[0].to == "obs2"

    # 往返后 Validator 正常工作：正例通过 / 编造 ref 拒绝
    ok_case = _case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target_ref": "obs1:e1", "target": {"role": "link", "name": "Buy"}},
    ])
    validate_state_grounding(ok_case, graph)
    bad_case = _case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target_ref": "obs1:e99", "target": {"role": "link", "name": "Buy"}},
    ])
    _expect_unknown_ref(bad_case, graph, step_index=2, ref="obs1:e99")


def test_orphan_observation_unreachable_rejected():
    """孤儿状态（obs5 被观察到但无 incoming 转移边）→ 引用被拒。

    评审收紧（BFC 实测）：obs5 无入边——若把"入度为零"当入口会被误判
    为可达 → 悬空引用放行。入口必须是探索起点（observations[0]）。
    """
    graph = StateGraph(
        observations=[
            GraphObservation(id="obs1", url="https://x.com", state_hash="h1",
                             elements=[GraphElement(ref="obs1:e1", role="link", name="Buy")]),
            GraphObservation(id="obs2", url="https://x.com/detail", state_hash="h2",
                             elements=[GraphElement(ref="obs2:e1", role="button", name="Buy")]),
            GraphObservation(id="obs5", url="https://x.com/detail", state_hash="h5",
                             elements=[GraphElement(ref="obs5:e30", role="button", name="View Cart")]),
        ],
        transitions=[
            GraphTransition(from_="obs1", action="click", target_ref="obs1:e1", to="obs2"),
        ],
    )
    case = _case([
        {"action": "goto", "value": "https://x.com"},
        {"action": "click", "target_ref": "obs1:e1", "target": {"role": "link", "name": "Buy"}},
        {"action": "wait_for", "target_ref": "obs5:e30",
         "target": {"role": "button", "name": "View Cart"}},
    ])
    try:
        validate_state_grounding(case, graph)
        raise AssertionError("孤儿状态引用必须被拒")
    except UnreachableObservationError as exc:
        assert exc.step_index == 3 and exc.obs_id == "obs5"


def test_reachable_chain_passes():
    """入口 → 转移链上的状态全部可达 → 不误拒。"""
    graph = StateGraph(
        observations=[
            GraphObservation(id="obs1", url="https://x.com", state_hash="h1",
                             elements=[GraphElement(ref="obs1:e1", role="link", name="Buy")]),
            GraphObservation(id="obs2", url="https://x.com/detail", state_hash="h2",
                             elements=[GraphElement(ref="obs2:e1", role="button", name="Buy")]),
        ],
        transitions=[
            GraphTransition(from_="obs1", action="click", target_ref="obs1:e1", to="obs2"),
        ],
    )
    case = _case([
        {"action": "goto", "value": "https://x.com"},
        {"action": "click", "target_ref": "obs1:e1", "target": {"role": "link", "name": "Buy"}},
        {"action": "click", "target_ref": "obs2:e1", "target": {"role": "button", "name": "Buy"}},
    ])
    validate_state_grounding(case, graph)   # 不抛


def test_graph_models_extra_forbid():
    """extra=forbid：缓存文件损坏/未知字段 → 校验错误，不静默丢弃。"""
    from pydantic import ValidationError
    try:
        GraphTransition.model_validate({
            "from": "a", "action": "click", "target_ref": "x", "to": "b", "junk": 1,
        })
    except ValidationError:
        return
    raise AssertionError("GraphTransition 未拒绝未知字段")


# ── 运行入口 ──────────────────────────────────────────────────────────────────

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
        except Exception as exc:   # 断言失败/异常都算失败
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
