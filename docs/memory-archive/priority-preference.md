---
name: priority-preference
description: 安全类改动优先级低于功能开发——功能先行，安全修复不阻塞推进
metadata: 
  node_type: memory
  type: feedback
  originSessionId: d8eafafe-5097-4a61-ae35-850fb8409f6f
  modified: 2026-08-13T09:57:21.776Z
---

用户明确要求：**以后安全类改动（SSRF 防护、路径穿越、XSS 等）优先级低于功能开发**。

**Why:** 项目是本地演示/学习用途（非生产部署），安全风险实际暴露面小；用户更关注功能推进和学习叙事完整性。

**How to apply:** 规划任务时功能优先；安全类修复可攒批处理或在用户明确要求时再做，不主动打断功能开发节奏。例外：若安全改动与正在开发的功能直接相关（如新增 goto 时顺带校验），可一并做。
