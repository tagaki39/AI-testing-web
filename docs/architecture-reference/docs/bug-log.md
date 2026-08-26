# Bug 日志

用于沉淀在开发、联调、测试和执行过程中发现的问题，跟踪影响、状态和修复结论。

---

## BUG-K | paragraph/StaticText role 在 semantic locator 正则和映射中缺失

- 日期：2026-06-05
- 状态：fixed
- 来源：E2E 测试执行失败（Run 195-198）
- 描述：AI 生成的 DSL 中大量使用 `paragraph "X"` 格式 target，但 semantic locator 的 `_A11Y_ROLE_TARGET_RE` 正则不包含 `paragraph`，`_A11Y_TO_PLAYWRIGHT_ROLE` 映射也没有 `paragraph`。同时 prompt 推荐 AI 使用的 `StaticText` 角色也不在正则和映射中。
- 复现步骤：
  1. AI 通过 explore_flow 采集页面，产品名称在 a11y tree 中角色为 `paragraph`
  2. AI 生成 DSL target=`paragraph "Blue Top"`
  3. semantic locator 解析时正则不匹配，退化到纯文本搜索 `get_by_text('paragraph "Blue Top"')`，找不到元素
  4. 所有 tier 失败 → needs_intervention
- 影响：所有使用 `paragraph` 角色作为 target 的 DSL 步骤全部失败；prompt 推荐 `StaticText` 但 parser 不支持，AI 被误导
- 根因：① 正则和映射只包含交互式 ARIA 角色，遗漏了 `paragraph`；② `paragraph` 是有效 ARIA role，`get_by_role("paragraph")` 能找到元素，但 `get_by_role("paragraph", name=...)` 因 `<p>` 元素无 accessible name 而失败
- 处理：
  1. `_A11Y_ROLE_TARGET_RE` 加入 `paragraph|statictext`
  2. `_A11Y_TO_PLAYWRIGHT_ROLE` 加入 `"paragraph": "paragraph"`
  3. 新增 `_TEXT_ONLY_ROLES` 集合，text-only 角色优先用 `get_by_text()` 而非 `get_by_role(name=)`
  4. inside scoping 容器查找改为爬 3 级父元素
- 验证：手动 18/18 步全部通过；`paragraph "Blue Top"` 正确解析为 `get_by_text("Blue Top", exact=True)`
- 关联记录：`docs/execution-log.md#2026-06-05`

## BUG-L | explore_flow 的 click 动作用 a11y text 匹配不可靠，导致探索数据错误

- 日期：2026-06-05
- 状态：fixed
- 来源：AI Session 302 探索数据追踪
- 描述：AI 通过 `explore_flow` 执行 `click "Polo"` 时，`_resolve_step_locator` 使用 a11y text 匹配（`get_by_text`），可能匹配到错误元素（如 breadcrumb、文本节点），且点击失败时静默继续，不报错。导致探索采集到的数据来自错误页面（全部产品页而非 Polo 筛选页）。
- 复现步骤：
  1. AI Session 302 调用 explore_flow，step 为 `{"action": "click", "target": "Polo"}`
  2. a11y locator 找到元素并点击 → 但没有验证是否成功导航
  3. 探索采集的全部产品（34 个），而非 Polo 专属产品（6 个）
  4. AI 基于错误数据生成 DSL，使用了全产品页中不存在的价格和产品名
- 影响：所有依赖 explore_flow click 导航的探索都可能采集到错误页面的数据，导致后续 DSL 生成失败
- 根因：探索阶段 click 只用 a11y text 匹配（`get_by_text`），没有利用已采集节点的精确选择器（`verified_selectors` 如 `a[href="/brand_products/Polo"]`），也没有验证导航是否成功
- 处理：新增 `_resolve_from_collected_nodes` 函数，优先用上一页采集的 `verified_selectors` 或 DOM 属性（`data-product-id`、`href`）构造 CSS 选择器精确定位，失败才回退 a11y locator
- 验证：修复后探索精确采集 6 个 Polo 专属产品（vs 修复前 35 个全部产品）
- 关联记录：`docs/execution-log.md#2026-06-05`

## BUG-M | explore_flow 页面 URL 在动作执行前捕获，导航后节点归因错误

- 日期：2026-06-05
- 状态：fixed
- 来源：修复 BUG-L 后的测试验证
- 描述：`_collect_flow_a11y` 在动作执行前捕获 `current_url`，如果动作导致页面导航（如 click 链接），采集的节点属于新页面但被归因到旧页面的 URL 和 state。导致 `_deduplicate_explore_results` 按 URL 合并时删除正确数据。
- 复现步骤：
  1. 在 products 页面执行 `click "Polo"` → 导航到 `brand_products/Polo`
  2. `current_url` 在 click 前捕获为 products URL
  3. click 后采集的 173 个 Polo 页面节点被归因到 products URL state
  4. dedup 按 URL 合并，Polo 页面数据被 products 页面覆盖
- 影响：通过 click 导航后采集的节点 URL 与实际页面不匹配，数据聚合时丢失
- 根因：`current_url = page.url` 在动作循环之前执行，导航后的 URL 变化未被反映
- 处理：在 `results.append(page_entry)` 前检查 `page.url != current_url`，若导航发生则更新 URL、分配新 state、回写节点 state
- 验证：修复后 Polo 页面获得独立 S2 state，节点归因正确
- 关联记录：`docs/execution-log.md#2026-06-05`

---

## 模板

```md
## BUG-XXX | 标题

- 日期：YYYY-MM-DD
- 状态：fixed
- 来源：需求 / 自测 / 联调 / 线上反馈
- 描述：问题现象
- 复现步骤：
  1. 步骤一
  2. 步骤二
- 影响：功能、页面、模块或用户范围
- 根因：如果尚未定位，写"待定位"
- 处理：修复动作或计划
- 验证：已执行的验证；如果没有写"未验证"
- 关联记录：执行日志日期或链接
```

## 记录规则

- 发现一个明确问题时新增一条记录。
- 状态建议使用：`open`、`in_progress`、`fixed`、`wont_fix`。
- 每条记录尽量包含复现条件、影响范围、定位结论和验证方式。
- 如果问题来自某次任务执行，请回链到 `docs/execution-log.md` 中的对应记录。
- 最新的记录优先放到最上面，方便阅读。

## 分类索引

