# S2 — Milestone-Guided Exploration（里程碑驱动的受控探索）

> 基线：2026-08-27，生产代码 6,785 物理行，测试 3,745 行，179 tests。
> 核心判断：复杂目标的主要问题不是全局预算不足，而是缺少贯穿目标的
> 进度控制平面。S2 不扩大 `MAX_STEPS`，而是让每一步只服务当前里程碑。

## 一、目标与非目标

S2 目标：

- 不同目标不复用同一条目标相关探索路径。
- 将“页面元素候选”升级为“动作候选”，0/1/N 决策语义正确。
- 用 Goal Contract 描述完整目标阶段，用 StateGraph/history/Observation
  纯推导当前进度。
- Explorer 在资源型终端副作用前停止，由 Runner 只执行一次副作用并验证。
- 每个停止结果都具有明确、可观测的 termination reason。

S2 不做：

- 不提高全局步骤或 LLM 调用上限掩盖走偏。
- 不继续增加完成正则、场景 policy 或站点专用 selector。
- 不恢复 Preflight repair，不引入 VLM、数据库、认证或 React 平台化。
- 不把 `logged_in`、`image_page_opened` 等可推导状态存入 ExploreState。

## 二、演进原则

1. **Replace, then delete**：新机制接管后，最迟下一个提交删除旧机制。
2. **Derive, don't duplicate**：进度从 verified transitions、history 和当前
   Observation 推导，不维护第二事实源。
3. **Action candidates, not element guesses**：确定性执行的单位是完整动作，
   包括 action、target_ref 和可选 value_ref。
4. **Fail with a reason**：`done` 只表示循环停止；成功、失败和预算耗尽由
   termination reason 区分。
5. **Side effects execute once**：可能消耗积分、额度或任务队列的动作只由
   Runner 执行一次。

## 三、目标架构

```text
用户目标
  ↓
数据提取与脱敏
  ↓
Goal Contract（一次生成；只描述阶段，不生成 locator/ref/DSL）
  ↓
derive_milestone_progress(contract, state)（纯函数）
  ↓
当前 Milestone
  ↓
Milestone-scoped ActionCandidate Policy
  ↓
0 → MILESTONE_BLOCKED
1 → 确定性执行
N → LLM 语义选择
  ↓
Observation / verified StateGraph
  ↓
READY_FOR_RUNNER → Planner → Compiler → Runner → postcondition
```

保留：Structured AX、Interaction Root、Stable Identity、StateGraph、G3、
Compiler、Resolver、Corrections、Runner。

替换：目标动作正则、复杂目标 UNKNOWN 临时判断、购物车式场景 Policy、
元素级 singleton 快捷路径、模糊的 `done=False` 终止表达。

## 四、实施顺序

### S2-P0 — 确定性基线

#### P0-A：结构化终止原因

首批原因：

```text
GOAL_COMPLETE
MODEL_FINISH
READY_FOR_RUNNER
AUTH_REJECTED
ERROR_PAGE
OBSERVATION_LIMIT
BUDGET_EXHAUSTED
MILESTONE_STALLED
CAPABILITY_MISSING
ACTION_FAILED
```

`ExploreState.terminate(reason)` 是设置 `done` 的唯一入口。目标轨迹缓存只把
`GOAL_COMPLETE` 当作可复用成功；`MODEL_FINISH` 在 Milestone 接管前保留行为，
但不进入目标轨迹缓存。

#### P0-B：目标隔离缓存

缓存键：

```text
normalized origin + auth_profile + redacted goal fingerprint + schema version
```

立即停止缓存 `done=False / steps>=2` 的探索路径。后续拆分：

- PageObservationCache：页面结构，可跨目标复用。
- ExplorationTraceCache：目标路径，只能按 Goal Contract hash 复用。

#### P0-C：singleton 动作安全

临时阶段只对 capability 恰好为 `{click}` 的角色做确定性 click。
button/link/textbox/combobox 继续进入决策层。P1 ActionCandidate 接管后删除
这个角色级过渡判断。

验收：

- login-only 与 image-generation 目标不能串缓存。
- 未完成、模型 finish、认证失败和错误页不能写目标轨迹缓存。
- 唯一 textbox 不会被自动 click；唯一 checkbox 可以确定性 click。
- 全量测试通过。

### S2-P1 — Goal Contract + Milestone Progress

新增小型 schema：

```text
Milestone: id / type / intent / target_terms / field_terms / value_ref / execution
GoalContract: version / milestones
```

