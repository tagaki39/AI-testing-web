"""
══════════════════════════════════════════════════════════════════════
explore_cache.py — 探索结果缓存（Speed v1）
══════════════════════════════════════════════════════════════════════

【为什么需要它】
  探索是生成链路的大头（~12s：多次 LLM 决策 + 浏览器动作）。
  同一站点重复生成时，探索结果高度可复用——缓存命中直接跳过
  整个探索回路（LLM 决策 0 次 + 浏览器动作 0 次）。

【设计（小而安全）】
  1. 内存 + 文件两级（backend/.cache/explore/*.json），不用 DB
  2. key = origin + auth_profile + 脱敏目标指纹 + schema version；
     目标相关 StateGraph/history 绝不跨目标复用
  3. 缓存内容脱敏：history 的 value 还原为 ${var} 占位，
     绝不落盘真实凭据（Secrets 边界）
  4. TTL=1h；schema version 负责结构失效，目标指纹负责语义隔离
══════════════════════════════════════════════════════════════════════
"""

import json
import hashlib
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("explore_cache")

CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache" / "explore"
TTL_SECONDS = 3600          # 1h（比 4h 保守，配合 stale guard）
ENABLED = True
CACHE_SCHEMA_VERSION = "s2-contract-v2"
GOAL_COMPLETE_REASON = "goal_complete"

_memory: dict[str, dict] = {}   # 内存级缓存 {key: {"data": ..., "created_at": ...}}


def _normalize_goal(goal: str) -> str:
    """稳定化已脱敏目标文本，避免空白/大小写差异制造重复条目。"""
    return " ".join((goal or "").casefold().split())


def goal_fingerprint(goal: str) -> str:
    """已脱敏目标的短 SHA-256 指纹；不把目标原文写入文件名。"""
    return hashlib.sha256(_normalize_goal(goal).encode("utf-8")).hexdigest()[:12]


def _cache_key(entry_url: str, auth_profile: str, goal: str) -> str:
    """缓存 key：站点 + 登录态 + 目标 + schema，避免跨目标/版本串缓存。"""
    origin = urlparse(entry_url).netloc.casefold()
    safe_origin = re.sub(r"[^a-z0-9._-]+", "_", origin).strip("_") or "unknown"
    safe_profile = re.sub(
        r"[^a-z0-9._-]+", "_", (auth_profile or "anonymous").casefold()
    ).strip("_") or "anonymous"
    return (
        f"{safe_origin}__{safe_profile}__{goal_fingerprint(goal)}__"
        f"{CACHE_SCHEMA_VERSION}"
    )


def is_cacheable_trace(data: dict | None) -> bool:
    """只有确定性证明目标完成的探索轨迹可复用。"""
    return bool(data) and data.get("termination_reason") == GOAL_COMPLETE_REASON


def _fresh(entry: dict) -> bool:
    return (time.time() - entry.get("created_at", 0)) < TTL_SECONDS


def load(entry_url: str, auth_profile: str, goal: str) -> dict | None:
    """内存 → 文件 → None。过期视为 miss。"""
    if not ENABLED:
        return None
    key = _cache_key(entry_url, auth_profile, goal)

    entry = _memory.get(key)
    if entry and _fresh(entry):
        return entry["data"]

    path = CACHE_DIR / f"{key}.json"
    if path.exists():
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if _fresh(entry):
            _memory[key] = entry
            return entry["data"]
    return None


def save(entry_url: str, auth_profile: str, goal: str, data: dict) -> None:
    """写入已证明完成的脱敏轨迹；其他终止原因一律拒绝缓存。"""
    if not ENABLED or not is_cacheable_trace(data):
        return
    key = _cache_key(entry_url, auth_profile, goal)
    entry = {"data": data, "created_at": time.time()}
    _memory[key] = entry
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{key}.json").write_text(
            json.dumps(entry, ensure_ascii=False), encoding="utf-8",
        )
    except Exception as exc:
        # S1：缓存写失败不再静默——诊断时才能区分"没保存"与"保存失败"
        logger.warning("[CACHE] save failed key=%s err=%r", key, exc)


def clear_all() -> None:
    """清空全部探索缓存（内存 + 磁盘）。

    S1：fresh 验证的标准手段——替代 rm json + 重启 backend
    （save 先写内存，rm 文件后内存仍命中，会造成"假 fresh"）。
    """
    _memory.clear()
    if CACHE_DIR.exists():
        for p in CACHE_DIR.glob("*.json"):
            try:
                p.unlink(missing_ok=True)
            except Exception as exc:
                logger.warning("[CACHE] clear failed %s err=%r", p.name, exc)


def invalidate(entry_url: str, auth_profile: str, goal: str) -> None:
    """删除缓存（stale guard 用：Preflight 发现缓存证据过期时）。"""
    key = _cache_key(entry_url, auth_profile, goal)
    _memory.pop(key, None)
    try:
        (CACHE_DIR / f"{key}.json").unlink(missing_ok=True)
    except Exception:
        pass
