# AI Web Testing Demo

**中文自然语言进 → 可执行测试用例 + 真浏览器执行报告出。**

核心设计原则一句话：**AI 只做选择题，事实与执行交给确定性代码**——
LLM 负责语义理解与有限选择，定位、校验、执行、时间上界全部由代码保证。

| 实测指标（可复现） | 数据 |
|---|---|
| BFC 场景（品牌筛选加购） | 生成 97.1s / 16 次 LLM 调用 → **28.8s / 7 次**；执行 **8/8 步全过（5.6s）** |
| 单次定位耗时上界 | 全局预算 ≤5s（旧实现最坏叠加 60-110s；实测执行 280s → **17.7s**） |
| 跨站点 E2E | SauceDemo / AutomationExercise / xywhaigc 登录+加购流程全通过 |
| 测试 | **12 个测试文件、191 个测试函数全部通过**（含真实浏览器冒烟），零依赖 plain-assert |

---

## 快速开始

```bash
# 1. 创建 .env（参考 .env.example，填你的 DeepSeek key）
cp .env.example .env

# 2. 安装依赖（无 requirements，四个包）
py -m pip install fastapi uvicorn playwright pydantic

# 3. 安装浏览器（如已安装过可跳过）
py -m playwright install chromium

# 4. 启动
cd backend
python main.py
```

浏览器打开 **http://127.0.0.1:9000**（端口可由 `.env` 的 `APP_PORT` 覆盖）。

`.env` 配置项：`AI_API_KEY`（必填）/ `AI_BASE_URL`（默认 DeepSeek）/ `AI_MODEL`（默认 `deepseek-chat`）/ `APP_PORT`。

---

## 使用流程

1. 输入自然语言需求，如：
   `打开 saucedemo.com，用 standard_user / secret_sauce 登录，把第一个商品加入购物车，进入购物车验证商品存在`
2. 点击「AI 生成 DSL」→ 平台自动探索页面 → 生成结构化用例（可在编辑框中人工调整）
3. 点击「执行测试」→ **SSE 实时进度**逐步展示每步状态、耗时、定位策略与截图
4. 失败的步骤可在报告内**提交修正**（`css=xxx` / `test_id=xxx` / `text=xxx`），下次执行优先复用

命令行指标聚合（成功率 / p50·p95 / 定位策略分布 / 探索调用数）：

```bash
py backend/metrics.py
```

---

## 架构（v2）

```
自然语言需求（凭据进系统即脱敏为 ${var}）
    │
    ▼
① 探索（explore/）        真实浏览器 + CDP 无障碍树观察
    │                     LLM 每步只从"合法候选"里选动作（候选=代码过滤的产物）
    │                     成功且状态变化 → 记一条已验证转移边
    ▼
   Observation State Graph（状态节点 + 转移边 + state-scoped 元素表 obs3:e17）
    │
    ▼
② 规划（ai_agent.py）     refs-only Planner：LLM 只选已验证转移 + 断言
    │                     不写定位字段、不写步骤（违规进 Schema Recovery）
    ▼
③ 校验（grounding/compiler）  纯代码，全部发生在浏览器启动之前
    │                     契约检查 → G3 状态推导 → 质量门 → 确定性编译 locator
    ▼
④ 执行（execution/）      三分法定位 + 评分置信度门槛 + 5s 全局预算
    │                     "宁可明确失败，不允许低置信度点击"
    ▼
执行报告（每步截图 + 耗时 + 定位策略 + 异常详情）
```

---

## 项目结构

