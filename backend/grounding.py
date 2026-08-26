"""
══════════════════════════════════════════════════════════════════════
grounding.py — State Grounding Validator（G3）
══════════════════════════════════════════════════════════════════════

【这个文件在项目中的位置】
  Architecture v2 管线的独立阶段（ROADMAP §4）：

    Planner refs-only →【State Grounding Validator：这里】→ LocatorSpec Compiler → ...

  职责：跨状态引用在执行前被拒绝——target_ref 属于 S0，而该步骤执行时
  页面应处于 S1（S0 --action--> S1 之后仍引用 S0 的元素）→
  STATE_GROUNDING_MISMATCH，明确失败，绝不带着错位计划进入执行器。

【为什么需要它】
  两个站点复现同一模式（Regression 1/2）：底层 locator 没坏——
  Planner representation 缺少 state identity，生成了"下一步操作上一个
  页面状态元素"的计划。这是 v1 换 v2 的主要证据。

【核心算法：静态推导 expected state（不依赖运行时 state_hash 重算）】
  current = 当前步骤执行时应处的 observation id，由计划静态推导：
    goto      → 按 URL 匹配 observation（匹配不到 → None）
    有 ref 的 click 且有唯一转移边 → current = edge.to
    其他动作 → current 不变
    不可追踪的 click（无 ref / 无转移边 / 多边歧义）→ current = None

  Fail-open 原则：只拒绝【可证明】的错位；推导断链时不猜——
  宁可放过，不可误拒合法计划（false rejection 同样破坏信任）。

【为什么不进 DSL】
  transition graph 是 Planner 的输入，位于 Planner 与 Executor 之间的
  Registry，不进入 DSL（DSL 只携带 target_ref / observation_ref 引用）。
  因此 Validator 在生成链路运行（图在 ai_agent 的作用域内），
  执行器本期不变（Runner 仍用 target 语义回放）。

【学习路径】
  图模型（StateGraph）→ from_explore_result（构建入口）
  → validate_state_grounding（推导主循环）→ 异常语义
══════════════════════════════════════════════════════════════════════
"""

from pydantic import ConfigDict, Field

from dsl import DSLModel, DSLCase


# ── 图模型（探索结果 → 校验用 State Graph，去掉 snapshot 保持紧凑）─────────────
# 复用 DSLModel（extra=forbid）：缓存文件在磁盘上，损坏/未知字段明确报错，
# 不静默丢弃（"虚假生效比报错更危险"）。

class GraphElement(DSLModel):
    """图中的一个已观察元素（state-scoped ref，如 obs3:e17）。

    role + name = 可交互元素（R1 Compiler 将用它确定性生成 locator）；
    text        = 文本节点（无角色语义）；
    verified    = I1：探索期 _resolve_locator 成功命中（当时页面 count==1）
                 的身份证据——是证据不是豁免，运行时仍过全部闸门；
    scope_has_text = I1：容器文本锚点（observation 内同名元素的消歧证据，
                   Compiler 发现同名重复时附加为 scope）。
    identity = A4.2：稳定业务 identity（{attr, value}，如 data-product-id）——
                   Compiler 确定性编译进 Locator（执行消歧），LLM 不生成。
    新字段可选——旧探索缓存缺字段时取默认值（向后兼容）。
    """
    ref: str
    role: str | None = None
    name: str | None = None
    text: str | None = None
    verified: bool = False
    identity: dict | None = None
    scope_has_text: str | None = None


class GraphObservation(DSLModel):
    """状态节点：url + state_hash + 元素表（同 URL 不同状态是不同节点）。"""
    id: str
    url: str
    state_hash: str | None = None
    elements: list[GraphElement] = []


class GraphTransition(DSLModel):
    """状态转移边：obs3 --click obs3:e17--> obs4。

    from_ 用别名 "from"（Python 关键字）：支持
      GraphTransition(from_="obs3", ...)      按字段名构造
      GraphTransition.model_validate({...})   按探索结果 dict 校验
    target_name = 【临时 diagnostic 字段，非核心事实】R7.2 完成校验按
    成功 transition 的语义判定（购物车入口 View Cart——不靠 URL 启发）。
    最终应由 target_ref 从 ObservationStore 反查派生，这里仅为
    避免探索结果带字段时校验失败；核心事实仍是 from/action/target_ref/to。
    """
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_: str = Field(alias="from")
    action: str
    target_ref: str
    target_name: str | None = None   # 临时 diagnostic，后续改为派生
    to: str


