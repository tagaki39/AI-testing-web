"""
══════════════════════════════════════════════════════════════════════
ai_agent.py — AI 生成 DSL（自然语言 → 结构化测试用例）
══════════════════════════════════════════════════════════════════════

【这个文件在项目中的位置】
  数据流第二站：
    用户输入自然语言 →【这里：调 DeepSeek API 生成 DSL】→ dsl.py 校验 → 执行

【核心思想（面试重点）】
  1. AI 只负责"生成"，不负责"执行"
     - 执行是确定性的 Playwright 代码，AI 生成完就退场
     - 这保证测试结果可复现、可审计（AI 不可信）
  2. 生成结果必须过 Pydantic 校验（dsl.py 的 validate_case）
     - AI 输出任何非法内容，在进入执行器之前就被拒绝
  3. Prompt 工程约束输出格式
     - 白名单 action + JSON 格式约束 → 降低 AI 自由发挥/幻觉的概率
     - 低温度（temperature=0.2）→ 输出稳定，不"创作"

【HTTP 调用为什么用 urllib 而不是 requests/httpx】
  标准库，零依赖——演示项目尽量少装包。真实项目会用 httpx/requests。

【学习路径】
  SYSTEM_PROMPT（约束规则）→ _call_llm（调 API）
  → _extract_json（容错解析）→ generate_dsl（对外入口）
══════════════════════════════════════════════════════════════════════
"""

import json
import os
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from dsl import DSLCase, Locator, Scope, validate_case
from explore_flow import explore
from runner import _parse_target

# ── 配置（环境变量）───────────────────────────────────────────────────────────
# os.getenv("名字", 默认值)：读环境变量，没设置就用默认值。
# .env 文件的值由 main.py 在启动时灌入 os.environ（见 main.py 顶部）。

API_KEY = os.getenv("AI_API_KEY", "")
BASE_URL = os.getenv("AI_BASE_URL", "https://api.deepseek.com/v1")
MODEL = os.getenv("AI_MODEL", "deepseek-chat")

# ── Prompt（约束 LLM 输出符合格式的 JSON）──────────────────────────────────────
# 这段提示词是"AI 生成质量的第一个保障"：
#   - 给出完整的 JSON 示例（few-shot 示范）
#   - 白名单 action（告诉它只能做这些）
#   - 明确规则（语义定位、变量引用、只输出 JSON）
# 没有这段约束，AI 会自由发挥，输出各种奇怪格式。

SYSTEM_PROMPT = """你是一个 Web UI 自动化测试的 DSL 生成器。
根据用户描述的自然语言测试需求，输出一个 JSON 对象，格式如下：

{
  "name": "用例名称",
  "description": "用例描述",
  "base_url": "被测网站入口URL",
  "input_contract": [{"key": "变量名", "value": "默认值"}],
  "steps": [
    {"action": "goto", "value": "https://xxx.com"},
    {"action": "fill", "target": {"role": "textbox", "name": "用户名"}, "value": "${变量名}"},
    {"action": "click", "target": {"role": "button", "name": "登录"}},
    {"action": "wait_for", "target": {"role": "heading", "name": "首页"}},
    {"action": "assert_visible", "target": {"text": "购物车"}},
    {"action": "assert_url", "value": "/inventory.html"}
  ]
}

规则：
1. action 只能是: goto, click, fill, select, check, wait_for, assert_visible, assert_text, assert_url
2. target 使用结构化定位（多字段组合，按优先级）：
   - 语义定位: {"role": "button", "name": "登录"}
   - 文本定位: {"text": "Products"}（快照中 'text: xxx' 的标题必须用 text，禁止 role=heading）
   - 测试 id:  {"test_id": "login-button"}
   - CSS 兜底: {"css": ".btn"}
3. 同名元素消歧用 scope（先定位容器再找目标）：
   {"action": "click", "scope": {"has_text": "Blue Top"}, "target": {"role": "button", "name": "Add to cart"}}
4. 用户提供的测试数据（如账号密码）用 ${var} 占位，并加入 input_contract 给出默认值
5. assert_text 用于验证页面/元素包含某段文字；assert_visible 验证元素可见；assert_url 验证当前 URL 包含片段
6. 只输出 JSON，不要输出任何解释或代码块标记"""


# ── LLM 调用（标准库实现，无外部依赖）──────────────────────────────────────────