```
backend/
  dsl.py          ~160    DSL 数据结构 + Pydantic 强校验（extra=forbid + action 级校验）
  ai_agent.py     ~1220   生成链路：入口 URL 解析 / refs-only Planner / Schema Recovery / 质量门
  grounding.py    ~270    StateGraph + G3 State Grounding Validator（跨状态引用执行前拒绝）
  compiler.py     ~115    LocatorSpec Compiler（target_ref → Locator 确定性编译 + I1 消歧）
  goal_contract.py ~155   S2 目标契约（目标 → 里程碑，只描述意图不生成 DSL）
  explore_cache.py ~105   探索结果缓存（脱敏落盘，冷 21s → 热 6s）
  anti_patterns.py ~75    失败模式档案（按 reason_code 分类，重生时作负例注入）
  metrics.py      ~195    timings.jsonl → 聚合指标
  main.py         ~300    FastAPI：/api/generate /api/execute /api/runs/{id}/events(SSE)
                          /api/corrections /api/artifacts + 静态托管
  explore/
    observation.py ~795   CDP AX 树归一化 / 元素表 / 语义 state_hash / 状态记录
    action_space.py ~130  ActionSpace 结构过滤 + 动作能力矩阵 + 可操作性评估
    explorer.py    ~940   bounded 探索循环 / 决策校验 / 目标约束 / 完成判定
    progress.py    ~170   里程碑进度推导（只从 Observation/history/StateGraph 派生）
  locator/
    resolver.py    ~525   定位语义单一事实源（候选链 / 评分裁决 / 快照匹配）
    corrections.py ~130   L1 持久化覆盖规则（统计 + 熔断）
  execution/
    runner.py      ~530   执行引擎：三分法编排 + 5s 预算 + 证据采集
    action_executor.py ~100  动作执行器：ToolResult + 危险操作闸口
  tests/           12 个测试文件，190+ 测试函数（零依赖 plain-assert）
  regressions/     两个 grounding regression（SauceDemo / AutomationExercise）
frontend/index.html ~350  单页 UI（零构建）
docs/                     设计文档与执行日志（见文末索引）
```

---

## 核心设计

### 1. DSL 作为安全边界

AI 生成、代码执行，中间隔着 Pydantic 强校验：`extra="forbid"`（未知字段直接报错，
防"AI 以为生效、代码其实丢了"的虚假生效）+ action 白名单 + action 级业务校验
（click 无 target、goto 无 value 都拒）。前端传入的 DSL 走同一条校验，前后端都不能绕过。

### 2. 探索：CDP 观察 + 受限选择

- **观察**：CDP `Accessibility.getFullAXTree` → 归一化 `AXNode`（role/name/disabled/层级）；
  元素表带 **state-scoped ref**（`obs3:e17` = 第 3 个状态的第 17 个元素）；
  `state_hash` 用语义签名——文本/输入变化不产生新状态，防状态膨胀
- **受限选择**（Restrict, don't repair）：LLM 每步只从候选里选一个动作。
  候选 = 代码四道过滤的产物：失败黑名单 → disabled/遮罩（dialog/overlay 外不暴露）
  → 动作能力矩阵（textbox 只能 fill…）→ 目标 Policy（加满 2 件后终态动作不暴露）
- **验证过的边**：动作真实执行成功且页面状态确实变化 → 记转移边
  `obs2 --click obs2:e5--> obs3`；失败/无变化不记边（"边是走出来的，不是声明的"）
- **完成判定**：LLM 提议 + 代码校验——执行 <2 步的完成宣告无效；
  目标动作必须有 `from≠to` 的已验证转移（"点过失败 ≠ 完成"）

### 3. refs-only Planner 与确定性编译（架构 v2 核心）

职责分离——**AI 负责"想操作谁"，代码负责"DOM 里谁对应它"**：

- grounded 模式下 Planner **只从引用表选 `target_ref`**，禁止生成任何定位字段——连
  `click` 都不写，状态变化由"已验证转移编号"表达；违规进 Schema Recovery（约束修复
  ×1，带引用表上下文），恢复仍失败 → 明确拒绝
- locator 由 Compiler 从观察到的元素数据**确定性编译**：
  `obs3:e17` → `{"role": "button", "name": "Add to cart", "identity": {...}}`
  （确定性 > Planner：编译产物覆盖 Planner 手写字段）