| 类别 | 概述 | 典型编号 |
|------|------|----------|
| **A. DSL 生成与归一化** | LLM 输出→结构化 DSL 链路的格式、字段、校验问题 | Bug #A–#G, BUG-077/078/083/070/085/056/048/045 |
| **B. 定位器系统** | 语义/CSS/VLM/坐标定位器匹配、策略、回退问题 | BUG-084/082/080/076/075/074/073/072/071/064/057/053/050/049/046, Bug #1 |
| **C. 页面探索与数据采集** | explore_page/flow、DOM/A11y 采集、缓存问题 | BUG-060/059/067(05-07)/068(05-06), Bug #2, Bug #A |
| **D. Prompt / AI 行为** | 硬编码 prompt、指令矛盾、角色推荐不一致 | Bug #K, Bug #L, Bug #M |
| **D. AI 决策与提示词** | ReAct 循环、提示词遵循、工具调用去重 | BUG-085/081/069(05-06)/066(05-12)/065(05-06)/054, Bug #C |
| **E. SSE 流式与前端** | 流式输出、会话管理、前端渲染 | BUG-063(05-04)/064(05-04)/058/044/042, Bug #D |
| **F. 执行引擎** | Playwright runner、变量替换、证据采集 | BUG-079/057/053/051/047, Bug #3 |
| **G. 配置与基础设施** | API 合同、权限、网络重试、DB 配置 | BUG-045/043/041, Bug #B |

> 注：同一 BUG 编号在不同日期出现不同内容时，以日期区分（如 BUG-065 (2026-05-06) vs BUG-065 (2026-05-12)）。

---

## A. DSL 生成与归一化

LLM 输出 → Pydantic 校验 → 结构化 DSL 步骤链路中的格式错误、字段缺失、校验失败问题。

### Bug #I | AI 生成 paragraph 格式导致 VLM fallback

- 日期：2026-06-04
- 状态：open
- 来源：E2E 测试
- 描述：AI 生成了 `paragraph "Premium Polo T-Shirts"` 格式，但 `paragraph` 不是有效的 Playwright role，导致语义定位失败，回退到 VLM。
- 复现步骤：
  1. 用户输入测试需求
  2. AI 生成 DSL 草案
  3. 执行 DSL 时，`paragraph` 格式导致语义定位失败
  4. 回退到 VLM，但 VLM 也失败（429 限流、返回 None、TypeError）
- 影响：所有使用 `paragraph` 格式的步骤都会失败
- 根因：`_format_elements_flat` 函数显示容器时使用了 `paragraph="Blue Top"` 格式，AI 混淆了容器格式和子元素格式
- 处理：
  1. 修改 `_format_elements_flat` 函数，使用 `[container]` 前缀区分容器和子元素
  2. 更新 DSL 生成器的 prompt，添加更明确的指导
  3. 添加 WRONG examples 告诉 AI 不要使用 `paragraph` 格式
- 验证：测试 `test_format_elements_flat_uses_container_prefix` 通过，但 E2E 测试仍失败
- 关联记录：Session 302, Execution 197/198

**可能产生的原因**：

1. **AI 模型理解能力不足**
   - AI 没有理解 `[container]` 前缀的含义
   - AI 混淆了容器格式和子元素格式
   - AI 没有遵循 prompt 中的指导

2. **Prompt 设计问题**
   - Prompt 中的示例不够清晰
   - AI 没有看到正确的示例
   - Prompt 中的规则太多，AI 无法全部遵循

3. **DSL 生成器与 Playwright 不适配**
   - DSL 生成器生成的是 a11y role 格式（如 `paragraph "Blue Top"`）
   - 但 Playwright 的语义定位器不支持 `paragraph` 格式
   - 这是一个架构问题：DSL 生成器和 Playwright 之间存在格式不匹配

4. **与其他项目的区别**
   - testbrand(mimov2.5pro) 使用 Selenium + CSS 选择器/XPath
   - 当前项目使用 Playwright + a11y role 格式
   - Selenium 支持多种定位方式，容错性强
   - Playwright 依赖 a11y role 格式，容错性弱

**为什么与 DSL 不适配**：

1. **格式不匹配**
   - DSL 生成器生成的是 a11y role 格式（如 `paragraph "Blue Top"`）
   - 但 Playwright 的语义定位器不支持 `paragraph` 格式
   - 这导致语义定位失败，回退到 VLM

2. **容错性不足**
   - Playwright 的语义定位器只支持 a11y role 格式
   - 没有其他定位方式作为备选
   - 当 a11y role 格式失败时，只能回退到 VLM

3. **VLM 模型不稳定**
   - VLM 模型遇到问题（429 限流、返回 None、TypeError）
   - 当 VLM 失败时，整个步骤就会失败

### Bug #J | AI 获取错误信息后没有生成新的 draft

- 日期：2026-06-04
- 状态：open
- 来源：E2E 测试
- 描述：AI 调用 `get_execution_detail` 获取错误信息后，没有生成新的 draft，而是重新执行了同一个 draft。
- 复现步骤：
  1. 用户输入测试需求
  2. AI 生成 DSL 草案（draft_id=218）
  3. 执行 DSL 失败
  4. AI 调用 `get_execution_detail` 获取错误信息
  5. AI 重新执行同一个 draft_id=218，而不是生成新的 draft
- 影响：错误信息注入没有生效，AI 无法从错误中学习
- 根因：AI 没有理解 `user_context` 中的错误信息，或者没有生成新的 draft 的逻辑
- 处理：需要修改 AI 的 ReAct 循环，让它在获取错误信息后生成新的 draft
- 验证：从日志验证，AI 确实获取了错误信息，但没有生成新的 draft
- 关联记录：Session 302, Execution 197/198

**可能产生的原因**：

1. **AI 没有理解错误信息**
   - AI 获取了错误信息，但没有理解如何使用它
   - AI 没有将错误信息与 DSL 步骤关联起来
   - AI 没有生成新的 draft 的逻辑

2. **ReAct 循环设计问题**
   - ReAct 循环中没有"生成新的 draft"的工具
   - AI 只能调用现有的工具（如 `get_execution_detail`）
   - 没有工具让 AI 生成新的 draft

3. **错误信息注入位置问题**
   - 错误信息被注入到 `user_context` 中
   - 但 `user_context` 只在 DSL 生成器的 prompt 中使用
   - AI 的 ReAct 循环没有使用 `user_context`

4. **AI 模型能力不足**
   - AI 没有理解"重新生成"的含义
   - AI 没有从错误中学习的能力
   - AI 只是重新执行了同一个 draft

**为什么与 DSL 不适配**：

1. **错误信息没有传递给 DSL 生成器**
   - 错误信息被注入到 `user_context` 中
   - 但 AI 的 ReAct 循环没有使用 `user_context`
   - DSL 生成器没有看到错误信息

