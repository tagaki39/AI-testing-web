# Execution Log（执行日志）

## 2026-08-27 — 参考项目探索形态复核

- `explore_page` 只返回指定 URL 当前状态的 A11y 快照，不代表整站未来状态。
- 交互流程由 `explore_flow` 执行预先给定的多步 actions，并在每个动作后重新
  采集 A11y；它减少的是逐步 LLM 决策，不是浏览器交互或失败可能性。
- 参考实现对定位失败采取 retry/fallback/skip，并由 ReAct safety cap 控制工具
  轮次；当前项目则把失败前移为 verified transition。两者成本位置不同。
- 后续 S2 采用混合路线：一次生成 Goal Contract，按 milestone 批量决定动作，
  但继续逐动作验证真实迁移；不把完整当前页 AX Tree 误当作整站全局视野。

---

## 2026-08-27 — S2-P0：目标隔离、结构化终止与 singleton 安全

### 实现

- 新增 `TerminationReason` 与 `ExploreState.terminate(reason)`；探索结果明确区分
  `goal_complete`、`model_finish`、认证拒绝、错误页、观察上限和预算耗尽。
- 探索轨迹缓存 key 改为
  `origin + auth_profile + redacted goal fingerprint + schema version`；仅
  `goal_complete` 轨迹允许落盘，未完成或语义性 finish 不再跨目标复用。
- singleton 快捷路径只对 capability 恰好为 `{click}` 的完整动作生效；唯一
  textbox、button、link 等继续进入决策层，避免把“唯一元素”误当成“唯一动作”。
- metrics 增加 termination reason 分布；README 与 ROADMAP 同步移除旧的
  `done=False / steps>=2` 缓存口径。

### 验证与规模

| 项目 | 结果 |
|---|---|
| 目标测试 | 98/98 passed |
| 全量测试 | 187/187 passed |
| 后端生产代码 | 6,423 → 6,503 物理行（P0 净增 80） |
| 前端代码 | 362 物理行（不变） |
| 测试代码 | 3,745 → 3,881 物理行 |

本阶段允许为契约与失败可观测性小幅增量；S2-P1～P6 按
`docs/S2-plan.md` 的 replace-then-delete 原则接管旧路径，最终再做集中瘦身。

### 规划材料口径

`AI_Web_Testing_架构收敛与未来规划.docx` 可作为架构决策、回归矩阵和质量门禁
参考，但其中 10,312 行是 `docs/source-code.txt` 的静态快照，不是本次 P0 后的
工作树计数；执行阶段与当前状态以 `docs/S2-plan.md` 和本日志为准。

---

## 2026-08-27 — SSE 实时执行进度（功能文档 ⑥ + P5）

### 实现（功能文档定稿方案）

- **POST /api/execute → 异步化**：validate_case（第二道安全门）→ 创建 run
  （内存状态 + 事件队列）→ 后台线程执行 → 立即返回 `{"run_id": "..."}`。
- **GET /api/runs/{id}/events → SSE 事件流**（EventSource 只支持 GET）：
  - 事件：`step_started` / `step_completed` / `run_finished`（带完整 report）/
    `run_error`（带错误信息）
  - 重连幂等：执行已终结（done/error）且队列耗尽 → 补发最终事件再关闭
    （EventSource 自动重连不丢结果）；队列空闲发心跳注释帧保活
- **runner.execute_case 加 `on_event` 回调**（可选，默认 None——同步调用
  路径完全兼容）：核心循环逐步推送，回调异常吞掉（进度不影响执行主链路）
- **前端 execute()**：POST → run_id → EventSource 订阅 → 步骤骨架实时更新
  （运行中高亮/通过/失败/跳过徽章 + 边界色）→ run_finished 渲染完整报告
- timings 记录移入 worker 完成分支（执行耗时数据不丢）

### 回归（SSE 新链路全站验证）

| 场景 | 结果 | 事件序列 |
|---|---|---|
| SauceDemo 登录+加购 | ✅ 6/6 | 13 事件（6 步 × start/complete + run_finished） |
| AutomationExercise BFC | ✅ 8/8 | 17 事件（探索 5/3/done） |
| xywhaigc 登录 | ✅ 6/6 | 13 事件（探索 3/3/done） |
| 单元测试 | ✅ 173/173 | on_event=None 默认兼容 |

---

## 2026-08-26 — P3 + 清理 + 最终回归（`6d51837` → 最终确认）

### P3：低价值动作拦截（`6d51837`）

- **textbox/searchbox 不支持 click**：fill() 本身完成 focus，先 click 文本框是
  低价值动作（浪费预算，LLM 常误用）。能力矩阵 `ACTION_CAPABILITIES` 收紧。
- **重复同值 fill 拦截**：同一 ref 已成功 fill 相同值且期间无状态迁移 →
  NO_PROGRESS 拒绝（确定性判断，不进 LLM 预算）。
- 效果：SauceDemo 6/6、BFC 10/10、xywhaigc 6/6 回归通过。

### 收尾清理（dead-code audit，只删除不重构）

- 删除 BFC 300s 超时定位埋点：`[GEN]` stage marks、`[LLM]` HTTP 计时、
  `[EXPLORE]` DECIDE 打点、`[OBS]` 全量性能埋点（A4.1 性能根因已修复，
  计时器变量仅服务于 print，一并删除；`identity_enrich_ms` 等 metrics 保留）。
- 删除临时诊断文件：`_diag_stability.py`、`_*_prompt.json`、`_*_result.json`、
  `bfc_result.json`、`server.log`。
- 修正 `target_name` 过时注释（原标注"临时 diagnostic"——实际已被 R7.2
  购物车入口校验使用，防止后续审计误删）。

