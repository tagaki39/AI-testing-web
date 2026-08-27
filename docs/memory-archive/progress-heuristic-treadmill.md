---
name: progress-heuristic-treadmill
description: 探索进度判定不得堆字符串匹配规则——LLM 决定语义结构，程序按真实执行事实判断进度
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 4f4c7919-a33e-4aa0-b5ea-c5a4bc05e005
  modified: 2026-08-27T08:11:59.253Z
---

项目最重要的原则（多次方向错误后确立）：

> **LLM 决定语义结构，程序根据真实执行事实判断进度。**

**Why:** 我在探索完成判定（Milestone Progress）上连续犯了同样的方向错误——遇到一个站点匹配不上就补一条字符串规则，把 Locator 时代的 heuristic treadmill 转移到了 Goal Progress 上。具体犯错链：

1. `_GOAL_NAVIGATION_RE`（"进入 X 页面"→ 要求 verified 导航）——只补了导航段，补不完整个目标（填表单/点生成还会漏），被外部评审否决
2. 三态 Completion（READY/INCOMPLETE/UNKNOWN）——方向正确（login 后不再 auto_finish 截断），但被 S2-P1 的 Goal Contract 正式化取代，定位为临时
3. progress.py 字符串匹配累积：去空格归一（"登 录"="登录"）、反向 substring（元素名 ⊂ term）、纯动词过滤（≤2 字 intrinsic）、navigate 用任意 obs 元素文本判定、verify 自动关联前置 milestone——每个规则都是"遇到一个表达补一个"的演化，偏离 typed evidence

**How to apply:**
- 完成判定只从四类真实执行事实推导：auth→verified transition；navigate→URL/title 页面身份（仅 entry obs 或 transition.to 的 obs，**不匹配任意元素文本**——侧边栏菜单词一直存在 ≠ 到达）；input→成功 fill + field_terms/value_ref；ready→当前 obs 目标 action 存在且 enabled；side_effect→runner pending；verify→evidence_ready（不参与 Explorer 完成判断）
- LLM 输出的结构有随机性（Contract 一次生成但形态波动）→ 用**确定性 Contract Canonicalizer**（删入口 navigate、合并连续 navigate、重新编号）归一化，不靠 progress 兼容坏输出
- 语义同义变体（图片生成/图像生成、账号/账户、中文目标 vs 英文页面）**程序判不了就交给 LLM 语义判断**（探索继续到 LLM finish / model_finish），不补词表
- 原则：LLM 生成语义结构，程序只验证真实执行事实，NLP heuristic 只减不增。相关：[[reference-project-and-determinism]]、[[priority-preference]]
