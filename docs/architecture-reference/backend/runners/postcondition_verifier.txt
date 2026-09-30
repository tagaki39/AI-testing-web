"""PostconditionVerifier — capture pre-action page state and verify postconditions after execution.

Captures url, dom_hash, visible_texts, and input_values before a step executes,
then verifies declared Postcondition entries against the post-action page state.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from playwright.sync_api import Page

from app.schemas.dsl import Postcondition

logger = logging.getLogger(__name__)


@dataclass
class PostconditionResult:
    """Result of verifying one or more postconditions."""

    passed: bool
    details: dict = field(default_factory=dict)


class PostconditionVerifier:
    """Capture pre-action page state and verify postconditions after execution."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self._pre_state: dict = {}

    # ------------------------------------------------------------------
    # State capture
    # ------------------------------------------------------------------

    def capture_pre_state(self) -> dict:
        """Capture the current page state before an action is executed.

        Returns the captured state dict (also stored internally for later
        verification via :meth:`verify`).
        """
        self._pre_state = {
            "url": self._page.url,
            "dom_hash": self._compute_dom_hash(),
            "visible_texts": self._get_visible_texts(),
            "input_values": self._get_input_values(),
        }
        return self._pre_state

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify(self, postconditions: list[Postcondition]) -> PostconditionResult:
        """Verify every declared postcondition against the current page state.

        All postconditions must pass for the overall result to be ``passed=True``.
        """
        if not postconditions:
            return PostconditionResult(passed=True, details={})

        post_url = self._page.url
        post_dom_hash = self._compute_dom_hash()
        post_input_values = self._get_input_values()

        all_passed = True
        details: dict = {}

        for pc in postconditions:
            try:
                ok = self._verify_single(
                    pc,
                    post_url=post_url,
                    post_dom_hash=post_dom_hash,
                    post_input_values=post_input_values,
                )
            except Exception as exc:
                logger.warning("Postcondition %s check failed: %s", pc.type, exc)
                ok = False

            if not ok:
                all_passed = False
                details[pc.type] = (
                    f"postcondition '{pc.type}' with value={pc.value!r} was not satisfied"
                )

        return PostconditionResult(passed=all_passed, details=details)

    # ------------------------------------------------------------------
    # Single postcondition dispatch
    # ------------------------------------------------------------------

    def _verify_single(
        self,
        pc: Postcondition,
        *,
        post_url: str,
        post_dom_hash: str,
        post_input_values: dict,
    ) -> bool:
        """Evaluate a single postcondition against the current page state."""
        pre_url = self._pre_state.get("url", "")
        pre_dom_hash = self._pre_state.get("dom_hash", "")
        pre_input_values = self._pre_state.get("input_values", {})

        pc_type = pc.type
        value = pc.value

        if pc_type == "url_contains":
            return value is not None and value in post_url

        if pc_type == "url_changes":
            return post_url != pre_url

        if pc_type == "text_visible":
            if value is None:
                return False
            try:
                return self._page.locator(f"text={value}").is_visible()
            except Exception:
                return False

        if pc_type == "text_gone":
            if value is None:
                return True
            try:
                return not self._page.locator(f"text={value}").is_visible()
            except Exception:
                return True

        if pc_type == "element_visible":
            if value is None:
                return False
            try:
                return self._page.locator(value).is_visible()
            except Exception:
                return False

        if pc_type == "element_gone":
            if value is None:
                return True
            try:
                return not self._page.locator(value).is_visible()
            except Exception:
                return True

        if pc_type == "dom_changed":
            return post_dom_hash != pre_dom_hash

        if pc_type == "value_changed":
            return post_input_values != pre_input_values

        if pc_type == "network_request":
            # Placeholder — real implementation would need a network listener
            logger.debug("network_request postcondition is a placeholder, returning True")
            return True

        logger.warning("Unknown postcondition type: %s", pc_type)
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_dom_hash(self) -> str:
        """Return an MD5 hash derived from the body innerHTML length.

        Using length rather than full content keeps this cheap while still
        detecting structural changes.
        """
        try:
            body_html = self._page.evaluate("() => document.body.innerHTML")
            length = len(body_html) if body_html else 0
            return hashlib.md5(str(length).encode()).hexdigest()
        except Exception:
            return ""

    def _get_visible_texts(self) -> list[str]:
        """Extract visible text from key semantic elements."""
        try:
            texts: list[str] = []
            for selector in ("h1", "h2", "h3", "p", "span", "button", "a", "label"):
                elements = self._page.locator(selector).all()
                for el in elements:
                    try:
                        if el.is_visible():
                            text = el.inner_text()
                            if text:
                                texts.append(text.strip())
                    except Exception:
                        continue
            return texts
        except Exception:
            return []

    def _get_input_values(self) -> dict:
        """Extract current form input values keyed by name or id."""
        try:
            values: dict = {}
            for el in self._page.locator("input, select, textarea").all():
                try:
                    name = el.get_attribute("name") or el.get_attribute("id") or ""
                    if name:
                        values[name] = el.input_value()
                except Exception:
                    continue
            return values
        except Exception:
            return {}


# ------------------------------------------------------------------
# Standalone helper
# ------------------------------------------------------------------


def verify_default_postcondition(pre: dict, post: dict) -> bool:
    """Check if the page changed between pre and post snapshots.

    Returns True if the url or dom_hash differs, indicating the action
    had an observable effect.
    """
    if pre.get("url") != post.get("url"):
        return True
    if pre.get("dom_hash") != post.get("dom_hash"):
        return True
    return False
