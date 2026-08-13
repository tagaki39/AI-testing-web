"""
══════════════════════════════════════════════════════════════════════
main.py — FastAPI 入口（HTTP 层：把前端请求接进来）
══════════════════════════════════════════════════════════════════════

【这个文件在项目中的位置】
  数据流的出入口：
    前端 fetch →【这里：FastAPI 路由】→ ai_agent / runner → 响应回前端

【三个 API + 一个静态托管】
    POST /api/generate        自然语言 → AI 生成 DSL（只生成不执行）
    POST /api/execute         DSL → Playwright 执行 → 报告
    GET  /api/artifacts/...   执行截图（图片文件）
    GET  /（静态托管）         前端页面 index.html

【FastAPI 的核心魔法（面试点）】
  函数参数写成 Pydantic 模型 → FastAPI 自动解析请求体 JSON、
  自动校验（非法直接返回 422）、自动转成模型对象。
  你写的是"声明"，框架做的是"处理"。

【学习路径】
  顶部 .env 加载（环境配置）→ 路由注册 → 每个 API 函数
══════════════════════════════════════════════════════════════════════
"""

import os
from pathlib import Path

# ⚠️ 必须在导入 ai_agent / runner 之前加载 .env！
# 因为 ai_agent.py / runner.py 的模块顶层会读取环境变量（os.getenv）。
# 如果先 import 再加载 .env，它们读到的就是空值。
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

# 第三方库：FastAPI（Web 框架）、Pydantic（数据校验）、StaticFiles（托管静态文件）
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 本项目模块（注意：现在才 import，.env 已加载完毕）
from ai_agent import generate_dsl
from dsl import DSLCase, validate_case
from runner import execute_case

# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="AI Web Testing", version="1.0.0")
ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "artifacts"


# ── API ────────────────────────────────────────────────────────────────────────
# 每个 API = 装饰器声明路由 + Pydantic 模型声明请求格式 + 业务函数

class GenerateRequest(BaseModel):
    """POST /api/generate 的请求体契约：必须有一个非空字符串 prompt。"""
    prompt: str              # 自然语言需求


class ExecuteRequest(BaseModel):
    """POST /api/execute 的请求体契约。"""
    case: dict               # 完整的 DSL JSON（前端传的）
    input_values: dict = {}  # 变量值，如 {"email": "test@x.com"}


@app.post("/api/generate")
def api_generate(req: GenerateRequest):
    """自然语言 → AI 生成 DSL（只生成不执行，让用户先审阅）。

    返回 {"ok": true, "case": {...}, "meta": {...}} 给前端展示。
    meta.snapshot_used 表示 AI 是否参考了真实页面结构（ARIA 快照）。
    AI 生成失败（网络/校验）→ HTTP 400 + 错误信息。
    """
    try:
        case, meta = generate_dsl(req.prompt)
        return {"ok": True, "case": case.model_dump(), "meta": meta}   # 模型 → dict → JSON
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"AI 生成失败: {str(exc)[:300]}")


@app.post("/api/execute")
def api_execute(req: ExecuteRequest):
    """DSL → Playwright 执行 → 报告。

    注意：这里再次 validate_case（安全边界第二道门）——
    前端传来的 DSL 是"用户可控"的，不能只信任 AI 生成那次校验。
    任何入口都不能绕过 DSL 校验直接执行。
    """
    try:
        case = validate_case(req.case)   # 再次校验（前后端都不能绕过）
        report = execute_case(case, req.input_values)
        return {"ok": True, "report": report}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"执行失败: {str(exc)[:300]}")


@app.get("/api/artifacts/{run_id}/{filename}")
def api_artifact(run_id: str, filename: str):
    """返回执行截图（图片文件）。

    URL 示例：/api/artifacts/run-1/step-01.png
    前端 <img src="..."> 直接引用这个地址显示截图。

    ⚠️ 路径穿越防护：run_id/filename 可能被传 ../——先 resolve 再校验
    最终路径必须仍在 ARTIFACTS_DIR 内（修复 #6）。
    """
    base = ARTIFACTS_DIR.resolve()
    path = (base / run_id / filename).resolve()
    if base not in path.parents:
        raise HTTPException(status_code=404, detail="路径不合法")
    if not path.exists():
        raise HTTPException(status_code=404, detail="截图不存在")
    return FileResponse(path, media_type="image/png")


# ── 静态前端（零构建，直接托管 HTML）────────────────────────────────────────────
# 把 frontend/ 目录整体托管：访问 http://127.0.0.1:9000/ 直接返回 index.html。
# 这就是"单端口"架构：前端页面和 API 同一个服务，没有跨域问题。

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


# ── 启动入口 ────────────────────────────────────────────────────────────────────
# python main.py 直接运行本文件时执行这里（被 import 时不执行，靠 __name__ 判断）

if __name__ == "__main__":
    import uvicorn
    # uvicorn 是 FastAPI 的服务器：把 app 跑在 127.0.0.1:9000
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("APP_PORT", "9000")))