2. **没有重新生成机制**
   - AI 的 ReAct 循环中没有"生成新的 draft"的工具
   - AI 只能调用现有的工具
   - 没有机制让 AI 重新生成 draft

3. **架构问题**
   - 错误信息注入和 DSL 生成是分离的
   - 没有将错误信息传递给 DSL 生成器的机制
   - 需要重构架构，让错误信息能够传递给 DSL 生成器

### Bug #H | 分段生成模式 input_contract 为空导致变量占位符未替换

- 日期：2026-05-28
- 状态：fixed
- 来源：E2E 测试
- 描述：用户提供测试数据 `账号：Xjy13302412005@outlook.com，密码：123456`，但执行时 input 步骤直接输入 `${email}` 而非实际邮箱。
- 根因：`generate_segmented_case_draft` 硬编码 `"input_contract": []`，导致步骤中的 `${email}` 等占位符无法被 `_build_input_values_from_session` 解析为实际变量值。
- 处理：新增 `_extract_input_contract_from_steps` 函数，从步骤的 `${...}` 占位符自动提取并生成 `input_contract`，支持变量类型推断和去重。
- 验证：37 单元测试通过
- 关联记录：Session 247 Draft 176

### Bug #G | LLM 生成 assert_text 缺 value；字段别名表定义但未接入 normalizer

- 日期：2026-05-25
- 状态：fixed (`2d3161a`)
- 来源：E2E 回归测试
- 描述：5 个 segment 全部成功生成共 16 步骤，但 `DSLCase.model_validate` 抛 `steps.15.assert_text.value Field required`。LLM 把期望文本放进 `target` 字段，漏填 `value`。
- 根因：
  - `_normalize_llm_step` 此前仅处理 `goto/assert_url_contains` 的 target→value 移动，未覆盖 `assert_text`
  - `_STEP_TARGET_ALIASES` / `_STEP_VALUE_ALIASES` / `_STEP_TIMEOUT_ALIASES` 三张别名表已定义但全文无调用（孤儿数据）
- 处理：
  - `_normalize_llm_step` 接入三张别名表，用 `_promote_first_alias()` 转换
  - `assert_text` 特殊修复：value 缺 + target 在 → target 移到 value，target 兜底为 `"body"`
  - `input`/`click`/`wait_for`/`capture_text` 必填字段缺失时返回 None 让 normalizer 丢弃
  - `_build_segment_prompt` 分别枚举每类 action 的字段要求，给正反例
- 验证：542/544 单元测试通过
- 关联记录：execution-log.md 2026-05-25（Bug A→F 链路收尾）

### Bug #F | LLM 生成 goto/assert_url_contains 步骤时 target↔value 字段错位

- 日期：2026-05-25
- 状态：fixed
- 来源：E2E 回归测试
- 描述：LLM 调用成功返回，但 `DSLCase.model_validate` 抛 `steps.0.goto.value Field required`，LLM 把 URL 错填到 `target` 字段。
- 根因：`goto` 和 `assert_url_contains` 用 `value` 存 URL，但 segment prompt 没显式区分字段；`_ACTION_ALIASES` 字典已定义但全文未使用（governance 清理遗留废代码）。
- 处理：
  - 新增 `_normalize_llm_step(step)`：激活 `_ACTION_ALIASES`（open/navigate/visit → goto 等），对 `_URL_VALUE_ACTIONS = {"goto", "assert_url_contains"}` 自动把 target 搬到 value
  - `_build_segment_prompt` 增加显式规则：`goto/assert_url_contains 使用 'value' 存放 URL`
- 验证：5 个 segment 全部成功
- 关联记录：execution-log.md 2026-05-25

### Bug #E | _log_dsl_cache_usage 函数被 governance 清理误删但调用方残留

- 日期：2026-05-25
- 状态：fixed (`f22ccb8`)
- 来源：E2E 回归测试
- 描述：LLM 调用成功返回，但 segment 报 `name '_log_dsl_cache_usage' is not defined`，最终又抛出"所有分段均未生成步骤"。
- 根因：commit `8d92654`（refactor: delete governance system）把函数定义一起删掉，但 `_call_llm:402` 和 `_call_dsl_flash_llm:510` 仍保留调用。Bug A 修复让 LLM 调用真正成功后，这个潜伏代码 rot 才暴露。
- 处理：恢复 `_log_dsl_cache_usage` 函数定义（参照 commit `6372a8f`），加 `isinstance` 防御
- 验证：segment 正常生成步骤
- 关联记录：execution-log.md 2026-05-25

### Bug #B | LLM 调用无重试 + 网络错误消息误导

- 日期：2026-05-25
- 状态：fixed
- 来源：E2E 回归测试
- 描述：`urlopen WinError 10060`（TCP 超时连接 `api.deepseek.com`）单次失败即终止整段 DSL 生成，且最终错误为"所有分段均未生成步骤"——误导用户以为是元素问题。
- 根因：`_call_dsl_flash_llm` 直接调 `request.urlopen` 无重试、无退避；错误消息未区分网络失败 vs 真正的"无元素"问题。
- 处理：
  - 新增 `_urlopen_with_retry`（指数退避 1s→2s，2 次重试）+ `_is_transient_network_error`
  - 新增 `DslGenerationNetworkError` 异常，给出准确诊断（含 host、错误类型、排查建议）
  - `generate_segmented_case_draft` 末尾判断 warnings 是否全为网络错误关键字，命中则抛 `DslGenerationNetworkError`
- 验证：网络错误时给出准确诊断而非误导信息
- 关联记录：execution-log.md 2026-05-25

### Bug #D | stream_planning_turn 把 Pydantic plan 当 dict 用导致 AttributeError

- 日期：2026-05-25
- 状态：fixed (`67f021a`)
- 来源：E2E 回归测试
- 描述：日志反复出现 `Auto DSL generation failed: 'AIPlanningPlan' object has no attribute 'get'`，导致计划生成后跳过自动 DSL 草案。
- 根因：`response.plan` 是 `AIPlanningPlan` Pydantic 模型，代码写成 `plan_json.get("scenarios", [])`。
- 处理：改为 `plan_data = response.plan.model_dump(mode="json") if response.plan else {}`
- 验证：自动 DSL 草案正常生成
- 关联记录：execution-log.md 2026-05-25

### Bug #A | single-segment 路径下 a11y_nodes 数据丢失