- 执行前防线 `ensure_executable_targets`：拒绝未编译的 ref-only 步骤（防手改 DSL 绕过）
- 无探索的降级路径保留 legacy 生成能力（LLM 直接生成定位字段），行为不变

### 4. 状态接地校验（G3）

纯静态推导，不跑浏览器：建 ref → 所属状态的权威映射 + 转移边索引，逐步推导
每步的 expected state（goto 按 URL、click 沿唯一转移边推进、fill/断言不改状态）：

```
被引用元素的所属状态 ≠ 推导出的当前状态 → STATE_GROUNDING_MISMATCH
编造不存在的 ref                      → UnknownTargetRefError
引用不可达的孤儿状态                  → UnreachableObservationError
```

来源是两个独立站点复现的真实回归（`backend/regressions/`）：列表页点进详情页后，
下一步却引用列表页元素。**fail-open**：推导断链处不猜不拒——只拒绝可证明的错位。

### 5. 三分法定位 + 评分门槛 + 时间预算

```
1 个命中 → 评分裁决后操作
N 个命中 → 可见性过滤 → 同一元素判定 → 业务实体聚类；仍不确定 → 明确拒绝（绝不 nth 猜测）
0 个命中 → 5s 全局预算内轮询 → 超时明确失败
```

- **评分裁决**（R2）：不再是"第一个唯一命中胜出"——correction(130) > identity_exact(120)
  > test_id(100) > role exact(90) > decorated(80) > text(60) > fuzzy(50，**导航短名禁用**)
  > css(30)；winner 与不同身份来源的竞争证据分差 < 20 → `LowConfidenceError` 拒绝
- **两阶段 + 全局 deadline**：Phase A 立即全量 `count()` 扫描；全零才在 ≤5s 预算内轮询
  （150ms/次）——整个定位过程时间上界与候选数量无关
- `wait_for` / `assert_visible` 走 allow_lazy：定位唯一后再单独等可见（预算 ≠ 超时）

> 为什么多个匹配不自动选第一个？点错元素可能"执行成功、测试变绿"——假成功
> 比明确失败更危险。真实踩坑：`"Cart"` 模糊匹配命中 `"Add to cart"`。

### 6. 实例身份（I1）

同名元素（12 个 Add to cart）的"哪一个"由探索期采集的证据确定：

- **探索期**：对 observation 内同名重复元素采集容器锚点（`scope_has_text`，
  跳过价格/短行）与稳定业务身份（`data-product-id` / `data-item-id`）；非重复零开销
- **编译期**：同名 >1 且有锚点 → 自动附加 `Scope(has_text=...)`；有 identity →
  编译为最高分自动策略 `identity_exact`；容器外无锚点 → 记录 `unscoped_duplicates`
  （诚实拒绝，留给 corrections）
- **执行期**：scope 是证据不是命令——仍过三分法 + 评分 + margin 门槛

### 7. 生成质量门与自愈（GQ / GQ2）

- **探索完成性校验**：动作过少的"探索完成"宣告被拒绝并反馈继续探索
- **缓存门槛**：`done=True 或已执行 ≥2 步` 才缓存——好探索不浪费，浅探索不毒化
- **目标覆盖检查**：goal 要求"加购/登录/结算"而计划无对应 click → 警告/硬失败
  （断言可见性不算覆盖——实测 9/10 类"看似完整实则漏动作"被提前暴露）
- **硬失败 + 自愈重生 ×1**：可证明不完整的计划不返回——记录反模式（脱敏摘要）→
  带负例重新规划一次 → 仍失败 400 明确报错；**绝不静默返回不完整计划**

### 8. 修正闭环（L1）

执行失败步骤可提交持久化覆盖规则（`corrections.json`，键 = URL 模式 + 语义键）：

- **不绕过 Resolver**：修正以 130 分最高候选进入统一裁决，仍过唯一性 + 评分 + margin
- **统计与熔断**：成功 `verified_count+1`；连续失败 ≥3 自动禁用
- 措辞红线：叫"持久化覆盖规则"，不叫"学习"

