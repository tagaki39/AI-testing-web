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
from dsl import DSLCase, validate_case
from explore_cache import is_cacheable_trace, load as cache_load, save as cache_save
from goal_contract import build_goal_contract
from explore import (
    GOAL_ACTION_PATTERNS, _ACTION_KEYWORDS, explore,
    missing_verified_goal_actions,
)
import anti_patterns
from grounding import (
    StateGraph, StateGroundingMismatchError, UnknownTargetRefError,
    UnreachableObservationError, _reachable_observations,
    validate_state_grounding,
)
from locator.resolver import is_navigation_target, parse_target

# ── 配置（环境变量）───────────────────────────────────────────────────────────
# os.getenv("名字", 默认值)：读环境变量，没设置就用默认值。
# .env 文件的值由 main.py 在启动时灌入 os.environ（见 main.py 顶部）。

class ExplorationIncompleteError(Exception):
    """探索未验证目标动作（history 点过 ≠ 成功状态迁移）——不进入 Planner。
    S1 第二防线：目标要求 verified outcome 但探索未形成对应转移。"""


class OutputBudgetExceededError(Exception):
    """LLM 输出超过 max_tokens 预算（finish_reason=length）——输出失控。
    S4：safety cap，不做 schema recovery（截断 JSON 修复无意义）。"""


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
根据用户描述的自然语言测试需求，从系统提供的【已验证状态转移】和【元素引用表】中选择，输出一个 JSON 对象。示例：

{
  "name": "前两个商品加入购物车并验证",
  "description": "筛选品牌后加购两个商品，进入购物车验证",
  "base_url": "https://xxx.com",
  "input_contract": [
    {"key": "username", "type": "string", "required": true, "secret": false, "default": "standard_user"}
  ],
  "transition_refs": ["t1", "t2", "t3", "t4"],
  "assertions": [
    {"action": "assert_visible", "target_ref": "obs3:e11"},
    {"action": "assert_text", "value": "Blue Top"}
  ]
}

规则：
1. transition_refs（状态变化步骤，最重要）：
   - 从系统提供的"已验证状态转移"表中选择（t1/t2/...，格式 t1: obs1 --click obs1:e29--> obs2）
   - 按执行顺序排列；系统沿已验证边确定性展开为 action/target_ref/observation_ref
   - 你不需要（也无权）推导状态机——只做语义选择：哪些已验证转移属于用户目标
   - 数量要求（"前两个商品"）：选择不同业务实体的转移（不同 t 对应不同目标）
2. assertions（验证步骤）：
   - 追加在 transition_refs 之后执行（observation_ref 由系统自动设置为当前状态）
   - 元素级断言（assert_visible/assert_text 带 target_ref）：只能引用【当前执行位置】状态的元素
   - 页面级断言（assert_text 整页 / assert_url）：不需要 target_ref
   - 验证策略：登录/跳转 → assert_url 或目标页关键元素；文本/价格 → assert_text；元素出现 → assert_visible
3. 变量：
   - 所有可变测试输入必须使用 ${var}，每个变量必须声明在 input_contract
   - secret=true → default 必须为 null（执行时本地注入）
   - 非敏感变量只有上下文明确提供 default 时才能填写；不得猜测真实值
4. 业务动作覆盖（最重要）：
   - 用户目标中要求的每个业务动作都必须对应 transition_refs 中的转移——
     目标说"加入购物车"就必须有加购元素的转移，"登录"就必须有登录转移
   - 禁止只选导航转移而跳过目标要求的业务动作
