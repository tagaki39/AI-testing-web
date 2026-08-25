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
import tempfile
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


# ── 7. 探索 prompt 契约（真实 E2E 发现的 bug 防回归）────────────────────────

def test_explore_prompt_example_ref_is_state_scoped():
    """DECIDE_PROMPT 示例必须用 state-scoped ref（obs1:e1）。

    真实 E2E 踩坑：示例写页面级 e1，探索决策全部被 _decide 严格校验
    拒绝（元素表里是 obs1:e1）→ 探索 0 步夭折，Planner 只看得到首页。
    """
    from explore import DECIDE_PROMPT
    assert '"target_ref": "obs1:e1"' in DECIDE_PROMPT


def test_credential_redaction_chinese_phrasing():
    """用 X / Y 登录 的中文写法必须命中凭据提取。

    真实 E2E 踩坑：该写法此前不在匹配模式内 → 凭据未脱敏（泄漏进 LLM
    上下文）、探索无登录凭据、Planner 编造 ref 被 Validator 拒绝。
    """
    from ai_agent import _extract_and_redact_goal
    redacted, runtime = _extract_and_redact_goal(
        "打开 saucedemo.com，用 standard_user / secret_sauce 登录，"
        "把第一个商品加入购物车"
    )
    assert runtime == {"username": "standard_user", "password": "secret_sauce"}
    assert "standard_user" not in redacted and "secret_sauce" not in redacted
    assert "${username}" in redacted and "${password}" in redacted


def test_origin_guard_tolerates_www_redirect():
    """www/非 www 重定向不得误判为跨域。

    真实 E2E：入口 saucedemo.com 302 → www.saucedemo.com，严格 netloc
    相等把初始重定向当跨域 → go_back 落在 about:blank → 探索彻底失效。
    """
    from explore import _within_origin
    assert _within_origin("https://www.saucedemo.com/", "https://saucedemo.com")
    assert _within_origin("https://saucedemo.com/", "https://www.saucedemo.com")
    assert _within_origin("https://www.saucedemo.com/inventory.html", "https://saucedemo.com")
    assert not _within_origin("https://evil.com/", "https://saucedemo.com")
    assert not _within_origin("https://saucedemo.com.evil.com/", "https://saucedemo.com")


def test_credential_redaction_english_phrasing():
    """login with email / password 写法：邮箱与密码都提取、都脱敏。"""
    from ai_agent import _extract_and_redact_goal
    redacted, runtime = _extract_and_redact_goal(
        "login with test123@example.com / test123 on automationexercise"
    )
    assert runtime["email"] == "test123@example.com"
    assert runtime["password"] == "test123"
    assert "test123@example.com" not in redacted
    assert "test123" not in redacted


# ── I1：实例身份（GraphElement 扩展 + scope 编译）────────────────────────────

def test_graph_element_identity_fields_roundtrip():
    """verified / scope_has_text 字段往返；旧缓存形态（缺字段）取默认值。"""
    explore_result = {
        "observations": [{
            "id": "obs1", "url": "https://x.com/", "state_hash": "h",
            "elements": [
                {"ref": "obs1:e1", "role": "button", "name": "Buy",
                 "verified": True, "scope_has_text": "Blue Top"},
                {"ref": "obs1:e2", "type": "text", "text": "Products"},   # 旧形态
            ],
        }],
        "transitions": [],
    }
    graph = StateGraph.from_explore_result(explore_result)
    e1 = graph.observations[0].elements[0]
    assert e1.verified is True and e1.scope_has_text == "Blue Top"
    e2 = graph.observations[0].elements[1]
    assert e2.verified is False and e2.scope_has_text is None


def _dup_graph(with_anchors: bool) -> StateGraph:
    return StateGraph(observations=[
        GraphObservation(
            id="obs1", url="https://x.com/list", state_hash="h",
            elements=[
                GraphElement(ref="obs1:e1", role="button", name="Buy",
                             scope_has_text="Blue Top" if with_anchors else None),
                GraphElement(ref="obs1:e2", role="button", name="Buy",
                             scope_has_text="Red Top" if with_anchors else None),
                GraphElement(ref="obs1:e3", role="link", name="Home"),
            ],
        ),
    ])


