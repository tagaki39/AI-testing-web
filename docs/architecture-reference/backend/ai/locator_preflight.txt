"""Static locator preflight — validate DSL targets against collected page elements.

Runs without a live browser; all data comes from the DOM snapshots that
``explore_page`` / ``explore_flow`` already collected.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# A11y role → Playwright role normalization
# ---------------------------------------------------------------------------

_A11Y_TO_PLAYWRIGHT_ROLE: dict[str, str] = {
    "searchbox": "search",
    "menuitemcheckbox": "checkbox",
    "menuitemradio": "radio",
}
"""Roles that differ between the a11y tree and Playwright's get_by_role()."""


def _normalize_role_for_playwright(role: str) -> str:
    """Map a11y roles to Playwright-compatible role names."""
    return _A11Y_TO_PLAYWRIGHT_ROLE.get(role, role)


# ---------------------------------------------------------------------------
# Target-type detection (mirrors semantic.py's _resolve_explicit_locator)
# ---------------------------------------------------------------------------

_CHAINED_SELECTOR_RE = re.compile(
    r"^(\.\w[\w-]*|#[\w-]+|\w[\w-]*\.\w[\w-]*)\s*>>?\s*text\s*=\s*(.+)$"
)
_COMPOUND_CSS_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9]*[\.\#\[\s\>:,~\+]")
_GENERIC_REPEATED_TARGETS = {"add to cart", "view product"}

# Matches: ... inside product "name"
_SCOPE_RE = re.compile(
    r"""\s+inside\s+product\s*["'](.+?)["']""",
    re.IGNORECASE,
)

# Matches: role="name" or role "name" at the start of a target
_ROLE_NAME_RE = re.compile(
    r'^(button|link|textbox|checkbox|radio|menuitem|combobox|listbox|option'
    r'|tab|switch|searchbox|heading|dialog|alert|navigation|main|form|region'
    r'|banner|contentinfo|complementary|article|list|listitem|img|progressbar'
    r'|slider|spinbutton|treeitem|menu|menubar|tablist|toolbar|status|timer'
    r'|tooltip|separator|group|presentation|none)'
    r"""\s*[=\s]\s*["'](.+?)["']""",
    re.IGNORECASE,
)
_KNOWN_TAGS = {
    "button", "input", "select", "textarea", "a", "form",
    "div", "span", "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "ul", "ol", "img", "label", "nav", "header", "main", "footer",
}


def _classify_target(target: str) -> tuple[str, str]:
    """Return (strategy, value) for a target string.

    Mirrors the detection logic from ``semantic.py:_resolve_explicit_locator``.
    """
    t = target.strip()

    if _CHAINED_SELECTOR_RE.match(t):
        return "chained_css_text", t
    if t.startswith("css="):
        return "css", t[4:]
    if t.startswith("xpath="):
        return "xpath", t[6:]
    if t.startswith("//"):
        return "xpath", t
    if t.startswith("#") or t.startswith("[") or t.startswith("."):
        return "css", t
    if t.startswith("data-testid="):
        return "data-testid", t[12:]
    tag_name = re.split(r"[\.\#\[\s\>:,~\+]", t, maxsplit=1)[0]
    if tag_name.lower() in _KNOWN_TAGS:
        return "css_tag", t
    if _COMPOUND_CSS_RE.match(t):
        return "css_tag", t
    return "semantic", t


def _target_is_generic_repeated_action(target: str) -> bool:
    return _normalize_text(target) in _GENERIC_REPEATED_TARGETS


# ---------------------------------------------------------------------------
# Text matching (mirrors fallback.py's _dom_snapshot_matches_target)
# ---------------------------------------------------------------------------

TOKEN_PATTERN = re.compile(r"[0-9a-z]+|[一-鿿]+", re.IGNORECASE)


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip().casefold())


def _tokenize(value: str | None) -> set[str]:
    if not value:
        return set()
    return set(TOKEN_PATTERN.findall(value))


def _cjk_char_tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    return {ch for ch in value if "一" <= ch <= "鿿"}


def _jaccard_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _css_selector_matches(target_css: str, element_css: str, element_id: str) -> bool:
    """Match a CSS selector target against element selectors without substring false positives.

    ``#login`` must match exactly ``#login``, not ``#login-button``.
    """
    if not target_css:
        return False
    t = target_css.strip()
    # Exact match
    if element_css == t:
        return True
    # ID selector: must be exact word match
    if t.startswith("#") and element_id and t[1:] == element_id:
        return True
    # Class selector: check if class appears in element's CSS selector as a word
    if t.startswith(".") and element_css:
        class_name = t[1:]
        return any(cls_part == class_name for cls_part in re.findall(r"\.([\w-]+)", element_css))
    # Compound selector: exact match on css_selector
    return element_css == t


