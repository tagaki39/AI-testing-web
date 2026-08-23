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
| **I1** | 实例身份恢复 | 编译后 locator 可区分 observation 内同名元素（容器内 scope 编译 + 身份证据前移；容器外留给 L1）（✅ 已完成：探索采集容器锚点 `scope_has_text` + verified 标记 → Compiler 同名重复附加 `Scope(has_text)`；E2E saucedemo 6/6、automationexercise 定位步骤全通——剩余失败为探索完整性/规划质量波动，属生成链路增强阶段） |
| **L1** | corrections JSON loop | correction 是 candidate source，不绕过 Resolver；成功/失败统计 + 连续失败 disable（✅ 已完成：`backend/corrections.py`——URL 泛化 + 语义键匹配、upsert、连续失败 3 次熔断；correction 以 130 分最高候选进入统一裁决；前端失败步骤可提交修正；P4 E2E 验收：失败 → 提交 → 重跑命中 verified_count=1） |
| **GQ** | 生成链路可靠性（质量门 + 探索完整性） | 见下方 GQ 决策记录（✅ 已完成：finish 完整性校验 + 缓存门槛放宽 done/steps≥2 + 目标覆盖警告 + GQ2 硬失败/自愈重生/反模式负例；E2E：saucedemo 无回归、automationexercise 不完整计划被明确拦截） |

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

### I1 决策记录（实例身份恢复，动手前冻结）

**问题**：refs-only 编译的 LocatorSpec 只含 (role, name)——observation 内同名的
元素在执行时无法区分（automationexercise View Product ×N，实测 6/9 失败）。

**决策 1 — 分而治之**（参考实测结论，见 docs/机制参考与借鉴评估.md）：
- **容器内重复**（元素在业务容器内，如 Add to cart ×6）：探索期为重复元素
  采集"容器上下文"（data-product-id/data-item-id 容器 → 容器首行稳定文本），
  Compiler 发现 observation 内同名 >1 时附加 scope 编译
- **容器外元素**（View Product 不在产品容器内，a11y 树独立元素）：
  本期**不做** DOM has 链自动生成——明确拒绝 + 可解释错误，
  兜底职责留给 L1 corrections（人工提交 css 修正）

**决策 2 — 身份证据前移（verified 标记）**：探索期 `_resolve_locator` 成功
命中的元素标记 verified（探索时页面 count==1）。verified 是证据不是豁免——
运行时仍过三分法 + R2 评分；作用：编译时无需 scope 也有把握，入 meta 供指标。

**决策 3 — scope 是证据不是命令（fail-safe）**：
- 编译出的 scope 运行时仍过三分法 + R2 评分 + margin 门槛
- 消歧失败 → 明确拒绝（Ambiguous / LowConfidence），**绝不 nth 猜测**
- 探索期只对同名重复元素采集上下文（性能上界：非重复元素零开销）

### GQ 决策记录（生成链路可靠性，动手前冻结）

**问题**（automationexercise 三次生成三种结果）：① Planner 编造 ref 被拒
② 计划漏掉"点击加购"动作（9/10——断言了可见性却无点击，静默不完整）
③ 探索 1 步后宣告完成（计划只覆盖登录）。探索完成率 50%，缓存命中率 20%。

**决策 1 — finish 完整性校验（治过早完成）**：探索器宣告
exploration_complete 时做代码校验——已执行动作 < 2 的完成宣告无效，
把"探索不充分"作为 decision_rejected 反馈进历史，预算内继续
（复用既有自纠机制，零新概念）。

**决策 2 — 缓存门槛放宽（治重探索浪费）**：`done=True OR steps_used ≥ 2`
才缓存。saucedemo done=False 但 steps=4 的探索产出了 7/7 计划——被拒
缓存是浪费；而毒化历史的坏探索是 steps=1（同页状态堆叠）——阈值 2
恰好放行前者、拒绝后者。

