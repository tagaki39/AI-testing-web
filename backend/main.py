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

import json
import os
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 本项目模块（注意：现在才 import，.env 已加载完毕）
from ai_agent import generate_dsl
from locator.corrections import list_all, upsert
from dsl import DSLCase, Locator, validate_case
from locator.resolver import target_key
from execution.runner import execute_case

# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="AI Web Testing", version="1.0.0")
ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "artifacts"

# ── SSE 执行 run 存储（内存级，单进程即可）─────────────────────────────
# POST /api/execute → 创建 run（后台线程执行）→ 立即返回 run_id；
# 前端 EventSource 订阅 GET /api/runs/{id}/events 实时进度。
# 状态：running → done（带 report）/ error（带 error）。事件队列
# 消费即弃（重连时由状态补发最终事件，幂等）。
_RUNS: dict[str, dict] = {}
_RUNS_LOCK = threading.Lock()


def _json_sse(item: dict) -> str:
    """事件 → SSE 帧（event: + data:，UTF-8 安全序列化）。"""
    return f"event: {item['type']}\ndata: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"


# ── 耗时自动记录（每次生成/执行成功追加 JSONL，便于 jq 分析）──────────
# 记录内容：生成各阶段计时（url/explore/planner/preflight）+
# 执行每步耗时与定位策略。不含敏感值（不记录 value 明文）。
TIMINGS_LOG = Path(__file__).resolve().parents[1] / "timings.jsonl"


def _append_timing(record: dict) -> None:
    """自动追加耗时记录（失败静默，不影响主链路）。"""
    try:
        record["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(TIMINGS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ── API ────────────────────────────────────────────────────────────────────────
# 每个 API = 装饰器声明路由 + Pydantic 模型声明请求格式 + 业务函数

# 统一 JSON 响应（带 charset=utf-8）：无 charset 时非浏览器客户端按 Latin-1
# 解码，PUA 图标字符（FontAwesome 等）会被拆坏——真实 E2E 用 PowerShell 调用
# 时踩坑（浏览器 fetch 不受影响，但 API 应对任意客户端健壮）。
def _json_utf8(content: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content, status_code=status_code,
                        media_type="application/json; charset=utf-8")

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
    meta: dict | None = None
    try:
        case, meta = generate_dsl(req.prompt)
        pf = meta.get("preflight") or {}
        _append_timing({
            "type": "generate",
            "timings": meta.get("timings"),
            "planner": meta.get("planner"),
            "cache_hit": meta.get("cache_hit"),
            "explore": meta.get("explore"),          # pages/steps_used/llm_calls
            "preflight": {                           # 修复链路各阶段结果
                "blocking": len(pf.get("blocking_issues") or []),
                "warnings": len(pf.get("warnings") or []),
                "issues_before": pf.get("issues_before"),
                "issues_after": pf.get("issues_after"),
                "effective_repairs": pf.get("effective_repairs"),
                "coverage": pf.get("observation_coverage"),
            },
            "normalize_removed_assertions": meta.get("normalize_removed_assertions"),
            "grounding": meta.get("grounding"),   # G3/R1：ref 校验覆盖 + 编译产出
        })
        return _json_utf8({"ok": True, "case": case.model_dump(), "meta": meta})   # 模型 → dict → JSON
    except Exception as exc:
        # 失败轮次也要记录（可观测性：异常路径不记录 = 失败无数据，
        # 无法分析失败成本/耗时构成——之前 400 轮次全部丢失）
        _append_timing({
            "type": "generate",
            "error": str(exc)[:200],
            "timings": meta.get("timings") if meta else None,
            "explore": meta.get("explore") if meta else None,
        })
        raise HTTPException(status_code=400, detail=f"AI 生成失败: {str(exc)[:300]}")


@app.post("/api/execute")
def api_execute(req: ExecuteRequest):
    """DSL → Playwright 执行（异步：立即返回 run_id，SSE 订阅实时进度）。

    流程：
      1. validate_case（安全边界第二道门——任何入口都不能绕过 DSL 校验）
      2. 创建 run（内存状态 + 事件队列），后台线程执行
      3. 立即返回 {"ok": true, "run_id": "..."}
    前端随后 EventSource 订阅 GET /api/runs/{id}/events：
      step_started / step_completed / run_finished（带完整 report）/
      run_error（带错误信息）
    """
    try:
        case = validate_case(req.case)   # 再次校验（前后端都不能绕过）
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"执行失败: {str(exc)[:300]}")
    run_id = uuid4().hex[:12]
    events: queue.Queue = queue.Queue()
    state = {"status": "running", "events": events, "report": None, "error": None}
    with _RUNS_LOCK:
        _RUNS[run_id] = state

    def _worker() -> None:
        try:
            report = execute_case(case, req.input_values, on_event=events.put)
            state["report"] = report
            state["status"] = "done"
            events.put({"type": "run_finished", "report": report})
            _append_timing({
                "type": "execute",
                "case_name": report.get("case_name"),
                "total_ms": report.get("total_duration_ms"),
                "avg_step_ms": report.get("avg_step_ms"),
                "passed": f"{report.get('passed_steps')}/{report.get('total_steps')}",
                "steps": [
                    {"i": r["step_index"], "action": r["action"], "status": r["status"],
                     "ms": r.get("duration_ms"), "resolve_ms": r.get("resolve_ms"),
                     "resolved_by": r.get("resolved_by"),
                     "target": r.get("target"), "scope": r.get("scope"),
                     "error": (r.get("error") or "")[:200]}   # error 截断；不含 value 明文
                    for r in report.get("results", [])
                ],
            })
        except Exception as exc:
            state["error"] = str(exc)[:300]
            state["status"] = "error"
            events.put({"type": "run_error", "error": state["error"]})

    threading.Thread(target=_worker, daemon=True).start()
    return _json_utf8({"ok": True, "run_id": run_id})