5. 输出紧凑性（硬约束）：
   - 输出必须是【单个 JSON 对象】本身，禁止任何前置/后置文本、代码块标记
   - 禁止复制、引用或重述页面结构、元素引用表、状态转移表、ARIA snapshot 内容
   - 输出通常 <60 行；禁止输出任何 action 为 click/fill/goto 的步骤对象
     （状态变化由 transition_refs 表达，你只输出转移编号和断言）"""


# ── LLM 调用（标准库实现，无外部依赖）──────────────────────────────────────────

def _call_llm(user_prompt: str, system_prompt: str | None = None,
              timeout: float = 60, max_tokens: int = 1500) -> str:
    """调用 DeepSeek chat completions API，返回文本内容。

    这是最原始的 HTTP POST 请求，拆解每一步：
      1. 构造 payload（JSON 请求体）：model + messages + temperature
      2. urllib.request.Request：封装 URL、请求体、请求头
      3. urlopen()：真正发出网络请求（默认 timeout=60 秒上限；
         Explorer 单次决策传 20s——决策不是长文生成）
      4. 解析响应 JSON，取 choices[0].message.content（LLM 的回答文本）

    请求体格式是 OpenAI 兼容规范（DeepSeek 兼容它）：
      messages = [system（角色设定）] + [user（用户输入）]

    参数 system_prompt：可覆盖默认 SYSTEM_PROMPT（阶段 1 提取 URL 时用专用 prompt）
    timeout：网络超时秒数（P0：探索决策 20s / Planner 60s）
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
        "max_tokens": max_tokens,   # S4：safety cap（正常输出 <1KB；防 output runaway）
    }
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",            # DeepSeek 的 OpenAI 兼容端点
        data=json.dumps(payload).encode("utf-8"),  # dict → JSON 字符串 → 字节
        headers={
            "Content-Type": "application/json",    # 告诉服务器：请求体是 JSON
            "Authorization": f"Bearer {API_KEY}",  # 认证：Bearer token 标准格式
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    data = json.loads(body.decode("utf-8"))
    choice = data["choices"][0]
    # S4：输出超预算（finish_reason=length）→ 明确失败（output runaway
    # 不做 schema recovery——截断 JSON 的修复没有意义）
    if choice.get("finish_reason") == "length":
        raise OutputBudgetExceededError(
            f"LLM 输出超过预算 {max_tokens} tokens（finish_reason=length）"
            "——输出失控，不做修复，请重试")
    return choice["message"]["content"]


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
    # "账号 test1 密码 147258"（无斜杠格式）——用户名 + 密码一起提取，
    # 否则 username 残留进 LLM 上下文（LLM 输出真实值 → Data Grounding 拒）
    re.compile(
        r'(?:账号|用户名|login\s+with|using|用|使用)\s*[:：]?\s*'
        r'([^\s，,。;；]+)\s+(?:密码|口令)[：:\s]+([^\s，,。;；]+)',
        re.IGNORECASE,
    ),
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
    # 注意：
    #   pattern[0] = "账号 X 密码 Y"（无斜杠）：group(1)=用户名, group(2)=密码
    #   pattern[3] = "用户名 / 密码"（斜杠）：group(1)=用户名, group(2)=密码
    #   其余写法（密码:/password:/口令）只有密码，取 group(1)
    m = _PASSWORD_PATTERNS[0].search(redacted)
    if m:
        # 账号 X 密码 Y：group(1)=用户名（提取），group(2)=密码
        username = m.group(1)
        if not username.startswith("${"):
            runtime["username"] = username
            redacted = redacted.replace(username, "${username}")
        secret = m.group(2)
    else:
        m = _PASSWORD_PATTERNS[1].search(redacted)
        if m:
            secret = m.group(1)
        else:
            m = _PASSWORD_PATTERNS[2].search(redacted)
            if m:
                secret = m.group(1)
            else:
                m = _PASSWORD_PATTERNS[3].search(redacted)
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
    # 真实网站验证（xywhaigc）："login," 尾随逗号被当路径——
    # goto 到非标准路径触发 SPA guard 异常重定向 → redirect=/login →
    # 登录成功 push 回 /login → 404。提取后清理尾随标点。
    value = value.rstrip(" ,.;:，。；：")
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
        # 去空白（与完成性校验同口径：真实网站"登 录"按钮 name 带 CSS 间距）
        return f"{parsed.name or ''} {parsed.text or ''}".strip().casefold().replace(" ", "")

    for label, pattern in GOAL_ACTION_PATTERNS.items():
        if not pattern.search(goal):
            continue
        keywords = _ACTION_KEYWORDS[label]
        covered = any(
            step.action == "click" and step.target is not None
            and any(k.replace(" ", "") in _step_text(step) for k in keywords)
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


def _build_retry_hint(error_info: str) -> str:
    """重生提示：上次失败原因（R4：不做负例 few-shot 注入——
    格式错 retry、grounding 错 replan、仍错 fail honestly）。"""
    return (
        "\n\n【重新规划提示】上一次生成失败，必须修正：\n"
        f"- {error_info}\n"
        "请重新输出完整修正后的 JSON。"
    )


def _expand_plan_schema(case_dict: dict,
                        verified_edges: list[dict],
                        observations: list[dict] | None = None,
                        entry_url: str | None = None) -> dict:
    """S3：Planner 输出 transition_refs + assertions → DSL steps 确定性展开。

    Planner 不再输出状态变化步骤的详细结构（LLM 无空间生成 28KB 回吐）：
      - transition_refs：沿 verified edges 顺序展开（action/target_ref/
        observation_ref 由边决定，State Cursor 推进）
      - assertions：追加到末尾，observation_ref 由当前 cursor 自动赋值，
        target_ref 必须属于当前状态（跨状态引用结构上不可能）
      - goto 步骤自动注入（entry_url，observation_ref 匹配入口 obs）

    未知 transition_ref / cursor 错位 / 断言跨状态 → ValueError
    （schema recovery 干净重生，不靠 prompt 提醒）。
    """
    if not verified_edges:
        return case_dict
    refs_by_obs: dict[str, set[str]] = {}
    if observations:
        for o in observations:
            refs_by_obs[o["id"]] = {e["ref"] for e in o.get("elements", [])}
    index = {f"t{i + 1}": t for i, t in enumerate(verified_edges)}

    def match_url(value) -> str | None:
        if not value:
            return None
        url = str(value).strip().rstrip("/")
        for o in observations or []:
            if o.get("url") == url or (o.get("url") or "").rstrip("/") == url:
                return o["id"]
        return None

    steps: list[dict] = []
    cursor: str | None = None
    for tref in case_dict.pop("transition_refs", []):
        edge = index.get(tref)
        if edge is None:
            raise ValueError(f"未知 transition_ref {tref}（verified 边外）")
        if cursor is not None and edge["from"] != cursor:
            raise ValueError(
                f"TRANSITION_OUT_OF_ORDER: {tref} 起点 {edge['from']} "
                f"≠ 当前状态 {cursor}（路径沿已验证转移边推进）")
        # S1：pre_actions（该转移前的成功非转移动作，如登录 fill）——
        # 探索期确定性恢复并绑定到边，Planner 不生成
        for pa in edge.get("pre_actions") or []:
            steps.append({
                "action": pa["action"],
                "target_ref": pa["target_ref"],
                "value": pa.get("value"),
                "observation_ref": edge["from"],
            })
        steps.append({
            "action": edge["action"],
            "target_ref": edge["target_ref"],
            "observation_ref": edge["from"],
        })
        cursor = edge["to"]

    # goto 注入（入口，observation_ref 匹配入口 obs）
    if steps and entry_url:
        steps.insert(0, {
            "action": "goto",
            "value": entry_url,
            "observation_ref": match_url(entry_url),
        })

    # assertions：observation_ref 由 cursor 自动赋值；元素级引用必须
    # 属于当前状态（跨状态引用在结构上不可能）
    for a in case_dict.pop("assertions", []):
        if cursor is not None:
            a["observation_ref"] = cursor
            tref2 = a.get("target_ref")
            if tref2:
                allowed = refs_by_obs.get(cursor, set())
                if allowed and tref2 not in allowed:
                    raise ValueError(
                        f"ASSERTION_REF_OUT_OF_CURRENT_STATE: 断言引用 "
                        f"{tref2} 不属于当前状态 {cursor}（断言只能引用"
                        "当前状态元素；页面级断言可不提供 target_ref）")
        steps.append(a)
    case_dict["steps"] = steps
    return case_dict


def _generate_planner_case(
    grounded_prompt: str,
    mode: str = "legacy",
    tables: str | None = None,
    verified_edges: list[dict] | None = None,
    observations: list[dict] | None = None,
    entry_url: str | None = None,
) -> tuple[DSLCase, dict]:
    """Planner 生成 + 校验；schema 失败 constrained recovery ×1。

    mode: "refs_only"（grounded，有元素表——状态变化型步骤用
          transition_ref，由 verified_edges 确定性展开）
          "legacy"（无探索降级——保留 role/name/scope 生成能力）

    verified_edges: R7——探索的成功转移边（带顺序，t1..tN）；
          Planner 的 click 步骤从这里选 transition_ref。

    返回 (case, planner_meta)，meta 记录：
      planner_attempts / schema_recovery_used / schema_recovery_success
      initial_validation_errors / planner_recovery_ms / mode

    只对"生成结果不合法"（JSON 解析 / Pydantic ValidationError /
    refs-only 契约违规 / 未知 transition_ref）做 recovery；LLM API
    异常（超时/网络）不在此吞掉，让上层 fail safely。
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
        "transitions_expanded": 0,
    }

    def parse_and_validate(text: str) -> DSLCase:
        case_dict = _extract_json(text)
        # S3：transition_refs + assertions 确定性展开（在 DSL 校验前——
        # 展开后才合法；LLM 不输出步骤结构，无 28KB 回吐空间）
        if refs_only and verified_edges:
            n_before = len(case_dict.get("transition_refs") or [])
            case_dict = _expand_plan_schema(
                case_dict, verified_edges,
                observations=observations, entry_url=entry_url)
            meta["transitions_expanded"] = n_before
        case = validate_case(case_dict)
        if refs_only:
            check_refs_only(case)
        return case

    # R5：输入/输出尺寸指标（验证 compact 化是否生效——正常应几 KB）
    meta["prompt_chars"] = len(grounded_prompt)
    raw_text = _call_llm(
        grounded_prompt,
        system_prompt=SYSTEM_PROMPT_REFS_ONLY if refs_only else SYSTEM_PROMPT,
    )
    meta["output_chars"] = len(raw_text)
    try:
        return parse_and_validate(raw_text), meta
    except (ValueError, ValidationError) as exc:
        meta["schema_recovery_used"] = True
        meta["planner_attempts"] = 2
        meta["initial_validation_errors"] = _summarize_validation_error(exc)
        # R5：坏输出只留诊断日志（前 300 字符），绝不重新喂模型
        print(f"[PLANNER] bad_output chars={len(raw_text)} "
              f"preview={raw_text[:300]!r}", flush=True)

        # R5：recovery 不嵌入上次坏输出——32KB 坏 JSON 全文回灌 =
        # 雪崩放大器（大 prompt → 更坏输出 → 更大 prompt）。只给错误
        # 摘要 + 原始任务，干净重生；坏输出不再进入任何后续 LLM 上下文。
        recovery_prompt = (
            "上一次 Planner 输出未通过 DSL Schema 校验，错误：\n"
            f"{meta['initial_validation_errors']}\n\n"
            "请根据下面的原始任务重新生成完整的 DSL JSON。"
            "不要解释、不要复述输入、不要输出 Markdown、不要复制任何页面结构。\n\n"
            f"原始任务：\n{grounded_prompt}"
        )
        if refs_only and tables:
            # refs-only 修复需要引用表上下文（补 ref / 改 ref 都只能在表内选）
            recovery_prompt += f"\n\n元素引用表（target_ref 只能从这里选择）：\n{tables}"
        meta["recovery_prompt_chars"] = len(recovery_prompt)
        t = perf_counter()
        repaired_text = _call_llm(
            recovery_prompt,
            system_prompt=(SYSTEM_PROMPT_REFS_ONLY
                            if refs_only else PLANNER_RECOVERY_SYSTEM_PROMPT),
        )
        meta["planner_recovery_ms"] = int((perf_counter() - t) * 1000)
        meta["recovery_output_chars"] = len(repaired_text)

        case = parse_and_validate(repaired_text)   # 仍失败 → 抛异常（fail safely）
        meta["schema_recovery_success"] = True
    return case, meta


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


def _target_to_dict(t) -> dict:
    """把 target（str / Locator 模型 / dict）统一转成 dict。"""
    if hasattr(t, "model_dump"):
        return t.model_dump()
    if isinstance(t, dict):
        return t
    return {"text": str(t)}


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


_MAX_COMPACT_ACTIONS_PER_OBS = 20   # R5：compact ref 表每 obs 的 action 限量
_MAX_COMPACT_EVIDENCE_PER_OBS = 3   # R5：compact ref 表每 obs 的 evidence 限量


def _build_compact_refs(pages: list[dict],
                        transitions: list[dict] | None = None) -> str:
    """R5：compact ref 表——canonical path 优先，只给 ref + role/name。

    与 _pages_to_text 的区别：refs-only Planner 只需要从 ref 表选
    target_ref——ARIA snapshot 全文是噪音（8 obs × 全文曾把 prompt
    撑到几十 KB，LLM 开始回吐坏 JSON）。

    优先级（防"巨大 snapshot → 巨大 ref table"）：
      1. 成功 transition 涉及的 ref（被验证可操作，规划必需）排最前
      2. 各 obs 其余 action 限量（Add/View Cart 等业务关键元素在 AX
         树前部，前 20 足够；过量 refs 同样撑爆 prompt）
      3. evidence 限量（断言用文本锚点）

    observation_ref 校验（valid_refs）不受影响——只是给 LLM 的视角缩小。
    """
    path_refs: set[str] = {
        t.get("target_ref") for t in (transitions or []) if t.get("target_ref")
    }
    sections = []
    for page in pages:
        obs_id = page.get("id", "?")
        url = page.get("url", "")
        elements = page.get("elements") or []
        path_lines = []
        rest_action = 0
        ev_count = 0
        for e in elements:
            if e["ref"] in path_refs:
                name = (e.get("name") or "").strip()
                path_lines.append(f"      {e['ref']}: {e.get('role', '')} \"{name}\"")
        lines = list(path_lines)
        for e in elements:
            if e["ref"] in path_refs:
                continue
            if e.get("kind") == "action" or "role" in e:
                if rest_action >= _MAX_COMPACT_ACTIONS_PER_OBS:
                    continue
                rest_action += 1
                name = (e.get("name") or "").strip()
                lines.append(f"      {e['ref']}: {e.get('role', '')} \"{name}\"")
            else:
                if ev_count >= _MAX_COMPACT_EVIDENCE_PER_OBS:
                    continue
                ev_count += 1
                text = (e.get("text") or e.get("name") or "").strip()[:60]
                lines.append(f"      {e['ref']}: text \"{text}\"")
        if not lines:
            continue
        sections.append(f"[{obs_id}] {url}\n" + "\n".join(lines))
    return "\n\n".join(sections)


def _pages_to_text(pages: list[dict]) -> str:
    """把探索到的 observation 快照合并成 Planner 可读文本（每页分段标记）。

    分段标记用 observation id（obs1/obs2/...）——Planner 生成 DSL 时
    用 observation_ref 引用（Commit 2 接入）。

    G1：每页附 state-scoped 元素表（refs）——Planner 可输出 target_ref
    引用系统观察到的真实元素（obs3:e17），而非自由构造 role/name/scope。

    注意：refs-only 主路径已改用 _build_compact_refs（R5，不含 snapshot
    全文）；本函数保留给 Preflight 弱验证等需要 snapshot 的调用方。
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
    contract_llm_calls = 0
    auth_profile = "authenticated" if runtime_inputs else "anonymous"
    if entry_url:
        cached = cache_load(entry_url, auth_profile, explore_goal)
        if cached:
            explore_result = cached
            pages = cached.get("observations", [])
            cache_hit = True
        else:
            try:
                # S2-P1：Goal Contract 一次生成（内部 constrained retry ×1）。
                # 两次不合法 → GoalContractError 向上抛（fail closed——
                # 不静默降级旧 completion 模式，保持 S2 控制平面一致）。
                def _contract_llm_call(*args, **kwargs):
                    nonlocal contract_llm_calls
                    contract_llm_calls += 1
                    return _call_llm(*args, **kwargs)

                contract = build_goal_contract(
                    explore_goal, _contract_llm_call,
                    set(runtime_inputs) if runtime_inputs else None)
                explore_result = explore(
                    explore_goal, entry_url, _call_llm, runtime_inputs,
                    contract=contract,
                    initial_llm_calls=contract_llm_calls)
                pages = explore_result.get("observations", [])   # ← observations 模型
                # 保存前脱敏：history 的 value 还原为 ${var}（缓存不落盘真实凭据）
                # S2-P0：StateGraph/history 是目标相关轨迹。只有代码可证明
                # GOAL_COMPLETE 的结果可按目标指纹缓存；MODEL_FINISH、错误页、
                # 认证失败和预算耗尽均不可复用。
                if is_cacheable_trace(explore_result):
                    cache_save(
                        entry_url, auth_profile, explore_goal,
                        _sanitize_for_cache(explore_result, runtime_inputs),
                    )
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
    # R5：canonical path（成功转移边）先行——compact ref 表按 path 优先
    tr = (explore_result or {}).get("transitions") or []
    compact_refs = (_build_compact_refs(pages, transitions=tr)
                    if pages else None)
    if compact_refs:
        # P0-3：canonical path 只来自成功转移边（State Graph transitions），
        # 失败动作单独标注为负例——Planner 不会学到"点击文本超时 →
        # 进入 obs4"的错误因果（temporal attribution bug：失败动作的
        # 15s 超时窗口恰好吞掉了前一个动作的延迟状态）。
        # R7：verified transitions 带 ID（t1..tN）——Planner 的状态变化型
        # 步骤（click）从这里选 transition_ref，由代码确定性展开成
        # action/target_ref/observation_ref——结构上不可能生成跨状态引用，
        # G3 从"经常拦截"降级为"safety invariant"。
        verified_edges = [
            t for t in tr
            if t.get("from") and t.get("to") and t.get("from") != t.get("to")
        ]
        path_lines = [
            f"t{i}: {t['from']} --{t['action']} {t['target_ref']}--> {t['to']}"
            for i, t in enumerate(verified_edges, start=1)
        ]
        fail_lines = [
            f"- {h.get('action')} {h.get('target_ref')} 失败:"
            f" {(h.get('error') or '')[:70]}"
            for h in (explore_result or {}).get("history", [])
            if h.get("error") and h.get("action") != "decision_rejected"
        ]
        # R5：refs-only Planner 只吃 canonical path + compact ref 表——
        # 不注入 ARIA snapshot 全文（Planner 只选 ref，snapshot 是噪音；
        # 完整快照把 prompt 撑到几十 KB → LLM 回吐坏 JSON 的根因）。
        grounded_prompt = (
            f"目标页面入口: {entry_url}\n\n"
            f"已验证状态转移（State Graph 成功边，规划路径只能沿这些边）:\n"
            + ("\n".join(path_lines) if path_lines else "- (无)")
            + ("\n\n失败动作（不要模仿，这些动作未产生有效状态变化）:\n"
               + "\n".join(fail_lines) if fail_lines else "")
            + "\n\n元素引用表（target_ref 只能从这些 ref 中选择，禁止编造）:\n\n"
            + compact_refs
            + "\n\n用户测试需求（已脱敏，密码等敏感信息已替换为 ${var} 占位符）: "
            + explore_goal
            + "\n\n规则："
            "1. 用户提供的测试数据用 ${var} 占位并声明在 input_contract："
            "需求中给出的值填 default；密码等敏感信息 secret=true 且 default=null；"
            "2. （R7）状态变化型步骤（click/导航）用 transition_ref 引用"
            "『已验证状态转移』表中的边（t1/t2/...）——系统确定性展开为"
            "action/target_ref/observation_ref，你无需推导状态机；"
            "fill/select/check/wait_for/assert_visible/assert_text 用 target_ref"
            "引用元素引用表中的系统观察元素（如 obs3:e17），禁止编造；"
            "禁止生成 target/scope 等定位字段（locator 由系统根据 ref 编译）；"
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
        # R5：显式空字符串也算"已传"（不用 `or`——语义严格，不会把
        # 显式传的空表误当"没传"回退到完整 snapshot）
        effective_tables = (
            tables if tables is not None else compact_refs
        )
        # R7.1：verified_edges + observations（state cursor grounding 用）
        case, planner_meta = _generate_planner_case(
            prompt, mode=planner_mode, tables=effective_tables,
            verified_edges=verified_edges, observations=pages,
            entry_url=entry_url,
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
    # R4（评审）：失败 → 记录反模式（诊断）→ 上次错误注入重生 prompt
    # （复用探索结果，不重新探索）→ 二次失败 → 异常冒出 → api 400。
    # 不做负例 few-shot 注入——格式错 retry、grounding 错 replan、
    # 仍错 fail honestly，三层结果就够（反模式库保留作 diagnostics）。
    # S1 第二防线：目标要求的 verified action 缺失 → 明确失败（不进入
    # Planner——空图/缺目标边生成 = 让 LLM 编测试）。与 Explorer
    # completion 共用 missing_verified_goal_actions（同一判断，不写两套）。
    missing_verified = missing_verified_goal_actions(explore_goal, verified_edges)
    if missing_verified:
        raise ExplorationIncompleteError(
            "探索未验证目标动作: " + ", ".join(missing_verified)
            + "（history 点过 ≠ 成功状态迁移）")

    generation_retries = 0
    anti_pattern_used = 0
    try:
        case, planner_meta, removed_assertions, compile_stats = attempt(grounded_prompt)
    except (GoalCoverageError, UnknownTargetRefError, StateGroundingMismatchError,
            UnreachableObservationError, ValueError, ValidationError) as exc:
        reason = _failure_reason_code(exc)
        failed_case = exc.case if isinstance(exc, GoalCoverageError) else None
        anti_patterns.record(reason, _plan_summary(failed_case, str(exc)))
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
            # R5：compact 化 + 转移边过滤到 reach 内（不回到 full snapshot）
            if explore_result is not None:
                sg = StateGraph.from_explore_result(explore_result)
                reach = _reachable_observations(sg)
                retry_pages = [p for p in pages if p["id"] in reach]
                retry_tr = [t for t in tr
                            if t.get("from") in reach and t.get("to") in reach]
                retry_tables = (_build_compact_refs(retry_pages, transitions=retry_tr)
                                if retry_pages else None)
        case, planner_meta, removed_assertions, compile_stats = attempt(
            grounded_prompt + _build_retry_hint(str(exc)) + extra,
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
            "contract_llm_calls": (
                (explore_result or {}).get("contract_llm_calls", 0)
            ),
            "decision_llm_calls": (
                (explore_result or {}).get(
                    "decision_llm_calls",
                    max(0, (explore_result or {}).get("llm_calls", 0)
                        - (explore_result or {}).get("contract_llm_calls", 0)),
                )
            ),
            "done": (explore_result or {}).get("done", False),
            "termination_reason": (
                (explore_result or {}).get("termination_reason")
            ),
            "transitions": (explore_result or {}).get("transitions", []),   # G2：状态转移边
            # S2-P1：Goal Contract 与里程碑进度（诊断/验收）
            "goal_contract": (explore_result or {}).get("goal_contract"),
            "milestone_progress": (
                (explore_result or {}).get("milestone_progress")
            ),
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

    # S1：Preflight 已删除（R4 降级后恒不执行——运行时 Resolver 是定位
    # 权威，探索快照模拟曾制造假阳性；主链 G3 → Compiler → Runner）
    preflight_ms = 0

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
