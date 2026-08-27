---
name: user-interview-prep
description: 用户在准备学习——用精简版 AI 测试平台 示例 学习和讲解
metadata: 
  node_type: memory
  type: user
  originSessionId: fa89dbdd-e4e5-45ca-9d79-96c14c901f99
  modified: 2026-08-04T07:52:37.728Z
---

用户在准备求职学习（目标：把 AI Web 测试平台项目写进简历并讲清楚）。

- 没有系统学过 A11y/无障碍概念，需要从"为什么选这个方案"的推理链讲起
- 已有一个精简版 示例（tagaki39/AI-testing-web），约 800 行，保留 AI 生成 DSL（DeepSeek）+ Playwright 执行两个核心
- 有 DeepSeek API key，配置在 `simplified-示例/.env`（[已脱敏]...，曾提醒过需重置）
- 学习方式偏好：对比 diff、真实 bug 故事、学习话术，不喜欢空泛理论
- Windows 环境，Python 用 `py` 启动器 + uv（`py -m uv run`），Node 在 `D:\nodejs\node-v24.18.1-win-x64`（需手动加 PATH）

**Why:** 讲解和写代码时要贴合他的学习目标与知识基础。
**How to apply:** 解释技术概念时用"痛点→推理链→方案"结构；优先给可运行的最小示例；提到定位/DSL/AI 生成时联系学习叙事。
