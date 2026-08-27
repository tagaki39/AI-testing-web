"""Goal Contract 与 Explorer 共用的受支持动作语义。

这里只维护已有确定性支持族和少量明确终端动作；不尝试成为通用 NLP 规则库。
"""

from __future__ import annotations

import re
import unicodedata


def normalize_semantic_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", "", text)


GOAL_SEMANTIC_PATTERNS: dict[str, re.Pattern] = {
    "login": re.compile(r"(登录|登陆|登入|login|sign\s*in)", re.IGNORECASE),
    "add_to_cart": re.compile(
        r"(加入购物车|加入購物車|加购|add\s+to\s+cart)", re.IGNORECASE),
    "checkout": re.compile(
        r"(结算|结账|下单|checkout|place\s+order)", re.IGNORECASE),
    "generate": re.compile(
        r"(生成(?!页面|页|器|功能|模块)|\bgenerate\b|"
        r"\bcreate\s+(?:an?\s+)?(?:image|content|result)\b)",
        re.IGNORECASE,
    ),
    "submit": re.compile(r"(提交|submit)", re.IGNORECASE),
    "publish": re.compile(r"(发布|publish)", re.IGNORECASE),
    "pay": re.compile(r"(支付|付款|pay)", re.IGNORECASE),
    "delete": re.compile(r"(删除|delete)", re.IGNORECASE),
}


ACTION_ALIASES: dict[str, tuple[str, ...]] = {
    "login": ("login", "sign in", "登录", "登陆", "登入"),
    "add_to_cart": ("add to cart", "加入购物车", "加入購物車", "加购"),
    "checkout": ("checkout", "place order", "结算", "结账", "下单"),
    "generate": ("generate", "create", "生成"),
    "submit": ("submit", "提交"),
    "publish": ("publish", "发布"),
    "pay": ("pay", "支付", "付款"),
    "delete": ("delete", "删除"),
}

FIELD_ALIASES_BY_KEY: dict[str, tuple[str, ...]] = {
    "username": ("username", "user name", "account", "账号", "用户名", "账户"),
    "email": ("email", "e-mail", "邮箱", "电子邮箱"),
    "password": ("password", "passcode", "密码", "口令"),
    "prompt": ("prompt", "提示词", "描述"),
}


CONTRACT_OBLIGATIONS: dict[str, tuple[str, str]] = {
    "login": ("auth", "explorer"),
    "add_to_cart": ("action", "explorer"),
    "checkout": ("terminal_action", "runner"),
    "generate": ("terminal_action", "runner"),
    "submit": ("terminal_action", "runner"),
    "publish": ("terminal_action", "runner"),
    "pay": ("terminal_action", "runner"),
    "delete": ("terminal_action", "runner"),
}


def required_semantics(goal: str) -> tuple[str, ...]:
    return tuple(
        label for label, pattern in GOAL_SEMANTIC_PATTERNS.items()
        if pattern.search(goal or "")
    )


def matches_semantic(value: object, label: str) -> bool:
    haystack = normalize_semantic_text(value)
    return any(
        normalize_semantic_text(alias) in haystack
        for alias in ACTION_ALIASES.get(label, ())
    )


def semantic_labels(value: object) -> tuple[str, ...]:
    return tuple(
        label for label in ACTION_ALIASES
        if matches_semantic(value, label)
    )
