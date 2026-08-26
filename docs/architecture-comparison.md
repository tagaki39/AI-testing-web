# 架构对比：参考项目 vs 当前项目

> 日期：2026-08-26（三方验证：旧版本 c1dab10 / 参考项目 / 当前版本 76bc711）
> 验证场景：BFC（AutomationExercise 品牌筛选加购，12 个同名 Add to cart + modal 交互）

## 参考项目架构

```
Observation
    explore_page / explore_flow（页面 a11y 快照采集）
    → page_elements / a11y_nodes_by_state（分段元素上下文）
↓
（无 Interaction Root——遮挡由执行时 click_preprocessor 处理）
↓
（无 ActionSpace——无 LLM 探索决策循环，agent 工具一次性探索）
↓
test_planning_agent（ReAct 对话式规划 + planning tools）
    → requirements 收集 → flow_steps 规划 → DSL draft
↓
（无 State Graph——无状态机/转移边概念）

        Generate

DSL Generator（governance + auto-repair + rejection tracking）
↓
（无 G3——Pydantic schema 校验 + Preflight 可选诊断）
↓
（无 Compiler——LLM 直接生成 target/scope 定位字段）
↓
Resolver（5-tier hybrid locator：corrections → semantic → visual → fallback）
↓
playwright_runner（同步 DSL 回放 + evidence 采集）
↓
click_preprocessor（pointer interception → 诊断 → wait → dismiss → retry）
```

## 当前项目架构

```
Observation
    CDP AX 结构化树（kind/disabled/语义上下文）
    → stable identity 提取（data-testid/test/qa/cy、data-*-id）
    → duplicate canonicalization（12 Add to cart → 6 业务动作）
    → interaction_root metadata（source=ax / dom_overlay）
↓
Interaction Root
    AX dialog/alertdialog（context_role）优先
    → AX 缺失时 DOM overlay bridge（一次批量 contains 判定，非站点特判）
↓
ActionSpace
    纯结构过滤（kind=action / 非 disabled / 非 failed / 在 root 内）
↓
Policy
    目标约束从 StateGraph 派生（数量未完成 → root 内终态动作不暴露）
↓
LLM semantic choice
    从 ActionSpace 候选选 ref（元素表带商品名/identity 消歧）
↓
Execute → State Graph
    成功转移边 = verified transitions（from≠to，带 ID：t1..tN）

        Generate

Compact Planner（transition-constrained：状态变化型步骤只选 transition_ref）
↓
State Cursor Grounding（transition_ref 确定性展开 + 断言 grounding）
↓
G3（safety invariant——理论不触发）
↓
Compiler（ref → Locator 确定性编译：role/name/identity）
↓
Resolver（identity_exact 优先 + normal/overlay 可见性裁决）
↓
Runner（DSL 回放：identity/role 语义定位）
↓
（E3 click recovery 未实现——参考项目"等待 → dismiss → retry once"留作后续）
```

## 三方验证数据（BFC 场景）

| 指标 | 旧版本 (c1dab10) | 参考项目 | 当前版本 (76bc711) |
|---|---|---|---|
| 生成总耗时 | 97.1s | 214.8s（规划+探索+draft） | 28.8s |
| 探索 LLM 调用 | 16 | agent 内部（ReAct） | 7 |
| 生成步骤 | 11 | 21 | 8 |
| 加购商品 | 1 个 | 2 个（Blue Top / Premium Polo T-Shirts） | 2 个（product 1/29） |
| 消歧方式 | 无（role/name 裸匹配） | scope `inside "Blue Top"` | identity（data-product-id 编译进 DSL） |
| 执行结果 | 未跑（断言对象已可疑） | passed，11.4s（21 步全过） | passed，5.6s（8/8 步） |
| 探索完成 | done=False（预算耗尽） | — | done=True |

## 分阶段策略与借鉴边界

| 阶段 | 参考项目策略 | 当前项目策略 | 借鉴结论 |
|---|---|---|---|
| 探索 | 一次性快照（agent 工具） | Restrict-first（ActionSpace 只给可操作元素） | 探索循环必须 Restrict——参考项目式"点失败再修复"在探索期成本过高（8 连超时实证） |
| 遮挡处理 | click_preprocessor（执行时修复） | Interaction Root（决策时限制） | 两者互补：探索端 Restrict，执行端可后续借鉴"等待 → 明确 dismiss → retry once"（E3，本期未做） |
| 消歧 | scope 容器锚点 + 进详情页绕歧义 | identity_exact（业务属性直接定位） | identity 是更强证据（全站唯一）；scope_has_text 保留为兜底 |
| 状态约束 | 无状态机（DSL 自由生成） | State Graph + transition-constrained | 当前项目路线：LLM 只选 verified 边，跨状态引用结构上不可能 |
| 断言 | 任意 target 引用 | state cursor grounding（必须当前状态元素） | 参考项目无此概念；G3 从"经常拦截"降级为 safety invariant |

## 核心分界线

- **参考项目**："执行时修复"——DSL 已确定，点击被遮挡就诊断 + dismiss + 重试，保证可执行。
- **当前项目**："决策时限制 + 生成时约束"——探索期不让 LLM 选错（ActionSpace/Policy），生成期不让错误产生（transition-constrained + cursor grounding）。
- 两者不重复：一个处理"当前合法交互上下文"，一个处理"已确定动作的运行时异常"。

## 相关文档

- [机制参考与借鉴评估.md](./机制参考与借鉴评估.md)（定位机制逐项评估）
- [A4-plan.md](./A4-plan.md)（Structured A11y 观察计划）
- [R4-plan.md](./R4-plan.md)（Control Flow Simplification）
