# AI Web Testing Demo

AI 增强的 Web UI 自动化测试平台。

- **AI 生成**：自然语言 → DeepSeek API → 结构化 DSL（Pydantic 强校验）
- **Playwright 执行**：DSL → 真实浏览器 → 步骤级证据（截图）

项目共 **4 个 Python 文件 + 1 个 HTML 文件**，无前端构建步骤。

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
| `backend/dsl.py` | ~80 | DSL 数据结构（含结构化 target/scope），Pydantic 强校验 |
| `backend/ai_agent.py` | ~100 | LLM 调用 + Prompt 工程 + JSON 容错解析 |
| `backend/runner.py` | ~250 | Playwright 执行引擎 + 三分法定位 + 作用域消歧 |
| `backend/main.py` | ~100 | FastAPI 路由 + 静态托管 |
| `frontend/index.html` | ~250 | 单页 UI（零构建） |

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
