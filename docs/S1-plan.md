# S1 — Simplification & Cleanup（架构冻结后的减法阶段）

> 冻结点：`f8b265f`（R7.x grounded flow complete，BFC 7/7，161 tests）
> 评审结论：核心闭环已跑通，不再加 R8 大功能——**停止加层，清理旧层，稳定当前版本**。

## 一、主链定稿（不再改动）

```
Observation
  Structured AX
  + Stable Identity（lazy）
  + Duplicate Canonicalization
  + Interaction Root metadata
        ↓
ActionSpace（只做结构合法性：kind/disabled/interaction-root 内）
        ↓
Goal Policy（从 goal + verified transitions 派生，只缩小候选）
        ↓
0 / 1 / N 决策
  0 → fail / complete judgment
  1 → deterministic execute（S1-P0）
  N → LLM semantic choice
        ↓
Execute
        ↓
State Graph（verified transitions only）
        ↓
R7 Minimal Planner（transition_ref 选择 + 断言 + input_contract）
        ↓
State Cursor Compiler（transition_ref → action/ref/state，deterministic）
        ↓
G3（纯 invariant assert，理论不触发）
        ↓
Locator Compiler（ref → LocatorSpec）
        ↓
Resolver
        ↓
Runner
```

**真正负责决策的地方只剩三个**：Policy、LLM semantic choice、Minimal Planner。
其余全部是确定性 transformation / invariant。

## 二、优先级清单

### P0 — 单候选 ActionSpace 跳过 LLM

现状：modal 被 Policy 限制后 `selectable_actions=1`（如只有 Continue Shopping / 只有 View Cart），
仍调用一次 LLM 让它"选择"唯一选项——无语义选择，纯浪费。

```python
if len(selectable_actions) == 1:
    decision = deterministic_action      # 不调用 LLM
elif len(selectable_actions) > 1:
    decision = llm_choose(...)           # 只有真正存在语义选择才调用
```

预期：BFC 探索 6 calls → 4 calls（两次 modal 单候选省 2 次）。不是 heuristic，是 0/1/N 决策原则。

### P1 — dead-code audit（grounded 主链是否还调用旧机制）

逐项 grep grounded 主链实际调用，**不调用的一律删除，不做 `if grounded: skip()` 残留**：

| 旧机制 | 判断标准 |
|---|---|
| Preflight repair（GENERATE_PREFLIGHT） | grounded 是否还触发 |
| candidate repair / LLM locator repair | 同上 |
| GQ2 多余 regeneration / anti_pattern self-heal | 只保留必需的重生路径 |
| full snapshot planner（_pages_to_text 主路径） | compact refs 已接管 → 删除主路径 |
| legacy scope normalization | identity 已取代 → 确认 |
| legacy fallback（无探索降级） | 保留（登录场景可能无探索）——审计后定 |

### P1 — 统一 memory/disk explore cache

现状：`load()` 先查内存再查盘，诊断时"删了文件内存还命中"造成混乱。

```python
class ExploreCache:
    load() / save() / invalidate()   # memory/disk 由它自己管理
# 测试/验证时：
cache.invalidate(site, auth_profile)  # 而不是 rm json + 重启 backend
```

### P2 — Stable Identity 更 lazy

现状：`_record_page` 对"重复 action 且无 context_role"做 identity enrichment——
已接近 lazy。确认边界：只 enrich unresolved duplicate groups，不做全量。

### P2 — Resolver 收紧

identity 是 **constraint（filter）** 而非 scoring 候选：

```
identity 有 → 先 filter candidate set
role/name → semantic verify
visibility/actionability → representation arbitration
```

删除 identity 参与 STRATEGY_SCORES 竞争的逻辑（score=120 等）——改为先过滤再验证。

## 三、明确不做（冻结/暂缓）

- **Policy 冻结**：不再出现 upload_policy / delete_policy / cart_policy 等场景 guard。
  永远维持一个抽象：`goal + verified transitions → derive progress → restrict candidates`。
  若未来必须为某网站硬编码 "View Cart"/"Continue Shopping"，说明方向偏了。
- **`_MAX_OBSERVATIONS` 暂缓删除**：本轮无 observation cap 干扰（5 steps 完成），
  属于清理项不紧急；MAX_OBSERVATIONS_PER_URL 已删（URL ≠ State）。
- **不再加 R8 大功能**。

## 四、G3 定位

```text
State Cursor = construction（deterministic）
G3 = invariant assert（理论上永远不触发）
```

**不是**：G3 fail → build retry hint → 告诉 LLM expected obs → regenerate。
后续 cleanup 逐步削掉 STATE_GROUNDING_MISMATCH 的 retry prompt 路径。

## 五、验收信号

- 核心代码行数**下降**（测试数可以上升）
- 不再出现"每修一个 BFC，多一个 guard / retry / prompt 规则"
- BFC 探索 LLM calls：7 → 4（P0 后）
- 全套测试保持 ≥161 通过
