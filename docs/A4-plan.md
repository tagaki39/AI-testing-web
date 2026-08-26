# A4 — Structured A11y Observation（结构化 A11y 观察）

> 目标：把 Observation 的事实源从「扁平文本化 aria_snapshot」升级为
> 「有层级、有状态、有语义容器的 A11y Tree」——用更强的 Grounding 替代兜底，
> 而不是删除兜底。
>
> 原则：CDP A11y Tree 不成为新的"大模块"，而是 ObservationStore 更高质量的数据源。
> 分 4 个 commit，每步不动其他主链（Planner/G3/Compiler/Resolver 原样）。

## 现状问题

```
aria_snapshot() → 文本 → parser → 扁平 elements（丢 parent/children/level/
focusable/disabled/dialog 祖先/语义容器）
→ 模态框场景只能靠 elementFromPoint/TARGET_OBSCURED/blacklist 补救
```

## 目标架构

```
Chromium
  ↓
CDP Accessibility.getFullAXTree
  ↓
AXNode Normalizer（锁死在 normalize_cdp_ax_node，全项目不认识 CDP 原始 JSON）
  ↓
Structured Observation（hierarchy / role+name / states / action|evidence|container / semantic context）
  ↓
ObservationStore（canonical semantic state hash）
  ↓
ActionSpace（dialog subtree restriction）
  ↓
state-scoped refs → refs-only Planner → G3 → Compiler → Resolver → Runner
```

## Commit 计划

| Commit | 内容 | 不做 |
|---|---|---|
| **A1 Provider** | CDP `getFullAXTree` + `AXNode` 规范化（`normalize_cdp_ax_node`）+ Provider 接口（CDP 默认 / aria_snapshot fallback——浏览器观察能力兼容 fallback，非定位猜测） | 不改 Planner/Compiler/Resolver |
| **A2 Structured Observation** | `ObservationElement`：parent_ref/children/level/focusable/disabled/checked/selected/expanded + `kind`（action/evidence/container）+ `semantic_context`（最近有区分能力的语义容器：dialog/form/article/listitem/group/region）+ backend_dom_node_id（仅 runtime bridge/diagnostics，绝不作为 locator） | 不砍 aria legacy（CDP 失败 fallback） |
| **A3 ActionSpace** | 当前存在 active dialog → interaction root = dialog → 只暴露 dialog subtree 的 action；`kind != action` 的元素不进 Planner 候选（Restrict，替代运行时 NON_ACTIONABLE_REF） | 不加 modal hint；elementFromPoint 保留为 cheap runtime filter |
| **A4 Semantic State** | `semantic_state_signature`（role/name/disabled/checked/expanded/语义父级，排序后 hash）替代全文 snapshot hash——相同业务状态匹配回原 obs，减少 phantom states | 不改 Resolver |

## 数据模型（A2 产出）

```python
@dataclass
class ObservationElement:
    ref: str
    role: str | None
    name: str | None
    kind: Literal["action", "evidence", "container"]
    parent_ref: str | None
    children: list[str]
    focusable: bool
    disabled: bool
    checked: bool | None
    selected: bool | None
    expanded: bool | None
    context_role: str | None      # 最近语义容器（dialog/listitem/form...）
    context_name: str | None
    backend_dom_node_id: int | None   # 仅诊断，不作为 locator
```

## 角色分类（确定性规则，不上 LLM）

```python
ACTION_ROLES = {button, link, textbox, searchbox, combobox, checkbox,
                radio, switch, option, menuitem, tab, slider}
CONTAINER_ROLES = {dialog, form, navigation, main, region, article,
                   list, listitem, group}
# 其他有 name/text 的 → evidence
```

## semantic context 构建

```python
def find_semantic_context(node, nodes):
    # 沿祖先找最近的、有 name 的语义容器（dialog/form/article/listitem/group/region）
    # → {"role": ..., "name": ...}
```

## ActionSpace（A3）

```python
dialog = find_active_dialog(observation)   # kind=container role=dialog
if dialog:
    actions = [e for e in actions if is_descendant(e, dialog)]
# 验收：modal 打开后 ActionSpace = [View Cart, Continue Shopping]，
# 底层 Add to cart ×10 不暴露（不再需要 TARGET_OBSCURED 循环）
```

## State Hash（A4）

```python
signature = sorted(semantic_signature(e) for e in elements if e.kind in {"action", "container"})
state_hash = sha256(json.dumps(signature))   # 绝不 hash AXNodeId（不稳定）
```

## 测试重点（新增 8-10 个，不堆 unit）

1. AX node normalization（CDP raw → AXNode）
2. parent/child 重建
3. ignored 节点祖先处理
4. button → action / text → evidence / dialog → container
5. dialog 限制 ActionSpace
6. disabled action 排除
7. 相同语义树 → 相同 state hash
8. dialog 状态变化 → 不同 state hash

## 真实回归（每步后跑）

- SauceDemo（登录+加购）
- AutomationExercise BFC（**验收核心**：modal → ActionSpace=[Continue Shopping, View Cart] → 直接正确决策，无 TARGET_OBSCURED 循环）
- xywhaigc 登录

## 后续（本计划不做）

- A5 AX-aware Compiler（scope 用 semantic context：`dialog "Added!"` 内 button）
- A6 Strict Resolver profile（metrics 证明 role_exact+AX scope ≥95% 后再关 fuzzy/css）

## 指标（A6 前积累）

```
semantic_grounding_rate = role_exact + role_normalized + semantic_scope 占比
```
