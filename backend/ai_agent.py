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

from pydantic import BaseModel, Field, ValidationError

from compiler import compile_targets
from dsl import DSLCase, Locator, Scope, validate_case
from explore_cache import invalidate as cache_invalidate, load as cache_load, save as cache_save
from explore import (
    GOAL_ACTION_PATTERNS, _ACTION_KEYWORDS, explore,
)
import anti_patterns
from grounding import (
    StateGraph, StateGroundingMismatchError, UnknownTargetRefError,
    UnreachableObservationError, _reachable_observations,
    validate_state_grounding,
)
from resolver import (
    PRICE_RE, build_locator_exact_first, build_locator_for_count,
    choose_scope_text, is_navigation_target, parse_target, snapshot_match,
)

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
根据用户描述的自然语言测试需求，输出一个 JSON 对象。示例（与最小步骤规则完全一致）：

{
  "name": "登录并进入商品页",
  "description": "登录后验证进入商品页",
  "base_url": "https://xxx.com",
  "input_contract": [
    {"key": "username", "type": "string", "required": true, "secret": false, "default": "standard_user"},
    {"key": "password", "type": "secret", "required": true, "secret": true, "default": null}
  ],
  "steps": [
    {"action": "goto", "value": "https://xxx.com"},
    {"action": "fill", "target": {"role": "textbox", "name": "用户名"}, "value": "${username}"},
    {"action": "fill", "target": {"role": "textbox", "name": "密码"}, "value": "${password}"},
    {"action": "click", "target": {"role": "button", "name": "登录"}},
    {"action": "assert_url", "value": "/inventory.html", "observation_ref": "obs2"}
  ]
}

规则：
1. action 只能是: goto, click, fill, select, check, wait_for, assert_visible, assert_text, assert_url
2. target 使用结构化定位（多字段组合，按优先级）：
   - 语义定位: {"role": "button", "name": "登录"}
   - 文本定位: {"text": "Products"}（快照中 'text: xxx' 的标题必须用 text，禁止 role=heading）
   - 测试 id:  {"test_id": "login-button"}
   - CSS 兜底: {"css": ".btn"}
3. scope 最小化原则（scope 的唯一用途是解决 target 歧义）：
   - 先判断 target 在对应 observation 中是否唯一；如果唯一，scope 必须为 null
   - 不得为了表达页面层级、业务归属或"增强精确度"而添加 scope
   - target 存在多个匹配时，scope.has_text 锚点按优先级选择：
     1) 用户明确指定的业务实体，如 Blue Top
     2) 当前 observation 中唯一稳定的业务标识
     3) 其他稳定文本
   - 避免价格、数量、状态词和 Login 等高频通用文本作为 scope 锚点
   - 导航级 target（Cart / Products / Home 等）不得使用商品卡片、商品名称或价格作为 scope
   - observation 中没有可靠 scope 时不要编造
4. 变量：
   - 所有可变测试输入必须使用 ${var}，每个变量必须声明在 input_contract
   - secret=true → default 必须为 null（执行时本地注入）
   - 非敏感变量只有上下文明确提供 default 时才能填写；不得猜测真实值
5. observation_ref（grounding 引用）：
   - 每个可定位步骤应引用产生该定位证据的 observation id（obs1/obs2/...）
   - observation_ref 必须来自系统提供的 observation 列表，禁止编造
   - Runner 不使用 observation_ref 控制页面跳转，它只供验证与诊断使用
6. 断言动作字段约束（机械规则）：
   - assert_text: value 必填（要验证的文本）；target 可选（有 target 验证该元素文本，无 target 验证页面文本）；
     禁止把待验证文本只放在 target.text 而省略 value
   - assert_visible: target 必填；如果只是"某段文字/元素出现"，使用 assert_visible，而不是无 value 的 assert_text
   - assert_url: value 必填（URL 片段）
7. 最小测试原则：
   - 仅生成完成用户需求所需的最少步骤
   - 不生成重复 wait、辅助 assertion 或用户未要求的业务检查
   - 单一最终目标默认生成恰好 1 个最终验证
   - 如果用户明确要求多个独立验证结果，则保留这些明确要求的验证
8. Wait after state-changing actions（等待修改动作的 postcondition）：
   - 当 click / submit / select 会触发异步页面状态变化、且后续步骤依赖
     该变化时，必须等待一个能证明变化已经完成的新状态元素（postcondition），
     再继续下一步
   - 正确：click "Add to cart" → wait_for 加购后的 "Added!" 弹窗 或
     按钮变 "Remove" → 再 click "Cart"
   - 错误：click "Add to cart" → wait_for "Cart"——Cart 链接在动作前就
     一直存在，它不能证明加购完成
   - 禁止机械地在每个 click 前生成 wait_for——Playwright 已自动等待目标
     可操作；wait_for 只用于等待业务状态变化（postcondition）
9. Modify-then-assert（修改后先等再断言）：
   - 修改值（fill/select/check）后，先 wait_for 更新生效，再断言新值
   - 不得在修改生效前断言新值（竞态：断言可能读到旧状态）
10. 验证策略：
   - 登录/页面跳转 → 优先 assert_url 或目标页面关键元素 assert_visible
   - 元素出现、按钮状态变化 → assert_visible
   - 文本、价格、数量变化 → assert_text
   - 用户未明确验证方式时，选择与最终动作因果关系最直接的可观察结果
11. 只输出 JSON，不要输出任何解释或代码块标记"""

# G3 refs-only 模式：grounded 生成（有探索元素表）时使用。
# 与 SYSTEM_PROMPT（legacy 模式，无探索降级路径）的区别只有定位方式：
#   Planner 只从元素引用表选 target_ref，禁止生成任何定位字段——
#   locator 由系统确定性编译（R1 Compiler），不再信任 LLM 的 role/name/scope。
SYSTEM_PROMPT_REFS_ONLY = """你是 Web UI 自动化测试的 DSL 生成器（refs-only 模式）。
根据用户描述的自然语言测试需求，从系统提供的元素引用表中选择元素，输出一个 JSON 对象。示例（与最小步骤规则完全一致）：

{
  "name": "登录并进入商品页",
  "description": "登录后验证进入商品页",
  "base_url": "https://xxx.com",
  "input_contract": [
    {"key": "username", "type": "string", "required": true, "secret": false, "default": "standard_user"},
    {"key": "password", "type": "secret", "required": true, "secret": true, "default": null}
  ],
  "steps": [
    {"action": "goto", "value": "https://xxx.com", "observation_ref": "obs1"},
    {"action": "fill", "target_ref": "obs1:e3", "value": "${username}", "observation_ref": "obs1"},
    {"action": "fill", "target_ref": "obs1:e4", "value": "${password}", "observation_ref": "obs1"},
    {"action": "click", "target_ref": "obs1:e5", "observation_ref": "obs1"},
    {"action": "assert_url", "value": "/inventory.html", "observation_ref": "obs2"}
  ]
}

