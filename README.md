# AI Web Testing Demo

AI 增强的 Web UI 自动化测试平台。

- **AI 生成**：自然语言 → DeepSeek API → 结构化 DSL（Pydantic 强校验）
- **Playwright 执行**：DSL → 真实浏览器 → 步骤级证据（截图）

项目共 **8 个 Python 文件 + 1 个 HTML 文件**，无前端构建步骤。

---

## 快速开始

```bash
cd ai-testing-demo

# 1. 创建 .env（填你的 DeepSeek key）
echo "AI_API_KEY=你的key" > .env

# 2. 安装依赖
py -m pip install fastapi uvicorn playwright pydantic

# 3. 安装浏览器（如已安装过可跳过）
py -m playwright install chromium

# 4. 启动
cd backend
python main.py
```

浏览器打开 **http://127.0.0.1:9000**。

---

## 使用流程

1. 输入自然语言需求，如：`打开 https://example.com，验证页面包含文字 "Example Domain"`
2. 点击「AI 生成 DSL」→ AI 返回结构化 DSL JSON，可在编辑框中人工调整
3. 点击「执行测试」→ Playwright 执行全部步骤，逐步骤展示状态与截图

### 量化指标

每次生成/执行自动追加耗时与定位策略记录（`timings.jsonl`，已脱敏）。
聚合输出 ROADMAP §8 核心指标（Planner 成功率 / 各阶段 p50/p95 /
定位策略分布 / resolve 延迟等）：

```bash
py backend/metrics.py
```

---

## 架构

```
用户自然语言
    │
    ▼
[ai_agent.py]  DeepSeek API 生成 DSL JSON
    │                 │
    │          [dsl.py] Pydantic 强校验 ← 安全边界
    ▼                 │
[runner.py]     Playwright 执行（for 循环逐步骤）
    │
    ▼
步骤级证据（状态 + 截图 + URL）
```

| 文件 | 行数 | 职责 |
|------|------|------|
| `backend/dsl.py` | ~150 | DSL 数据结构（含结构化 target/scope），Pydantic 强校验 |
| `backend/explore_flow.py` | ~420 | bounded 探索：element ref 表 + Observation State Graph |
| `backend/explore_cache.py` | ~70 | 探索结果缓存（脱敏落盘） |
| `backend/ai_agent.py` | ~1430 | 双模式 Planner（refs-only / legacy）+ Preflight 修复链路 |
| `backend/grounding.py` | ~175 | G3 State Grounding Validator（跨状态引用执行前拒绝） |
| `backend/compiler.py` | ~80 | R1 LocatorSpec Compiler（target_ref → Locator 确定性编译） |
| `backend/resolver.py` | ~220 | R1 Semantic Resolver：定位语义单一事实源（解析/候选顺序/导航限制/快照匹配） |
| `backend/runner.py` | ~390 | Playwright 执行引擎：三分法编排 + 作用域消歧 + 时间预算 |
| `backend/main.py` | ~150 | FastAPI 路由 + 静态托管 |
| `frontend/index.html` | ~230 | 单页 UI（零构建） |

---

## DSL 格式

`target` 支持字符串与结构化两种写法，`scope` 用于同名元素消歧：

```json
{
  "action": "click",
  "target": "button=Add to cart",
  "scope": "Blue Top"
}
```

```json
{
  "action": "click",
  "scope": {"role": "listitem", "has_text": "Blue Top"},
  "target": {"role": "button", "name": "Add to cart"}
}
```

`target` 支持的定位方式：`{"role","name"}` 语义 / `{"text"}` 文本 / `{"test_id"}` / `{"css"}`。

### 定位三分法

遵循 Playwright 官方推荐的心智模型：

- **0 个匹配** → 报"未找到"（`LocatorNotFoundError`）
- **1 个匹配** → 使用
- **2+ 个匹配** → 报"歧义"（`LocatorAmbiguousError`），**绝不自动选择第一个**，提示用 scope 消歧

> 为什么不在匹配到多个时自动选第一个？页面改版后，被选中的可能不再是目标元素——宁可靠错误，不可点错元素。

---

## 核心设计

### 1. DSL 作为安全边界

AI 只负责生成结构化测试步骤，执行是确定性的 Playwright 代码。所有 DSL 经过 Pydantic 强校验，非法 action 在进入执行器之前被拦截——安全、可复现、可审计。前端传入的 DSL 同样经过校验，前后端都不能绕过。

### 2. 定位策略

写 DSL 时优先使用语义定位（`get_by_role("button", name="登录")`）——它基于浏览器的无障碍树（Accessibility Tree），不依赖 DOM 结构和 CSS 类名，前端改版后测试依然稳定，且不需要被测系统埋点。

执行器内部按稳定性降级：`data-testid`（页面有测试属性时最稳）→ 语义定位 → 文本 → CSS 兜底。

使用模糊匹配（`exact=False`）：真实页面常见 icon 前缀空格、CSS text-transform 大小写等，accessible name 与可见文本常不一致；歧义仍由三分法拦截（2+ 匹配直接报错，绝不自动选第一个）。

### 3. AI 生成质量保障

