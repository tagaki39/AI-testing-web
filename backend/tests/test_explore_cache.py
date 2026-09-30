"""S2-P0：目标相关探索轨迹缓存隔离与写入门槛。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import explore_cache as cache  # noqa: E402


def _complete_trace(marker: str) -> dict:
    return {
        "termination_reason": "goal_complete",
        "done": True,
        "history": [{"action": "click", "target_ref": marker}],
    }


def _with_temp_cache(fn) -> None:
    old_dir = cache.CACHE_DIR
    old_enabled = cache.ENABLED
    old_memory = dict(cache._memory)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache.CACHE_DIR = Path(tmpdir)
            cache.ENABLED = True
            cache._memory.clear()
            fn()
    finally:
        cache.CACHE_DIR = old_dir
        cache.ENABLED = old_enabled
        cache._memory.clear()
        cache._memory.update(old_memory)


def test_same_site_and_profile_are_isolated_by_goal() -> None:
    def scenario() -> None:
        url = "https://test.example.com/login"
        cache.save(url, "authenticated", "只登录", _complete_trace("login"))
        assert cache.load(url, "authenticated", "只登录") is not None
        assert cache.load(url, "authenticated", "登录后生成图片") is None
    _with_temp_cache(scenario)


def test_goal_normalization_is_stable() -> None:
    assert cache.goal_fingerprint("  Login   Then Generate ") == \
        cache.goal_fingerprint("login then generate")


def test_cache_key_is_filename_safe_and_versioned() -> None:
    key = cache._cache_key(
        "http://localhost:9000/path", "Authenticated User", "登录")
    assert ":" not in key
    assert key.endswith(f"__{cache.CACHE_SCHEMA_VERSION}")


def test_non_complete_termination_reasons_are_not_saved() -> None:
    def scenario() -> None:
        url = "https://test.example.com"
        for reason in (
            "model_finish", "auth_rejected", "error_page",
            "observation_limit", "budget_exhausted",
        ):
            cache.save(url, "anonymous", reason, {
                "done": True, "termination_reason": reason,
            })
            assert cache.load(url, "anonymous", reason) is None
        assert list(cache.CACHE_DIR.glob("*.json")) == []
    _with_temp_cache(scenario)


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
