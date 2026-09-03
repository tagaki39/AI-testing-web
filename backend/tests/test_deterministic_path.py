"""
══════════════════════════════════════════════════════════════════════
test_deterministic_path.py — D1 确定性路径规划测试
══════════════════════════════════════════════════════════════════════

零依赖 plain-assert 脚本，直接运行：

    py backend/tests/test_deterministic_path.py

覆盖：
  1. 登录 + 加购族（无商品名"第一个商品"）：路径含 login 边 + 1 条
     add-to-cart（首遇），不含 Open Menu / 第二个商品边，终态为 cart obs
  2. goal 含明确商品名 → 只选归属匹配的 add-to-cart 边
  3. 含多阶段动词（"筛选"）→ None（LLM 兜底）
  4. 不可达终态 / 无候选边 → None
  5. 路径展开后 cursor 校验通过（_expand_plan_schema 无缝衔接）
══════════════════════════════════════════════════════════════════════
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/

from ai_agent import (   # noqa: E402
    _expand_plan_schema, deterministic_path_edges,
)

# ── 夹具：saucedemo 形态最小图 ────────────────────────────────────────────────
# obs1 登录页 → obs2 inventory（6 个商品 + Open Menu）→ obs3..obs8 各加购
# 自环/模态框？简化：每个商品 add-to-cart 边 → 各自 modal obs → View Cart
# 边 → cart obs。Open Menu 旁路：obs2 --Open Menu--> obs_menu。
OBS = [
    {"id": "obs1", "url": "https://saucedemo.com", "snapshot": "Username Password Login",
     "elements": [{"ref": "obs1:e1", "role": "textbox", "name": "Username"}]},
    {"id": "obs2", "url": "https://saucedemo.com/inventory.html",
     "snapshot": "Products Sauce Labs Backpack Sauce Labs Bike Light Open Menu",
     "elements": [
         {"ref": "obs2:e10", "role": "button", "name": "Add to cart",
          "scope_has_text": "Sauce Labs Backpack"},
         {"ref": "obs2:e20", "role": "button", "name": "Add to cart",
          "scope_has_text": "Sauce Labs Bike Light"},
         {"ref": "obs2:e30", "role": "button", "name": "Open Menu"},
     ]},
    {"id": "obs3", "url": "https://saucedemo.com/inventory.html",
     "snapshot": "Backpack added Continue Shopping View Cart",
     "elements": [{"ref": "obs3:e1", "role": "button", "name": "View Cart"}]},
    {"id": "obs4", "url": "https://saucedemo.com/inventory.html",
     "snapshot": "Bike Light added Continue Shopping View Cart",
     "elements": [{"ref": "obs4:e1", "role": "button", "name": "View Cart"}]},
    {"id": "obs_menu", "url": "https://saucedemo.com/inventory.html",
     "snapshot": "Open Menu All Items About",
     "elements": [{"ref": "obs_menu:e1", "role": "link", "name": "All Items"}]},
    {"id": "obs_cart", "url": "https://saucedemo.com/cart.html",
     "snapshot": "Your Cart Sauce Labs Backpack",
     "elements": [{"ref": "obs_cart:e1", "role": "link", "name": "Sauce Labs Backpack"}]},
]

EDGES = [
    {"from": "obs1", "action": "click", "target_ref": "obs1:e1",
     "target_name": "Login", "to": "obs2", "pre_actions": [
         {"action": "fill", "target_ref": "obs1:e2", "value": "${username}"},
         {"action": "fill", "target_ref": "obs1:e3", "value": "${password}"}]},
    {"from": "obs2", "action": "click", "target_ref": "obs2:e10",
     "target_name": "Add to cart", "to": "obs3"},   # Backpack
    {"from": "obs2", "action": "click", "target_ref": "obs2:e20",
     "target_name": "Add to cart", "to": "obs4"},   # Bike Light
    {"from": "obs2", "action": "click", "target_ref": "obs2:e30",
     "target_name": "Open Menu", "to": "obs_menu"},  # 旁路
    {"from": "obs3", "action": "click", "target_ref": "obs3:e1",
     "target_name": "View Cart", "to": "obs_cart"},
    {"from": "obs4", "action": "click", "target_ref": "obs4:e1",
     "target_name": "View Cart", "to": "obs_cart"},
    {"from": "obs_menu", "action": "click", "target_ref": "obs_menu:e1",
     "target_name": "All Items", "to": "obs2"},
]

GOAL_FIRST = ("打开 saucedemo.com，用 standard_user / secret_sauce 登录，"
              "把第一个商品加入购物车，然后进入购物车页面验证里面显示了该商品")
GOAL_BACKPACK = ("打开 saucedemo.com，用 standard_user / secret_sauce 登录，"
                 "把 Sauce Labs Backpack 加入购物车，然后进入购物车页面验证"
                 "里面有 Sauce Labs Backpack")


def test_first_product_path_minimal():
    """"第一个商品"：login + 1 条 add-to-cart（首遇 Backpack），不含旁路。"""
    path = deterministic_path_edges(GOAL_FIRST, EDGES, OBS)
    assert path is not None
    names = [e.get("target_name") for e in path]
    assert "Login" in names
    assert names.count("Add to cart") == 1          # 只加购一个
    assert "Open Menu" not in names                 # 旁路排除
    assert "All Items" not in names
    assert path[-1]["to"] == "obs_cart"             # 终态 = cart
    # 路径连续（cursor 可推进）
    assert path[0]["from"] == "obs1"
    for prev, cur in zip(path, path[1:]):
        assert prev["to"] == cur["from"]


def test_named_product_ownership():
    """goal 含明确商品名 → 只选归属 Backpack 的加购边。"""
    path = deterministic_path_edges(GOAL_BACKPACK, EDGES, OBS)
    assert path is not None
    adds = [e for e in path if e.get("target_name") == "Add to cart"]
    assert len(adds) == 1
    assert adds[0]["target_ref"] == "obs2:e10"      # Backpack 的边
    assert adds[0]["to"] == "obs3"                  # Backpack 的 modal


def test_multi_stage_goal_falls_back():
    """含多阶段动词（筛选）→ None（LLM 兜底，不强行确定性规划）。"""
    path = deterministic_path_edges(
        "打开 automationexercise.com 登录后按 Polo 品牌筛选把 Blue Top 加入购物车",
        EDGES, OBS)
    assert path is None


def test_unreachable_terminal_returns_none():
    """终态不可达（cart 边缺失）→ None。"""
    broken = [e for e in EDGES if e.get("target_name") != "View Cart"]
    path = deterministic_path_edges(GOAL_FIRST, broken, OBS)
    assert path is None


def test_no_candidate_edge_returns_none():
    """无候选动作边（goal 动作没探索到）→ None。"""
    path = deterministic_path_edges(GOAL_FIRST, [], OBS)
    assert path is None
    path = deterministic_path_edges("验证页面文字", EDGES, OBS)
    assert path is None


def test_path_expands_cleanly():
    """路径经 _expand_plan_schema 展开无 cursor 错位（与现有机制衔接）。"""
    path = deterministic_path_edges(GOAL_FIRST, EDGES, OBS)
    assert path is not None
    case_dict = {
        "name": "t",
        "steps": [],
        "transition_refs": [f"t{i + 1}" for i in range(len(path))],
        "assertions": [{"action": "assert_text", "value": "Sauce Labs Backpack"}],
    }
    expanded = _expand_plan_schema(
        case_dict, path, observations=OBS,
        entry_url="https://saucedemo.com")
    actions = [s["action"] for s in expanded["steps"]]
    assert actions[0] == "goto"
    assert "click" in actions and "fill" in actions   # pre_actions 恢复
    assert expanded["steps"][-1]["action"] == "assert_text"
    # 断言 observation_ref 自动赋为当前状态（cart）
    assert expanded["steps"][-1]["observation_ref"] == "obs_cart"


# ── 运行入口 ──────────────────────────────────────────────────────────────────

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
