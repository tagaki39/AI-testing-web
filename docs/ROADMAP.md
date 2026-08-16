# ROADMAP — 架构演进与功能规划

> 本文档定义项目的两条线：**Architecture v2 主线**（Grounding/Resolution 引擎演进）与 **Demo Feature 支线**（产品功能按依赖顺序接回）。
> 来源：整合多轮设计评审的结论（Grounding 架构 / State-aware 定位 / 评审修正版方案）。

---

## 1. Project Principle

```text
LLM → 语义规划、有限选择
Pydantic → 协议合法性
Normalizer → 消除无语义差异的 Planner 波动
Preflight → 执行前发现 grounding / locator 问题
Runner → 最终真实 DOM 安全边界
```

核心信条：

- **AI 负责生成，真实执行由确定性代码完成**
- **能在确定性程序内解决的，不交给 LLM**
- **宁可明确失败，不允许低置信度错误点击**（False resolution rate 比 success rate 重要）
- **每个决策必须可解释、时间有上界**（bounded + explainable）

---

## 2. Baseline（Locator v1 / 执行内核）

### 已完成能力

- 三分法定位（0/1/N，歧义绝不自动选第一个）
- 两阶段 Resolver + 全局等待预算（单次解析 ≤5s，实测执行 280s → 17.7s）
- 可见性过滤 → 同一元素判定 → 业务实体聚类（data-product-id/data-item-id 严格版）
- exact → decorated-exact → 非导航 fuzzy（accessible name 图标前缀容忍）
- 导航 target 禁止 fuzzy substring（防止 "Cart" 命中 "Add to cart"）
- 结构化 target/scope、Pydantic 强校验（extra=forbid + action 级校验）
- Planner Schema Recovery ×1（约束修复，不重新规划）
- Normalize：去完全重复断言，保留不同语义的显式验证
- Page-aware Preflight（observation_ref → 对应状态内 0/1/N）
- 探索结果缓存（冷 21s → 热 6s）
- 敏感信息隔离（凭据不进 LLM / 不进缓存）
- 耗时自动记录（timings.jsonl）
- 测试数据脱敏、URL 解析四级链、SSRF 基础防护

### 已知边界（Locator v1 不解决；前两项已由 v2 的 G3 Validator / R1 Compiler 解决）

- 无运行时变量捕获（capture_text）
- 无修正闭环（corrections）
- 无 Trace / SSE / 登录态复用

### Frozen Decision

**Locator v1 冻结**：不再添加新的定位启发式。已有能力（语义对齐、业务聚类、预算）作为 v2 的 Semantantic Resolver 基础保留。

---

## 3. Why Architecture v2

两个独立站点复现同一模式，底层 locator 并未失效：

```text
Regression 1 — SauceDemo：
inventory state → 点击商品名 → detail state
下一步却引用 inventory state 的 add-to-cart ref

Regression 2 — AutomationExercise：
list state → View Product → detail state
下一步却引用 list state 的 Add to cart（详情页是 button 不是 link）

统一抽象：
S0 --action--> S1
next target ∈ S0 而不是 S1
=> STATE_GROUNDING_MISMATCH
```

**结论**：不是"某网站 DOM 太奇怪"，而是 Planner representation 缺少 state identity。这是换架构的主要证据。

---

## 4. Architecture v2

```text
Explore
  ↓
Observation State Graph          ← 状态节点 + 转移边
  ↓
Planner refs-only                ← 只选 refs / transitions，不生成 role/name/scope
  ↓
State Grounding Validator        ← 代码 invariant：ref 必须属于当前 state
  ↓
LocatorSpec Compiler             ← 稳定 locator 由代码确定性生成
  ↓
Resolution Pipeline
  ├─ Tier 0 historical correction（高优先级 candidate source，不绕过 Resolver）
  ├─ Tier 1 semantic candidates + scoring + confidence gate
  ├─ resolved / ambiguous
  └─ [Tier 2+ VLM / coordinate / human — deferred]
  ↓
Executor
  ↓
Evidence / Trace
```

### 关键概念

```text
ObservedElement:
  { ref: "obs3:e17", role, name, text_context, attributes }

Observation = 状态节点（url + state_hash + elements）
用户操作 = 状态之间的边（obs3 --click e17--> obs4）

Planner 输出：
  { action: "click", target_ref: "obs3:e17" }
  （registry 知道 belongs_to=obs3, transition→obs4）

Validator（代码 invariant，不靠 Prompt）：
  if step.target_ref.observation_id != current_observation_id:
      raise StateGroundingMismatch(...)
```

### 职责拆分（核心）

```text
Planner grounding: "想操作谁？"    → refs / transitions
Locator resolution: "DOM 里谁对应它？" → semantic resolver
```

### 必须保留的 v1 原则（进入 v2）

- exact semantic match 优先；fuzzy 不可无条件使用
- visibility / actionability 是 evidence
- DOM count ≠ business entity count；business identity 必须显式证明
- ambiguous 时 fail safely；runtime 有明确 timeout budget
- 决策必须 traceable

---

## 5. Architecture Roadmap