规则：
1. action 只能是: goto, click, fill, select, check, wait_for, assert_visible, assert_text, assert_url
2. 定位元素只能通过 target_ref 引用元素引用表中的 ref（格式 obsN:eM）：
   - 每个需要定位元素的步骤（click/fill/select/check/wait_for/assert_visible）
     必须提供 target_ref，且只能从系统提供的元素引用表中选择
   - 禁止生成 target、scope、role、name、text、css、test_id 等任何定位字段
     （locator 由系统根据 ref 确定性编译）
   - 引用表中没有合适元素时，调整步骤设计（如改用 assert_text 验证页面文本），
     禁止编造 ref
3. 变量：
   - 所有可变测试输入必须使用 ${var}，每个变量必须声明在 input_contract
   - secret=true → default 必须为 null（执行时本地注入）
   - 非敏感变量只有上下文明确提供 default 时才能填写；不得猜测真实值
4. 业务动作覆盖（最重要）：
   - 用户目标中要求的每个业务动作都必须生成对应的执行步骤——
     目标说"加入购物车"就必须有 click 加购元素的步骤，说"登录"就必须
     有完整的登录步骤（fill + click）
   - 禁止只生成导航（goto）和断言而跳过目标要求的业务动作
5. observation_ref（grounding 引用）：
   - 每个可定位步骤应引用产生该定位证据的 observation id（obs1/obs2/...）
   - observation_ref 必须来自系统提供的 observation 列表，禁止编造
   - target_ref 的 obs 前缀必须与 observation_ref 一致（都是 obsN）
6. 断言动作字段约束（机械规则）：
   - assert_text: value 必填（要验证的文本）；验证某个元素内文本时用 target_ref
     引用该元素，验证整页文本时不提供 target_ref；
     禁止把待验证文本只放在 target 里而省略 value（target 字段本来就被禁止）
   - assert_visible: target_ref 必填（验证元素出现）
   - assert_url: value 必填（URL 片段），不需要 target_ref
7. 最小测试原则：
   - 仅生成完成用户需求所需的最少步骤
   - 不生成重复 wait、辅助 assertion 或用户未要求的业务检查
   - 单一最终目标默认生成恰好 1 个最终验证
   - 如果用户明确要求多个独立验证结果，则保留这些明确要求的验证
8. Wait after state-changing actions（等待修改动作的 postcondition）：
   - 当 click / submit / select 会触发异步页面状态变化、且后续步骤依赖
     该变化时，必须等待一个能证明变化已经完成的新状态元素（postcondition），
     再继续下一步
   - 正确：click "Add to cart" → wait_for 加购后的 "Added!" 弹窗 或
     按钮变 "Remove" → 再 click "Cart"
   - 错误：click "Add to cart" → wait_for "Cart"——Cart 链接在动作前就
     一直存在，它不能证明加购完成
   - 禁止机械地在每个 click 前生成 wait_for——Playwright 已自动等待目标
     可操作；wait_for 只用于等待业务状态变化（postcondition）
   - postcondition 元素必须来自元素表（target_ref 引用新状态中的元素，
     如 obs6 的 "Remove"；引用表中没有可靠 postcondition 时，宁可不加
     wait_for 也不编造 ref）
9. Modify-then-assert（修改后先等再断言）：
   - 修改值（fill/select/check）后，先 wait_for 更新生效，再断言新值
   - 不得在修改生效前断言新值（竞态：断言可能读到旧状态）
10. 验证策略：
   - 登录/页面跳转 → 优先 assert_url 或目标页面关键元素 assert_visible
   - 元素出现、按钮状态变化 → assert_visible
   - 文本、价格、数量变化 → assert_text
   - 用户未明确验证方式时，选择与最终动作因果关系最直接的可观察结果
11. 只输出 JSON，不要输出任何解释或代码块标记"""


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
        r'(?:login\s+with|账号|用户名|using|with|用|使用)\s*[:：]?\s*'
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
            username = m.group(1)
            if not username.startswith("${"):
                # 用户名位置可能是已脱敏的 ${email} 占位符（修复：
                # "login with ${email} / test123" 不再把占位符当真实用户名）
                runtime["username"] = username
                redacted = redacted.replace(username, "${username}")
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


# ── Planner Schema Recovery（P0：Pydantic 失败 → constrained retry ×1）─────────
# 修复：Planner 输出不符合 DSL schema 时（如 assert_text 缺 value），
# 整体 400 终止——应把"原输出 + 精简错误"反馈给专用 recovery LLM，
# 只修 schema，不重新规划（不增删重排步骤）。

PLANNER_RECOVERY_SYSTEM_PROMPT = """你是 Web 测试 DSL 的 schema 修复器。

你会收到：
1. 上一次 Planner 生成的 JSON
2. Pydantic 校验错误

只修复这些 schema 错误。

规则：
- 不新增无关步骤
- 不改变已有步骤顺序
- 不改变 locator、scope、业务对象，除非校验错误直接要求
- 不重新规划测试流程
- 只输出修复后的完整 JSON"""

# refs-only 模式的 recovery：除 schema 错误外还要修复 ref 契约违规
#（缺失 target_ref / 携带被禁止的 target/scope 字段 / 编造 ref——
#  引用表在 recovery prompt 中提供）。
PLANNER_RECOVERY_SYSTEM_PROMPT_REFS_ONLY = """你是 Web 测试 DSL 的 schema 修复器（refs-only 模式）。

你会收到：
1. 上一次 Planner 生成的 JSON
2. Schema 校验错误
3. 元素引用表（target_ref 只能从这个表中选择）

只修复这些 schema 错误。

