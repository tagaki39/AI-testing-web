# Grounding Regressions

两个跨页面状态错位（STATE_GROUNDING_MISMATCH）的标准回归用例。
作为 Architecture v2（refs-only Planner + State Grounding Validator）的验收基准。

| id | 场景 | 状态 |
|----|------|------|
| [saucedemo_grounding](./saucedemo_grounding.json) | inventory → detail 后引用 inventory ref | 待 v2 拒绝 |
| [automationexercise_grounding](./automationexercise_grounding.json) | list → detail 后引用 list 的 Add to cart | 待 v2 拒绝 |

**v2 验收**：两个用例在**执行前**被 State Grounding Validator 拒绝
（STATE_GROUNDING_MISMATCH），不要求自动修复。

**指标**：false-resolution rate 必须为零（不得因 fuzzy/业务聚类误命中等错误成功）。
