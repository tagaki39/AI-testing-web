"""SSE 内存 run 的保留与容量边界测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as app_main  # noqa: E402


def _state(status: str, created: float, finished: float | None = None) -> dict:
    return {
        "status": status,
        "created_at": created,
        "finished_at": finished,
    }


def test_expired_terminal_runs_are_removed_but_running_runs_survive() -> None:
    original = dict(app_main._RUNS)
    try:
        app_main._RUNS.clear()
        app_main._RUNS.update({
            "old-done": _state("done", 1, 10),
            "recent-done": _state("done", 90, 95),
            "old-running": _state("running", 1),
        })
        old_retention = app_main._RUN_RETENTION_SECONDS
        app_main._RUN_RETENTION_SECONDS = 20
        try:
            app_main._prune_runs_locked(now=100)
        finally:
            app_main._RUN_RETENTION_SECONDS = old_retention
        assert "old-done" not in app_main._RUNS
        assert "recent-done" in app_main._RUNS
        assert "old-running" in app_main._RUNS
    finally:
        app_main._RUNS.clear()
        app_main._RUNS.update(original)


def test_capacity_evicts_oldest_terminal_first() -> None:
    original = dict(app_main._RUNS)
    try:
        app_main._RUNS.clear()
        app_main._RUNS.update({
            "running": _state("running", 1),
            "old": _state("done", 2, 90),
            "new": _state("error", 3, 99),
        })
        old_limit = app_main._MAX_RETAINED_RUNS
        app_main._MAX_RETAINED_RUNS = 2
        try:
            app_main._prune_runs_locked(now=100)
        finally:
            app_main._MAX_RETAINED_RUNS = old_limit
        assert set(app_main._RUNS) == {"running", "new"}
    finally:
        app_main._RUNS.clear()
        app_main._RUNS.update(original)


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
