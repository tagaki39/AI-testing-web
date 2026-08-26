"""
observation.py — 状态观察（R3 拆分自 explore_flow）
  ARIA 抓取 → 元素解析 → 状态去重 → canonical obs id（ObservationStore 语义）
  一个状态只有一个事实源：current_obs 只由 _record_page 设置。
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from time import perf_counter
from typing import Literal

from locator.resolver import PRICE_RE, _strip_leading_decoration, choose_scope_text
from execution.runner import _resolve_locator

_MAX_SNAPSHOT_CHARS = 6000   # 裁剪后快照的最终兜底上限（重组后仍超限才截断）


# ── A1：A11y Tree Provider（CDP 结构化观察的数据源）───────────────────────────
# 原则：全项目不认识 CDP 原始 JSON——归一化锁死在 normalize_cdp_ax_node。
# AXNodeId 是临时浏览器状态标识，绝不作为 locator（只做树内部引用与诊断）。

@dataclass
class AXNode:
    """规范化后的无障碍树节点（结构化 A11y 观察的基础）。"""
    ax_id: str                       # CDP AXNodeId（仅树内部引用，不做 locator）
    role: str | None
    name: str | None
    parent_ax_id: str | None
    child_ax_ids: list[str]
    backend_dom_node_id: int | None  # 仅 runtime bridge / diagnostics
    ignored: bool
    focusable: bool = False
    disabled: bool = False
    checked: bool | None = None
    selected: bool | None = None
    expanded: bool | None = None
    pressed: bool | None = None
    level: int = 0


def _ax_prop(properties: list[dict], key: str):
    """从 CDP properties 数组取属性值（无则 None）。"""
    for p in properties or []:
        if p.get("name") == key:
            v = (p.get("value") or {}).get("value")
            if isinstance(v, bool):
                return v
            return v
    return None


def normalize_cdp_ax_node(raw: dict) -> AXNode:
    """CDP Accessibility.getFullAXTree 单节点 → AXNode（解析唯一入口）。"""
    return AXNode(
        ax_id=str(raw.get("nodeId", "")),
        role=(raw.get("role") or {}).get("value"),
        name=(raw.get("name") or {}).get("value") or "",
        parent_ax_id=str(raw["parentId"]) if raw.get("parentId") else None,
        child_ax_ids=[str(c) for c in raw.get("childIds", [])],
        backend_dom_node_id=raw.get("backendDOMNodeId"),
        ignored=bool(raw.get("ignored", False)),
        focusable=bool(_ax_prop(raw.get("properties", []), "focusable")),
        disabled=bool(_ax_prop(raw.get("properties", []), "disabled")),
        checked=_ax_prop(raw.get("properties", []), "checked"),
        selected=_ax_prop(raw.get("properties", []), "selected"),
        expanded=_ax_prop(raw.get("properties", []), "expanded"),
        pressed=_ax_prop(raw.get("properties", []), "pressed"),
        level=int(_ax_prop(raw.get("properties", []), "level") or 0),
    )


class AccessibilityProvider:
    """A11y 树观察接口（A1：浏览器观察能力兼容 fallback，非定位猜测）。"""

    def capture(self, page) -> list[AXNode]:
        raise NotImplementedError


class CDPAccessibilityProvider(AccessibilityProvider):
    """Chromium CDP Accessibility.getFullAXTree → 结构化 AXNode 列表。"""

    def capture(self, page) -> list[AXNode]:
        session = page.context.new_cdp_session(page)
        result = session.send("Accessibility.getFullAXTree")
        return [normalize_cdp_ax_node(n) for n in result.get("nodes", [])]


class AriaSnapshotProvider(AccessibilityProvider):
    """aria_snapshot 兼容 fallback（CDP 不可用时——非 Chromium 或受限环境）。"""

    def capture(self, page) -> list[AXNode]:
        # 扁平兼容：不建层级，全部作为忽略节点（A2 的 aria legacy 路径接管）
        return []


# ── A2：Structured Observation（结构化 A11y 元素模型）─────────────────────────
# kind 分类（确定性规则，不上 LLM）：action 可操作 / evidence 证据 /
# container 语义容器。semantic_context = 最近有区分能力的语义容器
#（dialog/listitem/form...）——Compiler 未来用它生成 scope，替代 scope_has_text。

ACTION_ROLES = {
    "button", "link", "textbox", "searchbox", "combobox", "checkbox",
    "radio", "switch", "option", "menuitem", "tab", "slider",
}

CONTAINER_ROLES = {
    "dialog", "form", "navigation", "main", "region", "article",
    "list", "listitem", "group",
}

EVIDENCE_ROLES = {"heading", "alert", "status", "img", "label"}


@dataclass
class ObservationElement:
    """结构化观察元素（A4 数据模型：层级/状态/kind/语义上下文）。"""
    ref: str
    role: str | None
    name: str | None
    kind: Literal["action", "evidence", "container"]
    parent_ref: str | None = None
    children: list[str] = field(default_factory=list)
    focusable: bool = False
    disabled: bool = False
    checked: bool | None = None
    selected: bool | None = None
    expanded: bool | None = None
    context_role: str | None = None      # 最近语义容器（dialog/listitem/form...）
    context_name: str | None = None
    backend_dom_node_id: int | None = None   # 仅诊断，不作为 locator


def semantic_state_signature(elements: list[dict]) -> str | None:
    """语义状态签名（A4）：action/container 元素的
    (role, 归一化 name, disabled, checked, expanded, context_role,
    context_name) 排序后 hash。

    相同业务状态 → 相同 hash（modal 开/关、表单 value 变化不再产生
    phantom obs）；无 role 元素（纯文本页）→ None（回落全文 hash）。
    绝不 hash AXNodeId（浏览器临时标识，不稳定）。
    """
    items = []
    for e in elements:
        role = e.get("role")
        if not role:
            continue
        items.append((
            role,
            (e.get("name") or "").strip(),
            bool(e.get("disabled")),
            e.get("checked"),
            e.get("expanded"),
            e.get("context_role"),
            e.get("context_name"),
        ))
    if not items:
        return None
    sig = json.dumps(sorted(items), ensure_ascii=False)
    return hashlib.sha256(sig.encode()).hexdigest()[:10]


def _classify(role: str | None) -> str:
    if role in ACTION_ROLES:
        return "action"
    if role in CONTAINER_ROLES:
        return "container"
    if role in EVIDENCE_ROLES:
        return "evidence"
    return "evidence"   # 其他有 name 的节点 → evidence（text/StaticText 等）


def find_semantic_context(node: AXNode, by_id: dict[str, AXNode]) -> tuple[str | None, str | None]:
    """沿祖先找最近的、有 name 的语义容器（dialog/form/article/listitem/group/region）。"""
    parent = by_id.get(node.parent_ax_id or "") if node.parent_ax_id else None
    seen = 0
    while parent and seen < 32:
        if parent.role in {"dialog", "form", "article", "listitem", "group", "region"} \
                and parent.name:
            return parent.role, parent.name
        parent = by_id.get(parent.parent_ax_id or "") if parent.parent_ax_id else None
        seen += 1
    return None, None


_MAX_EVIDENCE_ELEMENTS = 20   # evidence 限量（防元素表膨胀 → 评估 O(N) 爆炸）

# ── A4.2 Stable Action Identity ──────────────────────────────────────────────
# 诊断定案：AutomationExercise 12 个 Add to cart = 6 商品 × 2 DOM 表现
#（normal + overlay）——data-product-id 在 action 自身。旧 scope_has_text
#（容器文本）模型过时——改为结构化 stable identity + duplicate canonicalization。
# backendDOMNodeId 仅 observation-time bridge，绝不进入 DSL/缓存长期事实。

_IDENTITY_ATTRS = ("data-testid", "data-test", "data-qa", "data-cy")


def extract_stable_identity(attrs: dict) -> dict | None:
    """从 DOM attributes 提取稳定 identity（通用规则，无站点特判）：
    优先 data-testid/test/qa/cy，其次 data-*-id。"""
    for attr in _IDENTITY_ATTRS:
        if attrs.get(attr):
            return {"attr": attr, "value": attrs[attr]}
    for attr, value in attrs.items():
        if attr.startswith("data-") and attr.endswith("-id") and value:
            return {"attr": attr, "value": value}
    return None


def _dom_identity_via_backend(page, backend_dom_node_id) -> dict | None:
    """backendDOMNodeId → 当前页面精确 DOM node → 稳定 identity。
    仅 observation-time bridge；结果只保存 identity（attr/value）。"""
    if not backend_dom_node_id:
        return None
    try:
        session = page.context.new_cdp_session(page)
        resolved = session.send("DOM.resolveNode",
                                {"backendNodeId": backend_dom_node_id})
        obj = resolved.get("object") or {}
        if not obj.get("objectId"):
            return None
        attrs = session.send("Runtime.callFunctionOn", {
            "objectId": obj["objectId"],
            "functionDeclaration": """function() {
                const attrs = {};
                for (const a of this.attributes) attrs[a.name] = a.value;
                return attrs;
            }""",
            "returnByValue": True,
        })
        raw = (attrs.get("result") or {}).get("value") or {}
        return extract_stable_identity(raw)
    except Exception:
        return None


def _detect_dom_overlay(page, elements: list[dict]) -> tuple[set[str], dict | None]:
    """A4.3：DOM interaction-root bridge（AX 缺 dialog 语义时的最小兼容）。

    AutomationExercise 的 Bootstrap modal 零 ARIA（无 role=dialog /
    aria-modal），Raw AX 无 dialog 容器 → A3 的 context_role 限制失效，
    背景 Add to cart 继续暴露给 LLM（8 连 ACTION_FAILED 浪费探索预算）。
    通用判定（非站点特判，事实标准选择器 + 可见性过滤）：
    可见的交互覆盖层（.modal.show / dialog[open] / [role=dialog][aria-modal]）。

    返回 (overlay 内 action ref 集合, overlay 描述 dict)；
    无 overlay → (set(), None)。描述只进 observation metadata——
    不冒充 AX 语义（不写 context_role="dialog"）。

    性能：一次 evaluate 判 overlay 存在性（无 → 零成本）；
    存在时逐 action resolveNode（毫秒级 CDP 轻调用，无等待）×
    一次批量 contains 判定（多 objectId 单次 callFunctionOn）。
    """
    actions = [e for e in elements
               if e.get("kind") == "action" and e.get("backend_dom_node_id")]
    if not actions:
        return set(), None
    try:
        session = page.context.new_cdp_session(page)
        # 1. 可见 overlay（返回 objectId + 描述；null → 无 overlay）
        ov = session.send("Runtime.evaluate", {
            "expression": """(() => {
                const ov = document.querySelector(
                    '.modal.show, dialog[open], [role="dialog"][aria-modal="true"], [aria-modal="true"]');
                if (!ov) return null;
                const r = ov.getBoundingClientRect();
                if (!(r.width > 0 && r.height > 0)) return null;
                return { id: ov.id || null, cls: ov.className || null };
            })()""",
            "returnByValue": True,
        })
        ov_desc = (ov.get("result") or {}).get("value")
        if not ov_desc:
            return set(), None
        # 再拿 overlay 的 objectId（归属判定用）
        ov2 = session.send("Runtime.evaluate", {
            "expression": """(() => {
                const ov = document.querySelector(
                    '.modal.show, dialog[open], [role="dialog"][aria-modal="true"], [aria-modal="true"]');
                if (!ov) return null;
                const r = ov.getBoundingClientRect();
                return (r.width > 0 && r.height > 0) ? ov : null;
            })()""",
        })
        ov_obj = (ov2.get("result") or {}).get("objectId")
        if not ov_obj:
            return set(), None
        # 2. backendDOMNodeId → objectId（observation-time bridge 的最后用途）
        object_ids: list[str] = []
        for e in actions:
            try:
                r = session.send("DOM.resolveNode",
                                 {"backendNodeId": e["backend_dom_node_id"]})
                obj = r.get("object") or {}
                if obj.get("objectId"):
                    object_ids.append(obj["objectId"])
            except Exception:
                pass
        if not object_ids:
            return set(), None
        # 3. 一次批量判定 overlay 归属（this = overlay 容器）
        verdict = session.send("Runtime.callFunctionOn", {
            "objectId": ov_obj,
            "functionDeclaration":
                "function(...els) { return els.map(el => this.contains(el)); }",
            "arguments": [{"objectId": o} for o in object_ids],
            "returnByValue": True,
        })
        raw = (verdict.get("result") or {}).get("value") or []
        refs = {e["ref"] for e, inside in zip(actions, raw) if inside}
        desc = {"source": "dom_overlay", "kind": "modal",
                "id": ov_desc.get("id") or None}
        return refs, desc if refs else None
    except Exception:
        return set(), None


def canonicalize_actions(elements: list[dict],
                         identity_map: dict[str, dict]) -> list[dict]:
    """A4.2：同一 (role, name, identity) 的重复 action 折叠为一个业务动作。

    规则：
      - 只折叠「有 stable identity 且相同」的重复（无 identity 绝不猜着合）
      - 保留 first-seen 顺序（文档/业务顺序，不排序）
      - 折叠后记录 representation_count（调试/诊断）
    """
    seen: set = set()
    counts: dict[tuple, int] = {}
    for e in elements:
        ident = identity_map.get(e["ref"])
        if e.get("kind") == "action" and ident:
            k = (e.get("role"), (e.get("name") or "").strip(),
                 ident["attr"], ident["value"])
            counts[k] = counts.get(k, 0) + 1
    out: list[dict] = []
    for e in elements:
        ident = identity_map.get(e["ref"])
        key = None
        if e.get("kind") == "action" and ident:
            key = (e.get("role"), (e.get("name") or "").strip(),
                   ident["attr"], ident["value"])
            if key in seen:
                continue   # canonical duplicate
            seen.add(key)
        out.append(e)
    for e in out:
        ident = identity_map.get(e["ref"])
        if e.get("kind") == "action" and ident:
            k = (e.get("role"), (e.get("name") or "").strip(),
                 ident["attr"], ident["value"])
            e["identity"] = ident
            if counts.get(k, 1) > 1:
                e["representation_count"] = counts[k]
    return out


def build_observation_elements(ax_nodes: list[AXNode]) -> list[ObservationElement]:
    """AX 树 → 结构化元素列表（扁平 ref 序列 + 层级/kind/语义上下文）。

    A2：Observation 的事实源从文本升级为结构化树；refs 保持 obsN:eM
    格式（Planner 只认 ref，AXNodeId 不进入 DSL）。
    """
    by_id = {n.ax_id: n for n in ax_nodes}
    # 有区分能力的节点：action / container / evidence（heading/alert/status/
    # 有名字的 StaticText）——generic/none/presentation/无名节点跳过；
    # evidence 限量（防元素表膨胀导致观察期评估 O(N) 协议往返爆炸）。
    active = [
        n for n in ax_nodes
        if not n.ignored
        and n.role not in ("generic", "none", "presentation",
                           "RootWebArea", "WebArea", "Iframe",
                           "IframePresentational", "cell", "row", "column")
        and (n.name or "").strip()
    ]
    ev_count = 0
    kept: list[AXNode] = []
    for n in active:
        kind = _classify(n.role)
        if kind == "evidence":
            if ev_count >= _MAX_EVIDENCE_ELEMENTS:
                continue
            ev_count += 1
        kept.append(n)
    ax_to_ref = {n.ax_id: f"e{i + 1}" for i, n in enumerate(kept)}
    elements: list[ObservationElement] = []
    for n in kept:
        kind = _classify(n.role)
        ctx_role, ctx_name = find_semantic_context(n, by_id)
        elements.append(ObservationElement(
            ref=ax_to_ref[n.ax_id],
            role=n.role,
            name=n.name or None,
            kind=kind,
            parent_ref=ax_to_ref.get(n.parent_ax_id or ""),
            children=[c for c in n.child_ax_ids if c in ax_to_ref],
            focusable=n.focusable,
            disabled=n.disabled,
            checked=n.checked,
            selected=n.selected,
            expanded=n.expanded,
            context_role=ctx_role,
            context_name=ctx_name,
            backend_dom_node_id=n.backend_dom_node_id,
        ))
    return elements
_MAX_HISTORY = 3     # 决策上下文只看最近 3 步历史
_MAX_TEXT_ELEMENTS = 20      # 文本节点最多注入 20 个（防上下文膨胀）
_MAX_TEXT_LINES = 25         # 智能裁剪：text 行限量
_MAX_OTHER_LINES = 40        # 智能裁剪：非交互语义行（heading/banner 等容器）限量
_MAX_OBSERVATIONS = 12       # 总 observation 上限（防膨胀）。
# R6：per-URL cap 已删除——URL ≠ State（BFC 加购循环同 URL 有列表↔modal
# 多个合法状态），且已有 MAX_STEPS/MAX_LLM_CALLS/MAX_EXPLORE_SECONDS/
# 总上限四重保险，per-URL 是第五重且会误杀正常业务。
# 可交互元素角色（element ref 表只收录这些——LLM 只能操作这些）
_INTERACTIVE_ROLES = {
    "button", "link", "textbox", "checkbox", "radio", "combobox",
    "searchbox", "menuitem", "tab", "option",
}

# 解析 aria_snapshot YAML 行的正则
_ELEMENT_RE = re.compile(r'-\s+(\w+)\s+"([^"]*)"')          # - button "Login"
_TEXT_RE = re.compile(r'-\s+text:\s*(.+)')                  # - text: Products
def _observe_until_stable(page, timeout_ms: int = 2000) -> str:
    """轮询页面 snapshot 直到状态稳定（等状态证据，不等固定时间）。

    评审 P0-2：点击后固定 300ms 等待不够——模态框/SPA 延迟渲染时
    观察还是旧状态 → 图里记 self-loop，新状态被归到下一次错误动作
    （BFC 场景：Add to cart 后 modal 未渲染，obs3→obs3，modal 状态
    被错误归因到下一次 text 点击的超时窗口）。
    轮询：snapshot hash 变化后连续两次相同 → 认为稳定。
    P1：本函数只用于【已确认分叉后】的新状态稳定（settle）——
    不再承担"等待分叉"职责（旧登录页"连续稳定"≠ 动作完成，
    见 _observe_after_action）。
    """
    deadline = perf_counter() + timeout_ms / 1000
    last_hash: str | None = None
    stable_count = 0
    latest = ""
    while perf_counter() < deadline:
        latest = _observe(page)
        h = hashlib.sha256(latest.encode()).hexdigest()[:10]
        if h == last_hash:
            stable_count += 1
            if stable_count >= 2 and last_hash is not None:
                return latest
        else:
            last_hash, stable_count = h, 0
        page.wait_for_timeout(150)
    return latest


def _observe_after_action(page, before_url: str, before_snapshot: str,
                          transition_timeout_ms: int = 5000,
                          settle_timeout_ms: int = 2000) -> str:
    """两阶段观察：先等状态分叉（transition divergence），再等新状态稳定。

    P1（RuoYi 异步登录实测）：旧页面"连续两次稳定" ≠ 动作完成——登录
    POST 后台跑几秒（SPA router push /index 延迟），登录页视觉稳定，
    单阶段 _observe_until_stable 提前返回 → 伪 self-loop → StateGraph
    无转移 → verified 门误判登录失败。

    Phase 1（transition watch，≤ transition_timeout_ms）：
      不断比较当前 URL/snapshot 与 before——出现有效变化 → 分叉确认
    Phase 2（settle，≤ settle_timeout_ms）：
      等新状态连续稳定（_observe_until_stable）

    整个窗口无变化 → 诚实判定 self-loop（返回最后观察）。
    """
    before_hash = hashlib.sha256(before_snapshot.encode()).hexdigest()[:10]
    deadline = perf_counter() + transition_timeout_ms / 1000
    latest = before_snapshot
    while perf_counter() < deadline:
        latest = _observe(page)
        if page.url != before_url \
                or hashlib.sha256(latest.encode()).hexdigest()[:10] != before_hash:
            return _observe_until_stable(page, timeout_ms=settle_timeout_ms)
        page.wait_for_timeout(150)
    return latest

@dataclass
class ExploreState:
    """探索状态。"""
    goal: str                          # 用户测试目标
    entry_url: str                     # 入口 URL
    current_url: str = ""              # 当前页面
    current_obs: str | None = None     # R3：当前状态唯一事实源（ObservationStore）
                                       # 只由 _record_page 设置——禁止
                                       # observations[-1] 参与状态推导
    snapshot: str = ""                 # 当前页面快照（原始）
    elements: list[dict] = field(default_factory=list)      # 当前页元素表（ref）
    history: list[dict] = field(default_factory=list)       # 操作历史
    observations: list[dict] = field(default_factory=list)  # 页面状态观察（含 state_hash）
    transitions: list[dict] = field(default_factory=list)   # G2：状态转移边（obs3 --click e17--> obs4）
    input_keys: set = field(default_factory=set)   # Data Grounding：允许的 ${key} 白名单
    failed_actions: set = field(default_factory=set)  # R3：{(obs_id, action, ref)} 失败黑名单
    step_count: int = 0                # 已执行动作数
    llm_calls: int = 0                 # 已用 LLM 调用数
    done: bool = False                 # 探索是否完成
    # A4.3：当前 observation 的 interaction root 描述（AX dialog 时
    # source="ax"；DOM overlay bridge 时 source="dom_overlay"）。只进
    # observation metadata，不冒充 AX 语义。
    interaction_root: dict | None = None
    # 计时（Speed v1：定位耗时构成，决定下一刀砍哪）
    # R3 细分：observe（aria 抓取+解析） / action_space（评估过滤） /
    # settle（稳定轮询） / llm / browser_action / fixed_wait
    timings: dict = field(default_factory=lambda: {
        "llm_ms": 0, "browser_action_ms": 0, "fixed_wait_ms": 0,
        "observation_ms": 0, "action_space_ms": 0, "settle_ms": 0,
    })


# ── element ref 解析（aria_snapshot → 带编号的元素表）───────────────────────────
# 核心：把"快照文本"变成"元素清单"，LLM 只能从中引用 ref。

def _parse_elements(snapshot: str) -> list[dict]:
    """解析 aria_snapshot → 可操作元素列表（带 ref 编号）。

    只收录两类：
      - 可交互元素（button/link/textbox...）：LLM 可以点击/填写
      - 文本节点（text: xxx）：可用于定位（span 标题等），限量注入

    例子：
      - button "Add to cart"  → {"ref": "e1", "role": "button", "name": "Add to cart"}
      - text: Products        → {"ref": "e2", "type": "text", "text": "Products"}
    """
    elements: list[dict] = []
    text_count = 0
    for line in snapshot.splitlines():
        line = line.strip()
        m = _ELEMENT_RE.match(line)
        if m:
            role, name = m.group(1), m.group(2)
            if role in _INTERACTIVE_ROLES and name.strip():
                elements.append({
                    "ref": f"e{len(elements) + 1}",
                    "role": role,
                    "name": name.strip(),
                    "kind": "action",   # A4.1：统一 kind 契约（fallback 也 classify）
                })
            continue
        m = _TEXT_RE.match(line)
        if m:
            text = m.group(1).strip()
            if text and text_count < _MAX_TEXT_ELEMENTS:
                text_count += 1
                elements.append({
                    "ref": f"e{len(elements) + 1}",
                    "type": "text",
                    "text": text[:50],
                    "kind": "evidence",
                })
    return elements


def _observe(page) -> str:
    """抓取当前页面 ARIA 快照并智能裁剪（第 7 项：不再粗暴截断）。

    修复：简单 [:4000] 截断会丢后半段元素（重要按钮在截断外时
    Preflight 误判"不存在"）。结构化裁剪按优先级保留：
      1. 可交互元素行（button/link/textbox...）——全部保留，永不丢
      2. heading 等语义行——限量
      3. text 行——限量
      4. 其他（容器/装饰）——限量
    层级缩进保留（只过滤不重排）；重组后仍超限才最终截断。
    """
    try:
        snapshot = page.locator("body").aria_snapshot() or ""
    except Exception:
        return ""
    return _smart_truncate(snapshot)


def _smart_truncate(snapshot: str) -> str:
    """结构化裁剪：按元素优先级过滤行（见 _observe 说明）。"""
    lines = snapshot.splitlines()
    kept: list[str] = []
    text_count = 0
    other_count = 0
    for line in lines:
        stripped = line.strip()
        m = _ELEMENT_RE.match(stripped)
        if m:
            kept.append(line)          # 可交互元素：全保留
            continue
        m = _TEXT_RE.match(stripped)
        if m:
            if text_count < _MAX_TEXT_LINES:
                kept.append(line)
                text_count += 1
            continue
        if stripped.startswith("-"):
            if other_count < _MAX_OTHER_LINES:   # 容器/heading 等语义行：限量
                kept.append(line)
                other_count += 1
            continue
        kept.append(line)              # 缩进/空行
    result = "\n".join(kept)
    return result[:_MAX_SNAPSHOT_CHARS] if len(result) > _MAX_SNAPSHOT_CHARS else result


def _record_page(state: ExploreState, page, snapshot: str | None = None) -> None:
    """记录当前页面状态为 observation（升级：URL + state_hash 去重）。

    Observation = URL + 页面状态 + ARIA 证据——
    同 URL 不同状态（如 Add to cart 点击后按钮变 Remove）也保存，
    解决 SPA 状态丢失（此前只按 URL 去重）。

    snapshot 可外部传入（P0-2：_observe_until_stable 的稳定快照），
    避免重复抓取 aria_snapshot。
    """
    url = page.url
    state.current_url = url
    if snapshot is None:
        snapshot = _observe(page)

    # A4 修正（用户诊断：Raw CDP AX 里 Add to cart 是 link，aria 文本表
    # 却是 text——"aria 表 + CDP 打补丁"的匹配投影会丢 actionable ancestor）：
    # CDP AX Tree 是 ObservationElement 的唯一事实源；aria_snapshot 仅
    # provider fallback（One source of truth）。
    try:
        ax_nodes = CDPAccessibilityProvider().capture(page)
        structured = build_observation_elements(ax_nodes) if ax_nodes else []
        if structured:
            elements = [
                {
                    "ref": e.ref,
                    "role": e.role,
                    "name": e.name or "",
                    "kind": e.kind,
                    "disabled": e.disabled,
                    "context_role": e.context_role,
                    "context_name": e.context_name,
                    "parent_ref": e.parent_ref,
                    "backend_dom_node_id": e.backend_dom_node_id,
                }
                for e in structured
            ]
        else:
            elements = _parse_elements(snapshot)   # CDP 不可用 → aria legacy
    except Exception:
        elements = _parse_elements(snapshot)   # CDP 不可用 → aria legacy

    # 状态哈希：A4 优先用语义状态签名（action/container 的 role/name/状态/
    # 语义父级排序 hash）——相同业务状态匹配回原 obs，减少 phantom states
    #（文本/输入值变化不再产生新 obs）。CDP 无增强时回落全文 hash。
    state_hash = semantic_state_signature(elements) or (
        hashlib.sha256(snapshot.encode()).hexdigest()[:10])

    # 当前 snapshot 命中已有 observation → 恢复该状态的 state-scoped
    # 元素表。修复：此前已存在路径会先污染 state.elements（无 obs 前缀
    # 新表）——决策校验拿 obs2:e10 对表校验全被拒（8 连拒）。
    # 注意必须是"命中哪个 obs 就恢复哪个"——A→B→A 场景恢复 obs1
    # 的元素表，而不是上一次的 state.elements（可能是 obs2）。
    matched = next((
        o for o in state.observations
        if o["url"] == url and o.get("state_hash") == state_hash
    ), None)
    if matched is not None:
        state.elements = matched["elements"]
        state.current_obs = matched["id"]   # R3：current_obs 唯一事实源
        return matched["id"]   # E1：transition 的 to 用实际所在状态

    if len(state.observations) >= _MAX_OBSERVATIONS:
        # 观察预算满（R6：仅总上限——URL ≠ State，同 URL 多状态是
        # 合法业务（列表↔modal 循环），per-URL cap 会误杀）。
        # 不给当前状态一个"裸元素表"（无 state owner）。
        # 停止探索（主循环检测 done），比带着无主元素继续决策安全。
        state.history.append({
            "url": url,
            "action": "observation_cap",
            "error": "观察预算已满（total 上限），停止探索",
        })
        state.done = True
        return None

    state.snapshot = snapshot
    state.elements = elements
    obs_id = f"obs{len(state.observations) + 1}"
    state.current_obs = obs_id   # R3：current_obs 唯一事实源
    # G1：state-scoped ref——元素 ref 从页面级 "e1" 升级为状态级 "obs3:e1"。
    # Planner 引用 obs3:e17 时，系统知道 belongs_to=obs3（state identity）。
    for element in state.elements:
        element["ref"] = f"{obs_id}:{element['ref']}"

    # A4.1：AX semantic context 优先——只有「重复 action 且无 AX context」
    # 才走 legacy DOM scope（evidence/container 永不进入；CDP 建树后
    # 不再对全量元素跑 DOM ancestor 采集——性能根因修复）
    legacy_candidates = [
        e for e in elements
        if e.get("kind") == "action" and not e.get("context_role")
    ]
    if legacy_candidates:
        _attach_legacy_dom_scope(page, legacy_candidates)

    # A4.2：只对「role+name 重复且 AX context 仍不能消歧」的 action
    # 做 identity enrichment（backendDOMNodeId bridge）→ canonicalize。
    t_id = perf_counter()
    counts: dict[tuple, int] = {}
    for e in elements:
        if e.get("kind") == "action" and e.get("name") and not e.get("context_role"):
            k = (e.get("role"), (e.get("name") or "").strip())
            counts[k] = counts.get(k, 0) + 1
    identity_map: dict[str, dict] = {}
    for e in elements:
        if e.get("kind") == "action" and e.get("name") and not e.get("context_role"):
            k = (e.get("role"), (e.get("name") or "").strip())
            if counts.get(k, 0) <= 1:
                continue   # 唯一 action 不需要 identity（非重复）
            ident = _dom_identity_via_backend(page, e.get("backend_dom_node_id"))
            if ident:
                identity_map[e["ref"]] = ident
    enriched = len(identity_map)
    canonical_dropped = 0
    if identity_map:
        pre_count = len(elements)
        elements = canonicalize_actions(elements, identity_map)
        canonical_dropped = pre_count - len(elements)
        state.elements = elements   # 折叠后的元素表接管（same dict 引用，
                                    # refs/identity 已在原 dict 上生效）
    # A4.2 metrics（累计——多次观测的聚合值，验收读"全程"而非最后一次）
    state.timings["identity_enrich_ms"] = \
        state.timings.get("identity_enrich_ms", 0) + int((perf_counter() - t_id) * 1000)
    state.timings["identity_nodes_enriched"] = \
        state.timings.get("identity_nodes_enriched", 0) + enriched
    state.timings["canonical_duplicates_removed"] = \
        state.timings.get("canonical_duplicates_removed", 0) + canonical_dropped

    # A4.3：DOM interaction-root bridge（AX 缺 dialog 语义时的最小兼容）。
    # 有 AX dialog（context_role）→ 走纯 AX（ActionSpace 的 in_dialog）；
    # 无 → 查可见 overlay（Bootstrap .modal.show 等事实标准），overlay 内
    # 的 action 标记 in_interaction_root——ActionSpace 只暴露它们（Restrict）。
    # 标记语义：interaction_root 是 observation metadata（source=ax 或
    # dom_overlay），action 上只标 in_interaction_root（不写 AX 字段）。
    state.interaction_root = None
    if any(e.get("context_role") == "dialog" for e in elements):
        state.interaction_root = {"source": "ax", "kind": "dialog"}
    else:
        overlay_refs, ov_desc = _detect_dom_overlay(page, elements)
        if overlay_refs:
            for e in elements:
                if e["ref"] in overlay_refs:
                    e["in_interaction_root"] = True
            state.interaction_root = ov_desc

    # A4.2（性能根因）：删除 Observation 全量 DOM actionability 验证——
    # 首页 1927 AX 节点 → 135 个 action × elementFromPoint 协议往返
    # → socket 压垮卡死。AX kind/disabled/tree 已足够决定 ActionSpace
    #（纯内存过滤）；"是否真能点"由选中后的 Playwright 实际执行权威
    # 回答（失败 → failed_actions → 本状态删 ref）。
    # actionable 字段退出 Observation（Observation = cheap + semantic，
    # Execution = strict + authoritative）。
    # A4.2 边界 8：backendDOMNodeId 只做 observation-time bridge——
    # identity 提取（_dom_identity_via_backend）完成后从元素表移除，
    # 绝不进入 explore_result / 缓存 / Planner 上下文 / DSL。
    for e in elements:
        e.pop("backend_dom_node_id", None)

    state.observations.append({
        "id": obs_id,
        "url": url,
        "title": _safe_title(page),
        "state_hash": state_hash,
        "snapshot": state.snapshot,
        "elements": state.elements,   # G1：observations 携带 state-scoped refs
        "interaction_root": state.interaction_root,   # A4.3：AX dialog / DOM overlay
    })

    # invariant：state.elements 的每个 ref 都必须有明确 state identity
    #（obsN:eM 格式）。裸 e1/e2 意味着状态所有权丢失——决策校验将失效。
    assert all(":" in e["ref"] for e in state.elements), (
        f"_record_page 后存在无 state 前缀的 ref: {state.elements[:5]}")
    return obs_id


def _safe_title(page) -> str:
    try:
        return page.title() or ""
    except Exception:
        return ""


def _pick_anchor_text(lines: list[str], node_text: str) -> str | None:
    """从容器文本行选锚点（跳过价格/短行/元素自身文本）。

    I1：与 Preflight 的 choose_scope_text 同族启发式，但需排除节点
    自身文本（Preflight 侧调用方已排除，这里统一处理）。
    """
    for line in lines:
        line = line.strip()
        if not line or len(line) < 2:
            continue
        if line == node_text:
            continue
        if PRICE_RE.fullmatch(line):
            continue
        return line[:60]
    return None


def _attach_legacy_dom_scope(page, candidates: list[dict]) -> None:
    """Legacy DOM scope enrichment（A4.1：从默认机制降级为少数兼容兜底）。

    只接收「重复的 action 且 AX context 不足」的候选——evidence/
    container 永远不进来（性能根因修复：CDP evidence 曾进 get_by_role
    空匹配 → inner_text 等满 15s ×N）。
      - 按 (role, name) 分组，单元素组跳过
      - get_by_role 前 count 防御（AX 节点存在 ≠ Playwright 能重新找到）
      - 不猜 ancestor（去掉 ../.. fallback——Restrict, don't repair）
      - 采集失败 → 无锚点 → Compiler 不附 scope → 运行时诚实拒绝
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for e in candidates:
        if e.get("kind") != "action":
            continue
        role = e.get("role")
        name = e.get("name")
        if not role or not name:
            continue
        groups.setdefault((role, name), []).append(e)

    for (role, name), group in groups.items():
        if len(group) <= 1:
            continue
        try:
            base = page.get_by_role(role, name=name, exact=True)
            count = base.count()
        except Exception:
            continue
        for i, e in enumerate(group):
            if e.get("scope_has_text"):
                continue
            if i >= count:
                continue   # 空 locator 防御（防 inner_text 等满超时）
            try:
                node = base.nth(i)
                # A4.2（微诊断定案）：identity 可能在 action 自身
                #（<a data-product-id="1">Add to cart</a>）——必须
                # ancestor-or-self，否则 container=0 → scope 全丢 →
                # 执行时 12 个 Add to cart 歧义。
                container = node.locator(
                    "xpath=ancestor-or-self::*[self::li or self::article "
                    " or @data-testid or @data-product-id or @data-item-id][1]"
                )
                if container.count() == 0:
                    continue   # 无明确业务容器 → 不猜（Restrict）
                raw = container.inner_text(timeout=300).strip()
                node_text = node.inner_text(timeout=300).strip()
                anchor = _pick_anchor_text(
                    [ln.strip() for ln in raw.splitlines() if ln.strip()],
                    node_text,
                )
                if anchor is None:
                    # 容器是 self（identity 在 action 自身），inner_text
                    # 只有按钮文本（商品名在父容器 productinfo）——
                    # 向上取父级文本提取锚点（仍非猜测：父容器是明确业务卡）。
                    parent = node.locator("xpath=..")
                    if parent.count():
                        raw2 = parent.inner_text(timeout=300).strip()
                        anchor = _pick_anchor_text(
                            [ln.strip() for ln in raw2.splitlines() if ln.strip()],
                            node_text,
                        )
            except Exception:
                continue
            if anchor:
                e["scope_has_text"] = anchor


# ── decide：LLM 决策（ref 强校验 + exploration_complete）──────────────────────