class StateGraph(DSLModel):
    """Observation State Graph：状态节点 + 转移边（G1/G2 的产物，G3 的输入）。"""
    observations: list[GraphObservation] = []
    transitions: list[GraphTransition] = []

    @classmethod
    def from_explore_result(cls, result: dict) -> "StateGraph":
        """从 explore_flow 的 explore_result 构建图（只取校验所需字段）。

        elements 两种形态（explore_flow._parse_elements）：
          {"ref": "obs3:e17", "role": "button", "name": "Add to cart"}
          {"ref": "obs3:e1", "type": "text", "text": "Products"}   ← type 丢弃
        """
        observations = [
            GraphObservation(
                id=o["id"],
                url=o["url"],
                state_hash=o.get("state_hash"),
                elements=[
                    GraphElement(
                        ref=e["ref"], role=e.get("role"),
                        name=e.get("name"), text=e.get("text"),
                        verified=e.get("verified", False),
                        scope_has_text=e.get("scope_has_text"),
                        identity=e.get("identity"),   # A4.2：稳定业务 identity
                    )
                    for e in o.get("elements", [])
                ],
            )
            for o in result.get("observations", [])
        ]
        transitions = [
            GraphTransition.model_validate(t)
            for t in result.get("transitions", [])
        ]
        return cls(observations=observations, transitions=transitions)


# ── 异常（拒绝语义明确，调用方/前端可读）────────────────────────────────────────

class StateGroundingMismatchError(Exception):
    """跨状态引用：step 的 grounding 证据指向非当前 expected state。

    step_index: 出问题步骤（1-based，与执行报告一致）
    ref:        target_ref（如 "obs3:e18"）
    expected:   该步骤执行时应处的 observation id
    actual:     ref 实际所属的 observation id
    """
    def __init__(self, step_index: int, ref: str,
                 expected: str | None, actual: str, contradiction: str | None = None):
        self.step_index = step_index
        self.ref = ref
        self.expected = expected
        self.actual = actual
        if contradiction:
            message = (
                f"步骤 {step_index}: STATE_GROUNDING_MISMATCH — "
                f"target_ref {ref} 属于 {actual}，但步骤的 observation_ref 声明为 "
                f"{contradiction}（两者必须一致）"
            )
        else:
            message = (
                f"步骤 {step_index}: STATE_GROUNDING_MISMATCH — "
                f"target_ref {ref} 属于 {actual}，但按转移图推导该步骤执行时应处于 "
                f"{expected}（引用已离开状态的元素）"
            )
        super().__init__(message)


class UnknownTargetRefError(Exception):
    """编造 ref：target_ref 不存在于 Observation State Graph。

    契约：Planner 没有权限创造元素（"你没有权限创造元素"）——
    ref 必须来自探索产出的元素表；编造 = 违反契约，硬拒绝（不静默清空，
    否则 AI 以为 ref 生效而代码丢了，正是 v1 的"虚假生效"陷阱）。
    """
    def __init__(self, step_index: int, ref: str):
        self.step_index = step_index
        self.ref = ref
        super().__init__(
            f"步骤 {step_index}: 未知 target_ref {ref} —— "
            "ref 不在 Observation State Graph 的元素表中（Planner 编造元素被拒绝）"
        )


class UnreachableObservationError(Exception):
    """悬空引用：ref 所属 observation 在状态图中不可达（无入口路径）。

    评审收紧（BFC 场景）：obs5 被观察到但没有 incoming transition 边
    （动作失败后页面自己变化产生的孤儿状态）——G3 的 fail-open
    （转移断链 current=None）会放行它的引用。invariant 强化：
    计划引用的每个 observation 必须从入口可达（goto URL 匹配或沿
    转移边到达）——不可达 = 探索未建立合法路径，引用 = 悬空。
    """
    def __init__(self, step_index: int, ref: str, obs_id: str):
        self.step_index = step_index
        self.ref = ref
        self.obs_id = obs_id
        super().__init__(
            f"步骤 {step_index}: 引用的状态 {obs_id} 在 State Graph 中不可达"
            f"（ref {ref}）——探索未建立到达该状态的转移路径，"
            "不可引用于测试计划"
        )


def _reachable_observations(graph: "StateGraph") -> set[str]:
    """从入口出发可达的 observation 集合。

    入口 = 探索起点（observations[0]，explore 总是从 entry_url 开始）。
    沿转移边 BFS。孤儿状态（被观察到但无 incoming 路径）不可达。

    注意：不能把"入度为零"当入口——孤儿状态同样无入边，
    会被误判为源点（BFC 实测：obs5 无入边 → 全部"可达"→ 悬空放行）。
    """
    if not graph.observations:
        return set()
    reachable = {graph.observations[0].id}
    stack = list(reachable)
    while stack:
        oid = stack.pop()
        for t in graph.transitions:
            if t.from_ == oid and t.to not in reachable:
                reachable.add(t.to)
                stack.append(t.to)
    return reachable


