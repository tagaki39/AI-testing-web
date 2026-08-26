import type { StepExecutionEvidence } from "../types/api";

export type LocatorStrategyBucket = "dom" | "vlm" | "correction" | "manual" | "not_applicable";

export function classifyLocatorStrategy(step: StepExecutionEvidence): LocatorStrategyBucket {
  const raw = `${step.resolved_by ?? ""} ${step.locator_trace?.match_strategy ?? ""}`.toLowerCase();
  if (step.intervention_request) return "manual";
  if (
    raw.includes("correction") ||
    raw.includes("tier0") ||
    raw.includes("test_id") ||
    raw.includes("xpath")
  )
    return "correction";
  if (raw.includes("visual") || raw.includes("vlm") || raw.includes("ai")) return "vlm";
  if (step.target || step.locator_trace) return "dom";
  return "not_applicable";
}

export function formatPassRate(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}
