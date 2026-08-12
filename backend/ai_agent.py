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

from dsl import DSLCase, validate_case
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
    {"action": "input", "target": "textbox=用户名", "value": "${变量名}"},
    {"action": "click", "target": "button=登录"},
    {"action": "wait_for", "target": "heading=首页"},
    {"action": "assert_text", "value": "要验证的文字"}
  ]
}

规则：
1. action 只能是: goto, click, input, wait_for, assert_text
2. target 定位格式：优先使用语义定位 "角色=名称"，例如 button=登录、link=首页、textbox=邮箱、heading=标题
3. 用户没提供的登录信息，用 input_contract 定义变量，steps 里用 ${变量名} 引用
4. assert_text 用于验证页面包含某段文字
5. 只输出 JSON，不要输出任何解释或代码块标记"""


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


# ── 阶段 1：从用户需求中提取入口 URL（与原项目 explore 前置一致）────────────────

EXTRACT_URL_PROMPT = """你是 URL 提取器。
从用户的测试需求中提取被测网站的入口 URL（以 http:// 或 https:// 开头）。

规则：
1. 如果没有明确提到 URL，返回 {"url": null}
2. 只输出 JSON，格式：{"url": "https://..."} 或 {"url": null}"""


def _extract_entry_url(user_prompt: str) -> str | None:
    """阶段 1：LLM 从用户需求中提取入口 URL。

    为什么用 LLM 提取而不是正则？
      用户可能说"打开 automation exercise 网站"（没写 URL）——
      正则提取不到，LLM 能根据上下文判断。提取失败返回 None，降级处理。
    """
    try:
        text = _call_llm(user_prompt, system_prompt=EXTRACT_URL_PROMPT)
        data = _extract_json(text)
        url = data.get("url")
        return url if isinstance(url, str) and url.strip() else None
    except Exception:
        return None   # 提取失败 → 降级为无快照生成


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


# ── Preflight 校验（生成后验证 target 能否命中，失败自动重生）──────────────────
# 这是"AI 生成质量闭环"的核心（原项目同款机制）：
#   AI 生成 DSL → 用真实页面快照验证每个 target → 不存在的/歧义的
#   → 把问题反馈给 LLM 重新生成 → 修正后再次校验
# 效果：AI 自己发现并修正错误，而不是执行时才失败、人工再改。

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


def _preflight_targets(case: DSLCase, snapshot: str) -> list[str]:
    """校验 DSL 每个 target 是否能在页面快照中命中，返回问题列表。

    三种结果：
      命中 1 次      → 通过
      不存在          → "快照中不存在"（必须重生修正）
      命中 2+ 次      → "存在 N 个同名元素，建议用 scope 消歧"（原项目同款约束）

    css=/test_id= 无法用快照文本验证（它们是 DOM 属性不是语义）→ 跳过。
    """
    issues: list[str] = []
    for step in case.steps:
        t = step.target
        if not t:
            continue   # goto / 无 target 断言，无需验证

        parsed = _parse_target(t)   # 复用 runner 的解析（单一实现）
        if parsed is None:
            continue
        role, name = parsed.role, parsed.name
        if not name:
            name = parsed.text
        if not name:
            continue   # 纯 css/test_id，无法验证

        found, count = _snapshot_check(snapshot, role, name)
        if not found:
            issues.append(f"{t!r}: 页面快照中不存在")
        elif count > 1 and role and not step.scope:
            # 已带 scope 的 target 视为已消歧，不报歧义
            issues.append(f"{t!r}: 页面存在 {count} 个同名 {role}，建议使用 scope 消歧")

    return issues


def _repair_with_llm(user_prompt: str, entry_url: str | None, snapshot: str, issues: list[str]) -> DSLCase:
    """把 Preflight 发现的问题反馈给 LLM，要求基于真实页面重新生成完整 DSL。"""
    repair_prompt = (
        f"目标页面入口: {entry_url}\n"
        f"页面真实结构（ARIA snapshot）:\n{snapshot}\n\n"
        f"你上次生成的 DSL 存在以下定位问题（共 {len(issues)} 处）：\n"
        + "\n".join(f"- {i}" for i in issues)
        + "\n\n请基于页面真实结构修正这些 target："
        "存在多个同名元素时，scope 的值必须是页面中真实可见的文本"
        "（如商品名称，不能是 CSS 类名）；不存在的元素改用快照中真实存在的元素；"
        "快照中以 'text: xxx' 形式出现的标题（span/div 无 heading 语义）"
        "必须用 text=xxx 定位，禁止写 heading=xxx。"
        "输出完整的修正后 DSL JSON（其余步骤保持不变）。只输出 JSON。"
    )
    raw_text = _call_llm(repair_prompt)
    return validate_case(_extract_json(raw_text))


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
    # ── 阶段 1：提取入口 URL（AI 从自然语言中找）────────────────────
    entry_url = _extract_entry_url(user_prompt)

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
            "必须用 text=xxx 定位，禁止写 heading=xxx。"
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

    # ── 阶段 4：Preflight 校验（多页面快照验证，失败自动重生一次）────
    if multi_snapshot:
        issues = _preflight_targets(case, multi_snapshot)
        if issues:
            try:
                case = _repair_with_llm(user_prompt, entry_url, multi_snapshot, issues)
                meta["preflight"] = {"repaired": True, "issues": issues}
                # 重生后再验证一次（只记录，不无限重生——预算控制）
                remaining = _preflight_targets(case, multi_snapshot)
                meta["preflight"]["remaining_issues"] = remaining
            except Exception:
                # 重生失败（LLM 又输出非法格式）→ 保留原 case，不中断
                meta["preflight"] = {"repaired": False, "issues": issues}

    return case, meta
