# Execution Log（执行日志）

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
| AutomationExercise BFC | ✅ | 探索 6 步/5 llm/done=True；case 9 步（两个商品 id=1/8）；执行 9/9（identity_exact ×2） |
| SauceDemo 登录+加购 | ✅ | 探索 4 步/done=True；case 6 步（fill ×2 由 pre_actions 恢复）；执行 6/6 |
| xywhaigc 登录 | ⚠️ BLOCKED（环境） | 站点手工登录失败（URL 不跳转）→ 明确返回 `探索未验证目标动作: login`（失败位置正确，非代码回归） |

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

### 已知限制（技术债，只记录不修）

- **verified outcome ≈ from_obs != to_obs** 是阶段性代理。若遇"业务成功但 canonical
  observation 未变化"的站点会产生 false negative。届时扩展为
  `state transition OR explicit postcondition evidence`。当前无真实失败案例，不加。
- **显式 wait_for 未注入**：DSL 无 wait_for 步骤（依赖 Playwright 自动等待 + resolve 5s
  轮询）。慢页面（SPA >5s 渲染）时 click resolve 可能失败。挂起（用户决策后实施）。
- **xywhaigc**：站点登录当前失败（外部环境）。账号恢复后需补跑正常链路。

### 清理项（S1）

- 删除 Preflight v2 全部（~30KB 死代码：RepairItem/PreflightIssue/_preflight_and_repair
  等，GENERATE_PREFLIGHT=False 恒不执行）+ 未用 recovery prompt + 临时诊断 print。
- 测试：173 passed。