def test_compile_attaches_scope_for_duplicates():
    """同名重复 + 锚点 → 编译附加 Scope(has_text)；唯一元素不附加。"""
    case = validate_case({"name": "t", "steps": [
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target_ref": "obs1:e1"},
        {"action": "click", "target_ref": "obs1:e3"},
    ]})
    stats = {}
    compiled = compile_targets(case, _dup_graph(with_anchors=True), stats=stats)
    assert compiled.steps[1].scope is not None
    assert compiled.steps[1].scope.has_text == "Blue Top"
    assert compiled.steps[2].scope is None          # 唯一元素不附加（scope 最小化）
    assert stats["scoped_compiled"] == 1 and stats["unscoped_duplicates"] == []


def test_compile_duplicate_without_anchor():
    """重复但无锚点（容器外）→ 不附加 scope，stats 记录 ref（L1 输入）。"""
    case = validate_case({"name": "t", "steps": [
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target_ref": "obs1:e1"},
    ]})
    stats = {}
    compiled = compile_targets(case, _dup_graph(with_anchors=False), stats=stats)
    assert compiled.steps[1].scope is None
    assert stats["scoped_compiled"] == 0
    assert stats["unscoped_duplicates"] == ["obs1:e1"]


def test_compile_verified_is_not_a_bypass():
    """verified 是证据不是豁免：重复元素无锚点时即使 verified 也不附加。"""
    graph = _dup_graph(with_anchors=False)
    graph.observations[0].elements[0].verified = True
    case = validate_case({"name": "t", "steps": [
        {"action": "goto", "value": "https://x.com/list"},
        {"action": "click", "target_ref": "obs1:e1"},
    ]})
    compiled = compile_targets(case, graph)
    assert compiled.steps[1].scope is None


# ── GQ：生成链路可靠性（finish 完整性校验 + 目标覆盖检查）─────────────────────

def test_goal_requires_actions():
    """动作表命中判定（中文/英文/大小写）。"""
    from explore import goal_requires_actions
    assert goal_requires_actions("把第一个商品加入购物车") is True
    assert goal_requires_actions("Add to Cart and verify") is True
    assert goal_requires_actions("用 x / y 登录后验证") is True
    assert goal_requires_actions("验证页面包含文字 Example Domain") is False


def test_validate_completion_exemption_and_gate():
    """完成校验：无操作目标豁免；有操作目标 <2 步或目标动作未探索 → 拒绝。"""
    from explore import ExploreState, _validate_completion
    s = ExploreState(goal="验证页面包含文字 Example Domain", entry_url="https://x.com")
    assert _validate_completion(s) is None          # example.com 单页 0 步豁免
    s = ExploreState(goal="加入购物车", entry_url="https://x.com")
    s.step_count = 1
    assert _validate_completion(s) is not None      # 1 步宣告 → 拒绝
    s.step_count = 2
    assert _validate_completion(s) is not None      # ≥2 步但 Add to cart 未探索 → 拒绝
    s.history = [{"action": "click", "target_ref": "obs3:e22",
                  "target": {"role": "link", "name": "Add to cart"}}]
    assert _validate_completion(s) is None          # 目标动作已探索 → 通过


