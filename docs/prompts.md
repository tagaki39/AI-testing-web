# 提示词（Prompt）设计清单

项目中的所有 LLM 提示词集中整理。共 **5 个 prompt**，分布在两个文件：
- [ai_agent.py](../backend/ai_agent.py)：生成链路（提取 URL → 生成 DSL → Preflight 重生）
- [explore_flow.py](../backend/explore_flow.py)：探索链路（决策下一步动作）

---

## 一、概览

| # | Prompt | 位置 | 用途 | 调用时机 |
|---|--------|------|------|---------|
| 1 | `EXTRACT_URL_PROMPT` | ai_agent.py | 从用户需求提取入口 URL | 生成链路阶段 1 |
| 2 | `DECIDE_PROMPT` | explore_flow.py | 探索决策：下一步动作 | 探索循环每步 |
| 3 | `SYSTEM_PROMPT` | ai_agent.py | DSL 生成器（角色设定） | 生成链路阶段 3 |
| 4 | grounded_prompt（动态组装） | ai_agent.py | 多页面结构 + 探索路径注入 | 生成链路阶段 3 |
| 5 | repair_prompt（动态组装） | ai_agent.py | Preflight 重生修正 | Preflight 发现问题时 |

**调用链全景**：

```
用户需求
  ↓ ① EXTRACT_URL_PROMPT     （提取入口 URL）
  ↓ ② DECIDE_PROMPT ×N       （bounded 探索，最多 8 次）
  ↓ ③ SYSTEM_PROMPT + ④ grounded_prompt（生成 DSL）
  ↓ Preflight 发现问题？
  ├─ 是 → ⑤ repair_prompt    （重生修正 → 再验证）
  └─ 否 → 执行
```

---

## 二、逐个拆解

### ① URL 提取器（EXTRACT_URL_PROMPT）

**完整内容**：
```
你是 URL 提取器。
从用户的测试需求中提取被测网站的入口 URL（以 http:// 或 https:// 开头）。

规则：
1. 如果没有明确提到 URL，返回 {"url": null}
2. 只输出 JSON，格式：{"url": "https://..."} 或 {"url": null}
```

**设计要点**：
- **为什么用 LLM 而不是正则**：用户说 "打开 saucedemo.com"（无协议头）正则提取不到，LLM 能补全成 `https://www.saucedemo.com`
- **null 降级**：没有 URL 就返回 null，上层降级为无探索直接生成（不中断）
- **输出格式锁死**：只允许 `{"url": ...}` 一种结构，解析零歧义

### ② 探索决策器（DECIDE_PROMPT）

**完整内容**（模板，运行时填充）：
```
你是 Web 页面探索器。目标：通过执行页面操作，找到完成用户目标所需的页面路径和元素。

当前状态：
- 用户目标: {goal}
- 当前 URL: {url}
- 页面标题: {title}
- 当前页面结构（ARIA snapshot）:
{snapshot}

最近操作历史:
{history}

你的任务：决定下一步动作。只输出 JSON：
{
  "reason": "为什么这么做",
  "goal_met": false,
  "action": "click | fill | press | wait | back | finish",
  "target": {"role": "button", "name": "..."} 或 {"text": "..."},
  "value": "fill 要填入的值（用户目标里给出的测试数据，直接用真实值）"
}

规则：
1. action 只能从上面 6 种选；goal_met=true 时 action 必须是 finish
2. target 必须基于当前页面快照中真实存在的元素
3. 每一步只做一个动作
4. 当页面已具备完成用户目标所需的信息时，goal_met=true 并输出 finish
```

**设计要点**：
- **上下文五件套**：目标 / URL / 标题 / 快照（截断 4000 字符）/ 最近 3 步历史——给 LLM 决策所需的全部信息
- **LLM=Planner，Playwright=Executor**：LLM 只输出结构化动作，绝不输出代码——可执行性、安全性由此保证
- **goal_check 内联**：`goal_met` 字段每步判断"页面是否已满足目标"——完成即停止，不机械走满流程
- **动作白名单 6 种**：click / fill / press / wait / back / finish——覆盖登录类流程的最小动作集
- **探索用真实值**：fill 的值直接用真实数据（探索需要真实登录才能前进），变量化留给 Planner

### ③ DSL 生成器（SYSTEM_PROMPT）

**完整内容**：
```
你是一个 Web UI 自动化测试的 DSL 生成器。
根据用户描述的自然语言测试需求，输出一个 JSON 对象，格式如下：

{
  "name": "用例名称",
  "description": "用例描述",
  "base_url": "被测网站入口URL",
  "input_contract": [{"key": "变量名", "value": "默认值"}],
  "steps": [
    {"action": "goto", "value": "https://xxx.com"},
    {"action": "input", "target": "textbox=用户名", "value": "${变量名}"},
    {"action": "click", "target": "button=登录"},
    {"action": "wait_for", "target": "heading=首页"},
    {"action": "assert_text", "value": "要验证的文字"}
  ]
}

规则：
1. action 只能是: goto, click, input, wait_for, assert_text
2. target 定位格式：优先使用语义定位 "角色=名称"，例如 button=登录、link=首页、textbox=邮箱、heading=标题
3. 用户没提供的登录信息，用 input_contract 定义变量，steps 里用 ${变量名} 引用
4. assert_text 用于验证页面包含某段文字
5. 只输出 JSON，不要输出任何解释或代码块标记
```

**设计要点**：
- **few-shot 完整示例**：给出完整 JSON 范例——"照着这个格式写"比"按规则写"有效得多
- **白名单 action**：5 种动作锁死，与 Pydantic 的 Literal 校验双重把关
- **语义定位约定**：target 用 `角色=名称`，与执行器三分法对齐
- **变量契约**：用户没给的信息用 `${var}` + input_contract——生成与执行解耦
- **temperature=0.2**：生成测试要确定性，不"创作"

