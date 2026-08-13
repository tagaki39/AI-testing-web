"""生成稳定性诊断 v2（临时脚本，用完可删）。

方法论（分层判断）：
  A/B/C = 生成阶段稳定性（Planner / Locator / Preflight）
  D     = 执行阶段稳定性（同一 DSL 重复执行）

流程：
  1. 4 次 generate + preflight + execute，记录 plan_hash
  2. 挑重复 plan_hash 的 case，固定执行 2-3 次（execute 不需要 LLM）
  3. 输出诊断矩阵
"""
import hashlib
import json
import urllib.request

GOAL = "Open saucedemo.com, login with standard_user / secret_sauce, click add to cart"
BASE = "http://127.0.0.1:9000"


def post(path, body):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def plan_hash(case) -> str:
    """DSL 结构指纹：action/target/scope/value 模板 → 哈希。"""
    normalized = [
        {
            "action": s.get("action"),
            "target": s.get("target"),
            "scope": s.get("scope"),
            "value": s.get("value"),
        }
        for s in case.get("steps", [])
    ]
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:10]


def semantic_signature(case) -> list[dict]:
    """核心行为签名：action + target 语义 + scope（忽略 description/变量顺序等噪音）。"""
    sig = []
    for s in case.get("steps", []):
        t = s.get("target") or {}
        scope = s.get("scope") or {}
        if isinstance(t, dict):
            role, name, text = t.get("role"), t.get("name"), t.get("text")
        else:
            role, name, text = None, None, (str(t) if isinstance(t, str) else None)
        sig.append({
            "action": s.get("action"),
            "role": role, "name": name, "text": text,
            "scope": scope.get("has_text") if isinstance(scope, dict) else scope,
        })
    return sig


def action_hash(case) -> str:
    """核心动作序列指纹（Planner 结构稳定性指标）。"""
    payload = json.dumps(semantic_signature(case), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:8]


def verify_hash(case) -> str:
    """最终验证步骤指纹（验证策略稳定性指标）。"""
    steps = case.get("steps", [])
    final = steps[-1] if steps else {}
    payload = json.dumps({
        "action": final.get("action"),
        "target": final.get("target"),
        "value": final.get("value"),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:8]


def execute_case(case, label="", verbose=False):
    input_values = {}
    for c in case.get("input_contract", []):
        v = c.get("default") or c.get("value")
        if v:
            input_values[c["key"]] = v
    input_values["password"] = "secret_sauce"
    r = post("/api/execute", {"case": case, "input_values": input_values})
    report = r.get("report", {})
    failed = next(
        (x for x in report.get("results", []) if x.get("status") == "failed"),
        None,
    )
    failed_desc = f"{failed['step_index']}:{failed['action']}" if failed else "-"
    print(f"    [{label}] {report.get('status')} {report.get('passed_steps')}/{report.get('total_steps')} failed_step={failed_desc}")
    if verbose and failed:
        t = json.dumps(failed.get("target"), ensure_ascii=False)[:60]
        print(f"      FAIL 详情: target={t} scope={json.dumps(failed.get('scope'), ensure_ascii=False)[:40]}")
        print(f"      ERR: {(failed.get('error') or '')[:120]}")
    return report


# ── Phase 1：4 次生成诊断 ──────────────────────────────────────────────
print("=== Phase 1: 4 次生成 + 执行 ===")
runs = []
for run in range(1, 5):
    d = post("/api/generate", {"prompt": GOAL})
    meta = d.get("meta") or {}
    pf = meta.get("preflight")
    case = d["case"]
    h = plan_hash(case)
    implicit = [x.get("selected", "")[:20] for x in (pf or {}).get("implicit_resolutions") or []]
    report = execute_case(case, f"RUN{run}", verbose=True)
    failed_step = next(
        (f"{x['step_index']}:{x['action']}" for x in report.get("results", [])
         if x.get("status") == "failed"),
        "-",
    )
    runs.append({
        "run": run, "hash": h, "steps": report.get("total_steps"),
        "pf_remaining": len((pf or {}).get("blocking_issues") or []),
        "implicit": implicit, "result": f"{report.get('passed_steps')}/{report.get('total_steps')}",
        "failed": failed_step, "case": case,
    })

print("\n=== 诊断矩阵 ===")
print(f"{'Run':<4} {'action_hash':<10} {'verify_hash':<10} {'Steps':<6} {'PF剩余':<6} {'结果':<8} {'失败步骤'}")
for r in runs:
    case = r["case"]
    print(f"{r['run']:<4} {action_hash(case):<10} {verify_hash(case):<10} {r['steps']:<6} {r['pf_remaining']:<6} {r['result']:<8} {r['failed']}")
print("\n   action_hash 一致 = Planner 核心动作序列稳定")
print("   verify_hash 一致 = 最终验证策略稳定")

# 稳定性指标汇总（区分"结构稳定"与"验证策略波动"）
n = len(runs)
step_stable = len({r["steps"] for r in runs}) == 1
action_hashes = [action_hash(r["case"]) for r in runs]
verify_hashes = [verify_hash(r["case"]) for r in runs]
core_action_stable = max(action_hashes.count(h) for h in set(action_hashes)) if action_hashes else 0
verify_stable = max(verify_hashes.count(h) for h in set(verify_hashes)) if verify_hashes else 0
passed_count = sum(1 for r in runs if r["failed"] == "-")
print("\n=== 稳定性指标 ===")
print(f"  step_count_stability      = {n}/{n}" if step_stable else f"  step_count_stability      = 波动（步骤数 {sorted({r['steps'] for r in runs})}）")
print(f"  execution_success_rate    = {passed_count}/{n}")
print(f"  core_action_stability     = {core_action_stable}/{n}")
print(f"  verification_strategy     = {verify_stable}/{n}")

# ── Phase 2：固定 DSL 重复执行（D 层独立测试，不调 LLM）──────────────
# 优先挑重复 plan_hash 的 case；没有重复就用第一个 run 的 case
from collections import Counter
hash_counts = Counter(r["hash"] for r in runs)
dup = next((h for h, c in hash_counts.items() if c >= 2), None)
case = next(r["case"] for r in runs if r["hash"] == dup) if dup else runs[0]["case"]
print(f"\n=== Phase 2: plan_hash={plan_hash(case)} 固定 DSL 重复执行 3 次（D 层测试，不调 LLM）===")
for i in range(1, 4):
    execute_case(case, f"EXEC{i}")
