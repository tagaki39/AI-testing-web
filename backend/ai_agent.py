"""AI 生成 DSL：自然语言 → DeepSeek API → 结构化测试用例。

核心思想：
1. AI 只负责"生成"，不负责"执行"——执行是确定性的 Playwright
2. 生成的输出必须通过 Pydantic 强校验，非法内容直接拒绝
3. Prompt 里明确约束 DSL 格式，降低幻觉
"""

import json
import os
import re
import urllib.request
import urllib.error

from dsl import DSLCase, validate_case

# ── 配置 ──────────────────────────────────────────────────────────────────────

API_KEY = os.getenv("AI_API_KEY", "")
BASE_URL = os.getenv("AI_BASE_URL", "https://api.deepseek.com/v1")
MODEL = os.getenv("AI_MODEL", "deepseek-chat")

# ── Prompt（约束 LLM 输出符合格式的 JSON）──────────────────────────────────────

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

def _call_llm(user_prompt: str) -> str:
    """调用 DeepSeek chat completions API，返回文本内容。"""
    if not API_KEY:
        raise RuntimeError("未配置 AI_API_KEY（环境变量或 .env 文件）")

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,   # 低温度 → 输出更稳定，不容易乱编
    }
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _extract_json(text: str) -> dict:
    """从 LLM 输出中提取 JSON（容错：去掉 ```json 代码块标记）。"""
    match = re.search(r"\{.*\}", text, re.DOTALL)   # 取第一个 { 到最后一个 }
    if not match:
        raise ValueError(f"LLM 输出中找不到 JSON: {text[:200]}")
    return json.loads(match.group(0))


# ── 对外接口 ───────────────────────────────────────────────────────────────────

def generate_dsl(user_prompt: str) -> DSLCase:
    """自然语言需求 → 校验通过的 DSLCase。

    如果 AI 输出格式非法，Pydantic 会抛异常，由上层捕获返回给用户。
    """
    raw_text = _call_llm(user_prompt)
    raw_json = _extract_json(raw_text)
    return validate_case(raw_json)   # ← 安全边界：不通过就不执行
