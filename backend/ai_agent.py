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
from time import perf_counter
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from dsl import DSLCase, Locator, Scope, validate_case
from explore_cache import invalidate as cache_invalidate, load as cache_load, save as cache_save
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
  "input_contract": [
    {"key": "username", "type": "string", "required": true, "secret": false, "default": "standard_user"},
    {"key": "password", "type": "string", "required": true, "secret": true, "default": null}
  ],
  "steps": [
    {"action": "goto", "value": "https://xxx.com"},
    {"action": "fill", "target": {"role": "textbox", "name": "用户名"}, "value": "${username}"},
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
4. 所有可变测试输入（账号、密码等）必须用 ${var} 占位，并声明在 input_contract：
   - 需求中给出的值 → default 填真实值（secret=false）
   - 密码等敏感信息 → secret=true 且 default=null（执行时本地注入）
5. assert_text 验证页面/元素包含文字；assert_visible 验证元素可见；assert_url 验证当前 URL 包含片段
6. 只输出 JSON，不要输出任何解释或代码块标记
7. 最小测试原则：
   - 仅生成完成用户目标所需的最少步骤
   - 步骤结构固定为：导航步骤 → 必要交互步骤 → 【恰好 1 个最终验证步骤】
   - 禁止额外辅助断言、重复等待、重复验证（如同时生成 wait_for 和 assert 同一元素）
8. 验证策略（按目标类型从下列规则中选择）：
   - 登录类 → assert_url 登录后页面片段，或 assert_visible 登录后关键元素
   - 添加/操作类 → assert_visible 操作结果（如按钮变为 Remove、数量徽章变为 1）
   - 页面跳转类 → assert_url 目标页面片段
   - 用户未明确验证内容时，按目标的最终可观察结果生成 1 个最小验证"""


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
        "temperature": 0,   # 确定性输出（结构化生成；≠ 完全 deterministic，但显著降低波动）
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

# ── 测试数据提取与脱敏（敏感信息不进 LLM 上下文）──────────────────────────────
# 核心原则（设计评审）：Secrets remain outside the model context.
#   LLM 看到：${email} ${password}
#   Executor 看到：真实值（本地注入）

_EMAIL_IN_GOAL_RE = re.compile(r'[\w.+-]+@[\w.-]+\.[\w.]+')
# 密码常见写法："密码 xxx" / "password: xxx" / "用户名 / 密码"（分隔）
# ⚠️ "xxx / yyy" 模式必须要求 / 两边有空格 + 前置标识词——
# 否则 "https://example.com" 的 // 会被误认为用户名/密码分隔（修复）
_PASSWORD_PATTERNS = [
    re.compile(r'(?:密码|口令)[：:\s]+([^\s，,。;；]+)'),
    re.compile(r'password[：:\s]+([^\s，,。;；]+)', re.IGNORECASE),
    re.compile(
        r'(?:login\s+with|账号|用户名|using|with)\s*[:：]?\s*'
        r'([^\s,/]+)\s+/\s+([^\s，,。;；]+)',
        re.IGNORECASE,
    ),
]


def _extract_and_redact_goal(goal: str) -> tuple[str, dict]:
    """从用户需求中提取测试数据（邮箱/密码），并从文本中脱敏。

    返回 (脱敏后的 goal, runtime_inputs)。
    LLM 上下文里只剩 ${email} ${password} 占位符；
    真实值只在 Executor 本地使用（探索登录、DSL 执行时注入）。
    """
    runtime: dict[str, str] = {}
    redacted = goal

    # ① 邮箱（标准格式，可靠识别）
    m = _EMAIL_IN_GOAL_RE.search(redacted)
    if m:
        runtime["email"] = m.group(0)
        redacted = redacted.replace(m.group(0), "${email}")

    # ② 密码（多种写法，命中一个即可）
    # 注意："用户名 / 密码" 格式：group(1)=用户名，group(2)=密码，
    # 两者都提取注入（探索时 fill 都用 ${var} 占位，Executor 本地填值）；
    # 其余写法（密码:/password:/口令）只有密码，取 group(1)。
    m = _PASSWORD_PATTERNS[0].search(redacted) or _PASSWORD_PATTERNS[1].search(redacted)
    if m:
        secret = m.group(1)
    else:
        m = _PASSWORD_PATTERNS[2].search(redacted)
        if m:
            runtime["username"] = m.group(1)
            redacted = redacted.replace(m.group(1), "${username}")
            secret = m.group(2)
        else:
            secret = None
    if secret:
        runtime["password"] = secret
        redacted = redacted.replace(secret, "${password}")

    return redacted, runtime


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


# ── Preflight v2：候选提取 + 确定性消歧 + LLM 受限选择 ────────────────────────
# 核心原则（设计评审）：
#   "LLM 最适合做语义判断，不应该承担能够由确定性程序完成的结构修复；
#    模型输出空间越小，Agent 越稳定。"
#
# 分层修复（不再是 LLM 自由生成 patch）：
#   Round 1  确定性代码修复：歧义 → 提取候选 → 需求匹配/首个候选；不存在 → 文本替代
#   Round 2  LLM 受限选择：只从候选里选 candidate_id，patch 由代码生成
#   Round 3  fail-safe：剩余问题标记 unresolved，不无限重试

@dataclass
class PreflightIssue:
    """结构化定位问题（机器可理解，供修复精确定位）。"""
    step_index: int        # 出问题的步骤（1-based，与执行报告一致）
    issue_id: str          # 唯一标识（"step6"）
    type: str              # "LOCATOR_NOT_FOUND" / "AMBIGUOUS_LOCATOR"
    target: dict           # 原始 target（结构化）
    detail: str            # 人类可读说明
    candidates: list[dict] | None = None   # 歧义候选 [{"candidate_id", "scope_text"}]


class RepairItem(BaseModel):
    """单步修复补丁：替换该步骤的 target / scope。"""
    step_index: int = Field(ge=1)
    target: Locator | None = None
    scope: Scope | None = None


class RepairPatch(BaseModel):
    """修复补丁集：只修出问题的步骤，其余步骤不动。"""
    repairs: list[RepairItem] = Field(default_factory=list)


class RepairChoice(BaseModel):
    """LLM 的选择题答案：只选 candidate_id，不生成任何 locator。"""
    issue_id: str
    candidate_id: str


class RepairResponse(BaseModel):
    """LLM 选择题响应（必须覆盖全部 issue，否则判为无效响应）。"""
    choices: list[RepairChoice] = Field(default_factory=list)


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


def _scope_snapshot_check(snapshot: str, scope_text: str | None) -> tuple[bool, int]:
    """scope 的三分验证：has_text 文本在快照中出现 0 / 1 / N 次。

    修复：有 scope ≠ 已消歧——scope 值可能是"不存在的商品"（0 次）
    或匹配多个容器（N 次），Preflight 必须和 Runner 的三分法一致。
    """
    if not scope_text:
        return True, 1   # 无 scope → 视为消歧通过（交由 target 检查）
    return _snapshot_check(snapshot, None, scope_text)


def _target_to_dict(t) -> dict:
    """把 target（str / Locator 模型 / dict）统一转成 dict。"""
    if hasattr(t, "model_dump"):
        return t.model_dump()
    if isinstance(t, dict):
        return t
    return {"text": str(t)}


def _preflight_targets(case: DSLCase, observations: list[dict]) -> list[PreflightIssue]:
    """Page-aware Preflight：按 step.observation_ref 在对应页面状态内做 0/1/N 验证。

    验证上下文选择（修复跨页面误判）：
      有合法 observation_ref → 强验证：在该 observation 的 snapshot 内做
                               存在性 + 次数（0/1/N 真实有效）
      无 observation_ref    → 弱验证：跨 observation presence-only
                               （存在即可，不做全局 count blocking——
                               避免把跨页面重复误判为同页歧义）

    css=/test_id= 无法用快照文本验证（DOM 属性不是语义）→ 跳过。
    """
    obs_map = {o["id"]: o for o in observations}
    fallback_snapshot = _pages_to_text(observations)   # 弱验证用

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

        # 验证上下文：有 ref → 对应 observation（强）；无 → 合并快照（弱）
        ref = step.observation_ref
        if ref and ref in obs_map:
            snapshot = obs_map[ref]["snapshot"]
            strong = True
        else:
            snapshot = fallback_snapshot
            strong = False

        found, count = _snapshot_check(snapshot, role, name)
        if not found:
            issues.append(PreflightIssue(
                step_index=index,
                issue_id=f"step{index}",
                type="LOCATOR_NOT_FOUND",
                target=_target_to_dict(t),
                detail=f"步骤 {index}: target 在页面快照中不存在",
            ))
        elif count > 1 and role and strong:
            # 强验证（有 observation_ref）：该页面状态内真歧义 → scope 检查
            # 弱验证（无 ref）：target 存在即通过（presence-only，
            #   不做全局 count blocking——避免跨页面重复误判为歧义）
            scope_text = None
            if step.scope is not None:
                scope_text = step.scope.model_dump().get("has_text") \
                    if hasattr(step.scope, "model_dump") \
                    else (step.scope.get("has_text") if isinstance(step.scope, dict) else str(step.scope))
            if not scope_text:
                issues.append(PreflightIssue(
                    step_index=index,
                    issue_id=f"step{index}",
                    type="AMBIGUOUS_LOCATOR",
                    target=_target_to_dict(t),
                    detail=f"步骤 {index}: 页面存在 {count} 个同名 {role}，需 scope 消歧",
                ))
            else:
                scope_found, scope_count = _scope_snapshot_check(snapshot, scope_text)
                if not scope_found:
                    issues.append(PreflightIssue(
                        step_index=index,
                        issue_id=f"step{index}",
                        type="AMBIGUOUS_LOCATOR",
                        target=_target_to_dict(t),
                        detail=f"步骤 {index}: scope 文本在快照中不存在（{scope_text!r}），消歧无效",
                    ))
                elif scope_count > 1:
                    # scope 文本计数不可靠（同一文本可出现在卡片/描述多处，
                    # 但业务容器唯一）——容器级消歧只由运行时联合三分法判断，
                    # 快照文本无法恢复容器关系 → 一律 warning（不 blocking）
                    issues.append(PreflightIssue(
                        step_index=index,
                        issue_id=f"step{index}",
                        type="SCOPE_CARDINALITY_UNKNOWN",
                        target=_target_to_dict(t),
                        detail=(
                            f"步骤 {index}: scope 文本在页面快照中出现 {scope_count} 次，"
                            "文本计数无法判断容器唯一性（由运行时联合三分法兜底）"
                        ),
                    ))
                # scope 存在 → 消歧通过（容器级唯一性交给运行时）

    return issues


# ── 候选提取（歧义 → 真实页面上下文）──────────────────────────────────────────

def _extract_candidate_contexts(
    urls: list[str], target: dict, login_inputs: dict | None = None,
    observations: list[dict] | None = None,
) -> list[dict] | None:
    """打开页面，提取 target 所有匹配元素的上下文文本（消歧候选）。

    核心：候选是【系统观察到的真实实体】——LLM 只能从中选择，
    没有权限创造 scope 文本。

    ⚠️ 注意：这里直接构建 locator 数 count，不能用 _resolve_locator——
    它是三分法，count>1 会抛 AmbiguousError（歧义正是我们要提取的）。

    login_inputs：登录后的页面（如商品页）在新会话会被重定向回登录页，
    用探索时提取的账号密码自动登录后重试。

    observations：性能优化（Speed B2）——先用已探索的页面快照文本筛选
    "哪些页面可能包含 target"，只访问这些 URL（从 4 个 → 1 个），
    避免遍历所有页面 + 反复登录。
    """
    from playwright.sync_api import sync_playwright

    # 快照筛选：只访问可能包含 target 的 observation
    if observations:
        parsed = _parse_target(target)
        role, name = (parsed.role, parsed.name or parsed.text) if parsed else (None, None)
        if role and name:
            hits = [o["url"] for o in observations
                    if _snapshot_check(o["snapshot"], role, name)[0]]
            if hits:
                urls = hits

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(10000)
            try:
                for url in urls:
                    try:
                        page.goto(url, wait_until="domcontentloaded")
                        page.wait_for_timeout(500)   # 渲染底线（之前 1000ms）
                    except Exception:
                        continue
                    # 被重定向到登录页（需要登录态）→ 自动登录后重试
                    if login_inputs and _try_login(page, login_inputs):
                        try:
                            page.goto(url, wait_until="domcontentloaded")
                            page.wait_for_timeout(500)
                        except Exception:
                            pass
                    locator = _build_locator_for_count(page, target)
                    if locator is None:
                        continue
                    count = locator.count()
                    if count <= 1:
                        continue
                    candidates = []
                    for i in range(count):
                        try:
                            node = locator.nth(i)
                            # 向上找稳定业务容器（li/article/data-testid），否则向上 2 层
                            container = node.locator(
                                "xpath=ancestor::*[self::li or self::article or @data-testid][1]"
                            )
                            container_count = container.count()   # count 缓存，避免重复查询
                            if container_count == 0:
                                container = node.locator("xpath=../..")
                                container_count = container.count()
                            raw = container.inner_text().strip() if container_count > 0 else ""
                            node_text = node.inner_text().strip()
                            # 候选上下文：容器文本 + 候选 scope 行（排除按钮自身文本）
                            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
                            scope_candidates = [
                                ln for ln in lines if ln != node_text
                            ][:5]
                        except Exception:
                            raw, node_text, scope_candidates = "", "", []
                        candidates.append({
                            "candidate_id": f"c{i + 1}",
                            "context_text": raw[:200],
                            "scope_candidates": scope_candidates,
                        })
                    if candidates:
                        return candidates
            finally:
                browser.close()
    except Exception:
        return None
    return None


def _try_login(page, login_inputs: dict) -> bool:
    """当前页面有登录表单时自动登录（候选提取用）。

    返回是否尝试了登录。表单定位用通用语义（Username/Password/Login），
    失败静默（不中断候选提取）。登录后用 wait_for_load_state 精确等待
    导航完成（修复：固定 1200ms sleep 浪费）。
    """
    try:
        username = page.get_by_role("textbox", name="Username")
        if username.count() == 0:
            return False
        username.fill(login_inputs.get("username") or "")
        page.get_by_role("textbox", name="Password").fill(login_inputs.get("password") or "")
        page.get_by_role("button", name="Login").click()
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(400)   # SPA 内容渲染底线
        return True
    except Exception:
        return False


def _build_locator_for_count(page, target: dict):
    """直接构建定位器（绕过三分法，允许 count>1）——候选提取专用。"""
    parsed = _parse_target(target)
    if parsed is None:
        return None
    if parsed.test_id:
        return page.get_by_test_id(parsed.test_id)
    if parsed.role and parsed.name:
        return page.get_by_role(parsed.role, name=parsed.name)
    if parsed.text:
        return page.get_by_text(parsed.text)
    if parsed.css:
        return page.locator(parsed.css)
    return None


def _text_alternative(snapshot: str, name: str) -> Locator | None:
    """NOT_FOUND 的确定性修复：目标名在快照中存在文本 → 换成文本定位。"""
    found, _ = _snapshot_check(snapshot, None, name)
    return Locator(text=name) if found else None


# 价格行正则（scope 选择时跳过 "$29.99" 这类噪音）
_PRICE_RE = re.compile(r"[$€£]?\s*\d+(?:\.\d{1,2})?")


def _choose_scope_text(scope_candidates: list[str]) -> str | None:
    """从候选行中选最终 scope 文本（启发式）：
    跳过空行/按钮文本（已排除）/纯价格/过短行，返回第一个像"名称"的行。
    """
    for line in scope_candidates:
        line = line.strip()
        if not line or len(line) < 2:
            continue
        if _PRICE_RE.fullmatch(line):
            continue
        return line[:60]
    return None


def _resolve_ambiguity(goal: str, issue: PreflightIssue) -> tuple[str, dict | None, str | None]:
    """需求明确性判断（区分 Locator / Requirement ambiguity）：
      - goal 命中某候选的 scope 行 → ("auto", 候选, scope)  需求明确，代码直接修
      - 无命中但有可用 scope 行  → ("first", 候选, scope)   需求歧义，确定性选第一个
      - 其他                    → ("llm", None, None)      多个候选匹配需求，LLM 选
    """
    # 确定性：goal 子串命中某个候选的 scope_candidates 行
    for cand in (issue.candidates or []):
        for scope in cand.get("scope_candidates", []):
            if scope and scope.lower() in goal.lower():
                return "auto", cand, scope

    # 需求歧义：取第一个候选的第一个"好" scope 行
    for cand in (issue.candidates or []):
        scope = _choose_scope_text(cand.get("scope_candidates", []))
        if scope:
            return "first", cand, scope

    return "llm", None, None


def _scope_patch(issue: PreflightIssue, scope_text: str) -> RepairItem:
    """由最终 scope 文本生成 patch（代码构造，LLM 不参与）。"""
    return RepairItem(
        step_index=issue.step_index,
        target=Locator(**issue.target),
        scope=Scope(has_text=scope_text),
    )


# 修复专用 system prompt（角色独立——修复是"选择题"，不是 DSL 生成）
REPAIR_SYSTEM_PROMPT = """你是测试定位歧义选择器。
只负责从系统提供的候选（candidate_id）中选择最符合用户目标的选项。
禁止创建、修改或推断任何 target、scope、文本、CSS 或 locator。
必须为每个 issue_id 返回且仅返回一个选择。"""


def _llm_choose_candidates(goal: str, issues: list[PreflightIssue]) -> list[RepairChoice]:
    """LLM 只做选择题：从系统观察到的候选中选 candidate_id。

    响应必须覆盖全部 issue（expected == received），否则判为无效响应。
    """
    issue_lines = "\n".join(
        f"- issue_id={i.issue_id} 步骤 {i.step_index}: {i.detail}\n"
        f"  候选: " + " | ".join(
            f"{c['candidate_id']}={'/'.join(c.get('scope_candidates', [])[:2]) or c.get('context_text', '')[:30]}"
            for c in (i.candidates or [])
        )
        for i in issues
    )
    prompt = (
        f"用户测试目标（已脱敏）: {goal}\n\n"
        f"以下每个问题都提供了系统实际观察到的候选。你只能从 candidates 中选择 candidate_id。\n"
        f"禁止创建新的 target、scope、文本或 locator。\n"
        f"必须为每个 issue_id 返回且仅返回一个选择。\n\n"
        f"{issue_lines}\n\n"
        '只输出 JSON: {"choices": [{"issue_id": "step6", "candidate_id": "c1"}]}'
    )
    raw_text = _call_llm(prompt, system_prompt=REPAIR_SYSTEM_PROMPT)
    resp = RepairResponse.model_validate(_extract_json(raw_text))
    expected = {i.issue_id for i in issues}
    received = {c.issue_id for c in resp.choices}
    if expected != received:
        raise ValueError(f"修复响应不完整: 期望覆盖 {expected}，实际收到 {received}")
    return resp.choices


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


def _preflight_and_repair(
    case: DSLCase, observations: list[dict], urls: list[str], goal: str,
    login_inputs: dict | None = None,
) -> dict:
    """分层修复主流程（Round1 确定性 → Round2 LLM 受限选择 → Round3 fail-safe）。

    返回统计：repairs_applied / implicit_resolutions / blocking_issues / warnings
    """
    multi_snapshot = _pages_to_text(observations)   # 修复 prompt / 弱验证用

    stats = {
        "repairs_applied": 0,
        "implicit_resolutions": [],
        "blocking_issues": None,
        "warnings": None,
        # 诊断指标：有 observation_ref 的步骤占比（强验证覆盖度）+
        # 降级为弱验证的步骤（无 ref / 非法 ref）
        "observation_coverage": f"{sum(1 for s in case.steps if s.observation_ref)}/{len(case.steps)}",
        "fallback_steps": [
            i for i, s in enumerate(case.steps, start=1) if not s.observation_ref
        ],
        # Speed B1：Preflight 细分计时（定位 13.8s 花在哪）
        "timings": {
            "initial_check_ms": 0,
            "candidate_extract_ms": 0,
            "round2_llm_ms": 0,
            "recheck_ms": 0,
        },
    }

    # Round1 提取过的 candidates 按 issue_id 保留——
    # run_preflight() 会重建 issue 对象（candidates=None），
    # 不回填会导致 Round2 过滤条件 i.candidates 为空而进不去（修复）
    known_candidates: dict[str, list[dict]] = {}

    def run_preflight() -> list[PreflightIssue]:
        t = perf_counter()
        result = _preflight_targets(case, observations)
        for iss in result:
            if iss.issue_id in known_candidates:
                iss.candidates = known_candidates[iss.issue_id]
        return result

    t0 = perf_counter()
    issues = run_preflight()
    stats["timings"]["initial_check_ms"] = int((perf_counter() - t0) * 1000)
    if not issues:
        return stats

    # ── Round 1：确定性代码修复（零 LLM 调用）────────────────────
    round1_patches: list[RepairItem] = []
    for issue in issues:
        if issue.type == "AMBIGUOUS_LOCATOR":
            # 候选提取：真实页面上下文（系统观察到的实体）
            t = perf_counter()
            issue.candidates = _extract_candidate_contexts(
                urls, issue.target, login_inputs, observations,
            )
            stats["timings"]["candidate_extract_ms"] += int((perf_counter() - t) * 1000)
            if issue.candidates:
                known_candidates[issue.issue_id] = issue.candidates
            else:
                continue   # 提取失败 → 留给 Round 2
            mode, chosen, scope_text = _resolve_ambiguity(goal, issue)
            if mode in ("auto", "first") and chosen and scope_text:
                round1_patches.append(_scope_patch(issue, scope_text))
                if mode == "first":
                    stats["implicit_resolutions"].append({
                        "step_index": issue.step_index,
                        "reason": "用户未指定具体对象，按确定性规则选择第一个候选",
                        "selected": scope_text,
                        "policy": "first_candidate",
                    })
            # mode == "llm" → 留给 Round 2
        elif issue.type == "LOCATOR_NOT_FOUND":
            parsed = _parse_target(issue.target)
            name = (parsed.name or parsed.text) if parsed else None
            if name:
                alt = _text_alternative(multi_snapshot, name)
                if alt:
                    round1_patches.append(RepairItem(step_index=issue.step_index, target=alt))

    if round1_patches:
        stats["repairs_applied"] += _apply_patch(case, RepairPatch(repairs=round1_patches))
        t = perf_counter()
        issues = run_preflight()
        stats["timings"]["recheck_ms"] += int((perf_counter() - t) * 1000)

    # ── Round 2：LLM 受限选择（只选 candidate_id，patch 代码生成）─
    if issues:
        llm_issues = [i for i in issues if i.type == "AMBIGUOUS_LOCATOR" and i.candidates]
        if llm_issues:
            try:
                t = perf_counter()
                choices = _llm_choose_candidates(goal, llm_issues)
                stats["timings"]["round2_llm_ms"] += int((perf_counter() - t) * 1000)
                patches: list[RepairItem] = []
                for ch in choices:
                    issue = next((i for i in llm_issues if i.issue_id == ch.issue_id), None)
                    candidate = next(
                        (c for c in (issue.candidates or []) if c["candidate_id"] == ch.candidate_id),
                        None,
                    ) if issue else None
                    # LLM 只选 candidate_id，最终 scope 文本由代码从候选行中确定
                    if issue and candidate:
                        scope_text = _choose_scope_text(candidate.get("scope_candidates", []))
                        if scope_text:
                            patches.append(_scope_patch(issue, scope_text))
                if patches:
                    stats["repairs_applied"] += _apply_patch(case, RepairPatch(repairs=patches))
                    t = perf_counter()
                    issues = run_preflight()
                    stats["timings"]["recheck_ms"] += int((perf_counter() - t) * 1000)
            except Exception:
                pass   # LLM 选择失败 → Round 3 fail-safe

    # ── Round 3：fail-safe（剩余问题分类记录，不无限重试）───────────
    # 语义拆分（避免"还有问题却 6/6 通过"的误导）：
    #   blocking_issues = 歧义未消（执行必然失败）
    #   warnings        = 非阻塞：快照未验证到（可能是操作后状态变化）、
    #                      scope 跨页面计数不确定（cardinality unknown）
    stats["blocking_issues"] = [
        asdict(i) for i in issues if i.type == "AMBIGUOUS_LOCATOR"
    ]
    stats["warnings"] = [
        asdict(i) for i in issues
        if i.type in {"LOCATOR_NOT_FOUND", "SCOPE_CARDINALITY_UNKNOWN"}
    ]
    return stats


# ── Plan Normalization（生成后归一化：LLM 输出可以波动，最终 DSL 稳定）─────────
# 设计原则：LLM 负责语义规划，代码负责计划规范化——
# 模型非确定性被限制在"不会影响执行语义"的范围内。

_ASSERT_ACTIONS = {"assert_visible", "assert_text", "assert_url"}


def _target_key(step) -> str:
    """步骤 target 的归一化键（用于判断 wait_for 与断言是否同一元素）。"""
    t = step.target
    if t is None:
        return ""
    if hasattr(t, "model_dump"):
        d = t.model_dump()
        return f"{d.get('role') or ''}:{d.get('name') or ''}:{d.get('text') or ''}"
    if isinstance(t, dict):
        return f"{t.get('role') or ''}:{t.get('name') or ''}:{t.get('text') or ''}"
    return str(t)


def _normalize_steps(case: DSLCase) -> DSLCase:
    """生成后归一化（Planner 输出波动 → 最终 DSL 稳定）：

      1. 只保留最后一个断言步骤作为最终验证，删除前面多余的断言
      2. 删除与最终断言同一元素的冗余 wait_for（重复等待）
      3. 重新校验（步骤变化后保证仍是合法 DSL）
    """
    steps = list(case.steps)
    if len(steps) <= 1:
        return case

    assert_indices = [i for i, s in enumerate(steps) if s.action in _ASSERT_ACTIONS]
    if not assert_indices:
        return case

    keep = assert_indices[-1]   # 最终验证步骤（保留）

    # 删除前面多余的断言步骤
    normalized = [s for i, s in enumerate(steps) if s.action not in _ASSERT_ACTIONS or i == keep]

    # 保守删除冗余 wait_for：只删"紧邻最终断言前一个、且同 target"的。
    # 前提：断言类动作（expect）自带 Playwright 自动等待，显式 wait_for 冗余；
    # 但中间隔着其他步骤的 wait_for 可能有业务意义（如等待跳转完成），不删。
    final_step = normalized[-1]
    final_key = _target_key(final_step)
    if len(normalized) >= 2:
        prev = normalized[-2]
        if prev.action == "wait_for" and _target_key(prev) == final_key:
            del normalized[-2]

    case.steps = normalized
    return validate_case(case.model_dump())   # 重新校验（安全边界）


# ── 多页面快照文本（探索结果 → Planner 可读上下文）──────────────────────────────

def _sanitize_for_cache(explore_result: dict, runtime_inputs: dict) -> dict:
    """缓存前脱敏：history 的 value 还原为 ${var} 占位。

    缓存会持久化到磁盘——Secrets 边界必须保持：
    真实凭据（密码等）绝不进入缓存文件。
    """
    result = json.loads(json.dumps(explore_result))   # 深拷贝
    for h in result.get("history", []):
        v = h.get("value")
        if v and runtime_inputs:
            for key, real in runtime_inputs.items():
                if real and real in v:
                    h["value"] = v.replace(real, f"${{{key}}}")
    return result


def _pages_to_text(pages: list[dict]) -> str:
    """把探索到的 observation 快照合并成 Planner 可读文本（每页分段标记）。

    分段标记用 observation id（obs1/obs2/...）——Planner 生成 DSL 时
    用 observation_ref 引用（Commit 2 接入）。
    """
    sections = []
    for page in pages:
        obs_id = page.get("id", f"obs{len(sections) + 1}")
        title = page.get("title") or ""
        sections.append(
            f"[{obs_id}] {page['url']}"
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
    t_url = perf_counter()
    entry_url = _resolve_entry_url(user_prompt)
    url_resolve_ms = int((perf_counter() - t_url) * 1000)

    # ── 阶段 1.5：测试数据脱敏（敏感信息不进 LLM 上下文）───────────
    explore_goal, runtime_inputs = _extract_and_redact_goal(user_prompt)

    # ── 阶段 2：bounded exploration（缓存命中直接跳过探索回路）──────
    t_explore = perf_counter()
    explore_result = None
    pages = []
    cache_hit = False
    auth_profile = "authenticated" if runtime_inputs else "anonymous"
    if entry_url:
        cached = cache_load(entry_url, auth_profile)
        if cached:
            explore_result = cached
            pages = cached.get("observations", [])
            cache_hit = True
        else:
            try:
                explore_result = explore(explore_goal, entry_url, _call_llm, runtime_inputs)
                pages = explore_result.get("observations", [])   # ← observations 模型
                # 保存前脱敏：history 的 value 还原为 ${var}（缓存不落盘真实凭据）
                cache_save(entry_url, auth_profile, _sanitize_for_cache(explore_result, runtime_inputs))
            except Exception:
                explore_result = None   # 探索异常 → 降级无快照生成
    explore_ms = int((perf_counter() - t_explore) * 1000)

    # ── 阶段 3：组装 prompt（多页面结构 + 探索路径）→ Planner 生成 ──
    multi_snapshot = _pages_to_text(pages) if pages else None
    if multi_snapshot:
        # 把探索路径也注入：Planner 能看到"怎么走到每个页面"
        path_lines = [
            f"- {h.get('action')} {json.dumps(h.get('target'), ensure_ascii=False) if h.get('target') else ''} "
            f"{h.get('value') or ''} @ {h.get('url')}"
            for h in (explore_result or {}).get("history", [])
        ]
        grounded_prompt = (
            f"目标页面入口: {entry_url}\n\n"
            f"探索路径（已按此流程访问过以下页面）:\n"
            + "\n".join(path_lines)
            + "\n\n各页面真实结构（ARIA snapshot）：\n\n"
            + multi_snapshot
            + "\n\n用户测试需求（已脱敏，密码等敏感信息已替换为 ${var} 占位符）: "
            + explore_goal
            + "\n\n规则："
            "1. 用户提供的测试数据用 ${var} 占位并声明在 input_contract："
            "需求中给出的值填 default；密码等敏感信息 secret=true 且 default=null；"
            "2. 快照中以 'text: xxx' 形式出现的标题（span/div 无 heading 语义）"
            '必须用 {"text": "xxx"} 定位，禁止用 {"role": "heading"}；'
            "3. 每个步骤必须设置 observation_ref，且只能从页面分段标记"
            "（[obs1] [obs2] ...）中选择——禁止创造不存在的 observation_ref；"
            "该步骤的 target/scope 必须来自该 observation 的页面结构。"
        )
    else:
        grounded_prompt = explore_goal   # 无快照时同样用脱敏后的需求

    t_planner = perf_counter()
    raw_text = _call_llm(grounded_prompt)
    planner_ms = int((perf_counter() - t_planner) * 1000)   # 只计 Planner 一次调用
    raw_json = _extract_json(raw_text)
    case = validate_case(raw_json)   # ← 安全边界：不通过就不执行
    case = _normalize_steps(case)    # ← 计划归一化：LLM 波动 → 稳定结构

    # ← grounding 验证：observation_ref 必须来自系统提供的真实 id
    #（不靠 Prompt——代码校验；非法 ref 清空为 None，降级弱验证）
    valid_refs = {obs["id"] for obs in pages}
    if valid_refs:
        for step in case.steps:
            if step.observation_ref and step.observation_ref not in valid_refs:
                step.observation_ref = None

    meta = {
        "snapshot_used": bool(multi_snapshot),
        "entry_url": entry_url,
        "cache_hit": cache_hit,      # Speed v1：探索结果是否命中缓存
        "explore": {
            "pages_visited": len(pages),
            "steps_used": (explore_result or {}).get("steps_used", 0),
            "llm_calls": (explore_result or {}).get("llm_calls", 0),
            "done": (explore_result or {}).get("done", False),
        } if explore_result else None,
        "preflight": None,           # Preflight 校验结果（有多页面快照时才执行）
    }

    # ── 阶段 4：Page-aware Preflight（按 observation_ref 验证 + 分层修复）─
    t_preflight = perf_counter()
    if pages:
        urls = [p["url"] for p in pages]
        # 只要跑了 Preflight 就始终返回 stats（修复：不再按条件访问
        # 可能不存在的 key——避免 repairs=0 时 KeyError）
        meta["preflight"] = _preflight_and_repair(
            case, pages, urls, explore_goal, runtime_inputs,
        )
    preflight_ms = int((perf_counter() - t_preflight) * 1000)

    # Speed v1：生成链路计时（定位耗时构成，决定下一刀砍哪）
    meta["timings"] = {
        "url_resolve_ms": url_resolve_ms,
        "explore_ms": explore_ms,
        "explore_detail": (explore_result or {}).get("timings"),
        "planner_ms": planner_ms,
        "preflight_ms": preflight_ms,
    }

    return case, meta
