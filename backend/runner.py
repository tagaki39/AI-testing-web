"""Playwright 执行引擎：DSL 步骤 → 真实浏览器操作 → 步骤级证据。

核心循环（整个项目的灵魂）：
    for step in case.steps:
        try:  执行动作
        except: 记录失败，不阻断后续步骤
        每步截图作为证据

定位器解析采用"三分法"（Playwright 官方推荐的心智模型）：
    count == 0  → LocatorNotFoundError   未找到，报错
    count == 1  → 直接使用               唯一，继续
    count > 1   → LocatorAmbiguousError  歧义，绝不自动选第一个（可能点错元素）
"""

import re
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

from dsl import DSLCase, DSLStep

ARTIFACTS_DIR = Path(__file__).resolve().parents[1] / "artifacts"

_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 语义定位支持的已知角色（白名单，防止把任意文本当角色解析）
_KNOWN_ROLES = {
    "button", "link", "textbox", "heading", "checkbox", "radio",
    "option", "menuitem", "listitem", "combobox", "tab", "searchbox",
}


# ── 异常类型（定位失败时的三种明确语义）──────────────────────────────────────────

class LocatorNotFoundError(Exception):
    """0 个匹配：元素不存在或未渲染。"""


class LocatorAmbiguousError(Exception):
    """2+ 个匹配：定位不唯一，必须通过作用域/更精确的 target 消歧。"""


# ── target 解析（字符串 / 结构化 → 统一数据结构）────────────────────────────────

@dataclass
class ParsedTarget:
    role: str | None = None
    name: str | None = None
    text: str | None = None
    test_id: str | None = None
    css: str | None = None


def _parse_target(target: str | dict | None) -> ParsedTarget | None:
    """把 DSL target 解析成 ParsedTarget，支持字符串和结构化两种格式。"""
    if target is None:
        return None

    if isinstance(target, dict):
        return ParsedTarget(
            role=target.get("role"),
            name=target.get("name"),
            text=target.get("text"),
            test_id=target.get("test_id"),
            css=target.get("css"),
        )

    t = target.strip()
    if t.startswith("css="):
        return ParsedTarget(css=t[4:].strip())
    if t.startswith(("test_id=", "testid=")):
        return ParsedTarget(test_id=t.split("=", 1)[1].strip())
    if "=" in t:
        role, _, name = t.partition("=")
        role = role.strip()
        if role in _KNOWN_ROLES:          # 只有已知角色才走语义定位
            return ParsedTarget(role=role, name=name.strip())
    return ParsedTarget(text=t)           # 兜底：当作纯文本定位


def _build_locators(container, t: ParsedTarget) -> list[tuple[str, object]]:
    """在 container（page 或 locator）内构建候选定位器，按稳定性排序。"""
    candidates: list[tuple[str, object]] = []
    if t.test_id:
        candidates.append(("test_id", container.get_by_test_id(t.test_id)))
    if t.role and t.name:
        # 用模糊匹配（exact=False）：真实页面常见 icon 前缀空格、
        # CSS text-transform 大小写等，accessible name 与可见文本常不一致。
        # 歧义仍会被三分法拦截（count > 1 → AmbiguousError）。
        candidates.append(("role", container.get_by_role(t.role, name=t.name)))
    if t.text:
        candidates.append(("text", container.get_by_text(t.text)))
    if t.css:
        candidates.append(("css", container.locator(t.css)))
    return candidates


# ── 作用域解析（消歧：先锁定容器，再在容器内找目标）──────────────────────────────

def _resolve_scope_containers(page, scope) -> list[object]:
    """返回候选"容器"列表；无作用域时返回 [page]（全页面查找）。"""
    if scope is None:
        return [page]

    if isinstance(scope, dict):
        containers = []
        if scope.get("role"):
            c = page.get_by_role(scope["role"])
            if scope.get("has_text"):
                c = c.filter(has_text=scope["has_text"])   # 容器必须包含该文本
            containers.append(c)
        if scope.get("test_id"):
            c = page.get_by_test_id(scope["test_id"])
            if scope.get("has_text"):
                c = c.filter(has_text=scope["has_text"])
            containers.append(c)
        if not containers:
            raise LocatorNotFoundError(f"scope 无效: {scope}")
        return containers

    # 字符串 scope（兼容格式 "inside Blue Top"）：
    # 找到包含该文本的元素，向上爬最多 3 层，每层尝试匹配目标。
    # ⚠️ 这是兜底方案，依赖 DOM 层级；优先使用结构化 scope（role + has_text）。
    base = page.get_by_text(scope).first
    containers = [base]
    current = base
    for _ in range(3):
        current = current.locator("xpath=..")
        containers.append(current)
    return containers


