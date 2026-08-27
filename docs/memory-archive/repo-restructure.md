---
name: repo-restructure
description: GitHub 仓库重构——示例 独立到 AI-testing-web，原仓库只保留完整项目
metadata: 
  node_type: memory
  type: project
  originSessionId: fa89dbdd-e4e5-45ca-9d79-96c14c901f99
  modified: 2026-08-04T07:52:29.622Z
---

2026-08-04 用户重构了 GitHub 仓库（学习用途）：

- **tagaki39/AI_Web_Testing**：完整参考实现（FastAPI+React），main 回退到 `4427060`（初始导入），无 示例/学习相关历史
- **tagaki39/AI-testing-web**（public）：示例 专用仓库，两条 commit（`8747db1` v1 定位器 + `d1d7606` 三分法升级），内容干净（无 .env/artifacts/"学习"字样）
- 示例 本地副本在 `d:\ai_test\ai-testing-示例-local`（不完整，需从 GitHub clone 补 README/frontend）

**参考实现本地重跑需要重新应用两个 workaround（已在重构中丢失）**：
1. `backend/alembic/versions/20260608_0025_sse_event_log.py` 的 `down_revision` 断链：`45061d8892d7`（不存在）→ `1c65d6ff37db`
2. `frontend/vite.config.ts` 代理端口：本地因僵尸进程占 8000 时改 8001（现已是 8000）

**Why:** 学习场景需要干净、独立、可公开的 示例 仓库。
**How to apply:** 涉及 git 操作或参考实现环境时，先确认在哪个仓库工作；参考实现跑不起来先检查上述两个修复。
