"""
══════════════════════════════════════════════════════════════════════
test_compiler.py — G3 refs-only Planner + R1 LocatorSpec Compiler 测试
══════════════════════════════════════════════════════════════════════

零依赖 plain-assert 脚本，直接运行：

    py backend/tests/test_compiler.py

覆盖：
  1. schema：ref-only 步骤通过校验；target/ref 皆无 → ValidationError
  2. compile_targets：role 元素 → Locator(role,name)；text 节点 →
     Locator(text)；覆盖 Planner 手写 target；未知 ref 拒绝；
     编译产物通过 validate_case + validate_state_grounding
  3. check_refs_only：无 ref 的定位步骤 / 携带 target → ValueError；
     合规计划通过
  4. _target_key 含 ref：同 action+value 不同 ref 的断言不误去重
  5. ensure_executable_targets：ref-only 步骤执行前被拒；编译后通过
  6. 全链路：refs-only 计划 → 编译 → grounding → 可执行
══════════════════════════════════════════════════════════════════════
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/

from pydantic import ValidationError   # noqa: E402

from ai_agent import check_refs_only, _normalize_steps   # noqa: E402
from compiler import compile_targets   # noqa: E402
from dsl import Locator, validate_case   # noqa: E402
from grounding import (   # noqa: E402
    GraphElement, GraphObservation, GraphTransition, StateGraph,
    UnknownTargetRefError, validate_state_grounding,
)
from runner import ensure_executable_targets   # noqa: E402


# ── 夹具（与 test_grounding.py 同形的最小图）───────────────────────────────────

GRAPH = StateGraph(
    observations=[
        GraphObservation(
            id="obs1", url="https://x.com/list", state_hash="h1",
            elements=[
                GraphElement(ref="obs1:e1", role="link", name="Buy"),
                GraphElement(ref="obs1:e2", role="textbox", name="Search"),
                GraphElement(ref="obs1:e3", text="Products"),
            ],
        ),
        GraphObservation(
            id="obs2", url="https://x.com/cart", state_hash="h2",
            elements=[GraphElement(ref="obs2:e1", role="button", name="Checkout")],
        ),
    ],
    transitions=[
        GraphTransition(from_="obs1", action="click", target_ref="obs1:e1", to="obs2"),
    ],
)


def _case(steps: list[dict]):
    return validate_case({"name": "t", "steps": steps})


# ── 1. schema：ref-only 步骤 ─────────────────────────────────────────────────

def test_schema_accepts_ref_only_steps():
    """click/fill/wait_for 等只有 target_ref 的步骤通过校验（G3 契约）。"""
    case = _case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target_ref": "obs1:e1"},
        {"action": "fill", "target_ref": "obs1:e2", "value": "hello"},
        {"action": "wait_for", "target_ref": "obs2:e1"},
        {"action": "assert_visible", "target_ref": "obs2:e1"},
        {"action": "assert_text", "value": "ok", "target_ref": "obs2:e1"},
    ])
    assert len(case.steps) == 6


def test_schema_rejects_neither_target_nor_ref():
    """target 与 target_ref 皆无 → ValidationError（安全边界不放松）。"""
    for steps in (
        [{"action": "goto", "value": "https://x.com"},
         {"action": "click"}],
        [{"action": "goto", "value": "https://x.com"},
         {"action": "fill", "value": "x"}],
        [{"action": "goto", "value": "https://x.com"},
         {"action": "assert_visible"}],
    ):
        try:
            _case(steps)
        except ValidationError:
            continue
        raise AssertionError(f"未拒绝: {steps}")


# ── 2. compile_targets ───────────────────────────────────────────────────────

def test_compile_role_and_text_elements():
    """可交互元素 → Locator(role,name)；文本节点 → Locator(text)。"""
    case = _case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target_ref": "obs1:e1"},
        {"action": "assert_text", "value": "Products", "target_ref": "obs1:e3"},
    ])
    compiled = compile_targets(case, GRAPH)
    assert compiled.steps[1].target == Locator(role="link", name="Buy")
    assert compiled.steps[2].target == Locator(text="Products")
    # target_ref 保留（provenance），编译后仍是合法 DSL
    assert compiled.steps[1].target_ref == "obs1:e1"
    validate_case(compiled.model_dump())


def test_compile_overwrites_planner_target():
    """Planner 手写的 target 被覆盖（确定性 > Planner，grounding 完整性）。"""
    case = _case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target_ref": "obs1:e1",
         "target": {"role": "link", "name": "买它"}},   # Planner 手写，不可信
    ])
    compiled = compile_targets(case, GRAPH)
    assert compiled.steps[1].target == Locator(role="link", name="Buy")


def test_compile_unknown_ref_rejected():
    """未知 ref → UnknownTargetRefError（编译器与 Validator 同一拒绝语义）。"""
    case = _case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target_ref": "obs9:e99"},
    ])
    try:
        compile_targets(case, GRAPH)
    except UnknownTargetRefError as exc:
        assert exc.step_index == 2 and exc.ref == "obs9:e99"
        return
    raise AssertionError("未抛出 UnknownTargetRefError")


def test_compile_empty_graph_noop():
    """空图 → 原样返回（legacy 降级路径不做编译）。"""
    case = _case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target": {"role": "link", "name": "Buy"}},
    ])
    assert compile_targets(case, StateGraph()) is case
    assert compile_targets(case, None) is case


# ── 3. check_refs_only ───────────────────────────────────────────────────────

def test_check_refs_only_rejects_missing_ref():
    """定位类动作无 target_ref → ValueError（进入 recovery）。"""
    case = _case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target": {"role": "link", "name": "Buy"}},
    ])
    try:
        check_refs_only(case)
    except ValueError as exc:
        assert "步骤 2" in str(exc)
        return
    raise AssertionError("未抛出 ValueError")


def test_check_refs_only_rejects_forbidden_fields():
    """携带 target/scope（即使同时有 ref）→ ValueError。"""
    case = _case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target_ref": "obs1:e1",
         "target": {"role": "link", "name": "Buy"}},
    ])
    try:
        check_refs_only(case)
    except ValueError as exc:
        assert "禁止生成 target/scope" in str(exc)
        return
    raise AssertionError("未抛出 ValueError")


def test_check_refs_only_accepts_compliant_plan():
    """合规 refs-only 计划通过；断言类步骤允许无 ref（页面级）。"""
    case = _case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target_ref": "obs1:e1"},
        {"action": "fill", "target_ref": "obs1:e2", "value": "x"},
        {"action": "assert_text", "value": "整页文本"},
        {"action": "assert_url", "value": "/cart"},
    ])
    check_refs_only(case)   # 不抛即通过


# ── 4. _target_key 含 ref（断言去重不误删）────────────────────────────────────

def test_target_key_uses_ref():
    """refs-only 步骤的归一化键必须含 ref（target 编译前为空）。"""
    from ai_agent import _target_key
    case = _case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "assert_text", "value": "ok", "target_ref": "obs1:e1"},
        {"action": "assert_text", "value": "ok", "target_ref": "obs1:e2"},
    ])
    assert _target_key(case.steps[1]) == "ref:obs1:e1"
    assert _target_key(case.steps[2]) == "ref:obs1:e2"


def test_normalize_keeps_distinct_ref_assertions():
    """同 action+value 不同 ref 的两条断言都保留；同 ref 重复才去重。"""
    case = _case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "assert_text", "value": "ok", "target_ref": "obs1:e1"},
        {"action": "assert_text", "value": "ok", "target_ref": "obs1:e2"},
        {"action": "assert_text", "value": "ok", "target_ref": "obs1:e2"},
    ])
    normalized, removed = _normalize_steps(case)
    assert removed == [4]                     # 只有完全重复的步骤 4 被删
    assert len(normalized.steps) == 3


# ── 5. ensure_executable_targets ─────────────────────────────────────────────

def test_executable_guard_rejects_ref_only():
    """执行前防线：ref-only 步骤（target 缺失）→ ValueError。"""
    case = _case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target_ref": "obs1:e1"},
    ])
    try:
        ensure_executable_targets(case)
    except ValueError as exc:
        assert "步骤 2" in str(exc) and "未编译" in str(exc)
        return
    raise AssertionError("未抛出 ValueError")


def test_executable_guard_accepts_compiled_case():
    """编译后的用例（target 已填入）通过执行前防线。"""
    case = compile_targets(_case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target_ref": "obs1:e1"},
        {"action": "click", "target_ref": "obs2:e1"},
    ]), GRAPH)
    ensure_executable_targets(case)   # 不抛即通过


# ── 6. 全链路：refs-only 计划 → 编译 → grounding → 可执行 ────────────────────

def test_pipeline_end_to_end():
    """生成链路的完整序列（ai_agent.generate_dsl 的校验部分）：
       refs-only 计划 → compile_targets → validate_state_grounding →
       ensure_executable_targets，全部通过。"""
    case = _case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "fill", "target_ref": "obs1:e2", "value": "hello"},
        {"action": "click", "target_ref": "obs1:e1"},
        {"action": "assert_text", "value": "Checkout", "target_ref": "obs2:e1"},
    ])
    check_refs_only(case)
    case = compile_targets(case, GRAPH)
    validate_state_grounding(case, GRAPH)
    ensure_executable_targets(case)

    # 跨状态错位在同一管线中被拒绝（衔接 G3 回归）
    bad_case = _case([
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target_ref": "obs1:e1"},          # → obs2
        {"action": "click", "target_ref": "obs1:e2"},          # 仍引用 obs1
    ])
    from grounding import StateGroundingMismatchError
    try:
        validate_state_grounding(compile_targets(bad_case, GRAPH), GRAPH)
    except StateGroundingMismatchError as exc:
        assert exc.step_index == 3
        return
    raise AssertionError("跨状态错位未被拒绝")


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
        except Exception as exc:
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