### 9. 安全与隐私

- **凭据提取即脱敏**：账号/密码在进入 LLM 之前替换为 `${var}`，真实值只存本地内存；
  探索历史与页面快照落盘前二次脱敏（含登录后页面显示的用户名）
- **goto SSRF 防护**：scheme 白名单 + 拒绝 localhost / 私网段（防内网探测）
- **artifact 路径穿越防护**：resolve + 前缀校验
- **XSS**：前端所有用户可控字段 `escapeHtml`
- **危险操作闸口**：action_executor 对破坏性操作的控制

### 10. 指标与观测

每次生成/执行自动落盘 `timings.jsonl`（脱敏）→ `metrics.py` 聚合：成功率 /
p50·p95 / 定位策略分布 / 探索 LLM 调用数。步骤级 `resolve_ms` / `resolved_by`
进入执行证据。

---

## 测试

零依赖 plain-assert 脚本（项目无 pytest），直接运行：

```bash
py backend/tests/test_grounding.py     # G3 状态接地（15 项）
py backend/tests/test_compiler.py      # 编译 + I1 消歧（36 项）
py backend/tests/test_resolver.py      # 定位语义防漂移（31 项）
py backend/tests/test_explore_state.py # 探索状态机（41 项）
py backend/tests/test_ax_provider.py   # CDP AX provider（真实浏览器冒烟，无浏览器则 SKIP）
py backend/tests/test_capture_text.py  # 运行时变量捕获（4 项）
# …共 12 个测试文件、191 个测试函数
```

测试纪律：**复现即固化**——每个真实 E2E 缺陷先复现、再固化成回归断言；
协议正确性用单测，集成正确性必须真实浏览器 E2E 背书。

---

## 当前范围与后续规划

### 已实现

- 自然语言 → 探索 → refs-only 规划 → 确定性编译 → Playwright 执行全链路
- 动作集：goto / click / fill / select / check / wait_for / assert_visible /
  assert_text / assert_url / **capture_text**（运行时变量捕获，跨页断言 `${var}`）
- 三分法定位 + 评分门槛 + 5s 全局预算；实例身份消歧（I1）
- G3 状态接地校验 + 质量门 + 自愈重生；L1 修正闭环
- SSE 实时进度；探索缓存；凭据脱敏；危险操作闸口
- 12 个测试文件、191 个测试函数全部通过；两个跨状态回归固化

### 后续规划

| 方向 | 说明 |
|------|------|
| AI 生成用例套件 | 一次探索生成多条用例（候选路径枚举 + LLM 选择 + 覆盖报告） |
| 登录态复用 | `storage_state` 保存被测站点会话，避免反复登录污染状态图 |
| 运行时点击恢复 | 被遮挡时的"等待 → dismiss → retry"（参考项目有，当前明确失败） |
| Playwright Trace | 完整操作轨迹证据，与步骤级截图互补 |
| 用例持久化 | 当前 JSON/内存足够，生产版落库 |
| 视觉定位兜底 | A11y 语义覆盖不到的 canvas/SVG 场景（VLM，deferred） |

### 已知边界

- 无 A11y 语义的页面（canvas 自绘控件、纯 SVG）无法定位
- 探索有预算上界（超大站点探索不完 → 只警告不静默）
- 仅 Chromium（CDP 依赖；aria_snapshot 仅降级 fallback）
- 覆盖率无法形式化证明——用"可证明不完整就拒绝 + 可疑就警告"代替

---

## 文档索引

| 文档 | 内容 |
|------|------|
| `docs/ROADMAP.md` | 架构演进路线与决策记录（G1-G3 / R1-R5 / I1 / L1 / GQ） |
| `docs/architecture-comparison.md` | 与参考项目三方对比（BFC 场景实测数据） |
| `docs/execution-log.md` | 执行日志与实测记录 |
| `docs/bug修复.txt` | 编号问题档案（20 条评审修复） |
| `backend/regressions/` | 两个跨状态回归用例（固化） |