def _text_matches_target(element: dict[str, Any], target: str) -> bool:
    """Check whether *element* matches a semantic *target*."""
    norm_target = _normalize_text(target)
    target_tokens = _tokenize(target)
    target_cjk = _cjk_char_tokens(target)

    for field in ("text", "aria_label", "placeholder", "data_testid", "name"):
        val = element.get(field)
        if not val:
            continue

        if _normalize_text(val) == norm_target:
            return True

        tokens = _tokenize(val)
        if target_tokens and target_tokens.issubset(tokens):
            return True
        if _jaccard_similarity(target_tokens, tokens) >= 0.5:
            return True

        cjk_set = _cjk_char_tokens(val)
        if target_cjk and _jaccard_similarity(target_cjk, cjk_set) >= 0.5:
            return True

    return False


# ---------------------------------------------------------------------------
# Stability scoring
# ---------------------------------------------------------------------------

def _compute_element_stability_static(element: dict[str, Any]) -> float:
    """Simplified stability score for a single element (no cross-element comparison)."""
    score = 0.30  # fallback
    if element.get("data_testid"):
        return 0.95
    eid = element.get("id") or ""
    if eid and not re.search(r"[0-9a-f]{8,}|auto\d+|tmp|rnd", eid):
        return 0.90
    if element.get("aria_label"):
        return 0.78
    if element.get("text"):
        return 0.55
    return score


# ---------------------------------------------------------------------------
# Main preflight function
# ---------------------------------------------------------------------------