规则：
- 不新增无关步骤
- 不改变已有步骤顺序
- 定位步骤必须通过 target_ref 引用元素引用表中的 ref
- 禁止生成 target、scope 或任何定位字段（locator 由系统编译）
- 禁止编造 ref——引用表中没有的元素不得引用
- 不重新规划测试流程
- 只输出修复后的完整 JSON"""


def _summarize_validation_error(exc) -> str:
    """把 Pydantic ValidationError 提炼成模型可读的错误行（不是 traceback）。"""
    if hasattr(exc, "errors"):
        lines = []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e.get("loc", []))
            msg = e.get("msg", "")
            lines.append(f"- {loc}: {msg}")
        return "\n".join(lines) or str(exc)[:500]
    return str(exc)[:500]


# ── refs-only 契约检查（代码执行，不靠 Prompt）────────────────────────────────
# G3：Planner 没有权限创造定位字段——grounded 模式下输出的每个定位步骤
# 必须且只能引用元素表；违规按 schema 失败处理（进入 recovery，引用表上下文）。

_LOCATABLE_ACTIONS = {"click", "check", "fill", "input", "select",
                      "wait_for", "assert_visible"}


def check_refs_only(case: DSLCase) -> None:
    """refs-only 契约（独立函数，可单测）：

      - 任何步骤不得携带 target/scope（locator 由系统编译，不由 Planner 生成）
      - 定位类动作必须有 target_ref
    违规 → ValueError（被 _generate_planner_case 的 recovery 捕获；
    恢复后仍违规 → 生成失败——宁可明确失败）。
    """
    for index, step in enumerate(case.steps, start=1):
        if step.target is not None or step.scope is not None:
            raise ValueError(
                f"步骤 {index}: refs-only 模式禁止生成 target/scope 字段"
                "（locator 由系统根据 target_ref 确定性编译）"
            )
        if step.action in _LOCATABLE_ACTIONS and step.target_ref is None:
            raise ValueError(
                f"步骤 {index}: {step.action} 必须通过 target_ref 引用元素引用表"
            )


# ── GQ：生成期目标覆盖检查（保守 allowlist，fail-open 只警告）────────────────

def _check_goal_coverage(goal: str, case: DSLCase) -> list[str]:
    """检查计划是否覆盖 goal 明确要求的动作（GQ 决策 3，可单测）。

    真实 E2E 踩坑（9/10 案例）：计划断言了 Add to cart 的可见性却漏了
    【点击】动作——assert_visible 不算覆盖，必须存在对应的 click 步骤。
    goal 未命中动作表（GOAL_ACTION_PATTERNS）→ 不检查（fail-open，
    不误伤"验证页面文字"这类无操作目标）。
    """
    missing: list[str] = []

    def _step_text(step) -> str:
        parsed = parse_target(step.target)
        if parsed is None:
            return ""
        return f"{parsed.name or ''} {parsed.text or ''}".strip().casefold()

    for label, pattern in GOAL_ACTION_PATTERNS.items():
        if not pattern.search(goal):
            continue
        keywords = _ACTION_KEYWORDS[label]
        covered = any(
            step.action == "click" and step.target is not None
            and any(k in _step_text(step) for k in keywords)
            for step in case.steps
        )
        if not covered:
            missing.append(label)
    return missing


# ── postcondition 覆盖检测（graph-aware：有已观察转移即视为有证据）─────────────

_MODIFY_ACTIONS = {"click", "fill", "input", "select", "check"}
_EXPLICIT_POSTCONDITION = {"wait_for", "assert_visible", "assert_url", "assert_text"}


def detect_missing_postconditions(
    case: DSLCase,
    observations: list | None = None,
    transitions: list | None = None,
) -> list[str]:
    """检测"state-changing 动作缺少可验证 postcondition"（graph-aware）。

    评审收紧（升级 missing_wait_for）：refs-only 架构下 step.target 为
    None，无法靠 target 判断导航——改用 Observation State Graph 判断：

      - 转移图里存在 (from, action, ref) → to（to != from）：
        该动作在探索期被观察到产生状态转换 = postcondition evidence
        已存在 → 不报（比 wait_for 更强的证据）
      - DSL 中该动作的下一步显式提供 wait_for / assert_visible /
        assert_url / assert_text：显式 postcondition → 不报
      - 否则 → UNVERIFIED_POSTCONDITION（报）

    只检查 state-changing 动作（click/fill/select/check）；
    goto 天然安全（执行器等待页面加载）不检查。
    """
    transition_index = {
        (t["from"], t["action"], t["target_ref"]): t["to"]
        for t in (transitions or [])
        if t.get("from") and t.get("to") and t.get("target_ref")
    }
    issues: list[str] = []
    steps = case.steps
    for i in range(len(steps) - 1):
        cur = steps[i]
        if cur.action not in _MODIFY_ACTIONS:
            continue
        # ① 已观察转移 = grounded postcondition evidence（graph-aware）
        if cur.target_ref and cur.observation_ref:
            to = transition_index.get(
                (cur.observation_ref, cur.action, cur.target_ref),
            )
            if to and to != cur.observation_ref:
                continue
        # ② 显式 postcondition（下一步是等待/验证）
        if steps[i + 1].action in _EXPLICIT_POSTCONDITION:
            continue
        # ③ 导航 click（target 可用时判定；refs-only 时靠 ①② 兜底）
        if cur.action == "click" and cur.target is not None \
                and is_navigation_target(cur.target):
            continue
        issues.append(
            f"{cur.action}({_brief_target(cur.target) or cur.target_ref or '-'}) "
            "无已观察转移也无显式 postcondition——异步状态可能未生效"
        )
    return issues


# ── GQ2：生成期质量门异常 + 自愈重生辅助 ──────────────────────────────────────

class GoalCoverageError(Exception):
    """计划可证明不完整：目标要求动作而计划无对应 click（硬失败，不返回）。"""

    def __init__(self, missing: list[str], case: DSLCase | None = None):
        self.missing = missing
        self.case = case
        names = "、".join(missing)
        super().__init__(f"目标要求 {names} 动作，但计划中无对应点击步骤")


def _failure_reason_code(exc: Exception) -> str:
    """失败 → 反模式原因码（missing_step / invalid_ref / invalid_structure）。"""
    if isinstance(exc, GoalCoverageError):
        return "missing_step"
    if isinstance(exc, (UnknownTargetRefError, StateGroundingMismatchError,
                        UnreachableObservationError)):
        return "invalid_ref"
    return "invalid_structure"


def _brief_target(target) -> str:
    """target → 短描述（反模式摘要用，不含 value 明文）。"""
    if target is None:
        return "-"
    if hasattr(target, "model_dump"):
        d = target.model_dump()
        return d.get("name") or d.get("text") or d.get("test_id") or d.get("css") or "-"
    if isinstance(target, str):
        return target[:30]
    return "-"


def _plan_summary(case: DSLCase | None, error_info: str) -> str:
    """失败计划的行为摘要（脱敏：action + target 简写 + 错误截断）。"""
    if case is None:
        return f"错误: {error_info[:200]}"
    steps = "; ".join(
        f"{s.action}({_brief_target(s.target)})" for s in case.steps[:8]
    )
    return f"计划: {steps} | 错误: {error_info[:160]}"


def _build_retry_hint(error_info: str, patterns: list[str]) -> str:
    """重生提示：上次失败原因 + 反模式负例（可单测）。"""
    lines = [
        "\n\n【重新规划提示】上一次生成失败，必须修正：",
        f"- {error_info}",
        "同类失败案例（不得重复犯）：",
    ]
    lines += [f"- {p}" for p in patterns] if patterns else ["- （暂无）"]
    lines.append("请重新输出完整修正后的 JSON。")
    return "\n".join(lines)


def _generate_planner_case(
    grounded_prompt: str,
    mode: str = "legacy",
    tables: str | None = None,
) -> tuple[DSLCase, dict]:
    """Planner 生成 + 校验；schema 失败 constrained recovery ×1。

    mode: "refs_only"（grounded，有元素表——只允许 target_ref 定位）
          "legacy"（无探索降级——保留 role/name/scope 生成能力）

    返回 (case, planner_meta)，meta 记录：
      planner_attempts / schema_recovery_used / schema_recovery_success
      initial_validation_errors / planner_recovery_ms / mode

    只对"生成结果不合法"（JSON 解析 / Pydantic ValidationError /
    refs-only 契约违规）做 recovery；LLM API 异常（超时/网络）不在此
    吞掉，让上层 fail safely。
    """
    from pydantic import ValidationError

    refs_only = mode == "refs_only"
    meta = {
        "planner_attempts": 1,
        "schema_recovery_used": False,
        "schema_recovery_success": False,
        "initial_validation_errors": None,
        "planner_recovery_ms": 0,
        "mode": mode,
    }

    def parse_and_validate(text: str) -> DSLCase:
        case = validate_case(_extract_json(text))
        if refs_only:
            check_refs_only(case)
        return case

    raw_text = _call_llm(
        grounded_prompt,
        system_prompt=SYSTEM_PROMPT_REFS_ONLY if refs_only else SYSTEM_PROMPT,
    )
    try:
        return parse_and_validate(raw_text), meta
    except (ValueError, ValidationError) as exc:
        meta["schema_recovery_used"] = True
        meta["planner_attempts"] = 2
        meta["initial_validation_errors"] = _summarize_validation_error(exc)

        recovery_prompt = (
            "上一次 Planner 输出：\n"
            f"{raw_text}\n\n"
            "Schema 校验错误：\n"
            f"{meta['initial_validation_errors']}"
        )
        if refs_only and tables:
            # refs-only 修复需要引用表上下文（补 ref / 改 ref 都只能在表内选）
            recovery_prompt += f"\n\n元素引用表（target_ref 只能从这里选择）：\n{tables}"
        t = perf_counter()
        repaired_text = _call_llm(
            recovery_prompt,
            system_prompt=PLANNER_RECOVERY_SYSTEM_PROMPT_REFS_ONLY
            if refs_only else PLANNER_RECOVERY_SYSTEM_PROMPT,
        )
        meta["planner_recovery_ms"] = int((perf_counter() - t) * 1000)

        case = parse_and_validate(repaired_text)   # 仍失败 → 抛异常（fail safely）
        meta["schema_recovery_success"] = True
        return case, meta


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
    """结构化定位问题（机器可理解，供修复精确定位）。

    类型区分（修复：把"scope 坏了"和"target 歧义"分开）：
      LOCATOR_NOT_FOUND    target 不存在
      AMBIGUOUS_LOCATOR    target 多匹配且无有效 scope
      AMBIGUOUS_SCOPE      scope 锚点选择错误（文本多次/低频实体）
      SCOPE_CARDINALITY_UNKNOWN  弱验证下 scope 计数不确定（warning）
    """
    step_index: int        # 出问题的步骤（1-based，与执行报告一致）
    issue_id: str          # 唯一标识（"step6"）
    type: str
    target: dict           # 原始 target（结构化）
    detail: str            # 人类可读说明
    scope: dict | None = None          # 出问题的 scope（一等公民）
    candidates: list[dict] | None = None   # 歧义候选 [{"candidate_id", "scope_candidates"}]


class RepairItem(BaseModel):
    """单步修复补丁。

    clear_scope 显式清除 scope（修复：scope=None 的"不修改 vs 清空"歧义——
    Step 9 导航级元素 scope 多余时应 clear_scope，而不是替换）。
    """
    step_index: int = Field(ge=1)
    target: Locator | None = None
    scope: Scope | None = None
    clear_scope: bool = False


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


def _scopesnapshot_match(snapshot: str, scope_text: str | None) -> tuple[bool, int]:
    """scope 的三分验证：has_text 文本在快照中出现 0 / 1 / N 次。

    修复：有 scope ≠ 已消歧——scope 值可能是"不存在的商品"（0 次）
    或匹配多个容器（N 次），Preflight 必须和 Runner 的三分法一致。
    匹配语义来自 resolver.snapshot_match（单一事实源，R1）。
    """
    if not scope_text:
        return True, 1   # 无 scope → 视为消歧通过（交由 target 检查）
    return snapshot_match(snapshot, None, scope_text)


def _target_to_dict(t) -> dict:
    """把 target（str / Locator 模型 / dict）统一转成 dict。"""
    if hasattr(t, "model_dump"):
        return t.model_dump()
    if isinstance(t, dict):
        return t
    return {"text": str(t)}


def _preflight_targets(
    case: DSLCase, observations: list[dict], first_pass: bool = True,
) -> list[PreflightIssue]:
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

        parsed = parse_target(t)   # 复用执行器的解析（单一实现）
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

        found, count = snapshot_match(snapshot, role, name)
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
                scope_found, scope_count = _scopesnapshot_match(snapshot, scope_text)
                scope_dict = None
                if step.scope is not None:
                    scope_dict = step.scope.model_dump() if hasattr(step.scope, "model_dump") \
                        else (step.scope if isinstance(step.scope, dict) else {"has_text": str(step.scope)})
                if not scope_found and first_pass:
                    # 首次检测：scope 文本不存在 → 真问题（blocking，Repair 处理）
                    issues.append(PreflightIssue(
                        step_index=index,
                        issue_id=f"step{index}",
                        type="AMBIGUOUS_SCOPE",
                        target=_target_to_dict(t),
                        scope=scope_dict,
                        detail=(
                            f"步骤 {index}: scope 文本不存在（{scope_text!r}）"
                            "——Repair 判断：target 唯一则清空 scope，否则换业务实体锚点"
                        ),
                    ))
                elif not scope_found:
                    # 修复后 recheck：文本不存在可能是智能裁剪/页面状态差异
                    # （如商品名 text 行被限量裁剪）→ warning，运行时兜底
                    issues.append(PreflightIssue(
                        step_index=index,
                        issue_id=f"step{index}",
                        type="SCOPE_CARDINALITY_UNKNOWN",
                        target=_target_to_dict(t),
                        detail=(
                            f"步骤 {index}: scope 文本在快照中不存在（{scope_text!r}）"
                            "——可能是裁剪/状态差异，由运行时定位兜底"
                        ),
                    ))
                elif scope_count > 1:
                    if strong and first_pass:
                        # 第一次检测：文本多次 + target 多匹配 = 锚点可能选错
                        # （如 Rs. 500 多商品同价）→ blocking，触发 Repair
                        # 判断"target 唯一则清空 / 否则换 goal 业务实体"
                        issues.append(PreflightIssue(
                            step_index=index,
                            issue_id=f"step{index}",
                            type="AMBIGUOUS_SCOPE",
                            target=_target_to_dict(t),
                            scope=scope_dict,
                            detail=(
                                f"步骤 {index}: scope 锚点（{scope_text!r}）在对应页面状态出现 "
                                f"{scope_count} 次——Repair 判断：target 唯一则清空，否则换业务实体"
                            ),
                        ))
                    else:
                        # 修复后 recheck / 弱验证：文本 count>1 不直接判真歧义
                        # （同一商品 normal+overlay 双 render 会重复）——
                        # 容器唯一性由运行时联合三分法（含可见性过滤）判定
                        issues.append(PreflightIssue(
                            step_index=index,
                            issue_id=f"step{index}",
                            type="SCOPE_CARDINALITY_UNKNOWN",
                            target=_target_to_dict(t),
                            detail=(
                                f"步骤 {index}: scope 文本在快照中出现 {scope_count} 次"
                                "（可能是同商品双 render），由运行时可见性+联合三分法判定"
                            ),
                        ))
                # scope 唯一 → 消歧通过

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
        parsed = parse_target(target)
        role, name = (parsed.role, parsed.name or parsed.text) if parsed else (None, None)
        if role and name:
            hits = [o["url"] for o in observations
                    if snapshot_match(o["snapshot"], role, name)[0]]
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
                    locator = build_locator_for_count(page, target)
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


def _text_alternative(snapshot: str, name: str) -> Locator | None:
    """NOT_FOUND 的确定性修复：目标名在快照中存在文本 → 换成文本定位。"""
    found, _ = snapshot_match(snapshot, None, name)
    return Locator(text=name) if found else None


def _resolve_ambiguity(goal: str, issue: PreflightIssue) -> tuple[str, dict | None, str | None]:
    """需求明确性判断（区分 Locator / Requirement ambiguity）：
      - goal 命中某候选的业务实体行 → ("auto", 候选, scope)  需求明确，代码直接修
      - 无命中但有可用 scope 行    → ("first", 候选, scope)   需求歧义，确定性选第一个
      - 其他                      → ("llm", None, None)      多个候选匹配需求，LLM 选

    ⚠️ 业务实体优先：goal 匹配前先跳过价格/短行等噪音——
    否则用户需求同时提到商品名和价格（"Blue Top ... Rs. 500"）时，
    候选行顺序靠前的价格会被误选为 scope 锚点（修复实测 bug）。
    """
    # 确定性：goal 子串命中某个候选的【业务实体】行（跳过价格等噪音）
    for cand in (issue.candidates or []):
        for scope in cand.get("scope_candidates", []):
            scope = scope.strip()
            if not scope or len(scope) < 2 or PRICE_RE.fullmatch(scope):
                continue   # 跳过价格/短行（Rs. 500 等易重复文本）
            if scope.lower() in goal.lower():
                return "auto", cand, scope

    # 需求歧义：取第一个候选的第一个"好" scope 行（同样跳过价格）
    for cand in (issue.candidates or []):
        scope = choose_scope_text(cand.get("scope_candidates", []))
        if scope:
            return "first", cand, scope

    return "llm", None, None


def _scope_patch(issue: PreflightIssue, scope_text: str) -> RepairItem:
    """由最终 scope 文本生成 patch（代码构造，LLM 不参与）。

    Invariant：导航 target 禁止商品/业务 scope——即使修复流程想加，
    也改为 clear_scope（导航 locator 的消歧走导航语义，不是商品 scope）。
    """
    if is_navigation_target(issue.target):
        return RepairItem(step_index=issue.step_index, clear_scope=True)
    return RepairItem(
        step_index=issue.step_index,
        target=Locator(**issue.target),
        scope=Scope(has_text=scope_text),
    )


def _normalize_invalid_scopes(case: DSLCase) -> DSLCase:
    """导航 target 的 scope 一律清空（invariant，不依赖 Repair round）。

    无论 Planner 生成还是 Repair 产生——导航元素（Cart/Products/Home）
    与商品/价格 scope 语义不兼容，是系统级 locator invariant。
    """
    changed = False
    for step in case.steps:
        if (step.scope is not None and step.target is not None
                and is_navigation_target(_target_to_dict(step.target))):
            step.scope = None
            changed = True
    if changed:
        return validate_case(case.model_dump())
    return case


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    """保序去重（scope 候选：同一商品 normal+overlay 双 render 会重复）。"""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = " ".join(value.split()).strip()
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _goal_match_anchor(goal: str, anchors: list[str]) -> str | None:
    """从锚点中选"用户明确指定的业务实体"（跳过价格/短行/动作文本）。

    修复：goal 同时含商品名和价格时（"Blue Top ... Rs. 500"），
    价格行不能因顺序靠前被误选——业务实体优先。
    """
    for anchor in anchors:
        if not anchor or len(anchor) < 2 or PRICE_RE.fullmatch(anchor):
            continue
        if anchor.lower() in goal.lower():
            return anchor
    return None


def _inspect_scoped_steps(
    scoped_items: list[tuple[int, dict]],
    urls: list[str], login_inputs: dict | None,
    observations: list[dict] | None,
) -> dict[int, dict]:
    """一次浏览器会话验证所有 scoped 步骤（性能：不 per-step launch）。

    判断语义与 Runner 联合三分法一致（不是文本 count）：
      - target 不带 scope 精确 count == 1 → scope 多余 → {"action": "remove_scope"}
      - 否则提取去重锚点 → {"action": "replace_scope", "anchors": [...]}
      （goal 业务实体匹配在调用方做）

    返回 {step_index: {"action": ..., "anchors": [...]}}。
    """
    from playwright.sync_api import sync_playwright

    pending = dict(scoped_items)
    result: dict[int, dict] = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(10000)
            try:
                # 快照筛选：所有待检查 target 的 URL 并集
                target_urls: list[str] = []
                for _, target in scoped_items:
                    parsed = parse_target(target)
                    role, name = (parsed.role, parsed.name or parsed.text) if parsed else (None, None)
                    if observations and role and name:
                        hits = [o["url"] for o in observations
                                if snapshot_match(o["snapshot"], role, name)[0]]
                        target_urls.extend(hits)
                if not target_urls:
                    target_urls = urls
                seen_urls: set[str] = set()

                for url in target_urls:
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    try:
                        page.goto(url, wait_until="domcontentloaded")
                        page.wait_for_timeout(500)
                    except Exception:
                        continue
                    if login_inputs and _try_login(page, login_inputs):
                        try:
                            page.goto(url, wait_until="domcontentloaded")
                            page.wait_for_timeout(500)
                        except Exception:
                            pass

                    for idx, target in list(pending.items()):
                        if idx in result:
                            continue
                        # 导航级 target：商品/价格 scope 一律禁止 → remove_scope
                        # （deterministic 规则，修复 Step 9 Cart→Blue Top 误修）
                        if is_navigation_target(target):
                            result[idx] = {
                                "action": "remove_scope",
                                "anchors": [],
                                "reason": "navigation_target_cannot_use_product_scope",
                            }
                            continue
                        locator = build_locator_exact_first(page, target)
                        if locator is None:
                            continue
                        count = locator.count()
                        if count == 0:
                            continue   # 该页面无此 target，留给其他页面
                        if count == 1:
                            result[idx] = {"action": "remove_scope", "anchors": []}
                            continue
                        # target 多匹配：提取去重锚点（replace_scope 候选）
                        anchors: list[str] = []
                        for i in range(min(count, 6)):
                            try:
                                node = locator.nth(i)
                                container = node.locator(
                                    "xpath=ancestor::*[self::li or self::article or @data-testid][1]"
                                )
                                cc = container.count()
                                if cc == 0:
                                    container = node.locator("xpath=../..")
                                    cc = container.count()
                                raw = container.inner_text().strip() if cc > 0 else ""
                                node_text = node.inner_text().strip()
                                for line in raw.splitlines():
                                    line = line.strip()
                                    if line and line != node_text:
                                        anchors.append(line)
                            except Exception:
                                pass
                        result[idx] = {
                            "action": "replace_scope",
                            "anchors": _dedupe_preserve_order(anchors),
                        }
            finally:
                browser.close()
    except Exception:
        pass
    return result


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
    """程序本地应用 patch：只替换 patch 中指定的步骤，其余分毫不动。

    clear_scope=True → 显式清除 scope（Step 9 类：导航级元素 scope 多余）。
    """
    applied = 0
    for rep in patch.repairs:
        idx = rep.step_index - 1
        if not (0 <= idx < len(case.steps)):
            continue
        step = case.steps[idx]
        if rep.target is not None:
            step.target = rep.target
        if rep.clear_scope:
            step.scope = None
        elif rep.scope is not None:
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
        # 修复有效性统计（修复 repairs_applied=6 无效果的误导）：
        # effective = blocking 数量变化，而不是"写了多少 patch"
        "issues_before": 0,
        "issues_after": 0,
        "effective_repairs": 0,
    }

    # Round1 提取过的 candidates 按 issue_id 保留——
    # run_preflight() 会重建 issue 对象（candidates=None），
    # 不回填会导致 Round2 过滤条件 i.candidates 为空而进不去（修复）
    known_candidates: dict[str, list[dict]] = {}

    first_pass = True

    def run_preflight() -> list[PreflightIssue]:
        nonlocal first_pass
        t = perf_counter()
        result = _preflight_targets(case, observations, first_pass=first_pass)
        first_pass = False   # 首次之后的 recheck 不再触发"锚点选错" blocking
        for iss in result:
            if iss.issue_id in known_candidates:
                iss.candidates = known_candidates[iss.issue_id]
        return result

    t0 = perf_counter()
    issues = run_preflight()
    stats["timings"]["initial_check_ms"] = int((perf_counter() - t0) * 1000)
    stats["issues_before"] = len(issues)
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
        # AMBIGUOUS_SCOPE 统一由 Round 1.5（Browser-backed）处理——
        # 浏览器判定 target 唯一性 + goal 业务实体锚点
        elif issue.type == "LOCATOR_NOT_FOUND":
            parsed = parse_target(issue.target)
            name = (parsed.name or parsed.text) if parsed else None
            if name:
                alt = _text_alternative(multi_snapshot, name)
                if alt:
                    round1_patches.append(RepairItem(step_index=issue.step_index, target=alt))

    # ── Round 1.5：Browser-backed scope 确认（静态快照可能漏）────────
    # 快照验证通过 ≠ 真实 DOM 唯一（如 Rs. 500 在快照出现 1 次、
    # 执行时 3 个商品同价）——一次浏览器会话核实所有 scoped 步骤：
    #   target 不带 scope 唯一 → remove_scope（Step 9 Cart）
    #   target 多匹配 → 提取锚点，goal 业务实体匹配替换（Step 8 Add to cart）
    # 修复 handled_steps 粒度：target 修复 ≠ scope 已处理——
    # 只有"已做过 scope 操作"（replace/clear）的步骤才跳过 Round 1.5
    scope_handled_steps = {
        p.step_index for p in round1_patches
        if p.scope is not None or p.clear_scope
    }
    scoped_items = [
        (index, _target_to_dict(step.target))
        for index, step in enumerate(case.steps, start=1)
        if step.scope is not None and index not in scope_handled_steps
        and step.target is not None
    ]
    if scoped_items:
        inspection = _inspect_scoped_steps(
            scoped_items, urls, login_inputs, observations,
        )
        for index, info in inspection.items():
            if info["action"] == "remove_scope":
                round1_patches.append(RepairItem(step_index=index, clear_scope=True))
            else:
                scope_text = _goal_match_anchor(goal, info.get("anchors", []))
                if scope_text:
                    step = case.steps[index - 1]
                    current_scope = step.scope.model_dump().get("has_text") \
                        if hasattr(step.scope, "model_dump") else str(step.scope)
                    if scope_text != current_scope:
                        round1_patches.append(RepairItem(
                            step_index=index, scope=Scope(has_text=scope_text),
                        ))

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
                        scope_text = choose_scope_text(candidate.get("scope_candidates", []))
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
    #   blocking_issues = 歧义/scope 问题未消（执行必然失败）
    #   warnings        = 非阻塞：快照未验证到（可能是操作后状态变化）、
    #                      scope 跨页面计数不确定（cardinality unknown）
    stats["issues_after"] = len(issues)
    stats["effective_repairs"] = stats["issues_before"] - stats["issues_after"]
    stats["blocking_issues"] = [
        asdict(i) for i in issues
        if i.type in {"AMBIGUOUS_LOCATOR", "AMBIGUOUS_SCOPE"}
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
    """步骤 target 的归一化键（用于判断 wait_for 与断言是否同一元素）。

    refs-only 模式：target 由 Compiler 在归一化之后才填入——
    此处必须用 target_ref 作为键（否则 ref 不同的步骤会被误判为同一元素，
    断言去重误删）。"""
    if step.target_ref:
        return f"ref:{step.target_ref}"
    t = step.target
    if t is None:
        return ""
    if hasattr(t, "model_dump"):
        d = t.model_dump()
        return f"{d.get('role') or ''}:{d.get('name') or ''}:{d.get('text') or ''}"
    if isinstance(t, dict):
        return f"{t.get('role') or ''}:{t.get('name') or ''}:{t.get('text') or ''}"
    return str(t)


def _normalize_steps(case: DSLCase) -> tuple[DSLCase, list[int]]:
    """生成后归一化（Planner 输出波动 → 最终 DSL 稳定）：

      1. 只保留最后一个断言步骤作为最终验证，删除前面多余的断言
      2. 删除与最终断言同一元素的冗余 wait_for（重复等待）
      3. 重新校验（步骤变化后保证仍是合法 DSL）

    返回 (case, removed_assertions)：被删除的断言步骤号（1-based）——
    修复 #20：不静默删，记录供 meta 展示（多目标用例时提示用户检查）。
    """
    steps = list(case.steps)
    removed: list[int] = []
    if len(steps) <= 1:
        return case, removed

    assert_indices = [i for i, s in enumerate(steps) if s.action in _ASSERT_ACTIONS]
    if not assert_indices:
        return case, removed

    # 修复语义冲突（"只保留最后断言" vs "用户明确要求多个验证"）：
    # 只删除【完全重复】的断言（同 action+target+value）——
    # 不同语义的显式验证必须保留（稳定性不能以删除用户验证为代价）。
    seen: set[tuple] = set()
    normalized: list[DSLStep] = []
    for i, s in enumerate(steps, start=1):
        if s.action in _ASSERT_ACTIONS:
            key = (s.action, _target_key(s), s.value)
            if key in seen:
                removed.append(i)
                continue
            seen.add(key)
        normalized.append(s)

    # 保守删除冗余 wait_for：只删"紧邻最后一个断言前、且同 target"的。
    # 前提：断言类动作（expect）自带 Playwright 自动等待；
    # 隔着其他步骤的 wait_for 可能有业务意义（如等待跳转完成），不删。
    if normalized and normalized[-1].action in _ASSERT_ACTIONS:
        final_key = _target_key(normalized[-1])
        if len(normalized) >= 2:
            prev = normalized[-2]
            if prev.action == "wait_for" and _target_key(prev) == final_key:
                del normalized[-2]

    case.steps = normalized
    return validate_case(case.model_dump()), removed   # 重新校验（安全边界）


# ── 多页面快照文本（探索结果 → Planner 可读上下文）──────────────────────────────

def _sanitize_for_cache(explore_result: dict, runtime_inputs: dict) -> dict:
    """缓存前脱敏：history 的 value 与 observations 快照中的真实凭据
    都还原为 ${var} 占位。

    缓存会持久化到磁盘——Secrets 边界必须保持：
    用户名/密码绝不进入缓存文件（登录后页面 header 会显示用户名，
    快照文本必须一并脱敏；对 Preflight 影响极小——target 很少用用户名文本）。
    """
    result = json.loads(json.dumps(explore_result))   # 深拷贝
    for h in result.get("history", []):
        v = h.get("value")
        if v and runtime_inputs:
            for key, real in runtime_inputs.items():
                if real and real in v:
                    h["value"] = v.replace(real, f"${{{key}}}")
    for obs in result.get("observations", []):
        snap = obs.get("snapshot", "")
        for key, real in runtime_inputs.items():
            if real:
                snap = snap.replace(real, f"${{{key}}}")
        obs["snapshot"] = snap
    return result


def _pages_to_text(pages: list[dict]) -> str:
    """把探索到的 observation 快照合并成 Planner 可读文本（每页分段标记）。

    分段标记用 observation id（obs1/obs2/...）——Planner 生成 DSL 时
    用 observation_ref 引用（Commit 2 接入）。

    G1：每页附 state-scoped 元素表（refs）——Planner 可输出 target_ref
    引用系统观察到的真实元素（obs3:e17），而非自由构造 role/name/scope。
    """
    sections = []
    for page in pages:
        obs_id = page.get("id", f"obs{len(sections) + 1}")
        title = page.get("title") or ""
        section = (
            f"[{obs_id}] {page['url']}"
            + (f"（标题: {title}）" if title else "")
            + f"\n{page['snapshot']}"
        )
        elements = page.get("elements") or []
        if elements:
            lines = [f"      {e['ref']}: {e.get('role', '')} \"{e.get('name', '')}\""
                     if "role" in e else f"      {e['ref']}: text \"{e.get('text', '')}\""
                     for e in elements[:30]]
            section += "\n   元素引用表（target_ref 只能从这些 ref 中选择）:\n" + "\n".join(lines)
        sections.append(section)
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
                # 缓存门槛（GQ 决策 2）：done=True，或已执行 ≥2 步——
                # saucedemo 的 done=False/steps=4 探索产出了 7/7 计划（好探索
                # 被拒缓存是浪费）；steps=1 的浅探索仍拒缓存（历史毒化案例）。
                if explore_result.get("done") or explore_result.get("steps_used", 0) >= 2:
                    cache_save(entry_url, auth_profile, _sanitize_for_cache(explore_result, runtime_inputs))
            except Exception:
                # R4（评审）：探索失败不再静默降级 legacy——Grounded mode
                # 下 Explore fail → generate fail（fail honestly）。
                # 维护两条生成路径（grounded/legacy）的隐形复杂度大于
                # 降级带来的可用性；无 entry_url 的 legacy 在上层显式处理。
                raise
    explore_ms = int((perf_counter() - t_explore) * 1000)

    # E1（评审收紧）：探索异常不得静默冒充 grounded 成功——显式暴露
    # degraded 标记与错误，前端/诊断能区分"真 grounded"与"legacy 降级"。
    # （降级仍保留：探索失败不阻断生成主链路，但 meta 必须说明。）
    explore_error: str | None = None
    if entry_url and explore_result is None and pages:
        explore_error = "explore 未执行（缓存为空且入口缺失）"
    elif entry_url and explore_result is None:
        explore_error = "探索异常（已降级 legacy 生成，见 server log）"

    # ── 阶段 3：组装 prompt（多页面结构 + 探索路径）→ Planner 生成 ──
    # E1：Planner 永远只看 reachable observations（孤儿状态只留诊断，
    # 不进 Planner evidence——refs-only 定义：Planner 只能引用成功
    # 转移图可证明的状态。之前 retry 时才过滤，首次生成也能引用孤儿）。
    if explore_result is not None:
        sg = StateGraph.from_explore_result(explore_result)
        reach = _reachable_observations(sg)
        if reach:
            pages = [p for p in pages if p["id"] in reach]
    multi_snapshot = _pages_to_text(pages) if pages else None
    if multi_snapshot:
        # P0-3：canonical path 只来自成功转移边（State Graph transitions），
        # 失败动作单独标注为负例——Planner 不会学到"点击文本超时 →
        # 进入 obs4"的错误因果（temporal attribution bug：失败动作的
        # 15s 超时窗口恰好吞掉了前一个动作的延迟状态）。
        tr = (explore_result or {}).get("transitions") or []
        path_lines = [
            f"- {t['from']} --{t['action']} {t['target_ref']}--> {t['to']}"
            for t in tr
            if t.get("from") and t.get("to") and t.get("from") != t.get("to")
        ]
        fail_lines = [
            f"- {h.get('action')} {h.get('target_ref')} 失败:"
            f" {(h.get('error') or '')[:70]}"
            for h in (explore_result or {}).get("history", [])
            if h.get("error") and h.get("action") != "decision_rejected"
        ]
        grounded_prompt = (
            f"目标页面入口: {entry_url}\n\n"
            f"已验证状态转移（State Graph 成功边，规划路径只能沿这些边）:\n"
            + ("\n".join(path_lines) if path_lines else "- (无)")
            + ("\n\n失败动作（不要模仿，这些动作未产生有效状态变化）:\n"
               + "\n".join(fail_lines) if fail_lines else "")
            + "\n\n各页面真实结构（ARIA snapshot）：\n\n"
            + multi_snapshot
            + "\n\n用户测试需求（已脱敏，密码等敏感信息已替换为 ${var} 占位符）: "
            + explore_goal
            + "\n\n规则："
            "1. 用户提供的测试数据用 ${var} 占位并声明在 input_contract："
            "需求中给出的值填 default；密码等敏感信息 secret=true 且 default=null；"
            "2. （G3 refs-only）定位元素一律通过 target_ref 引用元素引用表中的"
            "系统观察元素（如 obs3:e17）——target_ref 只能从元素引用表选择，"
            "禁止编造；禁止生成 target/scope 等定位字段（locator 由系统"
            "根据 ref 确定性编译，不要输出 role/name/text/css/test_id）；"
            "3. 每个步骤必须设置 observation_ref，且只能从页面分段标记"
            "（[obs1] [obs2] ...）中选择——禁止创造不存在的 observation_ref；"
            "target_ref 的 obs 前缀必须与 observation_ref 一致；"
        )
    else:
        grounded_prompt = explore_goal   # 无快照时同样用脱敏后的需求

    t_planner = perf_counter()
    planner_mode = "refs_only" if multi_snapshot else "legacy"

    # ← grounding 验证：observation_ref 必须来自系统提供的真实 id
    #（不靠 Prompt——代码校验；非法 ref 清空为 None，降级弱验证）
    valid_refs = {obs["id"] for obs in pages}

    # ── 计划生成单元（GQ2 重生循环的可重试体）──────────────────────────
    # planner → 归一化 → scope 清理 → observation_ref 清理 → 编译 →
    # grounding → 目标覆盖质量门。有探索证据时：target_ref → target
    # 确定性编译，跨状态/编造 ref 执行前拒绝（放在 Preflight 之前：
    # grounding 错位的计划不值得花浏览器轮次修复）。
    def attempt(prompt: str, tables: str | None = None):
        case, planner_meta = _generate_planner_case(
            prompt, mode=planner_mode, tables=tables or multi_snapshot,
        )   # ← Schema Recovery ×1（refs-only 模式含契约违规修复）
        case, removed = _normalize_steps(case)   # ← 计划归一化 + 记录删除
        case = _normalize_invalid_scopes(case)   # ← 导航 scope invariant
        if valid_refs:
            for step in case.steps:
                if step.observation_ref and step.observation_ref not in valid_refs:
                    step.observation_ref = None
        compile_stats: dict = {}
        if explore_result is not None:
            state_graph = StateGraph.from_explore_result(explore_result)
            case = compile_targets(case, state_graph, stats=compile_stats)   # ← R1+I1 编译
            validate_state_grounding(case, state_graph)        # ← G3 拒绝
        missing = _check_goal_coverage(explore_goal, case)
        if missing:
            raise GoalCoverageError(missing, case)   # ← GQ2 硬失败（可证明不完整）
        return case, planner_meta, removed, compile_stats

    # ── GQ2 自愈重生（bounded ×1；网络异常不捕获，仍 fail safely）──────
    # 失败 → 记录反模式 → 负例 few-shot + 上次错误注入重生 prompt
    # （复用探索结果，不重新探索）→ 二次失败 → 异常冒出 → api 400。
    generation_retries = 0
    anti_pattern_used = 0
    try:
        case, planner_meta, removed_assertions, compile_stats = attempt(grounded_prompt)
    except (GoalCoverageError, UnknownTargetRefError, StateGroundingMismatchError,
            UnreachableObservationError, ValueError, ValidationError) as exc:
        reason = _failure_reason_code(exc)
        failed_case = exc.case if isinstance(exc, GoalCoverageError) else None
        anti_patterns.record(reason, _plan_summary(failed_case, str(exc)))
        patterns = anti_patterns.list_for(reason)
        anti_pattern_used = len(patterns)
        generation_retries = 1
        # P0-4：grounding 错位时告诉重生 Planner"当前推导状态 + 允许引用
        # 哪个状态"——Planner 在 expected 状态内重选 ref（不跨状态/不编造）。
        # 不自动改 target_ref：换 ref 是语义决策，不是字符串替换。
        extra = ""
        retry_tables = None
        if isinstance(exc, (StateGroundingMismatchError, UnreachableObservationError)):
            extra = (
                f"\n- 该步骤执行时应处于状态 {exc.expected if isinstance(exc, StateGroundingMismatchError) else exc.obs_id}："
                "target_ref 只能引用可达状态（入口沿转移边可到达）的元素，"
                "不可引用孤儿/已离开状态；若目标元素在这些状态中不存在，"
                "请调整步骤设计"
            )
            # 关键：重生时只提供可达 observation 的快照——Planner 看不到
            # 不可达状态（如孤儿 obs5），物理上无法引用它（BFC 实测：
            # 只靠 prompt 提示不够，Planner 会因目标完整性压力继续引用）。
            if explore_result is not None:
                sg = StateGraph.from_explore_result(explore_result)
                reach = _reachable_observations(sg)
                retry_pages = [p for p in pages if p["id"] in reach]
                retry_tables = _pages_to_text(retry_pages) if retry_pages else None
        case, planner_meta, removed_assertions, compile_stats = attempt(
            grounded_prompt + _build_retry_hint(str(exc), patterns) + extra,
            tables=retry_tables,
        )
    planner_ms = int((perf_counter() - t_planner) * 1000)

    meta = {
        "generation_mode": "refs_only" if explore_result is not None else "legacy_fallback",
        "explore_error": explore_error,
        "snapshot_used": bool(multi_snapshot),
        "entry_url": entry_url,
        "cache_hit": cache_hit,      # Speed v1：探索结果是否命中缓存
        "normalize_removed_assertions": removed_assertions or None,   # #20：不静默删
        "planner": planner_meta,     # Schema Recovery 统计（attempts/recovery）
        # GQ2：自愈重生统计（0 = 一次通过；1 = 带反模式负例重来）
        "generation_retries": generation_retries,
        "anti_pattern_used": anti_pattern_used,
        "explore": {
            "pages_visited": len(pages),
            "steps_used": (explore_result or {}).get("steps_used", 0),
            "llm_calls": (explore_result or {}).get("llm_calls", 0),
            "done": (explore_result or {}).get("done", False),
            "transitions": (explore_result or {}).get("transitions", []),   # G2：状态转移边
        } if explore_result else None,
        "preflight": None,           # Preflight 校验结果（有多页面快照时才执行）
        # G3/R1 指标（ROADMAP §8）：ref 校验覆盖 + 确定性编译产出
        # （被拒绝的计划不会走到这里）
        "grounding": {
            "ref_steps_checked": sum(1 for s in case.steps if s.target_ref),
            "compiled_targets": sum(
                1 for s in case.steps if s.target_ref and s.target is not None
            ),
            # I1：实例身份编译产出（scope 消歧 / 容器外无锚点清单）
            "scoped_compiled": compile_stats.get("scoped_compiled", 0),
            "unscoped_duplicates": compile_stats.get("unscoped_duplicates") or None,
        },
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

    # ── GQ：生成期目标覆盖警告（fail-open，只提示不硬失败）───────────
    # 探索不完整 / 目标动作缺失 / 断言前缺等待 → 前端醒目提示，
    # 避免"看似能跑"的静默不完整计划（9/10 案例：断言了可见性却漏点击）。
    meta["goal_coverage"] = {
        "exploration_incomplete": bool(pages)
        and not (explore_result or {}).get("done", False),
        "missing_actions": _check_goal_coverage(explore_goal, case) or None,
        "unverified_postconditions": detect_missing_postconditions(
            case, pages, (explore_result or {}).get("transitions", []),
        ) or None,
    }

    # Speed v1：生成链路计时（定位耗时构成，决定下一刀砍哪）
    meta["timings"] = {
        "url_resolve_ms": url_resolve_ms,
        "explore_ms": explore_ms,
        "explore_detail": (explore_result or {}).get("timings"),
        "planner_ms": planner_ms,
        "preflight_ms": preflight_ms,
    }

    return case, meta
