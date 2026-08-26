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
  2. key = origin + auth_profile（不只 URL——同一 URL 可能对应
     未登录/已登录/不同状态）
  3. 缓存内容脱敏：history 的 value 还原为 ${var} 占位，
     绝不落盘真实凭据（Secrets 边界）
  4. TTL=1h；ENABLED 开关；stale 由 Preflight 兜底（Speed C）
══════════════════════════════════════════════════════════════════════
"""

import json
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger("explore_cache")

CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache" / "explore"
TTL_SECONDS = 3600          # 1h（比 4h 保守，配合 stale guard）
ENABLED = True

_memory: dict[str, dict] = {}   # 内存级缓存 {key: {"data": ..., "created_at": ...}}


def _cache_key(entry_url: str, auth_profile: str) -> str:
    """缓存 key：origin + auth_profile（防跨站点/跨登录态串缓存）。"""
    origin = urlparse(entry_url).netloc
    return f"{origin}__{auth_profile}"


def _fresh(entry: dict) -> bool:
    return (time.time() - entry.get("created_at", 0)) < TTL_SECONDS


def load(entry_url: str, auth_profile: str) -> dict | None:
    """内存 → 文件 → None。过期视为 miss。"""
    if not ENABLED:
        return None
    key = _cache_key(entry_url, auth_profile)

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


def save(entry_url: str, auth_profile: str, data: dict) -> None:
    """写内存 + 文件。data 必须已脱敏（见 ai_agent._sanitize_for_cache）。"""
    if not ENABLED:
        return
    key = _cache_key(entry_url, auth_profile)
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


def invalidate(entry_url: str, auth_profile: str) -> None:
    """删除缓存（stale guard 用：Preflight 发现缓存证据过期时）。"""
    key = _cache_key(entry_url, auth_profile)
    _memory.pop(key, None)
    try:
        (CACHE_DIR / f"{key}.json").unlink(missing_ok=True)
    except Exception:
        pass