- 低温度采样（`temperature: 0.2`），输出稳定
- Prompt 严格约束 DSL 格式与 action 白名单
- JSON 容错解析（兼容 ```json 代码块标记）
- Pydantic 校验兜底，非法输出直接拒绝

### 4. 作用域消歧

页面存在多个同名元素（如 6 个 "Add to cart" 按钮）时，通过 `scope` 先锁定容器再在容器内查找：

```python
container = page.get_by_role("listitem").filter(has_text="Blue Top")
button = container.get_by_role("button", name="Add to cart")
```

### 5. refs-only Planner 与确定性编译（Architecture v2）

职责分离——**AI 负责"想操作谁"，代码负责"DOM 里谁对应它"**：

- 生成时先探索页面，产出带编号的元素引用表（`obs3:e17`）与状态转移图
- grounded 模式下 Planner **只从引用表选 `target_ref`**，禁止生成任何定位字段
  （违反契约进入恢复修复，仍失败则明确拒绝）
- `target_ref` 由 Compiler 从观察到的元素数据**确定性编译**成 target
  （`obs3:e17` → `{"role": "button", "name": "Add to cart"}`），
  用户可见的 DSL 格式不变，只是来源从 LLM 变为代码
- State Grounding Validator 在执行前拒绝跨状态引用（页面已跳转却仍引用
  上一页元素的步骤 → `STATE_GROUNDING_MISMATCH`）
- 无探索的降级路径保留 legacy 生成能力（LLM 直接生成定位字段）

### 6. 评分与置信度门槛（R2）

定位解析不再"固定顺序第一个唯一命中胜出"，而是**收集全部策略证据后评分裁决**：

| 策略 | 分数 | 语义 |
|------|------|------|
| test_id | 100 | 显式测试契约（最强身份） |
| test_id_attr | 95 | data-test/data-qa 属性变体 |
| role（exact） | 90 | 语义定位精确匹配 |
| role_decorated | 80 | 容忍图标前缀 |
| text | 60 | 文本定位 |
| role_fuzzy | 50 | 语义模糊匹配 |
| css | 30 | 兜底 |

- **放松组**：role/decorated/fuzzy 是同一身份的放松阶梯，组内不互相竞争
- **置信度门槛**：winner 与最强竞争证据（不同身份来源的命中/多匹配）的分差
  < 20 → `LowConfidenceError` 拒绝——**高分但 margin 低仍拒绝**，
  宁可靠错误，不可低置信度点击
- 拒绝原因完整可解释（winner/竞争证据/分差都在错误信息里）

### 7. 实例身份（I1）

同名元素（6 个 Add to cart）的"哪个"由探索期采集的证据确定，而不是执行时现猜：

- **探索期**：`_resolve_locator` 命中即标 `verified`（身份证据前移）；
  对 observation 内同名重复的元素，沿 DOM 祖先链（li/article/
  data-testid/data-product-id/data-item-id）采集容器首行稳定文本
  （跳过价格/短行/自身文本）作为 `scope_has_text` 锚点——
  **只对重复元素采集，非重复零开销**
- **编译期**：Compiler 发现 observation 内同名 >1 且有锚点 → 自动附加
  `Scope(has_text=...)`；唯一元素不附加（scope 最小化）；
  重复无锚点（容器外元素）→ 记录 `unscoped_duplicates`（L1 corrections 输入）
- **执行期**：scope 是证据不是命令——仍过三分法 + R2 评分 + margin 门槛；
  失败明确拒绝，绝不 nth 猜测

### 8. 修正闭环（L1：持久化定位覆盖规则）

执行失败的步骤可在前端**提交修正**（`test_id=xxx` / `css=xxx` / `text=xxx`），
保存为持久化覆盖规则（`corrections.json`，键 = URL 模式 + 语义键）：

- **不绕过 Resolver**：修正以最高分（130）候选进入统一裁决——
  仍过唯一性 + 评分 + margin 门槛；过期（0 命中）自然落回标准候选，
  歧义照常拒绝
- **统计与熔断**：命中后执行成功 `verified_count+1`、失败计数清零；
  连续失败 ≥3 自动禁用
- 措辞红线：叫"持久化覆盖规则"，不叫"学习"

---

## 当前范围与后续规划

### 已实现

- AI 生成 DSL + 人工编辑确认
- Playwright 执行：goto / click / input / wait_for / assert_text
- 三分法定位、作用域消歧、失败截图证据
- 变量替换（`${var}`），缺失变量明确报错

### 后续规划

| 方向 | 说明 |
|------|------|
| 页面结构感知 | 执行前抓取页面 ARIA snapshot 注入 Prompt，提升 AI 定位准确率 |
| 运行时变量捕获 | `capture_text` / `capture_attribute`，支持"记录价格 → 断言一致"场景 |
| 流式执行日志 | SSE 推送每步状态到前端（服务端单向推送场景，优于 WebSocket） |
| Trace 证据 | Playwright Trace Viewer 记录完整操作轨迹，与步骤结果互补 |
| 登录态复用 | `storage_state` 保存被测站点会话，避免重复登录 |
| 持久化 | 用例与执行记录落库，支持多轮回归 |