- 日期：2026-05-25
- 状态：fixed
- 来源：E2E 回归测试
- 描述：AI 规划 fallback plan 时 `scenario["flow_steps"]=[]`，走 single-segment 分支。后端日志显示 `a11y_nodes=1136` 但 `has_page_elements=False`，LLM 拿到的 prompt 中 `Available elements: (no elements)`。
- 根因：`ai_planning.py:561` 调 `generate_dsl_case` 时未把 `a11y_nodes_raw` 传过去；`dsl.py:147` 中 `page_elements_by_state` 硬编码为 `{}`。
- 处理：
  - `GenerateDslRequest` 新增 `a11y_nodes_by_state: dict[str, list[dict]] | None` 字段
  - `dsl.py` 从 `payload.a11y_nodes_by_state` 读取
  - `ai_planning.py` 单段分支按 `page_state` 分组 `a11y_nodes_raw` 后通过 payload 传入
  - `dsl_generator.py` 在 `flow_steps=[]` 但 `page_elements_by_state` 有数据时按 page_states keys 迭代
- 验证：single-segment 路径正常生成步骤
- 关联记录：execution-log.md 2026-05-25

### BUG-085 | DeepSeek thinking 模式 + 高温导致 AI 不遵循提示词指令

- 日期：2026-05-10
- 状态：fixed
- 来源：BUG 日志聚合分析
- 描述：综合 BUG-081/069/065/054 等高频问题，根因指向两点：(1) thinking 模式对指令遵循有负面影响；(2) DeepSeek API 默认 temperature=1.0 过高。
- 处理：
  1. 在 dsl_generator.py、test_planning_agent.py、judge_agent.py 中移除 DeepSeek 的 thinking mode（仅保留 GLM）
  2. 按场景设置 temperature：DSL generator 0.0、DSL flash 0.0、Planning agent 0.1、Judge 0.0
- 验证：542/544 单元测试通过

### BUG-083 | AI 将 assert_text 的 ${var} 放在 target 而非 value 导致断言被删除

- 日期：2026-05-07
- 状态：fixed (7958d3b, 未验证)
- 来源：E2E 回归测试
- 描述：AI 生成 `assert_text target='${product_a_name}' value=''`，Pydantic `min_length=1` 拒绝空 value → 8 个断言步骤被归一化器删除。
- 根因：AI 模型混淆 assert_text 的 target（元素定位器）和 value（期望值）字段。
- 处理：prompt 添加"target 是页面文本，value 是 ${var}"规则 + 归一化器自动补全
- 验证：未验证（需新 session 生成 draft 确认）

### BUG-078 | DSL 归一化器删除了合法的 click/wait_for/capture_text 步骤

- 日期：2026-05-07
- 状态：fixed (`ecbbb3a`)
- 来源：E2E 回归测试
- 描述：AI 给 click/wait_for/capture_text 步骤添加了 `"value": null` 字段，Pydantic `extra_forbidden` 拒绝，步骤被静默删除。
- 根因：`_repair_step_shape` 未剥离 click/wait_for/capture_text 的 spurious `value` 字段。
- 处理：在 `_repair_step_shape` 末尾对这些步骤类型移除 `value` 键。
- 验证：58 个 DSL 单元测试通过，Exec 106 证实 0 步骤被删
- 关联记录：Draft 80 8 步骤被删，Draft 81 0 步骤被删

### BUG-077 | DSL 归一化器删除了合法的 goto/assert_url_contains 步骤

- 日期：2026-05-07
- 状态：fixed (`8d05871`)
- 来源：E2E 回归测试
- 描述：AI 给 goto 和 assert_url_contains 添加了 `candidates: []` 和 `postconditions: []` 字段，Pydantic `extra_forbidden` 拒绝，goto 步骤被丢弃。
- 根因：AI 给所有步骤统一加了 candidates/postconditions 空数组，但 GotoStep 和 AssertUrlContainsStep 模型没有这些字段。
- 处理：在 `_repair_step_shape` 中对 goto/assert_url_contains 剥离 candidates/postconditions。
- 验证：58 个 DSL 单元测试通过

### BUG-070 | DSL generator thinking mode 下 reasoning_content 空响应

- 日期：2026-05-06
- 状态：fixed (`9f67995`)
- 来源：E2E 回归测试
- 描述：DSL generator 使用 DeepSeek thinking mode 时，模型返回 `reasoning_content` 但 `content` 为空 → JSON 解析失败 → 草案状态 failed。
- 根因：`_extract_message_content` 只读 `content` 字段，忽略了 `reasoning_content`。
- 处理：在 content 为空时 fallback 到 `reasoning_content`
- 验证：Draft 62 生成成功（33 步）

### BUG-065 | DSL prompt 未要求 capture_text 后必须跟 assert_text

- 日期：2026-05-06
- 状态：fixed (`a631041`, `c5e4411`)
- 来源：E2E 回归测试
- 描述：AI 生成 5 个 capture_text 但 0 个 assert_text，测试表面全部通过但核心断言完全缺失。
- 根因：DSL prompt 只说明了 capture_text 用法，未强制要求 capture 后必须 assert。
- 处理：在系统 prompt 和用户规则中增加"capture 必须 assert"规则；新增"modify→input→assert"规则。
- 验证：Draft 69 有 10 个 assert_text（vs Draft 66 的 0 个）

### BUG-056 | DSL draft prompt 超 50000 字符导致 Pydantic 校验失败

- 日期：2026-05-03
- 状态：fixed
- 来源：Session 15 E2E 测试
- 描述：`_build_draft_prompt` 将 80K+ 字符的 page_elements 直接嵌入 `draft_prompt`，触发 `max_length=50000` 限制。
- 根因：`page_elements` 数据在两个渠道重复传递——嵌入 prompt + 独立字段。
- 处理：嵌入式 DOM section 替换为简短提示，实际数据通过 `GenerateDslRequest.page_elements` 单独传递。
- 验证：471 单元测试通过

### BUG-048 | AI DSL 规划阶段完整性校验缺失

- 日期：2026-04-21
- 状态：fixed
- 来源：白盒测试（The Internet Login Page）
- 描述：AI 生成 DSL 时遗漏 goto 步骤，base_url 设为完整登录页 URL 但不生成 goto 步骤，执行器在 about:blank 上操作。
- 处理：(1) Prompt 增加测试五要素完整性引导；(2) 后处理新增 `_check_dsl_completeness` 函数。
- 验证：5 passed

### BUG-045 | AI planning "保存并执行草案"链路被 DSL 生成配置阻断

- 日期：2026-04-13
- 状态：in_progress
- 来源：白盒排查 / session_id=27
- 描述：`AI_DSL_BASE_URL=https://api.unself.cn` 返回 `200 text/html` 站点首页而非 OpenAI 兼容 JSON → `JSONDecodeError`；且 draft 生成/执行结果未持久化到 `ai_planning_messages`。
- 处理进展：已修正 `AI_DSL_BASE_URL`；已为 `_call_llm()` 增加非 JSON 响应防御；已将结果持久化到 messages。剩余：执行中流式事件推送。
- 验证：数据库实查 + 最小 HTTP 复现

