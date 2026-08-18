"""
observation.py — 状态观察（R3 拆分自 explore_flow）
  ARIA 抓取 → 元素解析 → 状态去重 → canonical obs id（ObservationStore 语义）
  一个状态只有一个事实源：current_obs 只由 _record_page 设置。
"""
import hashlib
import re
from dataclasses import dataclass, field
from time import perf_counter

from resolver import PRICE_RE, _strip_leading_decoration, choose_scope_text
from runner import _resolve_locator

_MAX_SNAPSHOT_CHARS = 6000   # 裁剪后快照的最终兜底上限（重组后仍超限才截断）
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

    # 状态哈希：snapshot 变化 = 页面状态变化（即使 URL 相同）
    state_hash = hashlib.sha256(snapshot.encode()).hexdigest()[:10]

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