# ── Validator 主入口 ───────────────────────────────────────────────────────────

def _match_observation_url(value: str | None, observations: list[GraphObservation]) -> str | None:
    """goto URL → observation id（精确 → 去尾部斜杠 → 无匹配 None，fail-open）。"""
    if not value:
        return None
    url = value.strip()
    normalized = url.rstrip("/")
    for o in observations:
        if o.url == url or o.url.rstrip("/") == normalized:
            return o.id
    return None


def validate_state_grounding(case: DSLCase, graph: StateGraph | None) -> None:
    """State Grounding Validator（代码 invariant，不靠 Prompt）。

    对 case.steps 逐步骤静态推导 expected state，违反即抛异常：
      - target_ref 不在图中        → UnknownTargetRefError（编造）
      - ref 所属 state ≠ expected  → StateGroundingMismatchError（跨状态）
      - observation_ref 与 ref 矛盾 → StateGroundingMismatchError
      - 无 ref 步骤的 observation_ref ≠ expected → StateGroundingMismatchError

    graph 为 None / 空 → no-op（无探索的降级生成路径不受影响）。

    bounded：纯 Python 无 I/O；fail-open：推导断链处 current=None，不猜。
    """
    if graph is None or not graph.observations:
        return

    # ref → 所属 observation id（权威映射，不靠字符串拆分）
    ref_map: dict[str, str] = {
        e.ref: o.id
        for o in graph.observations
        for e in o.elements
    }
    # 转移边索引：(from, action, target_ref) → {to}（set 去重，多边歧义→fail-open）
    edges: dict[tuple[str, str, str], set[str]] = {}
    for t in graph.transitions:
        edges.setdefault((t.from_, t.action, t.target_ref), set()).add(t.to)

    # 可达性 invariant：计划引用的每个 observation 必须从入口可达
    #（评审收紧：孤儿状态无 incoming edge，G3 fail-open 会放行悬空引用）
    reachable = _reachable_observations(graph)

    current: str | None = None   # expected state（None = 不可追踪，fail-open）

    for index, step in enumerate(case.steps, start=1):
        # goto 不定位：按 URL 重置 expected state
        if step.action == "goto":
            current = _match_observation_url(step.value, graph.observations)
            continue

        ref = step.target_ref
        if ref is not None:
            # ① 契约校验：ref 必须存在于图中（编造 → 硬拒绝）
            if ref not in ref_map:
                raise UnknownTargetRefError(index, ref)

            belongs = ref_map[ref]
            # ①.5 可达性 invariant：所属 observation 必须从入口可达
            #（孤儿状态无路径 → 悬空引用，即使 ref 存在也拒绝）
            if belongs not in reachable:
                raise UnreachableObservationError(index, ref, belongs)
            # ② invariant：ref 必须属于当前 expected state
            if current is not None and belongs != current:
                raise StateGroundingMismatchError(
                    index, ref, expected=current, actual=belongs,
                )
            # ③ 同步骤 grounding 证据自洽：observation_ref 必须与 ref 一致
            if step.observation_ref and step.observation_ref != belongs:
                raise StateGroundingMismatchError(
                    index, ref, expected=current, actual=belongs,
                    contradiction=step.observation_ref,
                )
            # ④ 状态推进：click 且有唯一转移边 → current = edge.to
            if step.action == "click":
                tos = edges.get((belongs, "click", ref), set())
                # 无转移边 / 多边指向不同 to → 不可追踪，fail-open
                current = next(iter(tos)) if len(tos) == 1 else None
            # fill/check/wait_for/assert 不改变页面状态 → current 不变

        elif step.observation_ref and step.observation_ref not in reachable:
            # 无 ref 步骤声明的 observation 也不可指向孤儿状态
            raise UnreachableObservationError(
                index, f"(observation_ref={step.observation_ref})",
                step.observation_ref,
            )

        elif step.observation_ref and current is not None \
                and step.observation_ref != current:
            # 无 ref 但声明了 observation_ref 的步骤：grounding 证据也必须
            # 属于 expected state（同样的跨状态错位，只是没有 ref 可执行）
            raise StateGroundingMismatchError(
                index, f"(observation_ref={step.observation_ref})",
                expected=current, actual=step.observation_ref,
            )
