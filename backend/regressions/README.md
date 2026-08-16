# Grounding Regressions

两个跨页面状态错位（STATE_GROUNDING_MISMATCH）的标准回归用例。
作为 Architecture v2（refs-only Planner + State Grounding Validator）的验收基准。

| id | 场景 | 状态 |
|----|------|------|
| [saucedemo_grounding](./saucedemo_grounding.json) | inventory → detail 后引用 inventory ref | ✅ 已由 Validator 拒绝 |
| [automationexercise_grounding](./automationexercise_grounding.json) | list → detail 后引用 list 的 Add to cart | ✅ 已由 Validator 拒绝 |

**v2 验收**：两个用例在**执行前**被 State Grounding Validator 拒绝
（STATE_GROUNDING_MISMATCH），不要求自动修复。

**✅ 已达成**：验收实现在 [../tests/test_grounding.py](../tests/test_grounding.py)
（`py backend/tests/test_grounding.py`，零依赖）。两个 regression 的
最小图 + 用例复现 S0 --action--> S1 后引用 S0 元素的模式，
均被 `backend/grounding.py` 的 `validate_state_grounding` 在执行前拒绝。

**实现要点**（Validator 语义）：
- 静态推导 expected state：goto URL 匹配 + 转移边追踪；不可追踪处
  fail-open（只拒绝可证明的错位，不误拒合法计划）
- 编造 ref（不在图中）→ `UnknownTargetRefError` 硬拒绝，不清空降级
- mismatch 自动替换（按转移图换当前 state 对应元素）是第二阶段，本期不做

**指标**：false-resolution rate 必须为零（不得因 fuzzy/业务聚类误命中等错误成功）。
