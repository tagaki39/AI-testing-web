"""
══════════════════════════════════════════════════════════════════════
anti_patterns.py — 生成失败反模式库（GQ2：自愈重生的负例来源）
══════════════════════════════════════════════════════════════════════

【这个文件在项目中的位置】
  生成链路自愈循环的"记忆"：

    生成失败（缺动作/编造 ref/结构非法）
      → record(reason_code, summary) 记录反模式
      → 重生 prompt 注入 list_for(reason_code) 作为负例 few-shot
      → "带着教训重来"，而不是重新掷骰子

【设计（仿 corrections 模式，小而安全）】
  - 内存 + 文件两级（anti_patterns.json，gitignored），零 DB
  - 每 reason_code 保留最近 MAX_PER_CODE 条（去重：同 code+summary）
  - summary 只含行为摘要（action + target 简写 + 错误截断），
    不含 value 明文（敏感信息边界）

【学习路径】
  record（记录）→ list_for（负例查询）
══════════════════════════════════════════════════════════════════════
"""

import json
import time
from pathlib import Path

from dsl import DSLModel

STORE_FILE = Path(__file__).resolve().parents[1] / "anti_patterns.json"
MAX_PER_CODE = 5   # 每个原因码保留的负例上限（防上下文膨胀）


class AntiPattern(DSLModel):
    """一条生成失败反模式。"""
    reason_code: str    # missing_step / invalid_ref / invalid_structure / missing_wait_for
    summary: str        # 行为摘要（脱敏）
    created_at: str


_memory: list[AntiPattern] = []
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
                _memory.append(AntiPattern.model_validate(item))
        except Exception:
            pass   # 文件损坏 → 空库起步（不阻断主链路）


def _save() -> None:
    try:
        items = [a.model_dump() for a in _memory]
        STORE_FILE.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except Exception:
        pass


def record(reason_code: str, summary: str) -> None:
    """记录一条反模式：同 code+summary 去重；每 code 超出上限裁最旧。"""
    _load()
    summary = (summary or "").strip()
    if not summary:
        return
    if any(a.reason_code == reason_code and a.summary == summary for a in _memory):
        return
    _memory.append(AntiPattern(
        reason_code=reason_code,
        summary=summary[:300],
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    ))
    code_items = [a for a in _memory if a.reason_code == reason_code]
    while len(code_items) > MAX_PER_CODE:
        _memory.remove(code_items[0])   # 最旧在前
        code_items = code_items[1:]
    _save()


def list_for(reason_code: str) -> list[str]:
    """该原因码的反模式 summary 列表（最新在前，最多 MAX_PER_CODE 条）。"""
    _load()
    return [a.summary for a in reversed(_memory) if a.reason_code == reason_code]