@app.get("/api/runs/{run_id}/events")
def api_run_events(run_id: str):
    """SSE 事件流：实时推送执行进度（EventSource 只支持 GET）。

    重连幂等：执行已结束（done/error）且队列耗尽时，补发最终事件
    再关闭（EventSource 自动重连不会丢最终结果）。队列空闲时发
    心跳注释帧保活（不产生 data 事件）。
    """
    state = _RUNS.get(run_id)

    def _stream():
        if state is None:
            yield _json_sse({"type": "run_error", "error": "run 不存在（可能已过期）"})
            return
        sent_final = False
        while True:
            item = None
            try:
                item = state["events"].get(timeout=5)
            except queue.Empty:
                pass
            if item is not None:
                if item["type"] in ("run_finished", "run_error"):
                    sent_final = True
                yield _json_sse(item)
                if sent_final:
                    break
                continue
            # 队列空：已终结 → 补发最终事件（重连幂等）→ 关闭；否则心跳
            if state["status"] in ("done", "error") and not sent_final:
                if state["status"] == "done":
                    yield _json_sse({"type": "run_finished", "report": state["report"]})
                else:
                    yield _json_sse({"type": "run_error", "error": state["error"]})
                break
            yield ": ping\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class CorrectionRequest(BaseModel):
    """POST /api/corrections 的请求体契约（L1：持久化定位覆盖规则）。"""
    url: str               # 失败步骤所在页面 URL
    target: dict | str     # 失败步骤的原始 target（生成语义键）
    locator: dict          # 修正后的定位（test_id/css/text/role+name）


@app.post("/api/corrections")
def api_upsert_correction(req: CorrectionRequest):
    """保存一条定位覆盖规则（同 URL 模式 + 语义键 upsert）。

    L1 原则：correction 不绕过 Resolver——只是最高优先级候选，
    执行时仍过唯一性 + 评分 + margin 门槛（见 runner._resolve_locator）。
    """
    try:
        key = target_key(req.target)
        if not key:
            raise ValueError("target 无法生成语义键（无定位字段）")
        saved = upsert(req.url, key, Locator(**req.locator))
        return _json_utf8({"ok": True, "correction": saved.model_dump()})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"修正保存失败: {str(exc)[:200]}")


@app.get("/api/corrections")
def api_list_corrections():
    """全部覆盖规则（最新在前，供前端展示/管理）。"""
    return _json_utf8({"ok": True, "corrections": [c.model_dump() for c in list_all()]})


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
