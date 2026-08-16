"""
══════════════════════════════════════════════════════════════════════
metrics.py — timings.jsonl 聚合分析（ROADMAP §8 核心指标）
══════════════════════════════════════════════════════════════════════

用法（零依赖，直接运行）：

    py backend/metrics.py

读取项目根 timings.jsonl（main.py 在每次生成/执行成功后自动追加一行），
按 ROADMAP §8 的指标清单输出聚合表。没有数据时给出提示。

指标清单（对应 ROADMAP §8）：
  - Planner raw schema success rate / Schema recovery success rate
  - cache hit rate / explore 预算消耗
  - 各阶段耗时 p50/p95（url/explore/planner/preflight）
  - Preflight 修复有效率（issues_before → issues_after）
  - grounding 覆盖（ref_steps_checked / compiled_targets）
  - 执行通过率 / 总耗时 / 平均步耗时
  - 定位策略命中分布 + resolution latency p50/p95（resolve_ms）
  - 失败步骤错误类型 Top N

注意：timings.jsonl 已脱敏（不记录 value 明文），可放心提交分析结果。
══════════════════════════════════════════════════════════════════════
"""

import json
import sys
from collections import Counter
from pathlib import Path

LOG = Path(__file__).resolve().parents[1] / "timings.jsonl"


# ── 加载与基础统计 ────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    if not LOG.exists():
        return []
    records = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def _pct(values: list, p: int):
    """nearest-rank 百分位。"""
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))
    return s[idx]


def _fmt(v) -> str:
    return "-" if v is None else f"{v:,.0f}"


def _rate(hits: int, total: int) -> str:
    return "-" if total == 0 else f"{hits}/{total}（{hits / total:.0%}）"


# ── 各分组聚合 ────────────────────────────────────────────────────────────────

def _planner_metrics(gens: list[dict]) -> dict:
    planners = [g.get("planner") or {} for g in gens]
    attempts = [p.get("planner_attempts", 1) for p in planners]
    used = sum(1 for p in planners if p.get("schema_recovery_used"))
    success = sum(1 for p in planners if p.get("schema_recovery_success"))
    modes = Counter(p.get("mode", "?") for p in planners)
    return {
        "raw_schema_success": _rate(sum(1 for a in attempts if a == 1), len(attempts)),
        "recovery_used": _rate(used, len(planners)),
        "recovery_success": _rate(success, used),
        "modes": dict(modes),
    }


def _explore_metrics(gens: list[dict]) -> dict:
    explores = [g.get("explore") for g in gens if g.get("explore")]
    hits = sum(1 for g in gens if g.get("cache_hit"))
    done = sum(1 for e in explores if e.get("done"))
    return {
        "cache_hit": _rate(hits, len(gens)),
        "runs": len(explores),
        "done": _rate(done, len(explores)),
        "avg_pages": sum(e.get("pages_visited", 0) for e in explores) / len(explores) if explores else None,
        "avg_steps": sum(e.get("steps_used", 0) for e in explores) / len(explores) if explores else None,
        "avg_llm_calls": sum(e.get("llm_calls", 0) for e in explores) / len(explores) if explores else None,
    }


def _preflight_metrics(gens: list[dict]) -> dict:
    pfs = [g.get("preflight") for g in gens if g.get("preflight")]
    before = [pf.get("issues_before") or 0 for pf in pfs]
    after = [pf.get("issues_after") or 0 for pf in pfs]
    effective = [pf.get("effective_repairs") or 0 for pf in pfs]
    repairs = [pf.get("repairs_applied") or 0 for pf in pfs]
    return {
        "runs": len(pfs),
        "issues_before_avg": sum(before) / len(before) if before else None,
        "issues_after_avg": sum(after) / len(after) if after else None,
        "effective_repairs_avg": sum(effective) / len(effective) if effective else None,
        "repairs_applied_avg": sum(repairs) / len(repairs) if repairs else None,
        "coverage": pfs[-1].get("coverage") if pfs else None,   # 最新一轮
    }


def _grounding_metrics(gens: list[dict]) -> dict:
    gs = [g.get("grounding") or {} for g in gens]
    refs = [g.get("ref_steps_checked", 0) for g in gs]
    compiled = [g.get("compiled_targets", 0) for g in gs]
    return {
        "avg_ref_steps": sum(refs) / len(refs) if refs else None,
        "avg_compiled_targets": sum(compiled) / len(compiled) if compiled else None,
    }