---

## B. 定位器系统

语义定位器、CSS/XPath、VLM 视觉定位、坐标点击回退等定位策略的匹配、优先级、回退链路问题。

### BUG-084 | text_parent_chain 在品牌页第二个产品上回退到 VLM

- 日期：2026-05-07
- 状态：fixed
- 来源：E2E 回归测试
- 描述：Step 15 "Blue Top 附近的 Add to cart" 用 text_parent_chain 成功，但 Step 21 "Fancy Green Top 附近的" 回退到 ai_coordinate_click。`_find_in_ancestor` 始终用 `.first` 获取第一个匹配。
- 根因：`_find_in_ancestor` 和 `_resolve_text_parent_chain` 始终用 `.first`，不尝试其他 nth 候选。
- 处理：改为迭代 `.nth(0..4)` 多个候选，找到第一个成功匹配的返回。
- 验证：33/33 语义单元测试通过

### BUG-082 | capture_page_session 使用简化版 resolver 导致登录态不稳定

- 日期：2026-05-07
- 状态：fixed
- 来源：E2E 回归测试
- 描述：`capture_browser_session` 使用简化版 `_resolve_step_locator`（只尝试少量候选 + count()>0 即返回），Email placeholder 匹配到 3 个元素 → strict mode 失败。
- 根因：没有使用完整的定位器链路（semantic → a11y → VLM fallback）。
- 处理：改为直接调用 `resolve_with_fallback`；添加页载等待 + 元素 tag 验证 + 2 次重试。
- 验证：S161 capture_page_session 成功，page_elements 从 73K → 1.28MB

### BUG-080 | assert_text 的 target 字段中的运行时变量未被替换

- 日期：2026-05-07
- 状态：fixed
- 来源：E2E 回归测试
- 描述：`assert_text target="${cart_a_total}"` 中的 `${cart_a_total}` 未被 `_substitute_variables` 替换 → 定位器按字面量查找 → 永远找不到 → 走 VLM 兜底。
- 根因：`_substitute_variables` 只对 `step.value` 调用，未对 `step.target` 调用。
- 处理：所有 runner 和 helper 中，`step.target` 使用前统一替换。
- 验证：543/544 单元测试通过

### BUG-076 | DSL target 文本中的中文字符被 PostgreSQL JSON 序列化损坏

- 日期：2026-05-07
- 状态：fixed (`aaa3f18`)
- 来源：E2E 回归测试
- 描述：target 字段中"附近的"被序列化为 `\udc84`（lone low surrogate），`text_parent_chain` 的 regex 无法匹配。所有含中文的 target 均受影响（6 个步骤）。
- 根因：PostgreSQL JSONB 序列化过程中 Unicode BMP 字符被错误编码为 surrogate pair。
- 处理：DSL 归一化入口处检测并修复 surrogate 字符。
- 验证：Draft 81 证实 surrogate_targets=0

### BUG-075 | 元素视觉分组的 group label 太粗糙

- 日期：2026-05-07
- 状态：fixed
- 来源：E2E 回归测试
- 描述：`_group_label` 的 if-elif 链太刚性，价格检测遗漏纯数字价格，1-2 个元素的分组 label 无意义。
- 处理：价格检测增强 + 新增 8 种块类型 + 渐进阈值回退 + aria_label 命名。
- 验证：542/544 单元测试通过

### BUG-074 | text_parent_chain 定位器未在 runner 候选列表中被优先尝试

- 日期：2026-05-07
- 状态：fixed (`6922bc8`)
- 来源：E2E 回归测试
- 描述：`_resolve_with_confidence_gate` 在 `locator_confidence="low"` 时先调 VLM preverify，跳过了 text_parent_chain。
- 根因：VLM preverify 不应跳过语义定位链。
- 处理：重构为统一流程——语义优先、VLM 仅作最后兜底。添加 2.5 分钟步骤超时。
- 验证：Exec 102+ 证实 text_parent_chain 在候选列表中排在第一位

### BUG-073 | text_parent_chain 的正则表达式无法匹配含空格的父文本

- 日期：2026-05-07
- 状态：fixed (`aabea6a`)
- 来源：E2E 回归测试
- 描述：`_PARENT_TEXT_RE` 使用 `[^>\\s>{2,60}?`（惰性匹配 + 排除空格），"Blue Top 附近的 Add to cart" 无法匹配。
- 根因：惰性量词使匹配过短 + `[^>\\s]` 错误排除了空格。
- 处理：改用 split 方式——`_PARENT_SPLIT_RE` 直接在 `>>`/`的`/`附近的` 处分隔。
- 验证：33 个语义单元测试通过，Exec 102 Step 11 证实成功匹配

### BUG-072 | text_parent_chain 使用硬编码 XPath ancestor 无法适配不同页面结构

- 日期：2026-05-07
- 状态：fixed (`892889e`)
- 来源：E2E 回归测试
- 描述：`_find_in_ancestor` 使用 `xpath=ancestor::*[contains(@class,'product')]` 硬编码 class 名。购物车页 `<tr>` 不含此 class → 返回 0 元素。
- 处理：改为自适应深度遍历——从 parent_text 元素出发，逐层 `..` 向上（depth 2-8），每层尝试 `get_by_text(child_text)`。
- 验证：33 个语义单元测试通过，手动验证购物车页 depth=3 可找到 "Rs. 500"

### BUG-071 | text_parent_chain 的 child_text 使用 exact=True 导致 substring match 失败

- 日期：2026-05-07
- 状态：fixed (`dba307c`)
- 来源：E2E 回归测试
- 描述：`_find_in_ancestor` 使用 `get_by_text(child_text, exact=True)`，DOM 中价格文本有前后空格/格式差异导致不匹配。
- 处理：改回 `exact=False`（子串匹配），添加 try/catch 防止异常吞没。
- 验证：33 个语义单元测试通过

### BUG-064 | preflight 将所有 target 标记为 low confidence 导致 VLM 抢占

- 日期：2026-05-06
- 状态：fixed（通过 BUG-074 的流程重构规避）
- 来源：E2E 回归测试
- 描述：preflight 在已探索元素中找不到精确匹配 → 几乎所有步骤标记为 `locator_confidence="low"` → VLM 抢占语义定位链。
- 处理：通过 BUG-074 的执行流程重构绕过（语义链优先、VLM 兜底）。
- 验证：Exec 102+ 证实语义定位链在 VLM 之前执行

### BUG-057 | click_with_precheck 对 hidden 元素超时不触发恢复链