# ── 定位器解析（三分法入口）──────────────────────────────────────────────────────

def _resolve_locator(page, target, scope=None, *, allow_lazy: bool = False, timeout_ms: int = 15000):
    """target + 可选 scope → 唯一 Playwright locator。

    0 个匹配 → LocatorNotFoundError
    2+ 个匹配 → LocatorAmbiguousError（提示用 scope 消歧）

    allow_lazy=True（wait_for 步骤用）：元素尚未渲染时等待其出现
    （Playwright 的 wait_for 自带轮询），而不是立即判 NotFound。
    """
    t = _parse_target(target)
    if t is None or (t.role is None and t.text is None and t.test_id is None and t.css is None):
        raise LocatorNotFoundError(f"target 无法解析: {target!r}")

    containers = _resolve_scope_containers(page, scope)

    for container in containers:
        for strategy, locator in _build_locators(container, t):
            try:
                count = locator.count()
            except Exception as exc:
                raise LocatorNotFoundError(f"定位失败 ({strategy}): {exc}") from exc

            if count == 1:
                return locator
            if count > 1:
                hint = f"，请用 scope 消歧（如 scope={'{'}\"role\":\"listitem\",\"has_text\":\"...\"{'}'}）" if scope is None else ""
                raise LocatorAmbiguousError(
                    f"{strategy} 定位器匹配到 {count} 个元素: {target}{hint}"
                )
            # count == 0：wait_for 语义下，元素可能正在渲染 → 等待出现
            if allow_lazy:
                try:
                    locator.wait_for(state="visible", timeout=timeout_ms)
                    return locator
                except Exception:
                    continue   # 超时 → 尝试下一个策略（降级）

    raise LocatorNotFoundError(f"所有定位策略均未命中: {target}")


# ── 变量替换 ────────────────────────────────────────────────────────────────────

def _substitute(value: str | None, variables: dict[str, str]) -> str | None:
    """把 ${email} 之类的变量替换成真实值；缺失变量明确报错，不静默留下占位符。"""
    if not value:
        return value

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in variables:
            raise KeyError(f"运行时变量缺失: ${{{key}}}")
        return variables[key]

    return _VAR_PATTERN.sub(replace, value)


# ── 单步执行 ────────────────────────────────────────────────────────────────────

def _execute_step(page, step: DSLStep, variables: dict[str, str], step_dir: Path, index: int) -> dict:
    """执行单步，返回证据。失败不抛出，记录在结果里。"""
    evidence = {
        "step_index": index,
        "action": step.action,
        "target": step.target if isinstance(step.target, str) else (step.target.model_dump() if step.target else None),
        "scope": step.scope if isinstance(step.scope, str) else (step.scope.model_dump() if step.scope else None),
        "value": step.value,
        "status": "passed",
        "error": None,
        "url": None,
        "screenshot": None,
    }
    try:
        if step.action == "goto":
            page.goto(_substitute(step.value, variables) or "", wait_until="domcontentloaded", timeout=step.timeout_ms)

        elif step.action == "assert_text" and not step.target:
            # 无 target 的断言 → 验证整个页面包含文本
            text = _substitute(step.value, variables) or ""
            expect(page.locator("body")).to_contain_text(text, timeout=step.timeout_ms)

        else:
            # wait_for 的语义是"等待元素出现"（元素可能还在渲染），
            # 其他动作要求元素已存在。
            locator = _resolve_locator(
                page, step.target, step.scope,
                allow_lazy=(step.action == "wait_for"),
                timeout_ms=step.timeout_ms,
            )

            if step.action == "click":
                locator.click(timeout=step.timeout_ms)
            elif step.action == "input":
                locator.fill(_substitute(step.value, variables) or "", timeout=step.timeout_ms)
            elif step.action == "wait_for":
                locator.wait_for(state="visible", timeout=step.timeout_ms)
            elif step.action == "assert_text":
                text = _substitute(step.value, variables) or ""
                expect(locator).to_contain_text(text, timeout=step.timeout_ms)

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


# ── 执行入口 ────────────────────────────────────────────────────────────────────

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