允许的 milestone type：`auth / navigate / input / ready / side_effect / verify`。
Contract 不得包含 target_ref、CSS、XPath、role/name locator 或 DSL steps。

非敏感 literal 必须是原始目标的原文子串，否则拒绝或降级为无默认值变量。

`derive_milestone_progress()` 只读取：

- auth：verified login transition
- navigate：当前 Observation 或 verified transition 的目标语义
- input：成功 history/pre_action 的 fill
- ready：当前 Observation 中已启用的目标动作
- side_effect：Explorer 只推导到 ready
- verify：Runner postcondition

接管后删除 `_FURTHER_ACTION_RE` 和
`_goal_fully_covered_by_deterministic_model`。

### S2-P2 — ActionCandidate + Form Binder

```python
ActionCandidate(action, target_ref, value_ref=None, reason="")
```

Policy 只围绕当前 milestone 构建候选。唯一登录表单可确定性产生：

```text
fill username → fill password → click login
```

只有字段映射或动作选择有歧义时才调用 LLM。

验收：复杂目标 fresh 运行 3 次，均进入同一目标页；steps ≤8，LLM calls ≤4，
不点击重置/刷新/查看详情等无关动作。

### S2-P3 — 通用 contenteditable DOM Bridge

一次批量 DOM 查询补充 AX 未暴露的 contenteditable，仅接受：可见、未禁用、
未被现有 AX element 覆盖、具有稳定唯一 selector 的元素。

名称顺序：aria-label → aria-labelledby → placeholder → label → 最近短标题。
selector 顺序：test-id 属性 → id → 页面唯一 contenteditable；否则拒绝，
绝不使用 `nth()`。CSS 由 Observation 生成，LLM 无权生成。

### S2-P4 — Runner-only terminal side effect

Explorer：登录 → 导航 → 填值 → 验证终端按钮 ready → `READY_FOR_RUNNER`。

Runner：只点击一次终端动作，第一阶段只验证任务卡/排队中/生成中状态，
不等待最终资源完全生成。

### S2-P5 — Per-milestone Budget

在进度控制稳定后再引入双层预算：

```text
GLOBAL_MAX_ACTIONS=24 / GLOBAL_MAX_LLM_CALLS=12 / GLOBAL_TIMEOUT=90s
MAX_ACTIONS_PER_MILESTONE=4 / MAX_LLM_CALLS_PER_MILESTONE=2
MAX_REPLAN_PER_MILESTONE=1
```

确定性执行不消耗 LLM budget；同一 milestone 连续两次无进展才 replan，
replan 后仍无进展返回 `MILESTONE_STALLED`。

### S2-P6 — Cutover Cleanup

- 删除旧 Completion 正则和场景 Policy。
- 删除无生产消费者的 anti-pattern 查询/持久化机制。
- 删除 `_pages_to_text`、Preflight 空字段/指标/过期注释。
- 用实际指标决定是否删除无入口 URL 的 legacy Planner。
- 更新 README、ROADMAP、metrics 和三站回归记录。

## 五、规模与边界预算

- S2 实施峰值：生产代码 ≤7,200 物理行。
- S2 收尾：生产代码回到 6,200–6,800 行；测试不设下降目标。
- `ai_agent.py` 收敛为 ≤300 行编排；`explorer.py` 主循环 ≤350 行。
- 单个生产模块原则上不超过 600–700 行。
- 新机制接管后，最迟下一个 commit 删除其前任；禁止永久双轨。

建议最终模块：

```text
backend/
  goal_contract.py
  planning.py
  quality.py
  explore/
    observation.py
    progress.py
    policy.py
    explorer.py
```

## 六、提交与停止条件

| Commit | 内容 | 停止条件 |
|---|---|---|
| S2-P0 | termination + goal cache + singleton safety | P0 回归 + 全测通过 |
| S2-P1 | Goal Contract + progress | 登录后 current milestone=navigate |
| S2-P2 | ActionCandidate + Form Binder | fresh 3 次 steps≤8 / LLM≤4 |
| S2-P3 | contenteditable bridge | 识别为 textbox 且真实 fill 成功 |
| S2-P4 | runner-only terminal action | 终端请求只发生一次 |
| S2-P5 | milestone budget/replan | stalled 精确定位到 milestone |
| S2-P6 | dead-code audit + docs + regressions | 旧轨删除、规模预算达标 |

每个阶段必须独立验收；任何阶段失败时停在当前边界修正，不一次性改完整条主链。