def test_check_goal_coverage_detects_missing_click():
    """9/10 案例：断言可见性不算覆盖，必须存在对应 click。"""
    from ai_agent import _check_goal_coverage
    assert_visible_only = validate_case({"name": "t", "steps": [
        {"action": "goto", "value": "https://x.com"},
        {"action": "assert_visible", "target": {"role": "button", "name": "Add to cart"}},
    ]})
    missing = _check_goal_coverage("把商品加入购物车并验证", assert_visible_only)
    assert missing == ["add_to_cart"]

    full = validate_case({"name": "t", "steps": [
        {"action": "goto", "value": "https://x.com"},
        {"action": "click", "target": {"role": "button", "name": "Add to cart"}},
    ]})
    assert _check_goal_coverage("把商品加入购物车并验证", full) == []

    login_case = validate_case({"name": "t", "steps": [
        {"action": "goto", "value": "https://x.com"},
        {"action": "click", "target": {"role": "button", "name": "Login"}},
    ]})
    assert _check_goal_coverage("登录后验证", login_case) == []
    assert "login" in _check_goal_coverage("登录后验证", assert_visible_only)

    # 无动作目标 → fail-open 不检查
    assert _check_goal_coverage("验证页面包含文字 Example Domain", assert_visible_only) == []


# ── GQ2：质量门硬失败 + 自愈重生 ─────────────────────────────────────────────

def _reset_anti_patterns(tmpdir: str) -> None:
    import anti_patterns
    anti_patterns.STORE_FILE = Path(tmpdir) / "anti_patterns.json"
    anti_patterns._memory.clear()
    anti_patterns._loaded = False


def test_anti_patterns_store():
    """record / 去重 / 每 code 上限 / list_for 过滤 / 持久化。"""
    import anti_patterns
    tmp = tempfile.mkdtemp(prefix="ap_test_")
    _reset_anti_patterns(tmp)
    anti_patterns.record("missing_step", "计划: click(Cart) | 缺失 add_to_cart")
    anti_patterns.record("missing_step", "计划: click(Cart) | 缺失 add_to_cart")   # 去重
    anti_patterns.record("invalid_ref", "错误: 未知 target_ref obs2:e99")
    assert len(anti_patterns.list_for("missing_step")) == 1
    assert len(anti_patterns.list_for("invalid_ref")) == 1
    assert anti_patterns.list_for("invalid_structure") == []
    for i in range(6):   # 超上限：裁最旧，保留最近 5
        anti_patterns.record("missing_step", f"摘要 {i}")
    assert len(anti_patterns.list_for("missing_step")) == 5
    assert anti_patterns.list_for("missing_step")[0] == "摘要 5"
    # 持久化往返（模拟进程重启）
    anti_patterns._memory.clear()
    anti_patterns._loaded = False
    assert len(anti_patterns.list_for("missing_step")) == 5


def test_build_retry_hint():
    """重生提示只带失败原因（R4：不做负例 few-shot 注入）。"""
    from ai_agent import _build_retry_hint
    hint = _build_retry_hint("目标要求 add_to_cart 动作")
    assert "add_to_cart" in hint
    assert "重新规划提示" in hint and "完整修正后的 JSON" in hint


def test_goal_coverage_error_and_reason_mapping():
    """GoalCoverageError 携带缺失清单与失败计划；错误 → 反模式原因码。"""
    from ai_agent import GoalCoverageError, _failure_reason_code
    from grounding import UnknownTargetRefError
    case = validate_case({"name": "t", "steps": [
        {"action": "goto", "value": "https://x.com"},
    ]})
    exc = GoalCoverageError(["add_to_cart"], case)
    assert exc.missing == ["add_to_cart"] and exc.case is case
    assert _failure_reason_code(exc) == "missing_step"
    assert _failure_reason_code(UnknownTargetRefError(1, "obs2:e99")) == "invalid_ref"
    assert _failure_reason_code(ValueError("bad json")) == "invalid_structure"


def test_plan_summary_sanitized():
    """反模式摘要脱敏：不含 value 明文（密码等）。"""
    from ai_agent import _plan_summary
    case = validate_case({"name": "t", "steps": [
        {"action": "goto", "value": "https://x.com"},
        {"action": "fill", "target": {"role": "textbox", "name": "Password"},
         "value": "secret_sauce"},
    ]})
    summary = _plan_summary(case, "目标要求 add_to_cart 动作")
    assert "Password" in summary and "add_to_cart" in summary
    assert "secret_sauce" not in summary   # value 明文绝不入反模式


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