def _call_llm(user_prompt: str, system_prompt: str | None = None) -> str:
    """调用 DeepSeek chat completions API，返回文本内容。

    这是最原始的 HTTP POST 请求，拆解每一步：
      1. 构造 payload（JSON 请求体）：model + messages + temperature
      2. urllib.request.Request：封装 URL、请求体、请求头
      3. urlopen()：真正发出网络请求（timeout=60 秒上限）
      4. 解析响应 JSON，取 choices[0].message.content（LLM 的回答文本）

    请求体格式是 OpenAI 兼容规范（DeepSeek 兼容它）：
      messages = [system（角色设定）] + [user（用户输入）]

    参数 system_prompt：可覆盖默认 SYSTEM_PROMPT（阶段 1 提取 URL 时用专用 prompt）
    """
    if not API_KEY:
        raise RuntimeError("未配置 AI_API_KEY（环境变量或 .env 文件）")

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,   # 低温度 → 输出更稳定，不容易乱编（生成测试要确定性）
    }
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",            # DeepSeek 的 OpenAI 兼容端点
        data=json.dumps(payload).encode("utf-8"),  # dict → JSON 字符串 → 字节
        headers={
            "Content-Type": "application/json",    # 告诉服务器：请求体是 JSON
            "Authorization": f"Bearer {API_KEY}",  # 认证：Bearer token 标准格式
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


# ── 阶段 1：从用户需求中解析入口 URL（代码优先 + LLM fallback）────────────────

# 域名正则：匹配 "saucedemo.com" / "www.saucedemo.com" / "https://saucedemo.com/login"
# 不补 www（LLM 可能错补 www 而真实站点没有）；补 https 由代码统一处理
_URL_RE = re.compile(
    r'(?:(?:https?://)?(?:www\.)?)'
    r'([a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+)'
    r'(?:/[^\s]*)?'
)

# 邮箱正则：先剥掉邮箱，防止 "admin@example.com" 里的域名被误抓
_EMAIL_RE = re.compile(r'\S+@[a-zA-Z0-9.-]+')

# 站点别名表（alias resolver）：常见站点描述 → 真实 URL。
# 这是"resolution"的可靠来源——人为维护，零幻觉。
SITE_ALIASES: dict[str, str] = {
    "automation exercise": "https://automationexercise.com",
    "saucedemo": "https://www.saucedemo.com",
    "example": "https://example.com",
}


def _resolve_by_alias(prompt: str) -> str | None:
    """别名解析：在用户输入中查找已知站点描述（大小写/空格不敏感）。

    例："测试 automation exercise 网站的登录" → 命中 "automation exercise"。
    别名表人为维护、行为确定——比让 LLM 猜域名可靠得多。
    """
    normalized = prompt.lower().strip()
    for alias, url in SITE_ALIASES.items():
        if alias.lower() in normalized:
            return url
    return None


def _resolve_url_by_regex(prompt: str) -> str | None:
    """正则提取入口 URL（代码优先，零成本零幻觉）。

    处理流程：
      1. 剥掉邮箱（防止误抓域名）
      2. 正则匹配域名（支持 裸域名 / www. / http(s):// 三种写法）
      3. 补 https:// 前缀（不补 www）
      4. 校验 host 合法性（必须含点号，防止抓到奇怪字符串）

    返回 None 表示正则未命中 → 交给 LLM fallback。
    """
    cleaned = _EMAIL_RE.sub(" ", prompt)
    match = _URL_RE.search(cleaned)
    if not match:
        return None

    value = match.group(0).strip()
    if not value.startswith(("http://", "https://")):
        value = "https://" + value

    try:
        parsed = urlparse(value)
        if not parsed.netloc or "." not in parsed.netloc:
            return None
    except Exception:
        return None
    return value


EXTRACT_URL_PROMPT = """你是站点识别器。
判断用户的测试需求中是否提到了一个明确的被测网站名称。

规则：
1. 只从用户输入中识别站点名称，不要编造或联想域名
2. 没有明确站点名称时，返回 {"site_name": null}
3. 只输出 JSON，格式：{"site_name": "automation exercise"} 或 {"site_name": null}"""


def _extract_site_name_llm(user_prompt: str) -> str | None:
    """LLM 只做"站点名称识别"，绝不输出 URL（防止幻觉域名）。

    输出的是描述性名称（如 "automation exercise"），
    由代码查 SITE_ALIASES 得到真实 URL——Resolution 由代码保证。
    """
    try:
        text = _call_llm(user_prompt, system_prompt=EXTRACT_URL_PROMPT)
        data = _extract_json(text)
        name = data.get("site_name")
        return name if isinstance(name, str) and name.strip() else None
    except Exception:
        return None   # 识别失败 → 降级为无快照生成


def _resolve_entry_url(user_prompt: str) -> str | None:
    """入口 URL 解析链（Extraction 自动化，Resolution 不 hallucinate）：

      ① 正则提取 URL/域名（saucedemo.com → https://saucedemo.com）
      ② 别名表解析（"automation exercise 网站" → 人为维护的 URL）
      ③ LLM 只识别站点名称（不输出 URL），再查别名表
      ④ 全部失败 → None（降级无快照生成，绝不猜域名）

    原则：LLM 可以做"识别"，但"从名称到 URL 的映射"永远由代码决定。
    """
    # ① 正则提取（零成本、零幻觉）
    url = _resolve_url_by_regex(user_prompt)
    if url:
        return url

    # ② 别名表直接匹配（零成本、零幻觉）
    url = _resolve_by_alias(user_prompt)
    if url:
        return url

    # ③ LLM 识别站点名称 → 代码查别名表（LLM 不创造 URL）
    site_name = _extract_site_name_llm(user_prompt)
    if site_name:
        url = _resolve_by_alias(site_name)
        if url:
            return url

    # ④ 无法可靠解析 → 不猜，返回 None 降级
    return None


def _extract_json(text: str) -> dict:
    """从 LLM 输出中提取 JSON（容错解析）。

    为什么需要容错？AI 不守规矩：
      - 说好只输出 JSON，却包了 ```json ... ``` 代码块
      - 输出前/后附带解释文字
    解法：用正则找第一个 { 到最后一个 }，把中间的 JSON 提取出来。

    re.DOTALL 标志：让 . 也能匹配换行符（JSON 是多行的）。
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)   # 取第一个 { 到最后一个 }
    if not match:
        raise ValueError(f"LLM 输出中找不到 JSON: {text[:200]}")
    return json.loads(match.group(0))


# ── Preflight 校验（结构化 Issue + patch 修复）─────────────────────────────────
# 这是"AI 生成质量闭环"的核心：
#   AI 生成 DSL → 用真实页面快照验证每个 target → 不存在的/歧义的
#   → 输出【结构化 Issue】（step_index + 类型）→ LLM 只返回【patch】
#   → 程序本地应用 patch → 再验证（最多 2 轮）
#
# 为什么 patch 而不是整份重生（设计评审建议）：
#   整份重生时模型会顺手改其他步骤（改 base_url、删步骤、更改变量名）；
#   patch 只修出问题的步骤，程序保证其余步骤分毫不动。

@dataclass
class PreflightIssue:
    """结构化定位问题（机器可理解，供 patch 修复精确定位）。"""
    step_index: int        # 出问题的步骤（1-based，与执行报告一致）
    type: str              # "LOCATOR_NOT_FOUND" / "AMBIGUOUS_LOCATOR"
    target: dict           # 原始 target（结构化）
    detail: str            # 人类可读说明


class RepairItem(BaseModel):
    """单步修复补丁：替换该步骤的 target / scope。"""
    step_index: int = Field(ge=1)
    target: Locator | None = None
    scope: Scope | None = None


class RepairPatch(BaseModel):
    """修复补丁集：只修出问题的步骤，其余步骤不动。"""
    repairs: list[RepairItem] = Field(default_factory=list)


def _snapshot_check(snapshot: str, role: str | None, name: str) -> tuple[bool, int]:
    """在 ARIA 快照文本中查找 role+name 或纯文本，返回 (是否找到, 出现次数)。

    快照格式（aria_snapshot 输出）：
      - button "Add to cart"        ← role+name 格式
      - text: Your Cart             ← 纯文本格式
    匹配用"包含"而非"精确"：accessible name 可能有前缀空格/大小写差异。
    """
    if role:
        # 匹配 role "xxx" 形式，取引号内的 name 列表
        pattern = re.compile(rf'\b{re.escape(role)}\s+"([^"]*)"')
        matched = [m for m in pattern.findall(snapshot) if name.lower() in m.lower()]
        return bool(matched), len(matched)
    # 纯文本：直接在快照里找
    return name.lower() in snapshot.lower(), snapshot.lower().count(name.lower())


def _target_to_dict(t) -> dict:
    """把 target（str / Locator 模型 / dict）统一转成 dict。"""
    if hasattr(t, "model_dump"):
        return t.model_dump()
    if isinstance(t, dict):
        return t
    return {"text": str(t)}


def _preflight_targets(case: DSLCase, snapshot: str) -> list[PreflightIssue]:
    """校验 DSL 每个 target 是否能在页面快照中命中，返回结构化问题列表。

    三种结果：
      命中 1 次      → 通过
      不存在          → LOCATOR_NOT_FOUND（必须修复）
      命中 2+ 次      → AMBIGUOUS_LOCATOR（需 scope 消歧；已带 scope 视为已消歧）

    css=/test_id= 无法用快照文本验证（DOM 属性不是语义）→ 跳过。
    """
    issues: list[PreflightIssue] = []
    for index, step in enumerate(case.steps, start=1):
        t = step.target
        if not t:
            continue   # goto / 无 target 断言，无需验证

        parsed = _parse_target(t)   # 复用执行器的解析（单一实现）
        if parsed is None:
            continue
        role, name = parsed.role, parsed.name
        if not name:
            name = parsed.text
        if not name:
            continue   # 纯 css/test_id，无法验证

        found, count = _snapshot_check(snapshot, role, name)
        if not found:
            issues.append(PreflightIssue(
                step_index=index,
                type="LOCATOR_NOT_FOUND",
                target=_target_to_dict(t),
                detail=f"步骤 {index}: target 在页面快照中不存在",
            ))
        elif count > 1 and role and not step.scope:
            issues.append(PreflightIssue(
                step_index=index,
                type="AMBIGUOUS_LOCATOR",
                target=_target_to_dict(t),
                detail=f"步骤 {index}: 页面存在 {count} 个同名 {role}，需 scope 消歧",
            ))

    return issues


def _repair_patch_with_llm(snapshot: str, issues: list[PreflightIssue]) -> RepairPatch:
    """把结构化 Issue 反馈给 LLM，返回 patch（只修出问题的步骤）。"""
    issue_lines = "\n".join(
        f"- 步骤 {i.step_index} [{i.type}]: {i.detail} target={i.target}"
        for i in issues
    )
    repair_prompt = (
        f"页面真实结构（ARIA snapshot）:\n{snapshot}\n\n"
        f"生成的 DSL 存在以下定位问题（共 {len(issues)} 处）：\n{issue_lines}\n\n"
        "请为每个问题步骤输出修复 patch。只输出 JSON：\n"
        '{"repairs": [{"step_index": 5, "target": {"role": "button", "name": "Add to cart"}, '
        '"scope": {"has_text": "Blue Top"}}]}\n\n'
        "规则：\n"
        "1. step_index 必须是问题步骤号（1-based）\n"
        "2. 同名元素歧义（AMBIGUOUS_LOCATOR）：scope.has_text 必须用页面中真实可见的文本"
        "（如商品名称，不能是 CSS 类名）\n"
        "3. 元素不存在（LOCATOR_NOT_FOUND）：改用快照中真实存在的元素；"
        "快照中以 'text: xxx' 形式出现的标题必须用 {\"text\": \"xxx\"}，禁止 role=heading\n"
        "4. scope 不需要修复时给 null\n"
        "5. 只输出 JSON"
    )
    raw_text = _call_llm(repair_prompt)
    return RepairPatch.model_validate(_extract_json(raw_text))


def _apply_patch(case: DSLCase, patch: RepairPatch) -> int:
    """程序本地应用 patch：只替换 patch 中指定的步骤，其余分毫不动。"""
    applied = 0
    for rep in patch.repairs:
        idx = rep.step_index - 1
        if not (0 <= idx < len(case.steps)):
            continue
        step = case.steps[idx]
        if rep.target is not None:
            step.target = rep.target
        if rep.scope is not None:
            step.scope = rep.scope
        applied += 1
    return applied


# ── 多页面快照文本（探索结果 → Planner 可读上下文）──────────────────────────────

def _pages_to_text(pages: list[dict]) -> str:
    """把探索到的多页面快照合并成一份可读文本（每页分段标记）。"""
    sections = []
    for i, page in enumerate(pages, start=1):
        title = page.get("title") or ""
        sections.append(
            f"[页面 {i}] {page['url']}"
            + (f"（标题: {title}）" if title else "")
            + f"\n{page['snapshot']}"
        )
    return "\n\n".join(sections)


# ── 对外接口 ───────────────────────────────────────────────────────────────────

def generate_dsl(user_prompt: str) -> tuple[DSLCase, dict]:
    """对外入口：自然语言需求 → 校验通过的 DSLCase + 生成元信息。

    四阶段流水线（bounded exploration 方案）：
      阶段 1: _extract_entry_url() → LLM 提取入口 URL
      阶段 2: explore() → bounded 探索：跟随用户目标探索多页面，
              每个页面抓 ARIA 快照 + 记录操作路径
      阶段 3: _call_llm() → 多页面结构 + 探索路径注入 prompt → Planner 生成 DSL
      阶段 4: validate_case() + Preflight 校验（多页面验证，失败自动重生）

    降级策略（探索失败不中断主链路，与原项目保护原则一致）：
      - URL 提取失败  → 无快照直接生成
      - 探索失败/空   → 降级为单页快照 / 无快照直接生成
      - Preflight 重生失败 → 保留原 case

    返回 (case, meta)：meta 记录探索与校验信息，供前端展示。

    第 4 步是"最后一道防线"：AI 就算输出了合法 JSON，
    只要 action 不在白名单、缺字段、类型不对，照样拒绝。
    校验失败会抛异常，由 main.py 捕获后返回 400 给前端。
    """
    # ── 阶段 1：解析入口 URL（正则优先，描述性输入 LLM fallback）───
    entry_url = _resolve_entry_url(user_prompt)

    # ── 阶段 2：bounded exploration（目标驱动的多页面探索）──────────
    explore_result = None
    pages = []
    if entry_url:
        try:
            explore_result = explore(user_prompt, entry_url, _call_llm)
            pages = explore_result.get("pages", [])
        except Exception:
            explore_result = None   # 探索异常 → 降级无快照生成

    # ── 阶段 3：组装 prompt（多页面结构 + 探索路径）→ Planner 生成 ──
    multi_snapshot = _pages_to_text(pages) if pages else None
    if multi_snapshot:
        # 把探索路径也注入：Planner 能看到"怎么走到每个页面"
        path_lines = [
            f"- {h.get('action')} {h.get('target')} {h.get('value') or ''} @ {h.get('url')}"
            for h in (explore_result or {}).get("history", [])
        ]
        grounded_prompt = (
            f"目标页面入口: {entry_url}\n\n"
            f"探索路径（已按此流程访问过以下页面）:\n"
            + "\n".join(path_lines)
            + "\n\n各页面真实结构（ARIA snapshot）：\n\n"
            + multi_snapshot
            + "\n\n用户测试需求: " + user_prompt
            + "\n\n规则："
            "1. 用户提供的测试数据（如账号密码）用 ${var} 占位，并加入 input_contract 给出默认值；"
            "2. 快照中以 'text: xxx' 形式出现的标题（span/div 无 heading 语义）"
            '必须用 {"text": "xxx"} 定位，禁止用 {"role": "heading"}。'
        )
    else:
        grounded_prompt = user_prompt

    raw_text = _call_llm(grounded_prompt)
    raw_json = _extract_json(raw_text)
    case = validate_case(raw_json)   # ← 安全边界：不通过就不执行

    meta = {
        "snapshot_used": bool(multi_snapshot),
        "entry_url": entry_url,
        "explore": {
            "pages_visited": len(pages),
            "steps_used": (explore_result or {}).get("steps_used", 0),
            "llm_calls": (explore_result or {}).get("llm_calls", 0),
            "done": (explore_result or {}).get("done", False),
        } if explore_result else None,
        "preflight": None,           # Preflight 校验结果（有多页面快照时才执行）
    }

    # ── 阶段 4：Preflight 校验（结构化 Issue + patch 修复，最多 2 轮）─
    if multi_snapshot:
        issues = _preflight_targets(case, multi_snapshot)
        if issues:
            meta["preflight"] = {
                "issues": [asdict(i) for i in issues],
                "repairs_applied": 0,
                "remaining_issues": None,
            }
            # patch 修复循环（最多 2 轮：每轮应用 patch 后重新验证）
            for _ in range(2):
                try:
                    patch = _repair_patch_with_llm(multi_snapshot, issues)
                    applied = _apply_patch(case, patch)
                    meta["preflight"]["repairs_applied"] += applied
                except Exception:
                    break   # LLM 输出非法 patch → 停止（保留已应用的修复）
                remaining = _preflight_targets(case, multi_snapshot)
                meta["preflight"]["remaining_issues"] = [asdict(i) for i in remaining]
                if not remaining:
                    break   # 全部修复完成
                issues = remaining

    return case, meta
