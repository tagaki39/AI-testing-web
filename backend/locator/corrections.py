"""
══════════════════════════════════════════════════════════════════════
corrections.py — 持久化定位覆盖规则（L1 corrections 闭环）
══════════════════════════════════════════════════════════════════════

【这个文件在项目中的位置】
  架构 v2 管线的 Tier 0（高优先级 candidate source）：

    执行失败 → 人工提交修正（页面 + 语义键 → 正确 locator）
      → 再次执行时 correction 作为最高分候选进入解析管线
      → 仍过统一裁决（唯一性 + 评分 + margin 门槛）——不绕过 Resolver
      → 成功 verified_count+1；连续失败 ≥3 自动禁用（熔断）

【核心原则（ROADMAP/评审既定）】
  "correction 是 candidate source，不绕过 Resolver；成功/失败统计 +
  连续失败 disable"——与参考项目（绕过评分、只靠可见性+熔断兜底）的
  原则分歧点，本实现保持保守版。

【措辞红线】
  叫"持久化覆盖规则"，不叫"学习"。

【存储设计（小而安全，仿 explore_cache）】
  - 内存 + 文件两级（corrections.json，项目根，gitignored），零 DB
  - key = (generalize_url(url), target_key(target))，upsert 语义：
    同键提交更新 locator、清失败计数、重新启用（保留 verified_count）

【学习路径】
  generalize_url（URL 泛化）→ upsert / find_enabled（读写）
  → record_success / record_failure（统计与熔断）
══════════════════════════════════════════════════════════════════════
"""

import json
import time
from pathlib import Path
from urllib.parse import urlparse

from dsl import DSLModel, Locator

STORE_FILE = Path(__file__).resolve().parents[1] / "corrections.json"
MAX_CONSECUTIVE_FAILURES = 3   # 连续失败阈值（自动禁用）


class Correction(DSLModel):
    """一条持久化定位覆盖规则。"""
    url_pattern: str               # generalize_url 后的匹配模式
    target_key: str                # 语义键（target 序列化，见 resolver.target_key）
    locator: Locator               # 修正后的定位（test_id/css/text/role+name）
    created_at: str
    verified_count: int = 0        # 命中后执行成功次数
    consecutive_failures: int = 0  # 连续失败计数（≥3 → enabled=False）
    enabled: bool = True


_memory: dict[tuple[str, str], Correction] = {}
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    if STORE_FILE.exists():
        try:
            data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
            for item in data:
                c = Correction.model_validate(item)
                _memory[(c.url_pattern, c.target_key)] = c
        except Exception:
            pass   # 文件损坏 → 空库起步（不阻断主链路）


def _save() -> None:
    try:
        items = [c.model_dump() for c in _memory.values()]
        STORE_FILE.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except Exception:
        pass   # 落盘失败不影响内存命中


def generalize_url(url: str) -> str:
    """URL → 匹配模式：去 query/fragment，纯数字路径段 → *。

    例：https://x.com/products/1?ref=ad → x.com/products/*
    （参考原项目 url_pattern.py 的简化版：不处理 UUID/长 token 段，
     演示站点够用；语义保守——泛化不足只影响命中率，不影响正确性。）
    """
    try:
        parsed = urlparse(url)
        segments = []
        for seg in parsed.path.split("/"):
            if not seg:
                continue
            segments.append("*" if seg.isdigit() else seg)
        return parsed.netloc + "/" + "/".join(segments)
    except Exception:
        return url


# ── 读写 API ──────────────────────────────────────────────────────────────────

def upsert(url: str, key: str, locator: Locator) -> Correction:
    """保存/更新一条覆盖规则（同键 upsert：更新 locator、清失败计数、
    重新启用、保留 verified_count）。"""
    _load()
    pattern = generalize_url(url)
    existing = _memory.get((pattern, key))
    correction = Correction(
        url_pattern=pattern,
        target_key=key,
        locator=locator,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        verified_count=existing.verified_count if existing else 0,
        consecutive_failures=0,
        enabled=True,
    )
    _memory[(pattern, key)] = correction
    _save()
    return correction


def find_enabled(url: str, key: str) -> Correction | None:
    """命中（URL 模式 + 语义键）且 enabled 的覆盖规则；否则 None。"""
    _load()
    correction = _memory.get((generalize_url(url), key))
    return correction if correction and correction.enabled else None


def record_success(url: str, key: str) -> None:
    """命中后执行成功：verified+1、失败计数清零（重新启用）。"""
    _load()
    correction = _memory.get((generalize_url(url), key))
    if correction is None:
        return
    correction.verified_count += 1
    correction.consecutive_failures = 0
    correction.enabled = True
    _save()


def record_failure(url: str, key: str) -> None:
    """命中后执行失败：连续失败 ≥3 → 自动禁用（熔断）。"""
    _load()
    correction = _memory.get((generalize_url(url), key))
    if correction is None:
        return
    correction.consecutive_failures += 1
    if correction.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        correction.enabled = False
    _save()


def list_all() -> list[Correction]:
    """全部规则（最新在前，前端展示/管理用）。"""
    _load()
    return sorted(_memory.values(), key=lambda c: c.created_at, reverse=True)