def _execution_metrics(execs: list[dict]) -> dict:
    steps = [s for e in execs for s in e.get("steps", [])]
    passed_steps = sum(1 for s in steps if s.get("status") == "passed")
    total_ms = [e.get("total_ms") or 0 for e in execs]
    avg_step_ms = [e.get("avg_step_ms") or 0 for e in execs]
    resolve_ms = [s["resolve_ms"] for s in steps
                  if isinstance(s.get("resolve_ms"), int) and s["resolve_ms"] > 0]
    strategies = Counter(s.get("resolved_by") for s in steps
                         if s.get("status") == "passed" and s.get("resolved_by"))
    failed = [s for s in steps if s.get("status") == "failed"]
    errors = Counter(
        (s.get("error") or "?").split(":", 1)[0] if ":" in (s.get("error") or "?")
        else (s.get("error") or "?")
        for s in failed
    )
    return {
        "cases": len(execs),
        "step_pass_rate": _rate(passed_steps, len(steps)),
        "total_ms_p50_p95": (_fmt(_pct(total_ms, 50)), _fmt(_pct(total_ms, 95))),
        "avg_step_ms_p50_p95": (_fmt(_pct(avg_step_ms, 50)), _fmt(_pct(avg_step_ms, 95))),
        "resolve_ms_p50_p95": (_fmt(_pct(resolve_ms, 50)), _fmt(_pct(resolve_ms, 95))),
        "strategies": dict(strategies),
        "top_errors": errors.most_common(5),
    }


def _gen_timing_metrics(gens: list[dict]) -> dict:
    out = {}
    for key, label in (
        ("url_resolve_ms", "url 解析"),
        ("explore_ms", "探索"),
        ("planner_ms", "Planner"),
        ("preflight_ms", "Preflight"),
    ):
        vals = [g.get("timings", {}).get(key) for g in gens
                if isinstance(g.get("timings", {}).get(key), int)]
        out[label] = (vals, _pct(vals, 50), _pct(vals, 95))
    return out


# ── 输出 ──────────────────────────────────────────────────────────────────────

def main() -> int:
    records = _load()
    if not records:
        print("timings.jsonl 无数据——先跑几次真实生成/执行（/api/generate + /api/execute）。")
        return 1

    gens = [r for r in records if r.get("type") == "generate"]
    execs = [r for r in records if r.get("type") == "execute"]
    first, last = records[0]["ts"], records[-1]["ts"]

    print("═" * 66)
    print("ROADMAP §8 核心指标（来源: timings.jsonl）")
    print(f"时间范围: {first} → {last}")
    print(f"记录: generate ×{len(gens)}  execute ×{len(execs)}")
    print("═" * 66)

    if gens:
        pm = _planner_metrics(gens)
        print("\n【Planner 质量】")
        print(f"  raw schema 一次成功率 : {pm['raw_schema_success']}")
        print(f"  recovery 使用率       : {pm['recovery_used']}")
        print(f"  recovery 成功率       : {pm['recovery_success']}")
        print(f"  模式分布              : {pm['modes']}")

        em = _explore_metrics(gens)
        print("\n【探索】")
        print(f"  缓存命中率            : {em['cache_hit']}")
        print(f"  探索完成率            : {em['done']}（{em['runs']} 轮）")
        print(f"  平均 pages/steps/LLM  : {em['avg_pages'] or '-'} / {em['avg_steps'] or '-'} / {em['avg_llm_calls'] or '-'}")

        tm = _gen_timing_metrics(gens)
        print("\n【生成链路耗时（p50 / p95, ms）】")
        for label, (vals, p50, p95) in tm.items():
            print(f"  {label:<12}: {_fmt(p50):>8} / {_fmt(p95):>8}   (n={len(vals)})")

        pfm = _preflight_metrics(gens)
        print("\n【Preflight 修复】")
        print(f"  平均 issues before/after : {pfm['issues_before_avg']} / {pfm['issues_after_avg']}（{pfm['runs']} 轮）")
        print(f"  平均 effective repairs   : {pfm['effective_repairs_avg']}（applied {pfm['repairs_applied_avg']}）")
        print(f"  最新 observation 覆盖    : {pfm['coverage']}")

        gm = _grounding_metrics(gens)
        print("\n【Grounding（G3/R1）】")
        print(f"  平均 ref 步骤 / 编译产出 : {gm['avg_ref_steps']} / {gm['avg_compiled_targets']}")

    if execs:
        xm = _execution_metrics(execs)
        print("\n【执行】")
        print(f"  用例数 / 步骤通过率      : {xm['cases']} / {xm['step_pass_rate']}")
        print(f"  用例总耗时 p50/p95 (ms)  : {xm['total_ms_p50_p95'][0]} / {xm['total_ms_p50_p95'][1]}")
        print(f"  平均步耗时 p50/p95 (ms)  : {xm['avg_step_ms_p50_p95'][0]} / {xm['avg_step_ms_p50_p95'][1]}")
        print(f"  resolve 延迟 p50/p95 (ms): {xm['resolve_ms_p50_p95'][0]} / {xm['resolve_ms_p50_p95'][1]}")
        print(f"  定位策略命中分布         : {xm['strategies'] or '-'}")
        if xm["top_errors"]:
            print(f"  失败步骤错误 Top{min(5, len(xm['top_errors']))}:")
            for err, n in xm["top_errors"]:
                print(f"    - {err}: ×{n}")

    print("\n" + "═" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