| 阶段 | 内容 | 验收标准 |
|------|------|---------|
| **C0** | Resolver global timeout budget | 任意 resolution ≤5s（✅ 已完成） |
| **C0** | Schema Recovery 收尾 | malformed JSON 一次 recovery 后明确成功/失败（✅ 已完成） |
| **C0** | 固化 regressions + `locator-v1` tag | 两个 grounding regression 保存为文件；v1 freeze（✅ 已完成） |
| **G1** | ObservedElement + state-scoped target_ref | observation 能产生 `obs3:e17`（✅ 已完成：探索产出 + DSL 字段 + Prompt 引导） |
| **G2** | Observation State Graph | 能表达 `obs3 --click e17--> obs4`（✅ 已完成：transitions 边记录 + 注入） |
| **G3** | refs-only Planner | Planner 不再自由生成 role/name/scope（✅ 已完成：grounded 模式双 Prompt + `check_refs_only` 代码契约；无探索降级保留 legacy 生成能力） |
| **G3** | State Grounding Validator | 跨 state ref 在执行前被拒绝（`STATE_GROUNDING_MISMATCH`）（✅ 已完成：`backend/grounding.py`） |
| **R1** | NodeRef → LocatorSpec Compiler | locator 由代码确定性生成（✅ 已完成：`backend/compiler.py`，target_ref → Locator 查表编译） |
| **R1** | 独立 Semantic Resolver（抽离模块） | Runner / Preflight 共用 resolution semantics（✅ 已完成：`backend/resolver.py`——target 解析 / 候选顺序 / 导航名限制 / 图标前缀容忍 / 快照匹配 / 定位异常单一事实源；Runner 与 Preflight 全部改引，ai_agent 不再 import runner，DAG 无环；`backend/tests/test_resolver.py` 13/13 锁定语义防再漂移） |
| **R2** | scoring + confidence margin | 高分但 margin 低仍拒绝（✅ 已完成：`resolver.decide_resolution`——策略评分分层 + 放松组 + 置信度门槛；`LowConfidenceError` 继承 Ambiguous 保持兼容） |
| **L1** | corrections JSON loop | correction 是 candidate source，不绕过 Resolver；成功/失败统计 + 连续失败 disable |

**首个 milestone**：两个 regression 的执行前拒绝（不要求自动修复）：

```text
SauceDemo wrong-state ref      → STATE_GROUNDING_MISMATCH
AutomationExercise wrong-state → STATE_GROUNDING_MISMATCH
```

**✅ 已达成**（`backend/grounding.py` + `backend/tests/test_grounding.py`，13/13 通过）。
实现要点：静态推导 expected state（goto URL 匹配 + 转移边追踪，不可追踪处
fail-open——只拒绝可证明的错位，不误拒合法计划）；编造 ref 硬拒绝
（UnknownTargetRefError，不清空降级）；Validator 在生成链路 Preflight 之前
运行，mismatch 的自动替换留待第二阶段。

**G3 refs-only Planner + R1 Compiler 已达成**（`backend/compiler.py` +
`backend/tests/test_compiler.py`，14/14 通过）：
- grounded 模式 Planner 只从元素引用表选 ref——`check_refs_only` 代码契约
  （禁止 target/scope 字段、定位动作必须有 ref），违规进入 Schema Recovery
  （带引用表上下文），恢复仍失败 → 明确失败
- locator 由 Compiler 从观察到的元素数据确定性编译（覆盖 Planner 手写字段，
  确定性 > Planner）；执行侧防线 `ensure_executable_targets` 在浏览器启动前
  拒绝未编译的 ref-only 步骤（防手改 DSL 绕过编译）
- 无探索降级路径（legacy 模式）保留 role/name/scope 生成能力，行为不变

---

## 6. Demo Feature Backlog（按依赖顺序）

| 功能 | 时机 | 说明 |
|------|------|------|
| capture_text / capture_attribute | **target_ref 架构完成后** | 直接落到新 DSL（`target_ref` + save_as），避免建立在即将废弃的 representation 上 |
| Playwright Trace | Grounding 核心后，可较早旁路做 | Observability，独立于算法；为 grounding 决策提供可解释证据 |
| storage_state 登录态复用 | 扩大 regression suite 前 | 避免反复登录污染 state graph |
| SSE 实时进度 | 后期 | 产品表面；底层 grounded 前做只是"展示得更漂亮" |
| 前端四步流程（探索/生成/执行/报告） | 后期 | 同上 |
| 用例持久化 | 后期 | 当前 JSON/内存足够 |

---

## 7. Deferred / Non-goals

| 项 | 说明 |
|----|------|
| CDP full AX tree | 作为 Observation Adapter 替换 aria_snapshot（有真实数据证明需要后） |
| VLM 视觉定位 | 昂贵、慢、难 debug；先证明 A11y/DOM semantic 覆盖率 |
| coordinate click | 依赖 VLM |
| human intervention UI | 产品阶段 |
| 平台认证 / 多用户 | 本地演示无需 |
| 数据库 | JSON 文件足够；生产版用 DB（README 注明） |
| 定时调度 / 集群 / 分布式 | 超范围 |

---

## 8. Regression & Metrics

### Grounding regressions（已固化 + Validator 覆盖）

- Regression 1 — SauceDemo：detail state 后不得引用 inventory ref
- Regression 2 — AutomationExercise：detail state 后 target_ref 必须来自 detail observation
- 验收实现：`backend/tests/test_grounding.py`（零依赖 plain-assert，`py backend/tests/test_grounding.py`）

### 核心指标

```text
Planner raw schema success rate      ← Schema Recovery 统计已记录
Schema recovery success rate
target_ref grounding validity        ← v2 新增
Compiler success rate                ← v2 新增
Resolver success rate
Ambiguity rate
False-resolution rate ← 最重要（守住 "Cart→Add to cart" 类错误）
p50 / p95 resolution latency         ← resolve_ms 已记录
Correction reuse success rate        ← L1 后
```

### 可观测性

- 生成/执行耗时自动记录到 `timings.jsonl`（每轮完整数据）
- 步骤级 `resolve_ms` / `resolved_by` 已进入证据
