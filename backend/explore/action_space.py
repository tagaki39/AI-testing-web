"""
action_space.py — 可操作性候选过滤（R3 拆分自 explore_flow）
  Restrict, don't repair：LLM 只能看到"当前可操作"的元素（ActionSpace）。
  观察期（_record_page）用 elementFromPoint 毫秒级评估标记 actionable；
  决策候选直接过滤。cheap filter should be cheap, not perfect——
  执行失败再删 candidate（failed_actions）。
"""
from locator.resolver import LocatorNotFoundError, LocatorAmbiguousError
from execution.runner import _resolve_locator
from .observation import ExploreState   # 类型注解（observation 只在函数内 import 本模块，无循环）

# 动作能力矩阵（评审 P0-1）：LLM 提议动作，确定性代码决定该动作
# 对该元素是否结构合法。text 元素（无 role）不在矩阵内 → 任何动作
# 都被拒（只做 evidence/context，不可作为动作目标——E2E 暴露模型
# 乱点商品名文本 "Blue Top" 导致 get_by_text 15s 超时）。
ACTION_CAPABILITIES = {
    "button": {"click", "press"},
    "link": {"click", "press"},
    "textbox": {"click", "fill", "press"},
    "searchbox": {"click", "fill", "press"},
    "combobox": {"click", "press"},
    "checkbox": {"click"},
    "radio": {"click"},
    "menuitem": {"click"},
    "tab": {"click"},
    "option": {"click"},
}


def _validate_action_target(action: str, element: dict | None) -> tuple[bool, str | None]:
    """动作-元素结构合法性。返回 (是否合法, 拒绝原因码)。"""
    if element is None or "role" not in element:
        return False, "NON_ACTIONABLE_REF"
    allowed = ACTION_CAPABILITIES.get(element.get("role"), set())
    if action not in allowed:
        return False, "ACTION_NOT_SUPPORTED_BY_ROLE"
    return True, None
def _build_action_space(state: ExploreState) -> list[dict]:
    """R3：ActionSpace——当前状态下 LLM 真正能选择的动作候选。

    评审核心："模型没权限选择错误动作，比告诉模型不要选错误动作更简单。"
    对元素表逐个做执行前可操作性检查（trial，短超时）——被模态框遮挡
    的 Add to cart 直接从候选消失；模型只能在 [View Cart, Continue
    Shopping, ...] 里选。这取代 modal hint / 复杂拒绝反馈等补丁。

    过滤规则：
      - 黑名单 ref（确定性失败过）剔除
      - 可操作性检查失败（不可见/不可用/被遮挡）剔除
    返回可操作元素列表（供 prompt 与决策校验共用）。
    """
    if not state.current_obs:
        return list(state.elements)
    obs_id = state.current_obs
    # A3：active dialog → interaction root = dialog subtree（CDP 结构化
    # context 附加；检测不到时回落到旧行为——elementFromPoint 兜底）
    in_dialog = any(
        e.get("context_role") == "dialog"
        for e in state.elements if "role" in e
    )
    usable: list[dict] = []
    for e in state.elements:
        if (obs_id, "click", e["ref"]) in state.failed_actions:
            continue   # 确定性失败过
        if "role" not in e:
            usable.append(e)   # 文本元素保留（wait_for/定位参考用）
            continue
        if in_dialog and e.get("context_role") != "dialog":
            continue   # dialog 打开时，dialog 外元素被遮罩（Restrict）
        # 观察期已评估的 actionable 标记（R3：不预测，用 Page Explorer 输出）；
        # 无标记的元素保守剔除（防御：观察期评估失败 = 不可操作）
        if e.get("actionable"):
            usable.append(e)
    return usable


def _locator_for_element(page, element: dict) -> tuple[dict, dict | None, object]:
    """element → (target, scope, locator)。

    E1：_act 与 validate_actionability 共用的定位构建——
    I1 同名重复元素带 scope_has_text 锚点消歧。
    """
    target = {"role": element["role"], "name": element["name"]} if "role" in element \
        else {"text": element["text"]}
    scope = {"has_text": element["scope_has_text"]} if element.get("scope_has_text") else None
    _, locator = _resolve_locator(page, target, scope=scope)
    return target, scope, locator


def validate_actionability(page, locator, action: str) -> tuple[bool, str]:
    """R3：可操作性评估（观察期 Page Explorer 输出 actionable 标记用）。

    性能关键：观察期对全部可交互元素评估（60-70 个/页）——
    不能用 click(trial=True)（被遮挡元素要等满 3s 超时，模态框场景
    10+ 个被挡元素 = 30s/观察）。改 elementFromPoint 同步检测（毫秒级）：
      - 可见性 / 可用性（is_visible / is_enabled）
      - 遮挡：命中点最上层元素必须是 target 或其内部，或 target
        是它的祖先（el.contains(target)——修复：命中点落在父容器
        空白区时 el 是祖先，原判定误报遮挡）

    返回 (是否可操作, 拒绝原因码)。bounded：全部同步调用。
    """
    try:
        if not locator.is_visible():
            return False, "TARGET_NOT_VISIBLE"
        if not locator.is_enabled():
            return False, "TARGET_DISABLED"
        if action == "click":
            box = locator.bounding_box()
            if not box:
                return False, "TARGET_NOT_VISIBLE"
            handle = locator.element_handle()
            obscured = page.evaluate(
                """([x, y, target]) => {
                    const el = document.elementFromPoint(x, y);
                    if (!el) return true;
                    return !(el === target || target.contains(el) || el.contains(target));
                }""",
                [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, handle],
            )
            if obscured:
                return False, "TARGET_OBSCURED"
        return True, ""
    except Exception:
        return False, "ACTIONABILITY_CHECK_ERROR"

