"""
══════════════════════════════════════════════════════════════════════
compiler.py — LocatorSpec Compiler（R1：NodeRef → LocatorSpec）
══════════════════════════════════════════════════════════════════════

【这个文件在项目中的位置】
  Architecture v2 管线的独立阶段（ROADMAP §4）：

    Planner refs-only → State Grounding Validator →【LocatorSpec Compiler：这里】→ ...

  职责：target_ref（如 obs3:e17）→ 稳定 locator（Locator 模型），
  由代码确定性生成——Planner 只选 ref，不生成 role/name/scope。

【I1 实例身份（新增）】
  observation 内同名重复的元素（如 6 个 Add to cart）编译时附加 scope
  消歧证据：探索期采集的容器文本锚点（scope_has_text）→ Scope(has_text=...)。
  - 同名唯一 → 不附加（scope 最小化原则）
  - 同名重复 + 有锚点 → 附加 scope（确定性编译的消歧证据）
  - 同名重复 + 无锚点（容器外元素）→ 不附加，运行时诚实拒绝；
    计入 stats.unscoped_duplicates（可观测 + L1 corrections 输入）
  verified 是证据不是豁免——不影响本编译逻辑，只进 metrics。

【核心思想】
  Planner grounding: "想操作谁？" → refs（obs3:e17）
  Compiler: "DOM 里谁对应它？"  → 从观察到的元素数据确定性编译 Locator
  两者职责分离后，locator 不再依赖 LLM 的"想象力"——
  元素是系统真实观察到的（explore 的 element ref 表），
  编译只是查表，零幻觉、零波动。

【学习路径】
  compile_targets（主入口）→ _element_to_locator（单元素映射）
══════════════════════════════════════════════════════════════════════
"""

from dsl import DSLCase, Locator, Scope, validate_case
from grounding import StateGraph, UnknownTargetRefError


def _element_to_locator(element) -> Locator:
    """图元素 → Locator（确定性映射，无任何启发式）。

    可交互元素（role+name）→ 语义定位（最稳，官方推荐）
    文本节点（text）       → 文本定位（无语义元素的兜底）
    """
    if element.role:
        # A4.2：稳定 identity（data-product-id 等）确定性编译进 Locator——
        # 由观察元素携带（GraphElement.identity），LLM 不生成。
        identity = getattr(element, "identity", None)
        return Locator(role=element.role, name=element.name, identity=identity)
    return Locator(text=element.text)


def _element_key(element) -> tuple | None:
    """元素的归一化身份键（用于 observation 内重复计数）。"""
    if element.role and element.name:
        return ("role", element.role, element.name)
    if element.text:
        return ("text", element.text)
    return None


def compile_targets(case: DSLCase, graph: StateGraph,
                    stats: dict | None = None) -> DSLCase:
    """target_ref → target 确定性编译（R1 + I1 实例身份）。

    规则：
      - 每个带 target_ref 的步骤，从 graph 元素表查 ref：
        未知 ref → UnknownTargetRefError（编译器与 Validator 同一拒绝语义）
      - 覆盖步骤中已有的 target（Planner 手写的定位字段不可信——
        确定性 > Planner，保证 grounding 完整性）
      - I1：observation 内同名重复且元素有容器锚点 → 附加 Scope(has_text)
      - 结束后重新 validate_case（编译产物必须合法 DSL）

    stats（可选 out 参数）：{"scoped_compiled": int, "unscoped_duplicates": [ref]}
    graph 为空 → 原样返回（无探索的 legacy 降级路径不做编译）。
    """
    if stats is not None:
        stats.update({"scoped_compiled": 0, "unscoped_duplicates": []})
    if graph is None or not graph.observations:
        return case

    ref_map = {
        e.ref: e
        for o in graph.observations
        for e in o.elements
    }

    # I1：observation 内同名重复计数（scope 编译的依据）
    duplicate_keys: set[tuple] = set()
    for o in graph.observations:
        counts: dict[tuple, int] = {}
        for e in o.elements:
            key = _element_key(e)
            if key:
                counts[key] = counts.get(key, 0) + 1
        duplicate_keys.update(k for k, c in counts.items() if c > 1)

    for index, step in enumerate(case.steps, start=1):
        ref = step.target_ref
        if ref is None:
            continue
        element = ref_map.get(ref)
        if element is None:
            raise UnknownTargetRefError(index, ref)
        step.target = _element_to_locator(element)   # 覆盖，不信任 Planner 手写

        # I1：同名重复 → 附加容器锚点 scope（消歧证据，运行时仍过全部闸门）
        if _element_key(element) in duplicate_keys and element.scope_has_text:
            step.scope = Scope(has_text=element.scope_has_text)
            if stats is not None:
                stats["scoped_compiled"] += 1
        elif _element_key(element) in duplicate_keys and stats is not None:
            # 容器外元素（无锚点）：运行时诚实拒绝，留给 L1 corrections
            stats["unscoped_duplicates"].append(ref)

    return validate_case(case.model_dump())   # 重新校验（安全边界）
