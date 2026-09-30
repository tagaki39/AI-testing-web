import { describe, expect, it } from "vitest";
import { classifyLocatorStrategy, formatDuration, formatPassRate } from "./executionMetrics";
import type { StepExecutionEvidence } from "../types/api";

function makeStep(overrides: Partial<StepExecutionEvidence> = {}): StepExecutionEvidence {
  return {
    step_index: 0,
    action: "click",
    status: "passed",
    console_events: [],
    network_events: [],
    ...overrides,
  };
}

describe("classifyLocatorStrategy", () => {
  it("returns 'manual' when intervention_request is present", () => {
    const step = makeStep({
      intervention_request: {
        page_url: "https://example.com",
        target_description: "button",
        dom_snapshot: [],
      },
    });
    expect(classifyLocatorStrategy(step)).toBe("manual");
  });

  it("returns 'correction' when resolved_by contains 'correction'", () => {
    const step = makeStep({ resolved_by: "correction_tier0" });
    expect(classifyLocatorStrategy(step)).toBe("correction");
  });

  it("returns 'correction' when match_strategy contains 'tier0'", () => {
    const step = makeStep({
      locator_trace: {
        target: "btn",
        match_strategy: "tier0_hit",
        candidates: [],
      },
    });
    expect(classifyLocatorStrategy(step)).toBe("correction");
  });

  it("returns 'correction' when match_strategy contains 'test_id'", () => {
    const step = makeStep({
      locator_trace: {
        target: "btn",
        match_strategy: "test_id_direct",
        candidates: [],
      },
    });
    expect(classifyLocatorStrategy(step)).toBe("correction");
  });

  it("returns 'correction' when match_strategy contains 'xpath'", () => {
    const step = makeStep({
      locator_trace: {
        target: "btn",
        match_strategy: "xpath_fallback",
        candidates: [],
      },
    });
    expect(classifyLocatorStrategy(step)).toBe("correction");
  });

  it("returns 'vlm' when resolved_by contains 'visual'", () => {
    const step = makeStep({ resolved_by: "visual_locate" });
    expect(classifyLocatorStrategy(step)).toBe("vlm");
  });

  it("returns 'vlm' when resolved_by contains 'vlm'", () => {
    const step = makeStep({ resolved_by: "vlm_candidate" });
    expect(classifyLocatorStrategy(step)).toBe("vlm");
  });

  it("returns 'vlm' when resolved_by contains 'ai'", () => {
    const step = makeStep({ resolved_by: "ai_visual" });
    expect(classifyLocatorStrategy(step)).toBe("vlm");
  });

  it("returns 'vlm' when match_strategy contains 'visual'", () => {
    const step = makeStep({
      locator_trace: {
        target: "btn",
        match_strategy: "visual_match",
        candidates: [],
      },
    });
    expect(classifyLocatorStrategy(step)).toBe("vlm");
  });

  it("returns 'dom' when step has target but no special strategy", () => {
    const step = makeStep({ target: "登录按钮" });
    expect(classifyLocatorStrategy(step)).toBe("dom");
  });

  it("returns 'dom' when step has locator_trace but no special strategy", () => {
    const step = makeStep({
      locator_trace: {
        target: "btn",
        match_strategy: "css_selector",
        candidates: [],
      },
    });
    expect(classifyLocatorStrategy(step)).toBe("dom");
  });

  it("returns 'not_applicable' when no target, locator_trace, or intervention", () => {
    const step = makeStep({ action: "goto", value: "/home" });
    expect(classifyLocatorStrategy(step)).toBe("not_applicable");
  });

  it("prioritizes manual over correction", () => {
    const step = makeStep({
      resolved_by: "correction",
      intervention_request: {
        page_url: "https://example.com",
        target_description: "button",
        dom_snapshot: [],
      },
    });
    expect(classifyLocatorStrategy(step)).toBe("manual");
  });

  it("prioritizes correction over vlm", () => {
    const step = makeStep({ resolved_by: "visual_correction" });
    expect(classifyLocatorStrategy(step)).toBe("correction");
  });
});

describe("formatPassRate", () => {
  it("formats rate as percentage with one decimal", () => {
    expect(formatPassRate(0.856)).toBe("85.6%");
  });

  it("formats 1.0 as 100.0%", () => {
    expect(formatPassRate(1.0)).toBe("100.0%");
  });

  it("formats 0.0 as 0.0%", () => {
    expect(formatPassRate(0.0)).toBe("0.0%");
  });
});

describe("formatDuration", () => {
  it("formats milliseconds under 1000 as ms", () => {
    expect(formatDuration(500)).toBe("500ms");
  });

  it("formats exactly 999 as ms", () => {
    expect(formatDuration(999)).toBe("999ms");
  });

  it("formats 1000 as seconds", () => {
    expect(formatDuration(1000)).toBe("1.0s");
  });

  it("formats 1500 as seconds with decimal", () => {
    expect(formatDuration(1500)).toBe("1.5s");
  });

  it("formats 0 as ms", () => {
    expect(formatDuration(0)).toBe("0ms");
  });
});
