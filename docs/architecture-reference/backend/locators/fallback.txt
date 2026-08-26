"""Fallback locator chain with a11y-first resolution.

Priority chain:
  Tier 0: Manual correction store
  Tier 1: A11y semantic locator (role="name" format)
  Tier 2: VLM visual locate (screenshot-based)
  Tier 3: Coordinate click fallback
  Tier 4: Intervention needed
"""

from __future__ import annotations

import base64
import logging
from collections import OrderedDict
from threading import Lock

from app.locators.ai_visual import (
    AILocateResult,
    locate_element_by_vision,
    record_ai_visual_cache_hit,
    record_ai_visual_cache_invalidation,
    record_ai_visual_cache_miss,
)
from app.locators.corrections import CorrectionRecord, CorrectionStore, normalize_target_description
from app.locators.semantic import (
    LocatorResolutionError,
    ResolvedLocator,
    resolve_semantic_locator,
)
from app.locators.url_pattern import generalize_url
from app.schemas.executions import LocatorTrace
from app.core.structured_logging import get_structured_logger


logger = logging.getLogger(__name__)
slog = get_structured_logger(__name__)

AI_VISUAL_SESSION_CACHE_MAX_ENTRIES = 128
_AI_VISUAL_SESSION_CACHE: OrderedDict[tuple[str, str], str] = OrderedDict()
_AI_VISUAL_SESSION_CACHE_LOCK = Lock()


# ── Intervention error ───────────────────────────────────────────────────────


class InterventionNeededError(RuntimeError):
    """Raised when all active locator tiers fail."""

    def __init__(
        self,
        *,
        target: str,
        page_url: str,
        ai_candidate: AILocateResult | None = None,
        tier1_trace: LocatorTrace | None = None,
        vlm_failure_reason: str | None = None,
    ) -> None:
        super().__init__(f"All locate tiers failed for target: {target}")
        self.target = target
        self.page_url = page_url
        self.ai_candidate = ai_candidate
        self.tier1_trace = tier1_trace
        self.vlm_failure_reason = vlm_failure_reason


# ── Main entry point ─────────────────────────────────────────────────────────


def resolve_with_fallback(
    page,
    target: str,
    *,
    target_strategy: str | None = None,
    correction_store: CorrectionStore | None = None,
    execution_id: int | None = None,
    prefer_input: bool = False,
    require_visible: bool = True,
    require_enabled: bool = False,
    expected_text: str | None = None,
) -> ResolvedLocator:
    """Resolve *target* to a Playwright locator using the fallback chain."""
    page_url = getattr(page, "url", "") or ""
    tier1_trace: LocatorTrace | None = None
    ai_candidate: AILocateResult | None = None
    cache_key = _build_ai_visual_cache_key(page_url=page_url, target=target)

    # ── Tier 0: Manual correction ────────────────────────────────────────
    correction = (
        correction_store.find_active_correction(page_url=page_url, target_description=target)
        if correction_store is not None and page_url
        else None
    )
    if correction is not None:
        resolved = _try_resolve_correction(
            page,
            target=target,
            correction=correction,
            correction_store=correction_store,
            execution_id=execution_id,
        )
        if resolved is not None:
            slog.locator_fallback("correction_reuse", data={
                "target": target,
                "correction_id": correction.id,
                "correction_type": correction.correction_type,
                "success": True,
            }, execution_id=execution_id)
            return resolved

    # ── Tier 0.5: AI visual session cache ────────────────────────────────
    cached_resolution = _try_resolve_cached_ai_locator(page, target=target, cache_key=cache_key)
    if cached_resolution is not None:
        slog.locator_fallback("ai_cache_lookup", data={
            "target": target,
            "cache_hit": True,
        }, execution_id=execution_id)
        return cached_resolution

    # ── Tier 1: A11y semantic locator ────────────────────────────────────
    try:
        resolved = resolve_semantic_locator(
            page,
            target,
            target_strategy=target_strategy,
            prefer_input=prefer_input,
            require_visible=require_visible,
            require_enabled=require_enabled,
        )
        slog.locator_fallback("semantic_resolve", data={
            "target": target,
            "selected_strategy": resolved.strategy,
        }, execution_id=execution_id)
        return resolved
    except LocatorResolutionError as exc:
        tier1_trace = exc.trace
        slog.locator_fallback("fallback_tier_advance", data={
            "target": target,
            "from_tier": "semantic",
            "to_tier": "vlm_visual",
            "reason": "A11y semantic resolution failed",
        }, execution_id=execution_id, level=logging.WARNING)

    # ── Tier 2: VLM visual locate ────────────────────────────────────────
    ai_candidate = _try_ai_visual_locate(page, target=target)
    vlm_failure_reason = _get_vlm_failure_reason()
    if ai_candidate is not None:
        coord_resolved = _try_coordinate_click_fallback(
            page, target=target, ai_candidate=ai_candidate,
        )
        if coord_resolved is not None:
            slog.locator_fallback("vlm_locate", data={
                "target": target,
                "selected_strategy": "coordinate_click",
                "success": True,
            }, execution_id=execution_id)
            return coord_resolved

    # ── Tier 3: Intervention needed ──────────────────────────────────────
    slog.locator_fallback("intervention_needed", data={
        "target": target,
        "tiers_attempted": ["correction", "semantic", "vlm_visual", "coordinate_click"],
        "vlm_failure_reason": vlm_failure_reason,
    }, execution_id=execution_id, level=logging.ERROR)
    raise InterventionNeededError(
        target=target,
        page_url=page_url,
        ai_candidate=ai_candidate,
        tier1_trace=tier1_trace,
        vlm_failure_reason=vlm_failure_reason,
    )


