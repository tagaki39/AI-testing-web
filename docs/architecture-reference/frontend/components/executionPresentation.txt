import { Tag } from "antd";

import type {
  ExecutionStatus,
  FailureCategory,
  OverviewWindowDays,
  ReportScopeType,
  StoredCaseExecutionSummary,
} from "../types/api";

export const FAILURE_CATEGORY_LABELS: Record<FailureCategory, string> = {
  configuration: "配置",
  locator: "定位",
  assertion: "断言",
  navigation: "导航",
  network: "网络",
  runner: "运行器",
};

export function renderExecutionStatus(status: ExecutionStatus) {
  const colorMap: Record<ExecutionStatus, string> = {
    passed: "success",
    failed: "error",
    running: "processing",
    needs_intervention: "warning",
  };
  const labelMap: Record<ExecutionStatus, string> = {
    passed: "通过",
    failed: "失败",
    running: "运行中",
    needs_intervention: "待人工介入",
  };
  return (
    <Tag className="status-tag" color={colorMap[status]}>
      {labelMap[status]}
    </Tag>
  );
}

export function formatDuration(durationMs?: number | null) {
  if (durationMs === null || durationMs === undefined) {
    return "-";
  }
  return `${durationMs} ms`;
}

export function formatPassRate(passRate: number) {
  return `${(passRate * 100).toFixed(1)}%`;
}

export function truncateText(value: string | null, maxLength = 72) {
  if (!value) {
    return "-";
  }
  if (value.length <= maxLength) {
    return value;
  }
  return `${value.slice(0, maxLength - 1)}…`;
}

export function buildExecutionLink(record: Pick<StoredCaseExecutionSummary, "id" | "failed_step_index">) {
  if (record.failed_step_index === null || record.failed_step_index === undefined) {
    return `/run/${record.id}`;
  }
  return `/run/${record.id}#step-${record.failed_step_index + 1}`;
}

export interface ExecutionListQueryState {
  scope_type?: ReportScopeType;
  project_id?: number;
  window_days?: OverviewWindowDays;
  status?: ExecutionStatus;
  case_id?: number;
  failure_category?: FailureCategory;
  failure_fingerprint?: string;
  root_cause_title?: string;
  page?: number;
}

export function buildExecutionsSearch(query: ExecutionListQueryState) {
  const search = new URLSearchParams();
  if (query.scope_type) {
    search.set("scope_type", query.scope_type);
  }
  if (query.project_id) {
    search.set("project_id", String(query.project_id));
  }
  if (query.window_days) {
    search.set("window_days", String(query.window_days));
  }
  if (query.status) {
    search.set("status", query.status);
  }
  if (query.case_id) {
    search.set("case_id", String(query.case_id));
  }
  if (query.failure_category) {
    search.set("failure_category", query.failure_category);
  }
  if (query.failure_fingerprint) {
    search.set("failure_fingerprint", query.failure_fingerprint);
  }
  if (query.root_cause_title) {
    search.set("root_cause_title", query.root_cause_title);
  }
  if (query.page && query.page > 1) {
    search.set("page", String(query.page));
  }
  return search.toString();
}

export function buildExecutionsPath(query: ExecutionListQueryState) {
  const search = buildExecutionsSearch(query);
  return search ? `/executions?${search}` : "/executions";
}
