---
name: reference-project-and-determinism
description: 两条工作原则——问题优先参照参考实现方法解决；能确定性判断的地方不用 LLM
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 4f4c7919-a33e-4aa0-b5ea-c5a4bc05e005
  modified: 2026-08-26T07:55:44.226Z
---

用户的两条工作原则（2026-08-26 明确要求写入）：

1. **问题优先参照参考实现的方法解决**：遇到有问题的部分（bug、性能、架构缺口），先思考参考实现（d:\ai_test\AI_Web_Testing，完整版）是怎么解决的，能借鉴就借鉴。参考实现关键机制：click_preprocessor（点击被遮挡时自动诊断→dismiss→retry）、5-tier hybrid locator、Pydantic 校验 + constrained retry、确定性修复优先于 LLM 修复。注意借鉴的是"思路/机制"，不是照搬——本项目（AI-testing-web）的探索模型（LLM 决策循环）与参考实现（确定性 DSL 回放）架构不同，适配到本架构再落地。

2. **能确定性判断的地方就不要用 LLM**：结构约束（状态机、ref 合法性、数量完成度、输出格式）一律用代码实现，LLM 只做语义选择。实例：R7.1 State Cursor Grounding（transition_ref 展开 + 断言 grounding 全确定性）、R6 Policy 从 StateGraph 派生（不存第二状态源）、schema recovery 不嵌坏输出。

**Why:** 用户观察到参考实现没有我们踩过的坑（modal 遮挡等），因为它的确定性机制在处理；而 prompt 补丁会滚雪球（一个场景一个提示词）。

**How to apply:** 遇到问题先问"参考实现怎么处理这个？"和"这一步能用代码确定吗？"；prompt 写第三版之前先找确定性方案。相关：[[repo-restructure]]（两仓库分工）、[[priority-preference]]（功能优先于安全改动）。