### 最终回归（冻结基线后唯一一次完整验证）

| 场景 | 结果 | 关键指标 |
|---|---|---|
| AutomationExercise BFC | ✅ | 探索 7 步/5 llm/done=True；case 10 步；执行 10/10 |
| SauceDemo 登录+加购 | ✅ | 探索 4 步/done=True；case 6 步；执行 6/6 |
| xywhaigc 登录 | ✅ | 探索 3 步/3 llm/done=True；case 6 步；执行 6/6（真实登录） |
| 单元测试 | ✅ | 173/173 passed |

**冻结基线确认：** 主链路（Observation → ActionSpace → Goal Policy → 0/1/N 决策
→ StateGraph → Planner → Compiler → Resolver → Runner）自 `7be9638` 起未再
改动，`6d51837`（P3）+ 本清理均为收尾，无功能变更。

---

## 2026-08-26 — A4.x → S1 收尾：主链路冻结基线（`7be9638`）

### 本轮功能基线

**冻结 commit：`7be9638`**（learn-reference 分支）。此后不再修改 Explorer/Planner/Resolver 主逻辑。

主链路：Observation（Structured AX + identity + canonicalization + interaction root）
→ ActionSpace（结构过滤）→ Goal Policy（从 StateGraph 派生）→ 0/1/N 决策（单候选跳过 LLM）
→ Execute → StateGraph（verified transitions）→ S3 Planner（transition_refs + assertions）
→ State Cursor 展开（pre_actions 确定性恢复）→ G3（safety assert）→ Compiler → Resolver → Runner。

### 三站回归结果

| 场景 | 结果 | 关键指标 |
|---|---|---|
| AutomationExercise BFC | ✅ | 探索 7 步/5 llm/done=True；case 10 步（两个商品 id=1/8）；执行 10/10（identity_exact） |
| SauceDemo 登录+加购 | ✅ | 探索 4 步/done=True；case 6 步（fill ×2 由 pre_actions 恢复）；执行 6/6 |
| xywhaigc 登录 | ✅（P1 修复后） | 探索 3 步/3 llm/done=True；case 6 步（fill×2 + 登录 + assert_url /index）；执行 6/6（真实登录成功） |

### 核心 bug 链（本轮修复）

1. **S3 回归（fill 丢失）**：探索的 fill 不产生转移边 → S3 的 transition_refs 无表达位置
   → case 缺 fill。修复：pre_actions 从探索 history 确定性恢复（状态内成功动作绑定到
   真实转移边，Planner 不生成输入步骤）。
2. **StateGraph 统一标准**：只记录 `from_obs != to_obs` 的真实迁移（不按动作类型一刀切，
   select 真迁移仍落图）；状态内动作进 pending，真实迁移落图才消费。
3. **verified outcome 门（两层防线）**：目标性动作（login/add_to_cart/checkout，
   VERIFIED_OUTCOME_REQUIRED_ACTIONS 显式维护）必须形成 verified transition 才算完成；
   Planner 前 fail-closed（missing_verified_goal_actions 共享判断）→ ExplorationIncompleteError。
4. **identity 实体约束**：identity 全局候选集（scope 不硬过滤——scope_has_text 可能采集
   到相邻卡片）；禁止 role/text fallback（目标指定商品时降级会点错商品）；多 visible
   representation 须动作语义等价（同 tag+文本）才选一。
5. **PUA 语义归一化**：购物车入口判定剥 PUA 图标（" Cart" → Cart），不改原始名称。

### P1 修复（`5f8b55e`）——两阶段观察

- **根因**：点击 Login 后 RuoYi 登录 POST 后台跑几秒才 router push /index——
  旧 `_observe_until_stable` 在登录页"连续两次稳定"（几百 ms）就提前返回
  → 伪 self-loop → 无转移 → verified 门误判登录失败（错误归因为"环境"）。
- **修复**：`_observe_after_action` 两阶段——Phase 1 等 URL/hash 相对 before
  分叉（≤5s），Phase 2 新状态 settle（≤2s）。`_observe_until_stable` 收敛为
  纯 settle。
- **脱敏**：新增"账号 X 密码 Y"格式提取（username 曾残留进 LLM 上下文 →
  输出真实值被 Data Grounding 拒 → 预算浪费）；斜杠格式索引修正。
- **效果**：xywhaigc 从"10 llm 预算耗尽失败"到"3 步/3 llm 完成 + 6/6
  真实登录"（assert_url /index 真实验证）；BFC/SauceDemo 回归通过。

### 已知限制（技术债，只记录不修）

- **verified outcome ≈ from_obs != to_obs** 是阶段性代理。若遇"业务成功但 canonical
  observation 未变化"的站点会产生 false negative。届时扩展为
  `state transition OR explicit postcondition evidence`。当前无真实失败案例，不加。
- **显式 wait_for 未注入**：DSL 无 wait_for 步骤（依赖 Playwright 自动等待 + resolve 5s
  轮询）。慢页面（SPA >5s 渲染）时 click resolve 可能失败。挂起（用户决策后实施）。
- **P2/P3（观察）**：canonical observation 对 focus 等瞬时 UI 状态可能过敏（伪
  transition）；ActionSpace 允许低价值动作（click textbox / 重复同值 fill）浪费预算。
  当前无真实阻塞案例，观察后再修。

### 清理项（S1）

- 删除 Preflight v2 全部（~30KB 死代码：RepairItem/PreflightIssue/_preflight_and_repair
  等，GENERATE_PREFLIGHT=False 恒不执行）+ 未用 recovery prompt + 临时诊断 print。
- 测试：173 passed。
