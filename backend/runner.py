"""Playwright 执行引擎：DSL 步骤 → 真实浏览器操作 → 步骤级证据。

核心循环（整个项目的灵魂）：
    for step in case.steps:
        try:  执行动作
        except: 记录失败，不阻断后续步骤
        每步截图作为证据

定位策略（对应 Playwright 官方推荐）：
    "button=登录"  → get_by_role(role, name)   ← 语义定位，最稳定
    "css=..."     → page.locator()             ← CSS 兜底
    纯文本         → get_by_text()              ← 文本定位
"""

import re
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

from dsl import DSLCase, DSLStep

ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "artifacts"

_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _substitute(value: str | None, variables: dict[str, str]) -> str | None:
    """把 ${email} 之类的变量替换成真实值。"""
    if not value:
        return value
    return _VAR_PATTERN.sub(lambda m: variables.get(m.group(1), m.group(0)), value)


def _resolve(page, target: str):
    """把 DSL target 解析成 Playwright locator（三种定位策略）。"""
    target = target.strip()

    # 策略 1: CSS 显式定位（兜底）
    if target.startswith("css="):
        return page.locator(target[4:])

    # 策略 2: 语义定位 "角色=名称"（主力，官方推荐）
    if "=" in target:
        role, _, name = target.partition("=")
        role, name = role.strip(), name.strip()
        if role in {"button", "link", "textbox", "heading", "checkbox", "radio", "option", "menuitem"}:
            return page.get_by_role(role, name=name)

    # 策略 3: 纯文本定位（无语义角色的元素）
    return page.get_by_text(target)


def _execute_step(page, step: DSLStep, variables: dict[str, str], step_dir: Path, index: int) -> dict:
    """执行单步，返回证据。失败不抛出，记录在结果里。"""
    evidence = {
        "step_index": index,
        "action": step.action,
        "target": step.target,
        "value": step.value,
        "status": "passed",
        "error": None,
        "url": None,
        "screenshot": None,
    }
    try:
        if step.action == "goto":
            url = _substitute(step.value, variables)
            page.goto(url, wait_until="domcontentloaded", timeout=step.timeout_ms)

        elif step.action == "click":
            locator = _resolve(page, step.target)
            locator.click(timeout=step.timeout_ms)

        elif step.action == "input":
            locator = _resolve(page, step.target)
            locator.fill(_substitute(step.value, variables) or "", timeout=step.timeout_ms)

        elif step.action == "wait_for":
            locator = _resolve(page, step.target)
            locator.wait_for(state="visible", timeout=step.timeout_ms)

        elif step.action == "assert_text":
            text = _substitute(step.value, variables) or ""
            if step.target:   # 在指定元素内验证
                locator = _resolve(page, step.target)
                expect(locator).to_contain_text(text)
            else:             # 验证整个页面包含文字
                expect(page.locator("body")).to_contain_text(text)

        # 每步截图作为证据
        shot = step_dir / f"step-{index:02d}.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
            evidence["screenshot"] = f"/artifacts/{step_dir.name}/step-{index:02d}.png"
        except Exception:
            pass

        evidence["url"] = page.url

    except Exception as exc:
        evidence["status"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        # 失败也截图，方便排查
        try:
            shot = step_dir / f"step-{index:02d}.png"
            page.screenshot(path=str(shot))
            evidence["screenshot"] = f"/artifacts/{step_dir.name}/step-{index:02d}.png"
        except Exception:
            pass

    return evidence


def execute_case(case: DSLCase, variables: dict[str, str] | None = None) -> dict:
    """执行整个用例，返回报告。"""
    variables = dict(variables or {})
    # 把 input_contract 里的默认值合并进来
    for contract in case.input_contract:
        key = contract.get("key") or contract.get("context_key")
        if key and contract.get("value") is not None:
            variables.setdefault(key, contract["value"])

    # 每轮执行独立目录
    run_id = len(list(ARTIFACTS_DIR.glob("run-*"))) + 1
    run_dir = ARTIFACTS_DIR / f"run-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    latest_url = None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.set_default_timeout(15000)

        for index, step in enumerate(case.steps, start=1):
            evidence = _execute_step(page, step, variables, run_dir, index)
            results.append(evidence)
            if evidence["url"]:
                latest_url = evidence["url"]

        browser.close()

    passed = sum(1 for r in results if r["status"] == "passed")
    return {
        "run_id": run_id,
        "case_name": case.name,
        "total_steps": len(results),
        "passed_steps": passed,
        "status": "passed" if passed == len(results) else "failed",
        "latest_url": latest_url,
        "results": results,
    }
