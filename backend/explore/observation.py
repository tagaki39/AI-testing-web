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
    return "evidence"


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


def build_observation_elements(ax_nodes: list[AXNode]) -> list[ObservationElement]:
    """AX 树 → 结构化元素列表（扁平 ref 序列 + 层级/kind/语义上下文）。

    A2：Observation 的事实源从文本升级为结构化树；refs 保持 obsN:eM
    格式（Planner 只认 ref，AXNodeId 不进入 DSL）。
    """
    by_id = {n.ax_id: n for n in ax_nodes}
    active = [
        n for n in ax_nodes
        if not n.ignored and n.role not in ("generic", "text", "StaticText",
                                            "none", "presentation")
    ]
    ax_to_ref = {n.ax_id: f"e{i + 1}" for i, n in enumerate(active)}
    elements: list[ObservationElement] = []
    for n in active:
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
_MAX_OBSERVATIONS = 12       # 总 observation 上限（防膨胀）
_MAX_OBSERVATIONS_PER_URL = 5   # 同 URL 最多 5 个状态（登录表单 fill 的
# 可交互元素角色（element ref 表只收录这些——LLM 只能操作这些）
_INTERACTIVE_ROLES = {
    "button", "link", "textbox", "checkbox", "radio", "combobox",
    "searchbox", "menuitem", "tab", "option",
}