# ── Correction resolution ────────────────────────────────────────────────────


def _try_resolve_correction(
    page,
    *,
    target: str,
    correction: CorrectionRecord,
    correction_store: CorrectionStore | None,
    execution_id: int | None,
) -> ResolvedLocator | None:
    try:
        locator = _build_locator_from_correction(page, correction)
        locator.wait_for(state="visible", timeout=3000)
    except Exception as exc:
        updated_correction = (
            correction_store.record_failure(correction.id, execution_id=execution_id)
            if correction_store is not None
            else correction
        )
        logger.warning(
            "Correction reuse failed id=%s target=%s consecutive_failures=%s is_active=%s error=%s",
            correction.id,
            target,
            updated_correction.consecutive_failures if updated_correction is not None else correction.consecutive_failures,
            updated_correction.is_active if updated_correction is not None else correction.is_active,
            exc,
        )
        return None

    updated_correction = (
        correction_store.record_success(correction.id, execution_id=execution_id)
        if correction_store is not None
        else correction
    )
    strategy = f"correction:{correction.correction_type}"
    return ResolvedLocator(
        strategy=strategy,
        locator=locator,
        trace=LocatorTrace(
            target=target,
            match_strategy=strategy,
            selection_reason=(
                f"Matched correction #{correction.id} after "
                f"{(updated_correction.verified_count if updated_correction is not None else correction.verified_count)} "
                "successful reuses."
            ),
        ),
    )


def _build_locator_from_correction(page, correction: CorrectionRecord):
    if correction.correction_type == "test_id":
        return page.get_by_test_id(correction.correction_value)
    if correction.correction_type == "xpath":
        value = correction.correction_value
        selector = value if value.startswith("xpath=") else f"xpath={value}"
        return page.locator(selector)
    return page.locator(correction.correction_value)


# ── AI visual session cache ──────────────────────────────────────────────────


def _build_ai_visual_cache_key(*, page_url: str, target: str) -> tuple[str, str] | None:
    if not page_url:
        return None
    normalized_target = normalize_target_description(target)
    if not normalized_target:
        return None
    return (generalize_url(page_url), normalized_target)