- 日期：2026-05-03
- 状态：fixed
- 来源：Session 15 E2E 测试
- 描述：点击 modal 中 "Continue Shopping" 时，Playwright 报 `resolved to hidden`，但 `_is_interception_error` 只匹配 `"intercepts pointer events"`，5 策略恢复链完全被跳过。
- 处理：新增 `_HIDDEN_ELEMENT_PATTERN` 匹配 `"resolved to hidden"`，直接走 `_try_force`。
- 验证：471 单元测试通过

### BUG-053 | VLM bbox 坐标在 DOM 选择器提取失败时被丢弃

- 日期：2026-04-25
- 状态：fixed
- 来源：BUG-054 根因分析
- 描述：VLM 返回准确 bbox 坐标，但 `_build_locator_from_ai_point()` DOM 选择器提取失败时，整个 `AILocateResult` 被丢弃。Playwright 原生支持 `page.mouse.click(x,y)` 但从未使用。
- 处理：`ResolvedLocator` 新增 `click_coordinates` 字段；新增 `_try_coordinate_click_fallback()` Tier 2.5 回退。
- 验证：Exec 69/70 全部通过

### BUG-050 | AI DSL 生成定位策略不匹配 DOM 结构

- 日期：2026-04-23
- 状态：fixed
- 来源：白盒测试（Automation Exercise）
- 描述：AI 生成 `.productinfo text='View Product'` 链式选择器，但 `.productinfo` 和 "View Product" 是兄弟关系非父子，匹配 0 元素。
- 处理：五重修复——链式选择器解析 + prompt 禁止无效复合格式 + target_strategy 字段 + error_message 改 Text + DOM 证据注入。
- 验证：7/7 链式选择器测试通过

### BUG-049 | 语义定位器不支持标签名开头的复合 CSS 选择器

- 日期：2026-04-21
- 状态：fixed
- 来源：白盒测试（The Internet Login Page）
- 描述：`_resolve_explicit_locator` 只识别以 `css=`、`#`、`.` 等开头的目标，`button[type='submit']` 以字母开头落入文本匹配。
- 处理：新增 `_COMPOUND_CSS_RE` 启发式正则识别 `tag[attr]`、`tag.class`、`tag > child` 等复合模式。
- 验证：6 passed

### BUG-046 | 语义定位器缺少 element_id 和 case-insensitive 匹配策略

- 日期：2026-04-17
- 状态：fixed
- 来源：集成测试自测
- 描述：无法定位以 HTML id 属性命名的目标（如 "flash"），`get_by_label("username", exact=True)` 无法匹配 "Username"。
- 处理：新增 `element_id` 策略（优先级 100）+ `label_fuzzy`/`placeholder_fuzzy`/`text_fuzzy`/`button_role_fuzzy` 四个非精确匹配策略。
- 验证：6 passed

### Bug #1 | AIPlanningSession UnboundLocalError in explore_flow

- 日期：2026-05-16
- 状态：fixed
- 来源：E2E 测试
- 描述：`explore_flow` 工具调用时报 `cannot access local variable 'AIPlanningSession'`。import 被放在条件块内，当 `base_url` 已通过 params 提供时被跳过。
- 处理：将 import 移到条件块之前。
- 关联记录：execution-log.md 2026-05-16

---

## C. 页面探索与数据采集

explore_page / explore_flow、DOM/A11y 元素采集、缓存、数据压缩等页面数据获取链路问题。

### BUG-060 | AI planning 中间层三大架构断层

- 日期：2026-05-03
- 状态：fixed
- 来源：架构排查 / BUG-059 延伸
- 描述：三个架构断层：
  1. `explore_flow` 仍是 URL 级探索——不会在页面间执行点击/输入/等待动作
  2. 页面知识是扁平 `page_elements` 文本——无页面状态标记
  3. DSL 生成后无 locator preflight——定位器验证全部推迟到执行期
- 处理（Phase 1-3 全套升级）：
  - Phase 1：`collect_flow_elements(steps)` 支持动作式探索
  - Phase 2：`page_state_id` 页面状态标记 + DSL step `page_state` 字段
  - Phase 3：`locator_preflight.py` 静态校验 DSL targets 与已采集元素的匹配度
- 验证：485 单元测试全部通过

### BUG-059 | AI planning 中间层仍是 URL 级探索而非 flow 驱动探索

- 日期：2026-05-03
- 状态：fixed
- 来源：架构排查
- 描述：`_auto_explore_entry_url()` 只按入口页链接顺序抓取前 4 个链接，逻辑与 `core_user_flow` 无绑定。
- 处理：`_extract_internal_links()` 升级为 flow 驱动——按 URL 路径与关键词匹配度评分排序，优先探索流程相关页面。
- 验证：471 全部通过；含 login 流程时 /login 从位置 3 提升至位置 1

### BUG-067 | explore_flow 相对 URL 未解析导致页面探索失败

- 日期：2026-05-07
- 状态：fixed (`f53807d`)
- 来源：E2E 回归测试
- 描述：AI 传入相对 URL（`/products`, `/brand_products/Polo`），`collect_multi_page_elements` 未解析，Playwright `page.goto("/products")` 失败 → 返回空元素。
- 处理：用 `urljoin` 将相对 URL 解析为绝对 URL。
- 验证：S152 page_elements 从 81 字符增长到 157KB

### BUG-068 | 页面探索压缩子代理丢弃登录表单元素

- 日期：2026-05-06
- 状态：fixed (`081c49e`)
- 来源：E2E 回归测试
- 描述：`_filter_elements_for_compression` 硬编码取前 100 个元素，登录表单字段可能在第 100+ 位置被截断。子代理 prompt 对表单强调不足，压缩结果 `forms: []` 为空。
- 处理：改为优先保留交互元素（input/button/select/textarea/a），非交互元素限制 80 个；重写 prompt 强制 JSON 结构。
- 验证：65 个单元测试通过

### Bug #2 | explore_page networkidle 超时导致异常

- 日期：2026-05-16
- 状态：fixed
- 来源：E2E 测试
- 描述：`explore_page` 在 automationexercise.com 上反复超时 `Timeout 30000ms exceeded`。部分网站持续发送跟踪请求，networkidle 永远达不到。
- 处理：用 try-except 包装 `wait_for_load_state("networkidle")`。
- 关联记录：execution-log.md 2026-05-16

### Bug #A（2026-05-25）| single-segment 路径下 a11y_nodes 数据丢失

