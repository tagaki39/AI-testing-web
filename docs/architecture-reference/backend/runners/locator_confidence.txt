"""Locator confidence gate: pre-verify low-confidence targets with VLM before execution.

When the DSL generation AI marks a step's locator_confidence as "low",
this module intercepts the resolution flow and attempts a VLM-based
visual verification before falling through to the normal 4-tier fallback chain.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def preverify_with_vlm(page, target: str) -> object | None:
    """Attempt VLM pre-verification for a low-confidence target.

    Returns a ResolvedLocator if VLM successfully locates and verifies the element,
    or None if VLM is unavailable, disabled, or fails.
    """
    from app.locators.ai_visual import locate_element_by_vision, RUNTIME_STATE, _STATE_LOCK
    from app.locators.fallback import _take_screenshot_base64, _snapshot_dom_element_at_point, _dom_snapshot_matches_target
    from app.locators.semantic import ResolvedLocator, LocatorTrace
    from app.schemas.executions import LocatorTrace as LocatorTraceSchema

    try:
        screenshot_base64 = _take_screenshot_base64(page)
        viewport = getattr(page, "viewport_size", None) or {}
        width = int(viewport.get("width", 0))
        height = int(viewport.get("height", 0))
        if width <= 0 or height <= 0:
            return None

        ai_result = locate_element_by_vision(
            screenshot_base64=screenshot_base64,
            target_description=target,
            image_width=width,
            image_height=height,
        )
        if ai_result is None:
            logger.info("VLM preverify: no result for target=%s", target)
            return None

        snapshot = _snapshot_dom_element_at_point(page, *ai_result.center)
        if snapshot is None or not _dom_snapshot_matches_target(snapshot, target):
            logger.info("VLM preverify: DOM mismatch for target=%s at %s", target, ai_result.center)
            return None

        selector = snapshot.css_selector or (f"xpath={snapshot.xpath}" if snapshot.xpath else None)
        if selector is None:
            return None

        locator = page.locator(selector)
        locator.wait_for(state="visible", timeout=3000)

        logger.info("VLM preverify: success for target=%s via %s", target, selector)
        return ResolvedLocator(
            strategy="vlm_preverify",
            locator=locator,
            trace=LocatorTrace(
                target=target,
                match_strategy="vlm_preverify",
                selection_reason=f"VLM pre-verification located element at {ai_result.center} (confidence={ai_result.confidence:.2f}).",
            ),
            click_coordinates=ai_result.center,
        )
    except Exception as exc:
        logger.warning("VLM preverify failed for target=%s: %s", target, exc)
        return None