def _try_resolve_cached_ai_locator(
    page,
    *,
    target: str,
    cache_key: tuple[str, str] | None,
) -> ResolvedLocator | None:
    if cache_key is None:
        return None
    selector = _get_cached_ai_selector(cache_key)
    if selector is None:
        record_ai_visual_cache_miss()
        logger.debug("AI visual session cache_miss page_url_pattern=%s target=%s", cache_key[0], target)
        return None

    locator = page.locator(selector)
    try:
        locator.wait_for(state="visible", timeout=3000)
    except Exception as exc:
        _invalidate_cached_ai_selector(cache_key)
        record_ai_visual_cache_invalidation()
        logger.debug(
            "AI visual session cache_invalidated page_url_pattern=%s target=%s selector=%s error=%s",
            cache_key[0],
            target,
            selector,
            exc,
        )
        return None

    record_ai_visual_cache_hit()
    logger.debug("AI visual session cache_hit page_url_pattern=%s target=%s", cache_key[0], target)
    return ResolvedLocator(
        strategy="ai_visual_cache",
        locator=locator,
        trace=LocatorTrace(
            target=target,
            match_strategy="ai_visual_cache",
            selection_reason=f"Matched cached AI visual selector for {cache_key[0]}.",
        ),
    )


def _get_cached_ai_selector(cache_key: tuple[str, str]) -> str | None:
    with _AI_VISUAL_SESSION_CACHE_LOCK:
        selector = _AI_VISUAL_SESSION_CACHE.get(cache_key)
        if selector is None:
            return None
        _AI_VISUAL_SESSION_CACHE.move_to_end(cache_key)
        return selector


def _store_cached_ai_selector(cache_key: tuple[str, str], selector: str) -> None:
    with _AI_VISUAL_SESSION_CACHE_LOCK:
        _AI_VISUAL_SESSION_CACHE[cache_key] = selector
        _AI_VISUAL_SESSION_CACHE.move_to_end(cache_key)
        while len(_AI_VISUAL_SESSION_CACHE) > AI_VISUAL_SESSION_CACHE_MAX_ENTRIES:
            _AI_VISUAL_SESSION_CACHE.popitem(last=False)


def _invalidate_cached_ai_selector(cache_key: tuple[str, str]) -> None:
    with _AI_VISUAL_SESSION_CACHE_LOCK:
        _AI_VISUAL_SESSION_CACHE.pop(cache_key, None)


def _clear_ai_visual_session_cache() -> None:
    with _AI_VISUAL_SESSION_CACHE_LOCK:
        _AI_VISUAL_SESSION_CACHE.clear()


# ── VLM visual locate ────────────────────────────────────────────────────────


def _try_ai_visual_locate(page, *, target: str) -> AILocateResult | None:
    try:
        screenshot_base64 = _take_screenshot_base64(page)
        viewport = getattr(page, "viewport_size", None) or {}
        width = int(viewport.get("width", 0))
        height = int(viewport.get("height", 0))
        if width <= 0 or height <= 0:
            return None
        return locate_element_by_vision(
            screenshot_base64=screenshot_base64,
            target_description=target,
            image_width=width,
            image_height=height,
            deep_locate=True,
        )
    except Exception as exc:
        logger.warning("AI visual fallback failed for target=%s error=%s", target, exc)
        return None


def _get_vlm_failure_reason() -> str | None:
    """Retrieve the last VLM failure reason from the runtime state."""
    try:
        from app.locators.ai_visual import RUNTIME_STATE, _STATE_LOCK
        with _STATE_LOCK:
            return RUNTIME_STATE.last_failure_reason or None
    except Exception:
        return None


def _take_screenshot_base64(page) -> str:
    screenshot_bytes = page.screenshot(full_page=True)
    return base64.b64encode(screenshot_bytes).decode("utf-8")


# ── Coordinate click fallback ────────────────────────────────────────────────


def _try_coordinate_click_fallback(
    page,
    *,
    target: str,
    ai_candidate: AILocateResult,
) -> ResolvedLocator | None:
    """Use VLM bbox coordinates directly when DOM selector extraction fails."""
    x, y = ai_candidate.center
    if not (isinstance(x, int) and isinstance(y, int) and x >= 0 and y >= 0):
        return None
    return ResolvedLocator(
        strategy="ai_coordinate_click",
        locator=page.locator("body"),
        click_coordinates=(x, y),
        trace=LocatorTrace(
            target=target,
            match_strategy="ai_coordinate_click",
            selection_reason=f"VLM located at ({x},{y}), using coordinate click.",
        ),
    )


__all__ = [
    "InterventionNeededError",
    "resolve_with_fallback",
]