### ④ 生成上下文组装（grounded_prompt，动态）

**组装逻辑**（代码片段）：
```
目标页面入口: {entry_url}

探索路径（已按此流程访问过以下页面）:
- click {'role': 'button', 'name': 'Sign in'} @ https://xxx.com
- fill ... @ https://xxx.com/login

各页面真实结构（ARIA snapshot）：

[页面 1] https://xxx.com（标题: ...）
{snapshot}

[页面 2] https://xxx.com/inventory.html
{snapshot}

用户测试需求: {user_prompt}

规则：
1. 用户提供的测试数据（如账号密码）用 ${var} 占位，并加入 input_contract 给出默认值；
2. 快照中以 'text: xxx' 形式出现的标题（span/div 无 heading 语义）
   必须用 text=xxx 定位，禁止写 heading=xxx。
```

**设计要点**：
- **探索路径注入**：告诉 LLM"怎么走到每个页面"——生成 goto 时知道该去哪
- **多页面快照分段**：`[页面 N] URL` 分段标记，LLM 知道每个元素属于哪个页面
- **规则 1（变量化）**：测试数据不硬编码进 DSL，用 `${var}` + input_contract——执行时由用户填
- **规则 2（span 标题坑）**：实测踩坑总结——saucedemo 标题是 `<span>` 无 heading 语义，直接告诉 LLM 避免再犯

### ⑤ Preflight 重生器（repair_prompt，动态）

**组装逻辑**：
```
目标页面入口: {entry_url}
页面真实结构（ARIA snapshot）:
{multi_snapshot}

你上次生成的 DSL 存在以下定位问题（共 N 处）：
- 'button=Add to cart': 页面存在 6 个同名 button，建议使用 scope 消歧
- ...

请基于页面真实结构修正这些 target：
存在多个同名元素时，scope 的值必须是页面中真实可见的文本
（如商品名称，不能是 CSS 类名）；不存在的元素改用快照中真实存在的元素；
快照中以 'text: xxx' 形式出现的标题（span/div 无 heading 语义）
必须用 text=xxx 定位，禁止写 heading=xxx。
输出完整的修正后 DSL JSON（其余步骤保持不变）。只输出 JSON。
```

**设计要点**：
- **问题清单反馈**：把 Preflight 的验证结果（不存在 / 歧义）逐条列给 LLM——"错在哪"比"重新生成"有效
- **修复指令具体化**：scope 值必须用真实文本（实测 AI 曾用 CSS 类名当 scope）——把踩过的坑写进指令
- **要求输出完整 DSL**：不是只修错的步骤，而是整体重新生成（保持其他步骤不变）——避免破坏结构
- **重生后双重验证**：修复结果再跑一次 Preflight，记录 remaining_issues（不无限重生，预算控制）

---

## 三、设计模式总结（面试重点）

5 个 prompt 共用了 4 个设计模式：

| 模式 | 例子 | 作用 |
|------|------|------|
| **角色设定**（你是谁） | "你是 Web 页面探索器" / "你是 DSL 生成器" | 限定 LLM 行为范围 |
| **JSON 格式锁死** | 只输出 `{"url": ...}` / 完整 JSON 示例 | 输出可解析、可校验 |
| **白名单约束** | action 只能 6 种 / 5 种 | 降低幻觉，配合 Pydantic 双重把关 |
| **规则锚定**（把踩过的坑写进 prompt） | "span 标题用 text= 定位" / "scope 用真实文本" | 经验固化——每修一个 bug 就加一条规则 |

**一句话**：prompt 的质量 = 格式约束（让输出可解析）+ 经验规则（让输出不犯错），两者配合 Pydantic 校验形成"AI 输出质量"的三重保障。

---

## 四、演进历史（怎么迭代到现在的）

| 版本 | 变化 | 触发原因 |
|------|------|---------|
| v1 单 prompt | 只有一个 SYSTEM_PROMPT，AI 盲猜生成 | 初始版 |
| v2 + 快照注入 | grounded_prompt 加入单页 ARIA 快照 | AI 生成 heading=Home 不存在 |
| v3 + URL 提取 | 新增 EXTRACT_URL_PROMPT（阶段 1） | 用户不写完整 URL |
| v4 + Preflight 重生 | 新增 repair_prompt + 双重验证 | 生成歧义/不存在的 target |
| v5 + 探索决策 | 新增 DECIDE_PROMPT（bounded 探索） | 单页快照覆盖不了登录后页面 |
| v5.1 + 规则锚定 | grounded/repair 加"span 标题用 text= 定位" | 实测 AI 惯性写 heading=Products |

**规律**：每次"AI 出错"→ 修 prompt（加规则或加阶段），而不是修代码去容忍错误——prompt 是 AI 生成质量的第一道防线。

---

## 五、面试话术

> "项目的 AI 部分设计了 5 个提示词，覆盖提取、探索、生成、修复四个环节。核心设计三条：一是**角色 + 格式锁死**，所有 prompt 都要求只输出 JSON，配合 Pydantic 白名单校验，保证输出可解析可校验；二是 **LLM 只做规划不做执行**，探索阶段 LLM 只输出结构化动作，Playwright 负责执行；三是**规则锚定**，把实测踩过的坑写进 prompt——比如 saucedemo 的标题是 span 没有 heading 语义，就在生成和修复的 prompt 里都明确'必须用 text= 定位'。每修一个 bug，就沉淀一条 prompt 规则，这是 AI 输出质量能持续提升的根本原因。"
