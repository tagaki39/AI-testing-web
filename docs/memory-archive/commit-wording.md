---
name: commit-wording
description: "commit/push 措辞与署名约束——不带 Co-Authored-By、禁\"参考实现/学习/示例\"字样、说明 bug 与量化"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: b168fa3c-6a22-4c23-a3bc-3b96c8fb452f
  modified: 2026-08-12T09:43:26.657Z
---

用户在 push 过程中的明确要求：

1. **commit 默认不带 `Co-Authored-By: Claude` 行**——GitHub 上只显示用户一人（小功能/修复类 commit 不带）；**比较大的功能更新时可以带上**（用户明确说过"大功能更新时可以带上你"）
2. commit message 及推送文本**不得出现"参考实现""学习""示例""精简版"等字样**（仓库对外展示一致性）
3. **commit message 可以说明：遇到和修复的 bug、结果有优化时带量化数据**（如 8/8 → 10/10 首过、耗时、定位分布）

**Why:** 仓库 public，查看者可能翻阅 GitHub——commit 历史必须呈现为独立完整项目（作者单一、措辞中性、叙事有数据支撑）。

**How to apply:** 写 commit message 时：去掉 Co-Authored-By；用"测试平台""本项目""生产版/完整版"等中性表述；修复类 commit 写清 bug 根因；有优化结果时附量化对比。
