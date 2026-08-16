"""
══════════════════════════════════════════════════════════════════════
compiler.py — LocatorSpec Compiler（R1：NodeRef → LocatorSpec）
══════════════════════════════════════════════════════════════════════

【这个文件在项目中的位置】
  Architecture v2 管线的独立阶段（ROADMAP §4）：

    Planner refs-only → State Grounding Validator →【LocatorSpec Compiler：这里】→ ...

  职责：target_ref（如 obs3:e17）→ 稳定 locator（Locator 模型），
  由代码确定性生成——Planner 只选 ref，不生成 role/name/scope。

【核心思想】
  Planner grounding: "想操作谁？" → refs（obs3:e17）
  Compiler: "DOM 里谁对应它？"  → 从观察到的元素数据确定性编译 Locator
  两者职责分离后，locator 不再依赖 LLM 的"想象力"——
  元素是系统真实观察到的（explore 的 element ref 表），
  编译只是查表，零幻觉、零波动。

【数据流】
  Planner 输出 {action, target_ref: "obs3:e17"}（无 target 字段）
    → compile_targets 查 StateGraph 元素表
    → target = Locator(role="button", name="Add to cart")
    → 执行器仍用 target 语义回放（"运行时 target_ref 可以不存在，
      只靠稳定 target replay"——评审既定结论）

【学习路径】
  compile_targets（主入口）→ _element_to_locator（单元素映射）
══════════════════════════════════════════════════════════════════════
"""

from dsl import DSLCase, Locator, validate_case
from grounding import StateGraph, UnknownTargetRefError


def _element_to_locator(element) -> Locator:
    """图元素 → Locator（确定性映射，无任何启发式）。

    可交互元素（role+name）→ 语义定位（最稳，官方推荐）
    文本节点（text）       → 文本定位（无语义元素的兜底）
    """
    if element.role:
        return Locator(role=element.role, name=element.name)
    return Locator(text=element.text)


def compile_targets(case: DSLCase, graph: StateGraph) -> DSLCase:
    """target_ref → target 确定性编译（R1）。

    规则：
      - 每个带 target_ref 的步骤，从 graph 元素表查 ref：
        未知 ref → UnknownTargetRefError（编译器与 Validator 同一拒绝语义）
      - 覆盖步骤中已有的 target（Planner 手写的定位字段不可信——
        确定性 > Planner，保证 grounding 完整性）
      - 结束后重新 validate_case（编译产物必须合法 DSL）

    graph 为空 → 原样返回（无探索的 legacy 降级路径不做编译）。
    """
    if graph is None or not graph.observations:
        return case

    ref_map = {
        e.ref: e
        for o in graph.observations
        for e in o.elements
    }

    for index, step in enumerate(case.steps, start=1):
        ref = step.target_ref
        if ref is None:
            continue
        element = ref_map.get(ref)
        if element is None:
            raise UnknownTargetRefError(index, ref)
        step.target = _element_to_locator(element)   # 覆盖，不信任 Planner 手写

    return validate_case(case.model_dump())   # 重新校验（安全边界）
