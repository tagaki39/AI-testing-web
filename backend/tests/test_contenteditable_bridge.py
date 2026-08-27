"""S2-P3 contenteditable DOM Bridge 稳定性回归。"""

import sys
from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from explore.observation import _bridge_contenteditable  # noqa: E402
from compiler import _element_to_locator  # noqa: E402


def _launch():
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    return pw, browser, browser.new_page()


def test_single_visible_candidate_selector_excludes_hidden_nodes() -> None:
    pw, browser, page = _launch()
    try:
        page.set_content("""
          <div contenteditable="true" style="display:none">hidden</div>
          <div contenteditable="true">visible</div>
        """)
        elements: list[dict] = []
        _bridge_contenteditable(page, elements)
        assert len(elements) == 1
        assert ":visible" in elements[0]["css"]
        assert page.locator(elements[0]["css"]).count() == 1
    finally:
        browser.close()
        pw.stop()


def test_same_name_distinct_unique_nodes_are_both_preserved() -> None:
    pw, browser, page = _launch()
    try:
        page.set_content("""
          <div id="prompt-a" contenteditable="true" aria-label="Prompt"></div>
          <div id="prompt-b" contenteditable="true" aria-label="Prompt"></div>
        """)
        elements: list[dict] = []
        _bridge_contenteditable(page, elements)
        assert len(elements) == 2
        assert {item["css"] for item in elements} == {"#prompt-a", "#prompt-b"}
    finally:
        browser.close()
        pw.stop()


def test_ambiguous_candidates_without_unique_selector_are_rejected() -> None:
    pw, browser, page = _launch()
    try:
        page.set_content("""
          <div data-testid="prompt" contenteditable="true"></div>
          <div data-testid="prompt" contenteditable="true"></div>
        """)
        elements: list[dict] = []
        _bridge_contenteditable(page, elements)
        assert elements == []
    finally:
        browser.close()
        pw.stop()


def test_disabled_or_ax_covered_candidate_is_not_bridged() -> None:
    pw, browser, page = _launch()
    try:
        page.set_content(
            '<div id="disabled" contenteditable="true" '
            'aria-disabled="true" aria-label="Disabled"></div>'
        )
        elements: list[dict] = []
        _bridge_contenteditable(page, elements)
        assert elements == []

        page.set_content(
            '<div id="prompt" contenteditable="true" aria-label="Prompt"></div>'
        )
        elements = [{
            "ref": "e1", "role": "textbox", "name": "Prompt", "kind": "action",
        }]
        _bridge_contenteditable(page, elements)
        assert len(elements) == 1
    finally:
        browser.close()
        pw.stop()


def test_aria_labelledby_precedes_placeholder() -> None:
    pw, browser, page = _launch()
    try:
        page.set_content("""
          <span id="prompt-label">提示词</span>
          <div id="prompt" contenteditable="true"
               aria-labelledby="prompt-label" placeholder="placeholder"></div>
        """)
        elements: list[dict] = []
        _bridge_contenteditable(page, elements)
        assert len(elements) == 1
        assert elements[0]["name"] == "提示词"
        assert elements[0]["css"] == "#prompt"
    finally:
        browser.close()
        pw.stop()


def test_compiler_keeps_bridge_css_as_only_locator_evidence() -> None:
    locator = _element_to_locator(SimpleNamespace(
        role="textbox", name="Prompt", text=None, identity=None,
        css="#prompt",
    ))
    assert locator.css == "#prompt"
    assert locator.role is None
    assert locator.name is None