> 详见 [A. DSL 生成与归一化](#bug-a--single-segment-路径下-a11y_nodes-数据丢失)，该 bug 同时影响页面探索数据传递链路。

---

## D. AI 决策与提示词

ReAct 循环决策、提示词遵循、工具调用去重、AI 输出质量问题。

### Bug #C | agent 重复调用工具浪费安全帽轮次

- 日期：2026-05-25
- 状态：fixed
- 来源：E2E 回归测试
- 描述：agent 在 5 轮内调用 `create_project` ×2（轮 2&3）、`explore_flow` ×2（轮 4&5），耗尽 5 轮安全帽 → fallback plan → flow_steps 为空。
- 根因：ReAct loop 对工具调用无去重判断。
- 处理：
  - 新增 `_tool_call_signature(tool_name, params)` 生成调用签名
  - 工具执行前比对已有签名，命中重复时：yield 重复事件 + 注入警告系统消息 + `round_index -= 1` 不扣 round
- 关联记录：execution-log.md 2026-05-25

### BUG-085（2026-05-10）| DeepSeek thinking 模式 + 高温导致 AI 不遵循提示词

> 详见 [A. DSL 生成与归一化](#bug-085--deepseek-thinking-模式--高温导致-ai-不遵循提示词指令)，该 bug 同时影响 AI 决策层。

### BUG-081 | DSL 草案间质量剧烈波动 — 相同 prompt 产出 42 步和缺登录的 30 步

- 日期：2026-05-07
- 状态：fixed
- 来源：E2E 回归测试
- 描述：相同 draft_prompt（2069 chars），S155 产出 42 步完整草案，S162 产出缺少登录导航的 30 步草案。根因是 DSL 生成模型的随机性。
- 处理：(1) 系统提示词加入【流程-页面导航映射】规则；(2) 草案生成后一致性检查。
- 验证：Draft 89 包含完整 click "Signup / Login" 导航

### BUG-069 | 系统提示词引导 AI 在信息充足时仍使用 ask_user 询问确认

- 日期：2026-05-06
- 状态：fixed (`13016a6`)
- 来源：E2E 回归测试
- 描述：系统提示词第 91 行：当收集到 4+ 项信息时，通过 ask_user 询问"信息是否足够"。AI 第一轮动作为 ask_user 而非 explore_page。
- 处理：修改规则为"信息充足时直接 generate_plan，不用 ask_user"。
- 验证：Session 139 AI 第一轮动作为 call_tool（get_project_info），不再问废话

### BUG-066 | AI 不遵循 explore_flow 提示词，跳过页面探索直接生成方案

- 日期：2026-05-12
- 状态：fixed
- 来源：E2E 测试
- 描述：DSL 草案基于不完整的页面数据生成，缺少步骤、target 泛化。
- 根因：`_build_link_selection_message` 中有"如果信息足够，也可以直接 generate_plan"逃逸口；安全网消息误导 LLM。
- 处理：删除逃逸口；安全网消息改为"静态页面已采集，交互页面仍需 explore_flow"。
- 验证：AI 不再跳过探索

### BUG-066（2026-05-07）| AI 的 core_user_flow 被序列化为 Python list repr

- 日期：2026-05-07
- 状态：fixed (`23e3bc9`)
- 来源：E2E 回归测试
- 描述：AI 以 list 形式返回 `core_user_flow`，`_merge_requirements` 调用 `str(incoming)` 转成 Python repr 字符串 `"['打开首页...', '点击 Products...']"`。DSL prompt 收到畸形流程描述，质量极差。
- 处理：对 core_user_flow 和所有 list 字段统一 join 为编号列表。
- 验证：S146 draft 72 43 步（修复后） vs S145 draft 70 17 步（修复前）

### BUG-065（2026-05-12）| explore_flow 相对 URL 被解析为 example.com

- 日期：2026-05-12
- 状态：fixed
- 来源：E2E 测试
- 描述：`page_explorer.py:1594` 硬编码 `base_url or "https://example.com/"` 兜底；`planning_tools.py` explore_flow 工具定义缺少 `base_url` 参数。
- 处理：移除硬编码默认值；explore_flow 工具定义添加 `base_url` 参数；base_url 提取逻辑重构到函数开头共享。
- 验证：542/544 单元测试通过

### BUG-054 | AI 忽略用户描述的弹层交互步骤，用导航栏元素替代弹层元素

- 日期：2026-04-25
- 状态：fixed
- 来源：Session 52 E2E 测试
- 描述：用户明确写了"在弹层中点击 View Cart"，但 AI 使用导航栏 "Cart"。点击 "Add to cart" 后弹层遮挡了导航栏 "Cart"，导致 click 超时。
- 根因：(1) 静态 explore_flow 无法采集动态弹层元素；(2) AI 未严格遵循用户描述。
- 处理：三重修复——`_discover_interactive_elements()` 捕获弹层元素 + Prompt 追加动态交互规则 + `[dynamic]` 标记。
- 验证：Session 53 Draft 26/27 正确使用 "View Cart"；Exec 69/70 各 13/13 全部通过

---

## E. SSE 流式与前端

SSE 流式输出、会话管理、前端渲染、WebSocket 通信等前后端交互问题。

### BUG-063 | DeepSeek thinking 模式下 SSE 流式输出空白 + 会话消失

- 日期：2026-05-04（含 2026-05-05 追加修复）
- 状态：fixed
- 来源：线上反馈
- 描述：使用 `deepseek-v4-flash` 模型时，SSE 流式输出直接空白——思考阶段前端完全看不到任何文本内容。刷新后会话消失。
- 根因（多层）：
  1. `reasoning_content` 只在内存累积、仅发节流 status 消息，不产出 `text_chunk` 事件
  2. `reasoning_text` 未归入 `raw_response`，content 为空时触发 `empty_response` 错误
  3. `_call_planning_llm()` 非流式路径只提取 `message.content`，忽略 `reasoning_content`
  4. `turn_complete` 后 `loadSessionDetail()` 用服务端数据替换 transcript，`_thinkingContent` 丢失
  5. 历史消息加载后未清除 `_streaming: true` 标志
- 处理：
  - backend：每个 `reasoning` chunk 同步产出 `text_chunk` 事件（带 `thinking: true`）；content 为空时用 `reasoning_text` 兜底；非流式路径 fallback 到 `reasoning_content`
  - frontend：`_thinkingContent` 存入独立字段 + 渲染可折叠 `<details>` "思考过程"区块；加载历史消息时清除 `_streaming` 标志；`turn_complete` 后保留 `_thinkingContent`
- 验证：29 planning agent 单测 + 11 API 测试通过；TypeScript 编译无错误
- 关联记录：execution-log.md 2026-05-04、2026-05-05

### BUG-058 | AI Test Planning 面板切换会话后仍把项目操作发送到初始 session

- 日期：2026-05-03
- 状态：fixed
- 来源：架构排查
- 描述：`AITestPlanningPanel` 内部根据选择切换 `sessionId` 状态，但渲染 `SessionProjectPanel` 时仍传入初始 `sessionIdProp`。切换 session 后项目操作仍落到旧 session。
- 处理：`AITestPlanningPanel.tsx:621` 将 `sessionId={sessionIdProp}` 改为 `sessionId={sessionId ?? 0}`。
- 验证：TypeScript 类型检查通过；切换 session 后请求使用当前 session id

### BUG-044 | AI Planning 面板缓存失效会话时不会回退创建新会话

- 日期：2026-04-12
- 状态：fixed
- 来源：需求实现 / 静态检查
- 描述：`localStorage.ai_planning_last_session` 指向已删除 session 时，恢复失败后不会自动创建新会话，页面卡在无活跃 session 状态。
- 处理：引入 `loadSessionDetail()` / `createAndSelectSession()` helper，恢复失败时清理缓存并自动创建。
- 验证：前后端测试通过

### BUG-042 | AI 测试规划面板初始化首条消息可能丢失

- 日期：2026-03-30
- 状态：fixed
- 来源：自测
- 描述：session 尚未创建完成前允许点击"发送消息"，首条输入被忽略；`.gitignore` 中 `tests/` 规则导致新测试文件默认未跟踪。
- 处理：发送按钮增加 `isBootstrapping`/`sessionId`/空输入约束；`.gitignore` 新增白名单。
- 验证：前后端测试通过

---

## F. 执行引擎

Playwright runner、变量替换、步骤证据采集、点击预处理等执行层问题。

### Bug #3 | capture_text 步骤的 value 在报告中始终为 null

- 日期：2026-05-16
- 状态：fixed
- 来源：E2E 测试
- 描述：`capture_text` 成功执行但报告中 `value` 字段始终为 `null`。捕获的文本存到了 `runtime_context` 但 `StepExecutionEvidence.value` 读取的是 `getattr(step, "value", None)`。
- 处理：引入 `step_value` 局部变量，capture_text 分支更新为实际捕获值。修复覆盖 4 个代码路径。
- 关联记录：execution-log.md 2026-05-16

### BUG-079 | 购物车测试数据污染 — 前序测试遗留商品导致数量断言失败

- 日期：2026-05-07
- 状态：verified (Exec 107 42/42=100%)
- 来源：E2E 回归测试
- 描述：Exec 106 Step 27 `assert_text '1' value='${cart_a_quantity}'` 失败。capture 抓到数量 31（前序测试累积），断言期望 1。定位器本身工作正常。
- 根因：测试间缺少购物车清理步骤。
- 处理：(1) 测试开始前清空购物车；(2) AI 不应硬编码数量值，应 capture 后做一致性比较。
- 验证：用户手动清空购物车后 Exec 107 42/42=100%

### BUG-057（2026-05-03）| click_with_precheck 对 hidden 元素超时不触发恢复链

> 详见 [B. 定位器系统](#bug-057--click_with_precheck-对-hidden-元素超时不触发恢复链)，该 bug 同时影响执行引擎的点击预处理。

### BUG-053（2026-04-25）| VLM bbox 坐标在 DOM 选择器提取失败时被丢弃

> 详见 [B. 定位器系统](#bug-053--vlm-bbox-坐标在-dom-选择器提取失败时被丢弃)，该 bug 同时影响执行引擎的坐标点击回退。

### BUG-051 | input_contract 变量占位符在执行时未被替换

- 日期：2026-04-24
- 状态：fixed
- 来源：BUG-050 修复验证
- 描述：AI 生成的 DSL 包含 `${login_email}`、`${search_keyword}` 等变量占位符，save-and-execute 时未替换为实际值。`${search_keyword}` 被直接作为字符串输入到搜索框。
- 根因：变量替换功能完全未实现——runner 直接使用 `step.value` 原始字符串。
- 处理：`playwright_runner.py` 新增 `_substitute_variables` 函数；`CaseExecutionRequest` 增加 `input_values` 字段；4 处 step.value 使用处全部替换。
- 验证：303 单元测试全部通过

### BUG-047 | playwright_runner _capture_request_failed 对 request.failure 返回格式处理错误

- 日期：2026-04-17
- 状态：fixed (d73558e)
- 来源：集成测试执行日志
- 描述：新版 Playwright 的 `request.failure` 返回类型为 `str` 而非 `dict`，`failure.get("errorText")` 抛出 `AttributeError`。
- 处理：改为 `isinstance(failure, str)` 兼容两种类型。
- 验证：集成测试不再报 AttributeError

---

## G. 配置与基础设施

API 合同同步、权限校验、网络重试、数据库配置等基础设施层面的问题。

### Bug #B（2026-05-25）| LLM 调用无重试 + 网络错误消息误导

> 详见 [A. DSL 生成与归一化](#bug-b--llm-调用无重试--网络错误消息误导)，该 bug 同时影响基础设施层面的网络健壮性。

### BUG-045（2026-04-13）| AI planning "保存并执行草案"链路被 DSL 生成配置阻断

> 详见 [A. DSL 生成与归一化](#bug-045--ai-planning-保存并执行草案链路被-dsl-生成配置阻断)，该 bug 同时涉及配置问题（`AI_DSL_BASE_URL` 指向错误端点）。

### BUG-043 | 新增 AI planning 配置字段后，settings API 更新合同未同步

- 日期：2026-04-03
- 状态：fixed
- 来源：任务实现 / 回归测试
- 描述：新增 `enable_ai_planning` 等 planning 字段后，`AISettingsUpdateRequest` 已要求必填，但旧测试和前端仍用旧 payload，触发 422。
- 处理：补齐后端测试中的 planning 字段；前端 `AISettings`/`AISettingsPage` 一并纳入。
- 验证：前后端测试通过

### BUG-041 | 最新 CRUD 提交存在权限绕过、统计接口运行时失败与删除路径不闭合

- 日期：2026-03-30
- 状态：fixed (082ae22)
- 来源：代码评审 / commit 7eb71ae
- 描述：4 类问题——(1) 任意已登录用户能读取/更新/删除其他项目用例（权限绕过）；(2) `GET /stats/{project_id}` 缺少必填字段导致 500；(3) 有 test_cases 的项目删除时触发 RESTRICT 约束；(4) 历史接口响应合同变化但测试未更新。
- 处理：补齐项目成员权限校验、修正 stats 返回结构、处理外键约束下的项目删除语义、更新测试断言。
- 验证：全量测试通过