def preflight_locators(
    dsl_steps: list[dict[str, Any]],
    page_elements: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate every target-bearing step against collected page elements.

    Parameters
    ----------
    dsl_steps:
        The raw ``steps`` list from a DSL case dict.
    page_elements:
        Flat list of element dicts from all explored pages.  Each element
        must have at least: ``tag``, ``text``, ``role``, ``aria_label``,
        ``placeholder``, ``data_testid``, ``css_selector``, ``id``,
        ``visible``, ``enabled``, and optionally ``page_state``,
        ``candidates``.

    Returns
    -------
    dict with:
      ``locator_confidence`` — overall "high" / "medium" / "low"
      ``step_results`` — per-step list of {step_index, target, confidence,
                         match_count, matched_elements, warnings}
      ``warnings`` — human-readable top-level warnings
    """
    step_results: list[dict[str, Any]] = []
    all_confidences: list[str] = []

    for idx, step in enumerate(dsl_steps):
        target = (step.get("target") or "").strip()
        if not target:
            continue

        strategy, parsed_value = _classify_target(target)
        matches: list[dict[str, Any]] = []

        if strategy in ("css", "xpath", "css_tag"):
            # Explicit selector: check css_selector/xpath/id match
            for el in page_elements:
                css = el.get("css_selector", "") or ""
                xp = el.get("xpath", "") or ""
                eid = el.get("id", "") or ""
                if strategy == "css_tag" and el.get("tag", "") == parsed_value:
                    matches.append(el)
                elif strategy == "css" and _css_selector_matches(parsed_value, css, eid):
                    matches.append(el)
                elif strategy == "xpath" and parsed_value in xp:
                    matches.append(el)
        elif strategy == "data-testid":
            for el in page_elements:
                if (el.get("data_testid") or "") == parsed_value:
                    matches.append(el)
        elif strategy == "chained_css_text":
            # Rough match: just check text portion
            text_part = parsed_value.split("text", 1)[-1].strip().lstrip("=").strip().strip("'\"")
            for el in page_elements:
                if _text_matches_target(el, text_part):
                    matches.append(el)
        else:
            # Semantic: text/label/placeholder matching
            for el in page_elements:
                if _text_matches_target(el, target):
                    matches.append(el)

        match_count = len(matches)

        # Determine confidence per step
        if match_count == 0:
            confidence = "low"
            warnings = [f"target \"{target}\" 在已采集的 {len(page_elements)} 个元素中未找到匹配"]
        elif match_count == 1:
            best = matches[0]
            verified = best.get("verified_selectors") or []
            stable = _compute_element_stability_static(best)
            if len(verified) > 0 and best.get("visible") and best.get("enabled"):
                confidence = "high"
                warnings = []
            elif stable >= 0.70 and best.get("visible") and best.get("enabled"):
                confidence = "high"
                warnings = []
            elif best.get("visible") and best.get("enabled"):
                confidence = "medium"
                warnings = [f"target \"{target}\" 唯一匹配但稳定性不足 (stable≈{stable:.2f})"]
            else:
                confidence = "low"
                reasons = []
                if not best.get("visible"):
                    reasons.append("不可见")
                if not best.get("enabled"):
                    reasons.append("未启用")
                warnings = [f"target \"{target}\" 唯一匹配但元素{'且'.join(reasons)}"]
        elif match_count <= 3:
            confidence = "medium"
            visible_matches = [m for m in matches if m.get("visible")]
            warnings = [
                f"target \"{target}\" 匹配到 {match_count} 个元素（预期唯一），请检查是否有歧义"
            ]
            if not visible_matches:
                confidence = "low"
                warnings.append("所有匹配元素均不可见")
        else:
            confidence = "low"
            warnings = [f"target \"{target}\" 匹配到 {match_count} 个元素，歧义过高"]

        all_confidences.append(confidence)
        step_results.append({
            "step_index": idx,
            "target": target,
            "strategy": strategy,
            "confidence": confidence,
            "match_count": match_count,
            "matched_elements": [
                {
                    "tag": m.get("tag"),
                    "text": m.get("text"),
                    "css_selector": m.get("css_selector"),
                    "visible": m.get("visible"),
                    "enabled": m.get("enabled"),
                    "candidates": m.get("candidates", []),
                    "verified_selectors": m.get("verified_selectors", []),
                }
                for m in matches[:5]
            ],
            "warnings": warnings,
        })

    # Overall confidence: lowest of all steps
    if not all_confidences:
        overall = "high"
    elif "low" in all_confidences:
        overall = "low"
    elif "medium" in all_confidences:
        overall = "medium"
    else:
        overall = "high"

    top_warnings: list[str] = []
    for sr in step_results:
        for w in sr.get("warnings", []):
            top_warnings.append(f"Step {sr['step_index']}: {w}")

    return {
        "locator_confidence": overall,
        "step_results": step_results,
        "warnings": top_warnings,
    }


def apply_preflight_to_dsl(
    dsl_case: dict[str, Any],
    a11y_nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run preflight against a11y_nodes, write 1:N candidates + confidence.

    Matches step.target against a11y_node.name (exact + substring).
    Each match produces 3 candidates (role exact / role fuzzy / text).
    Mutates *dsl_case* in place and returns it.
    """
    steps = dsl_case.get("steps", [])
    if not steps or not a11y_nodes:
        return dsl_case

    # Build parent→children index for scoped matching
    node_by_id: dict[str, dict[str, Any]] = {}
    children_of: dict[str, list[dict[str, Any]]] = {}
    for n in a11y_nodes:
        nid = n.get("node_id", "")
        if nid:
            node_by_id[nid] = n
        pid = n.get("parent_id")
        if pid:
            children_of.setdefault(pid, []).append(n)

    confidences: list[str] = []
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        target = (step.get("target") or "").strip()
        if not target:
            continue

        # Parse scope: "link "Add to cart" inside product "Blue Top""
        scope_name = None
        scope_match = _SCOPE_RE.search(target)
        if scope_match:
            scope_name = scope_match.group(1).strip().lower()
            # Strip scope suffix for element matching
            target_core = target[:scope_match.start()].strip()
        else:
            target_core = target

        target_lower = target_core.lower()
        matches: list[dict] = []

        if scope_name:
            # Scoped matching: find the product container whose children
            # include the product name, then match target against its children.
            for n in a11y_nodes:
                if (n.get("role") or "").lower() != "product":
                    continue
                pid = n.get("node_id", "")
                children = children_of.get(pid, [])
                # Check if any child contains the product name
                container_matches_scope = False
                for child in children:
                    child_name = (child.get("name") or "").lower()
                    if child_name and (child_name == scope_name or scope_name in child_name):
                        container_matches_scope = True
                        break
                if not container_matches_scope:
                    continue
                # Found the right product container — match target against children
                for child in children:
                    cname = (child.get("name") or "").lower()
                    if cname and (cname == target_lower or target_lower in cname):
                        matches.append(child)
        else:
            # Unscoped matching: match against all nodes
            for n in a11y_nodes:
                name = (n.get("name") or "").lower()
                if not name:
                    continue
                if name == target_lower or target_lower in name:
                    matches.append(n)

        match_count = len(matches)
        candidates: list[dict] = []

        if match_count > 0:
            for n in matches:
                role = _normalize_role_for_playwright(n["role"])
                name = n["name"]
                scope_ctx = {"scope_name": scope_name} if scope_name else {}

                for vs in n.get("verified_selectors", []):
                    vs_strategy = vs.get("strategy", "")
                    vs_selector = vs.get("selector", "")
                    if vs_strategy and vs_selector:
                        candidates.append({
                            "strategy": f"verified_{vs_strategy}",
                            "selector": vs_selector,
                            "semantic_value": name,
                            "pre_score": 1.0,
                            "pre_features": {
                                "verified": True,
                                "source": vs.get("source") or "a11y_backend_dom_node",
                                **scope_ctx,
                            },
                        })

                if scope_name:
                    # Scoped candidates: higher scores to prioritize them
                    candidates.extend([
                        {"strategy": "a11y_scoped_role_exact", "selector": role,
                         "semantic_value": name, "pre_score": 0.95,
                         "pre_features": {"source": "a11y_scoped_role_exact", **scope_ctx}},
                        {"strategy": "a11y_scoped_role_fuzzy", "selector": role,
                         "semantic_value": name, "pre_score": 0.85,
                         "pre_features": {"source": "a11y_scoped_role_fuzzy", **scope_ctx}},
                        {"strategy": "a11y_scoped_text_exact", "selector": name,
                         "semantic_value": name, "pre_score": 0.70,
                         "pre_features": {"source": "a11y_scoped_text_exact", **scope_ctx}},
                        {"strategy": "a11y_scoped_text_fuzzy", "selector": name,
                         "semantic_value": name, "pre_score": 0.60,
                         "pre_features": {"source": "a11y_scoped_text_fuzzy", **scope_ctx}},
                    ])
                else:
                    candidates.extend([
                        {"strategy": "role", "selector": role, "semantic_value": name,
                         "pre_score": 0.90, "pre_features": {"verified": True, "source": "a11y_role_exact"}},
                        {"strategy": "role_fuzzy", "selector": role, "semantic_value": name,
                         "pre_score": 0.75, "pre_features": {"source": "a11y_role_fuzzy"}},
                        {"strategy": "text", "selector": name, "semantic_value": name,
                         "pre_score": 0.55, "pre_features": {"source": "a11y_text_exact"}},
                    ])
            if _target_is_generic_repeated_action(target) and match_count > 1:
                step["locator_confidence"] = "low"
            else:
                step["locator_confidence"] = "high" if match_count == 1 else "medium"
        else:
            step["locator_confidence"] = "low"

        step["candidates"] = candidates
        step["match_count"] = match_count
        confidences.append(step["locator_confidence"])

    overall = "high"
    if "low" in confidences:
        overall = "low"
    elif "medium" in confidences:
        overall = "medium"

    dsl_case["_preflight"] = {
        "locator_confidence": overall,
        "warnings": [
            f"Step {i}: match_count={s.get('match_count',0)}"
            for i, s in enumerate(steps)
            if isinstance(s, dict) and s.get("match_count", 0) == 0
        ] + [
            f"Step {i}: target '{s.get('target')}' is a repeated product action; add product context"
            for i, s in enumerate(steps)
            if isinstance(s, dict)
            and _target_is_generic_repeated_action(str(s.get("target") or ""))
            and s.get("match_count", 0) > 1
        ],
    }
    return dsl_case


def _collect_candidates_from_matches(
    matched: list[dict[str, Any]],
    target: str,
) -> list[dict[str, Any]]:
    """Collect and deduplicate pre-scored candidates from matched elements.

    Verified selectors (live-verified during page exploration) are placed
    first with pre_score=1.0, followed by statically-scored candidates.
    """
    seen: set[tuple[str, str]] = set()
    flattened: list[dict[str, Any]] = []

    # --- Phase 1: verified selectors from matched elements (highest priority) ---
    for element in matched:
        for vs in element.get("verified_selectors", []):
            strategy = vs.get("strategy", "")
            selector = vs.get("selector", "") or vs.get("role", "") or ""
            key = (f"verified_{strategy}", selector)
            if key in seen:
                continue
            seen.add(key)
            flattened.append({
                "strategy": f"verified_{strategy}",
                "selector": selector,
                "semantic_value": vs.get("name") or vs.get("selector") or "",
                "pre_score": 1.0,
                "pre_features": {"verified": True, "source_strategy": strategy},
            })

    # --- Phase 2: static pre-scored candidates ---
    for element in matched:
        for candidate in element.get("candidates", []):
            strategy = candidate.get("strategy", "")
            selector = candidate.get("selector", "") or ""
            key = (strategy, selector)
            if key in seen:
                continue
            seen.add(key)
            flattened.append({
                "strategy": strategy,
                "selector": selector,
                "semantic_value": candidate.get("semantic_value"),
                "pre_score": candidate.get("pre_score", 0.0),
                "pre_features": candidate.get("pre_features"),
            })

    # Deduplicate selectors that differ only in prefix (e.g. "#login" vs "css=#login")
    deduped: list[dict[str, Any]] = []
    dedup_seen: set[str] = set()
    for candidate in sorted(flattened, key=lambda c: c["pre_score"], reverse=True):
        selector = candidate["selector"]
        normalized = selector.lstrip("#") if selector else ""
        if normalized in dedup_seen:
            continue
        dedup_seen.add(normalized)
        deduped.append(candidate)

    # Ensure tag fallbacks don't crowd out high-quality selectors — cap at 20
    return deduped[:20]
