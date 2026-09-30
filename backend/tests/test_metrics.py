"""metrics.py 对成功与失败日志混合输入的回归测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metrics import _explore_metrics, _gen_timing_metrics  # noqa: E402


def test_null_or_missing_timings_are_skipped() -> None:
    result = _gen_timing_metrics([
        {"type": "generate", "timings": None},
        {"type": "generate"},
        {"type": "generate", "timings": {"explore_ms": 12}},
        {"type": "generate", "timings": {"explore_ms": "invalid"}},
    ])
    values, p50, p95 = result["探索"]
    assert values == [12]
    assert p50 == 12
    assert p95 == 12


def test_explore_metrics_report_structured_termination_reasons() -> None:
    result = _explore_metrics([
        {"explore": {"done": True, "termination_reason": "goal_complete"}},
        {"explore": {"done": True, "termination_reason": "auth_rejected"}},
        {"explore": {"done": False}},  # 历史记录兼容
    ])
    assert result["terminations"] == {
        "goal_complete": 1,
        "auth_rejected": 1,
        "legacy_unknown": 1,
    }


def main() -> int:
    tests = [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