**决策 3 — 生成期目标覆盖警告（治静默不完整，fail-open）**：
- 探索 done=False → meta 标记 exploration_incomplete，前端醒目警告
  "计划可能不覆盖全部目标，建议重新生成"
- 目标动作覆盖检查（保守 allowlist，人为维护）：goal 含"加购"类动词
  而计划无指向 add-to-cart 类元素的动作 → warning；匹配不到不警告
  （fail-open，不误伤合法计划）
- 硬失败与自愈重生见下方 GQ2 决策记录（✅ 已全部完成）

### GQ2 决策记录（质量门硬失败 + 自愈重生，✅ 已完成）

**决策 1 — 硬失败只针对可证明的缺陷**：`missing_actions` 非空（目标要求
动作而计划无对应 click）→ 计划可证明不完整 → 硬失败不返回；
`exploration_incomplete` 保持警告（done=False 太常见，且实测 done=False
的计划也能 7/7——不可证明不完整，不硬失败）。

**决策 2 — 重生 ×1（延续 Schema Recovery 哲学）**：失败 → 记录反模式 →
带负例 few-shot + 上次错误信息重新规划一次（复用探索结果，不重新探索）；
第二次仍失败 → 明确报错（400），绝不静默降级返回不完整计划。

**决策 3 — 反模式存储（anti_patterns.json，仿 corrections）**：
{reason_code, summary, created_at}；reason_code 分类：
missing_step（缺关键动作）/ invalid_structure（schema 恢复失败）/
invalid_ref（编造 ref / grounding 拒绝）；按 code 取最近 5 条注入重生
prompt；summary 为失败计划的行为摘要（actions+targets，脱敏）。

**明确不做**：生成期读修正库提示 Planner；多轮重规划。

### R3 决策记录（Tool-driven 重构，来源 docs/优化.txt 架构评审）

**目标**：从 Guard-driven Agent 重构为 Tool-driven Agent——
LLM 决策 → 少量强约束工具 → 结构化 ToolResult → LLM 再决策；
执行可靠性由 Runner/Preflight/Postcondition 负责，不塞进 Explorer。

**三条设计原则（冻结）**：
1. Restrict, don't repair——模型只看到合法选择，不修模型输出
2. Execute, don't predict actionability——不预测能否点击，短超时执行，
   可靠性下沉 Browser Action 层
3. One source of truth per concept——当前状态→ObservationStore /
   历史转移→StateGraph / 允许动作→ActionSpace / 定位→Resolver /
   预算→SafetyController / 浏览器执行→Runner

**里程碑（按序）**：
| 阶段 | 内容 | 验收 |
|------|------|------|
| R3.1 | Actionability 下沉 Browser Action Executor | 探索器不再做 visible/enabled/modal 预测；点击由 executor 短超时执行并返回结构化 ToolResult（✅ 已完成：`backend/execution/action_executor.py`——ToolResult + click_preprocessor + 危险操作闸口；explorer 循环消费结构化结果；顺带修复拆分遗漏的惰性 import bug——NameError 被静默吞掉导致全部元素误标 actionable=False、探索残废；测试 123/123） |
| R3.2 | SafetyController 统一预算 | 所有 retry（decision/action/G3/anti-pattern）汇入单一预算控制器 + tool-call 签名去重；删除分散的 retry 计数 |
| R3.3 | G3 纯 Validator 化 | PASS→Compiler / FAIL→整体 Planner retry ×1→fail closed；删除 repair 特例 |
| R3.4 | ai_agent 拆包 | planning/exploration/grounding/locators/execution/quality 分层；顶层 orchestration ≤ 几十行 |
| R3.5 | PostconditionVerifier（简单版） | 动作成功与状态生效分离（wait_for 业务 postcondition 的确定性验证） |

**保留（核心，不动）**：Observation State Graph / State-scoped refs /
State Grounding Validator / Compiler / Semantic Resolver / Corrections /
Metrics / Data Grounding。

**暂缓**：VLM / SSE / DB / Auth / React / Postcondition 丰富 schema。

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
