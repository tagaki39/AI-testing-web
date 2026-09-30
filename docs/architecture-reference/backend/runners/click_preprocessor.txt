"""Click preprocessor: diagnose overlay interception and apply recovery strategies.

When Playwright reports ``intercepts pointer events``, this module inspects the
blocking element, classifies it, and applies a degradation chain:

  等待(wait) → 关闭(dismiss) → 避让(avoid) → 强制(force) → 移除(remove)

Only *dismissible* overlay types (modal, toast, cookie_banner) are eligible for
the destructive "remove" strategy — structural elements (fixed headers, layout
overlaps) are never removed from the DOM to avoid masking real UX issues.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_INTERCEPT_PATTERN = re.compile(r"intercepts pointer events", re.IGNORECASE)
_HIDDEN_ELEMENT_PATTERN = re.compile(
    r"resolved to hidden|is not visible|not visible|empty bounding box|zero bounding box",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Diagnosis script — runs in the browser to classify the blocking element
# ---------------------------------------------------------------------------

_DIAGNOSE_OVERLAY_SCRIPT = """
(element) => {
  if (!(element instanceof Element)) return null;
  const style = window.getComputedStyle(element);
  const role = (element.getAttribute('role') || '').toLowerCase();
  const tag = element.tagName.toLowerCase();
  const cls = (element.className || '').toString().toLowerCase();
  const id = (element.id || '').toLowerCase();
  const text = (element.innerText || '').slice(0, 100);

  let overlayType = 'unknown';
  let dismissible = false;

  if (role === 'dialog' || role === 'alertdialog' || cls.includes('modal') || id.includes('modal')) {
    overlayType = 'modal'; dismissible = true;
  } else if (cls.includes('toast') || cls.includes('notification') || cls.includes('snackbar')) {
    overlayType = 'toast'; dismissible = true;
  } else if (cls.includes('loading') || cls.includes('spinner') || cls.includes('progress') || id.includes('loading')) {
    overlayType = 'loading'; dismissible = false;
  } else if (cls.includes('cookie') || cls.includes('banner') || cls.includes('consent') || cls.includes('gdpr')) {
    overlayType = 'cookie_banner'; dismissible = true;
  } else if (style.position === 'fixed' || style.position === 'sticky') {
    overlayType = 'fixed_element'; dismissible = false;
  } else if (['h1','h2','h3','header','nav'].includes(tag)) {
    overlayType = 'layout_overlap'; dismissible = false;
  }

  return { overlayType, dismissible, tag, text, role, position: style.position };
}
"""

# Script to find a dismiss button inside an overlay element
_FIND_DISMISS_BUTTON_SCRIPT = """
(element) => {
  const candidates = element.querySelectorAll(
    'button, [role="button"], [class*="close"], [class*="dismiss"], [aria-label*="close"], [aria-label*="dismiss"]'
  );
  for (const btn of candidates) {
    const t = (btn.innerText || btn.getAttribute('aria-label') || '').toLowerCase();
    if (t.includes('close') || t.includes('dismiss') || t.includes('x') || t.includes('×') || t.includes('skip') || t.includes('accept')) {
      return btn;
    }
  }
  return null;
}
"""

# Script to check if a point on the page is obscured
_IS_POINT_OBSCURED_SCRIPT = """
([x, y, excludeTag, excludeText]) => {
  const hits = document.elementsFromPoint(x, y);
  for (const el of hits) {
    const tag = el.tagName.toLowerCase();
    const text = (el.innerText || '').slice(0, 50);
    if (tag === excludeTag && text.startsWith(excludeText)) continue;
    if (tag === 'html' || tag === 'body') continue;
    // target element or its children — skip
    return false;
  }
  return true;
}
"""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ClickPrecheckResult:
    """Result of a click attempt with optional recovery."""

    succeeded: bool
    original_error: Exception | None = None
    diagnosis: dict | None = None
    recovery_strategy: str | None = None
    recovery_detail: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_interception_error(exc: Exception) -> bool:
    return bool(_INTERCEPT_PATTERN.search(str(exc)))


def _extract_interceptor_tag(error_message: str) -> tuple[str, str]:
    """Extract tag name and text preview of the intercepting element from the error."""
    m = re.search(r"<(\w+)[^>]*>([^<]*)</\1>", error_message)
    if m:
        return m.group(1).lower(), (m.group(2) or "").strip()[:50]
    return "", ""


def _diagnose(page, error_message: str) -> dict | None:
    """Run diagnosis JS in the browser to classify the overlay."""
    tag, text = _extract_interceptor_tag(error_message)
    if not tag:
        return None
    try:
        # Find the intercepting element by tag + text
        locator = page.locator(f"{tag}:has-text('{text}')").first
        if locator.count() == 0:
            return None
        result = locator.evaluate(_DIAGNOSE_OVERLAY_SCRIPT)
        return result
    except Exception:
        return {"overlayType": "unknown", "dismissible": False, "tag": tag, "text": text}


# ---------------------------------------------------------------------------
# Recovery strategies
# ---------------------------------------------------------------------------

def _try_wait(page, locator, *, click_coordinates, max_retries, interval_ms) -> ClickPrecheckResult | None:
    """Strategy 1: wait for transient overlays (loading spinners, toasts) to auto-dismiss."""
    for attempt in range(max_retries):
        page.wait_for_timeout(interval_ms)
        try:
            if click_coordinates is not None:
                page.mouse.click(*click_coordinates)
            else:
                locator.click(timeout=2000)
            return ClickPrecheckResult(
                succeeded=True,
                recovery_strategy="wait",
                recovery_detail=f"waited {(attempt + 1) * interval_ms}ms for overlay to dismiss",
            )
        except Exception:
            continue
    return None


def _try_dismiss(page, locator, *, click_coordinates, diagnosis) -> ClickPrecheckResult | None:
    """Strategy 2: actively dismiss the overlay (Escape / close button / click outside)."""
    tag = diagnosis.get("tag", "")
    text = diagnosis.get("text", "")
    # 2a. Press Escape
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        if click_coordinates is not None:
            page.mouse.click(*click_coordinates)
        else:
            locator.click(timeout=3000)
        return ClickPrecheckResult(
            succeeded=True,
            recovery_strategy="dismiss",
            recovery_detail="pressed Escape to dismiss overlay",
        )
    except Exception:
        pass

    # 2b. Find and click close/dismiss button inside the overlay
    try:
        overlay = page.locator(f"{tag}:has-text('{text}')").first
        if overlay.count() > 0:
            close_btn = overlay.evaluate_handle(_FIND_DISMISS_BUTTON_SCRIPT).as_element()
            if close_btn is not None:
                close_btn.click(timeout=2000)
                page.wait_for_timeout(300)
                if click_coordinates is not None:
                    page.mouse.click(*click_coordinates)
                else:
                    locator.click(timeout=3000)
                return ClickPrecheckResult(
                    succeeded=True,
                    recovery_strategy="dismiss",
                    recovery_detail="clicked close button inside overlay",
                )
    except Exception:
        pass

    # 2c. Click outside the overlay (top-left corner of viewport as fallback)
    try:
        page.mouse.click(10, 10)
        page.wait_for_timeout(300)
        if click_coordinates is not None:
            page.mouse.click(*click_coordinates)
        else:
            locator.click(timeout=3000)
        return ClickPrecheckResult(
            succeeded=True,
            recovery_strategy="dismiss",
            recovery_detail="clicked outside overlay to dismiss",
        )
    except Exception:
        pass

    return None


def _try_avoid(page, locator, *, click_coordinates) -> ClickPrecheckResult | None:
    """Strategy 3: scroll the target element away from the overlay."""
    if click_coordinates is not None:
        # Coordinate clicks can't be scrolled — skip
        return None
    try:
        locator.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        locator.click(timeout=5000)
        return ClickPrecheckResult(
            succeeded=True,
            recovery_strategy="avoid",
            recovery_detail="scrolled target element into clear view",
        )
    except Exception:
        pass

    # Second attempt: scroll up by half viewport to move past fixed headers
    try:
        viewport = page.viewport_size or {"height": 720}
        page.mouse.wheel(0, -(viewport["height"] // 2))
        page.wait_for_timeout(300)
        locator.click(timeout=5000)
        return ClickPrecheckResult(
            succeeded=True,
            recovery_strategy="avoid",
            recovery_detail="scrolled up to avoid fixed header overlap",
        )
    except Exception:
        pass

    return None


def _try_force(locator) -> ClickPrecheckResult | None:
    """Strategy 4: force click bypassing Playwright actionability checks."""
    try:
        locator.click(force=True, timeout=5000)
        return ClickPrecheckResult(
            succeeded=True,
            recovery_strategy="force",
            recovery_detail="force=True bypassed actionability check",
        )
    except Exception:
        pass

    # JS click as fallback within force strategy
    try:
        locator.evaluate("el => el.click()")
        return ClickPrecheckResult(
            succeeded=True,
            recovery_strategy="force",
            recovery_detail="JavaScript el.click() as force fallback",
        )
    except Exception:
        pass

    return None


def _try_remove(page, locator, *, click_coordinates, diagnosis) -> ClickPrecheckResult | None:
    """Strategy 5: remove the blocking element from DOM (dismissible types only)."""
    if not diagnosis.get("dismissible"):
        return None
    tag = diagnosis.get("tag", "")
    text = diagnosis.get("text", "")
    try:
        overlay = page.locator(f"{tag}:has-text('{text}')").first
        if overlay.count() > 0:
            overlay.evaluate("el => el.remove()")
            page.wait_for_timeout(200)
            if click_coordinates is not None:
                page.mouse.click(*click_coordinates)
            else:
                locator.click(timeout=5000)
            return ClickPrecheckResult(
                succeeded=True,
                recovery_strategy="remove",
                recovery_detail=f"removed <{tag}> overlay element from DOM",
            )
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def click_with_precheck(
    page,
    locator,
    *,
    click_coordinates: tuple[int, int] | None = None,
    max_wait_retries: int = 3,
    wait_interval_ms: int = 1000,
) -> ClickPrecheckResult:
    """Attempt a click; if intercepted, diagnose and apply recovery strategies.

    Non-interception errors (element not found, generic timeout) are returned
    immediately without entering the recovery chain.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    # Initial click attempt
    try:
        if click_coordinates is not None:
            page.mouse.click(*click_coordinates)
        else:
            locator.click()
        return ClickPrecheckResult(succeeded=True)
    except PlaywrightTimeoutError as exc:
        error_message = str(exc)
        # Hidden element (e.g. modal button during CSS animation):
        # force-click bypasses Playwright's visibility check.
        if _HIDDEN_ELEMENT_PATTERN.search(error_message):
            logger.info("Click target is hidden, trying force click: %s", error_message[:200])
            result = _try_force(locator)
            if result is not None:
                logger.info("Recovery succeeded: %s — %s", result.recovery_strategy, result.recovery_detail)
                return result
            return ClickPrecheckResult(succeeded=False, original_error=exc)
        if not _is_interception_error(exc):
            return ClickPrecheckResult(succeeded=False, original_error=exc)
        # Fall through to recovery
    except Exception as exc:
        return ClickPrecheckResult(succeeded=False, original_error=exc)

    # Phase 1: Diagnose
    diagnosis = _diagnose(page, error_message)
    overlay_type = (diagnosis or {}).get("overlayType", "unknown")
    dismissible = (diagnosis or {}).get("dismissible", False)
    logger.info(
        "Click interception detected: overlayType=%s, dismissible=%s, tag=%s",
        overlay_type, dismissible, (diagnosis or {}).get("tag", "?"),
    )

    # Phase 2: Degradation chain
    # Strategy 1 — wait (for transient overlays)
    if overlay_type in ("loading", "toast", "unknown"):
        result = _try_wait(page, locator, click_coordinates=click_coordinates,
                           max_retries=max_wait_retries, interval_ms=wait_interval_ms)
        if result is not None:
            logger.info("Recovery succeeded: %s — %s", result.recovery_strategy, result.recovery_detail)
            return result

    # Strategy 2 — dismiss (for dismissible overlays)
    if dismissible:
        result = _try_dismiss(page, locator, click_coordinates=click_coordinates, diagnosis=diagnosis)
        if result is not None:
            logger.info("Recovery succeeded: %s — %s", result.recovery_strategy, result.recovery_detail)
            return result

    # Strategy 3 — avoid (scroll away from fixed elements / layout overlap)
    if overlay_type in ("fixed_element", "layout_overlap", "unknown"):
        result = _try_avoid(page, locator, click_coordinates=click_coordinates)
        if result is not None:
            logger.info("Recovery succeeded: %s — %s", result.recovery_strategy, result.recovery_detail)
            return result

    # Strategy 4 — force (all types)
    if click_coordinates is None:
        result = _try_force(locator)
        if result is not None:
            logger.info("Recovery succeeded: %s — %s", result.recovery_strategy, result.recovery_detail)
            return result

    # Strategy 5 — remove (dismissible types only)
    if dismissible:
        result = _try_remove(page, locator, click_coordinates=click_coordinates, diagnosis=diagnosis)
        if result is not None:
            logger.info("Recovery succeeded: %s — %s", result.recovery_strategy, result.recovery_detail)
            return result

    logger.warning("All recovery strategies exhausted for overlayType=%s", overlay_type)
    return ClickPrecheckResult(
        succeeded=False,
        diagnosis=diagnosis,
        original_error=RuntimeError(
            f"Click intercepted by <{(diagnosis or {}).get('tag', '?')}> "
            f"(type={overlay_type}), all recovery strategies failed."
        ),
    )
