"""AI Web 测试平台 — FastAPI 入口。

启动:
    python main.py
然后浏览器打开 http://127.0.0.1:9000

三个 API:
    POST /api/generate   自然语言 → AI 生成 DSL
    POST /api/execute    DSL → Playwright 执行 → 报告
    GET  /artifacts/..   执行截图
"""

import os
from pathlib import Path

# ⚠️ 必须在导入 ai_agent / runner 之前加载 .env，
# 因为它们模块顶层会读取环境变量（os.getenv）。
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ai_agent import generate_dsl
from dsl import DSLCase, validate_case
from runner import execute_case

# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="AI Web Testing", version="1.0.0")
ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "artifacts"


# ── API ────────────────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    prompt: str              # 自然语言需求


class ExecuteRequest(BaseModel):
    case: dict               # 完整的 DSL JSON
    input_values: dict = {}  # 变量值，如 {"email": "test@x.com"}


@app.post("/api/generate")
def api_generate(req: GenerateRequest):
    """自然语言 → AI 生成 DSL（只生成不执行）。"""
    try:
        case = generate_dsl(req.prompt)
        return {"ok": True, "case": case.model_dump()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"AI 生成失败: {str(exc)[:300]}")


@app.post("/api/execute")
def api_execute(req: ExecuteRequest):
    """DSL → Playwright 执行 → 报告。"""
    try:
        case = validate_case(req.case)   # 再次校验（安全边界，前后端都不能绕过）
        report = execute_case(case, req.input_values)
        return {"ok": True, "report": report}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"执行失败: {str(exc)[:300]}")


@app.get("/api/artifacts/{run_id}/{filename}")
def api_artifact(run_id: str, filename: str):
    """返回执行截图。"""
    path = ARTIFACTS_DIR / run_id / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="截图不存在")
    return FileResponse(path, media_type="image/png")


# ── 静态前端（零构建，直接托管 HTML）────────────────────────────────────────────

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("APP_PORT", "9000")))