# 解析 aria_snapshot YAML 行的正则
_ELEMENT_RE = re.compile(r'-\s+(\w+)\s+"([^"]*)"')          # - button "Login"
_TEXT_RE = re.compile(r'-\s+text:\s*(.+)')                  # - text: Products
def _observe_until_stable(page, timeout_ms: int = 3000) -> str:
    """轮询页面 snapshot 直到状态稳定（等状态证据，不等固定时间）。

    评审 P0-2：点击后固定 300ms 等待不够——模态框/SPA 延迟渲染时
    观察还是旧状态 → 图里记 self-loop，新状态被归到下一次错误动作
    （BFC 场景：Add to cart 后 modal 未渲染，obs3→obs3，modal 状态
    被错误归因到下一次 text 点击的超时窗口）。
    轮询：snapshot hash 变化后连续两次相同 → 认为稳定。
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
    elements = _parse_elements(snapshot)   # ← ref 表（页面级 e1/e2，局部变量）

    # A2：CDP 结构化 A11y 增强——给 role 元素附加 kind/context/层级
    #（语义容器 context 供 A3 ActionSpace 用；CDP 不可用时静默跳过，
    #  保持 aria legacy 路径——观察能力兼容 fallback，非定位猜测）。
    try:
        ax_nodes = CDPAccessibilityProvider().capture(page)
        if ax_nodes:
            structured = build_observation_elements(ax_nodes)
            ax_by_sig: dict[tuple, ObservationElement] = {}
            for se in structured:
                ax_by_sig.setdefault((se.role, (se.name or "").strip()), se)
            for e in elements:
                if "role" not in e or not e.get("name"):
                    continue
                se = ax_by_sig.get((e["role"], str(e["name"]).strip()))
                if se is None:
                    continue
                e["kind"] = se.kind
                if se.context_role:
                    e["context_role"] = se.context_role
                    e["context_name"] = se.context_name
                if se.parent_ref:
                    e["parent_ref"] = se.parent_ref
                if se.backend_dom_node_id:
                    e["backend_dom_node_id"] = se.backend_dom_node_id
    except Exception:
        pass   # CDP 不可用 → 保持扁平（aria legacy）

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

    same_url_count = sum(1 for o in state.observations if o["url"] == url)
    if (len(state.observations) >= _MAX_OBSERVATIONS
            or same_url_count >= _MAX_OBSERVATIONS_PER_URL):
        # 观察预算满：不给当前状态一个"裸元素表"（无 state owner）。
        # 停止探索（主循环检测 done），比带着无主元素继续决策安全。
        state.history.append({
            "url": url,
            "action": "observation_cap",
            "error": "观察预算已满（total/per-url 上限），停止探索",
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

    # I1：同名重复元素采集容器文本锚点（只处理重复，非重复零开销）——
    # 先 enrich 再持久化，元素表与 observation 共享同一对象
    _attach_scope_context(state, page)

    # R3：观察期可操作性评估（Page Explorer 输出 actionable 标记——
    # 参考项目 page_explorer 的 verified 标记模式）。模态框打开时
    # 被遮挡的 Add to cart 标记 actionable=False → ActionSpace 直接
    # 过滤，模型看不到它（Restrict, don't repair）。
    # 性能边界（评审）：这层是 cheap/synchronous/best-effort——
    # 只做 elementFromPoint 毫秒级判断，绝不 trial（全量 trial 会
    # 被遮挡元素拖到秒级）。允许 false positive（执行失败再删 candidate）。
    t_as = perf_counter()
    # 惰性导入（R3.1 修复）：action_space 依赖本模块的 ExploreState
    #（类型注解），模块级导入会成环。拆分时漏掉的 import 导致 NameError
    # 被 except 静默吞掉——全部元素被误标 actionable=False，
    # ActionSpace 过滤所有 role 元素 → 探索残废（实测 actionable=True: 0）。
    from .action_space import _locator_for_element, validate_actionability
    for e in state.elements:
        if "role" in e:
            try:
                _, _, loc = _locator_for_element(page, e)
                e["actionable"], _ = validate_actionability(page, loc, "click")
            except Exception:
                e["actionable"] = False
    state.timings["action_space_ms"] += int((perf_counter() - t_as) * 1000)

    state.observations.append({
        "id": obs_id,
        "url": url,
        "title": _safe_title(page),
        "state_hash": state_hash,
        "snapshot": state.snapshot,
        "elements": state.elements,   # G1：observations 携带 state-scoped refs
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


def _attach_scope_context(state: ExploreState, page) -> None:
    """I1：为 observation 内同名重复的元素采集容器文本锚点（scope_has_text）。

    只处理重复的元素（role+name 或 text 键）——非重复零开销（决策 3
    性能上界）。锚点来自 DOM 祖先链（li/article/@data-testid/
    @data-product-id/@data-item-id）——比 a11y 树 parent 更贴近真实
    业务容器结构。文本节点（无 role 的 <a> 等）同样处理：图标前缀先
    剥掉再匹配（PUA 在 CSS 伪元素里，DOM 文本不含）。
    采集失败静默（无锚点 → Compiler 不附加 scope → 运行时诚实拒绝）。
    """
    name_counts: dict[tuple[str, str], int] = {}
    text_counts: dict[str, int] = {}
    for e in state.elements:
        if "role" in e and e.get("name"):
            key = (e["role"], e["name"])
            name_counts[key] = name_counts.get(key, 0) + 1
        elif e.get("text"):
            text_counts[e["text"]] = text_counts.get(e["text"], 0) + 1
    duplicates = {k for k, c in name_counts.items() if c > 1}
    dup_texts = {t for t, c in text_counts.items() if c > 1}
    if not duplicates and not dup_texts:
        return

    seen: dict[tuple[str, str], int] = {}
    text_seen: dict[str, int] = {}
    for e in state.elements:
        anchor = None
        try:
            if "role" in e and e.get("name"):
                key = (e["role"], e["name"])
                if key not in duplicates or e.get("scope_has_text"):
                    continue
                i = seen.get(key, 0)
                seen[key] = i + 1
                node = page.get_by_role(e["role"], name=e["name"], exact=True).nth(i)
            elif e.get("text") and e["text"] in dup_texts and not e.get("scope_has_text"):
                i = text_seen.get(e["text"], 0)
                text_seen[e["text"]] = i + 1
                # 图标前缀在 CSS 伪元素中 → 剥掉装饰后按 DOM 文本精确匹配
                node = page.get_by_text(
                    _strip_leading_decoration(e["text"]), exact=True,
                ).nth(i)
            else:
                continue
            container = node.locator(
                "xpath=ancestor::*[self::li or self::article or @data-testid"
                " or @data-product-id or @data-item-id][1]"
            )
            container_count = container.count()
            if container_count == 0:
                container = node.locator("xpath=../..")
                container_count = container.count()
            raw = container.inner_text().strip() if container_count > 0 else ""
            node_text = node.inner_text().strip()
            anchor = _pick_anchor_text(
                [ln.strip() for ln in raw.splitlines() if ln.strip()], node_text,
            )
        except Exception:
            continue
        if anchor:
            e["scope_has_text"] = anchor


# ── decide：LLM 决策（ref 强校验 + exploration_complete）──────────────────────

