import type {
  AIPlanningDraft,
  AIPlanningSessionDetail,
  AIPlanningSessionSummary,
  AIPlanningTurnResponse,
  AISettingsOverview,
  AISettings,
  AISettingsUpdatePayload,
  CreatePlanningSessionPayload,
  BatchUpdateCorrectionStatePayload,
  CaseMutationPayload,
  CaseExecutionRequest,
  CreateCorrectionPayload,
  DSLCasePayload,
  DslGenerationFeedbackPayload,
  DslGenerationPromptVariant,
  DslGenerationRejectionReasonCode,
  DslGenerationRunStatus,
  GeneratePlanningDraftsPayload,
  GenerateDslRequest,
  GenerateDslResponse,
  DSLValidationResult,
  ExecutionsOverview,
  LinkProjectPayload,
  CreateProjectInSessionPayload,
  LocatorCorrectionsOverview,
  LoginPayload,
  LogoutResponse,
  OverviewWindowDays,
  PaginatedCases,
  ProjectSummary,
  ProjectSummaryInSession,
  ReportPreference,
  ReportScopeType,
  SendPlanningMessagePayload,
  DslGenerationFeedbackStatus,
  GenerateDslImportMode,
  GenerateDslMode,
  CurrentUser,
  StoredDslGenerationRunDetail,
  StoredDslGenerationRunSummary,
  StoredCaseDetail,
  StoredCaseExecutionDetail,
  StoredCaseExecutionSummary,
  StoredCaseSummary,
  StoredLocatorCorrection,
  StoredLocatorCorrectionEvent,
  UpdatePlanningDraftStatusPayload,
  UpdateCorrectionStatePayload,
} from "../types/api";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";
export const AUTH_UNAUTHORIZED_EVENT = "auth:unauthorized";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) {
        message = payload.detail;
      }
    } catch {
      // Ignore non-JSON errors.
    }
    if (response.status === 401) {
      window.dispatchEvent(new CustomEvent(AUTH_UNAUTHORIZED_EVENT));
    }
    throw new ApiError(message, response.status);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export function login(payload: LoginPayload) {
  return request<CurrentUser>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function logout() {
  return request<LogoutResponse>("/api/v1/auth/logout", {
    method: "POST",
  });
}

export function getCurrentUser() {
  return request<CurrentUser>("/api/v1/auth/me");
}

export function getCases(params?: { project_id?: number }) {
  const search = new URLSearchParams();
  if (params?.project_id != null) {
    search.set("project_id", String(params.project_id));
  }
  const query = search.toString();
  return request<PaginatedCases>(`/api/v1/cases${query ? `?${query}` : ""}`);
}

export function getProjects() {
  return request<ProjectSummary[]>("/api/v1/projects");
}

export function createProject(payload: { name: string; description?: string }) {
  return request<ProjectSummary>("/api/v1/projects", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateProject(projectId: number, payload: { name?: string; description?: string }) {
  return request<ProjectSummary>(`/api/v1/projects/${projectId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function deleteProject(projectId: number) {
  return request<void>(`/api/v1/projects/${projectId}`, { method: "DELETE" });
}

export function getReportPreference() {
  return request<ReportPreference>("/api/v1/reports/preferences");
}

export function updateReportPreference(payload: ReportPreference) {
  return request<ReportPreference>("/api/v1/reports/preferences", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function getCaseDetail(caseId: number) {
  return request<StoredCaseDetail>(`/api/v1/cases/${caseId}`);
}

export function createCase(payload: CaseMutationPayload) {
  return request<StoredCaseDetail>("/api/v1/cases", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function deleteCase(caseId: number) {
  return request<void>(`/api/v1/cases/${caseId}`, {
    method: "DELETE",
  });
}

export function batchDeleteCases(caseIds: number[]) {
  return request<void>("/api/v1/cases/batch", {
    method: "DELETE",
    body: JSON.stringify({ case_ids: caseIds }),
  });
}

export function updateCase(caseId: number, payload: CaseMutationPayload) {
  return request<StoredCaseDetail>(`/api/v1/cases/${caseId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function validateDslCase(payload: DSLCasePayload) {
  return request<DSLValidationResult>("/api/v1/dsl/validate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function generateDslCase(payload: GenerateDslRequest) {
  return request<GenerateDslResponse>("/api/v1/dsl/generate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getDslGenerationRuns(params?: {
  status?: DslGenerationRunStatus;
  feedback_status?: DslGenerationFeedbackStatus;
  generation_mode?: GenerateDslMode;
  import_mode?: GenerateDslImportMode;
  prompt_variant?: DslGenerationPromptVariant;
  rejection_reason_code?: DslGenerationRejectionReasonCode;
  has_risk_flags?: boolean;
  model_name?: string;
  project_id?: number;
  case_id?: number;
  created_from?: string;
  created_to?: string;
  limit?: number;
  offset?: number;
}) {
  const search = new URLSearchParams();
  if (params?.status) {
    search.set("status", params.status);
  }
  if (params?.feedback_status) {
    search.set("feedback_status", params.feedback_status);
  }
  if (params?.generation_mode) {
    search.set("generation_mode", params.generation_mode);
  }
  if (params?.import_mode) {
    search.set("import_mode", params.import_mode);
  }
  if (params?.prompt_variant) {
    search.set("prompt_variant", params.prompt_variant);
  }
  if (params?.rejection_reason_code) {
    search.set("rejection_reason_code", params.rejection_reason_code);
  }
  if (typeof params?.has_risk_flags === "boolean") {
    search.set("has_risk_flags", String(params.has_risk_flags));
  }
  if (params?.model_name) {
    search.set("model_name", params.model_name);
  }
  if (params?.project_id != null) {
    search.set("project_id", String(params.project_id));
  }
  if (params?.case_id != null) {
    search.set("case_id", String(params.case_id));
  }
  if (params?.created_from) {
    search.set("created_from", params.created_from);
  }
  if (params?.created_to) {
    search.set("created_to", params.created_to);
  }
  if (params?.limit != null) {
    search.set("limit", String(params.limit));
  }
  if (params?.offset != null) {
    search.set("offset", String(params.offset));
  }
  const query = search.toString();
  return request<StoredDslGenerationRunSummary[]>(`/api/v1/dsl/generations${query ? `?${query}` : ""}`);
}

export function getDslGenerationRunDetail(generationId: number) {
  return request<StoredDslGenerationRunDetail>(`/api/v1/dsl/generations/${generationId}`);
}

export function recordDslGenerationFeedback(generationId: number, payload: DslGenerationFeedbackPayload) {
  return request<StoredDslGenerationRunSummary>(`/api/v1/dsl/generations/${generationId}/feedback`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteDslGenerationRun(generationId: number) {
  return request<void>(`/api/v1/dsl/generations/${generationId}`, { method: "DELETE" });
}

export function createPlanningSession(payload: CreatePlanningSessionPayload) {
  return request<AIPlanningSessionDetail>("/api/v1/ai-planning/sessions", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getPlanningSession(sessionId: number) {
  return request<AIPlanningSessionDetail>(`/api/v1/ai-planning/sessions/${sessionId}`);
}

/** SSE event log entry returned by the replay API. */
export interface SessionEventLogEntry {
  seq: number;
  event_type: string;
  event_data: Record<string, unknown>;
  message_id: number | null;
  created_at: string;
}

/**
 * Fetch SSE event logs for a planning session.
 * Used to replay missed events after a page refresh.
 * @param sessionId - The planning session ID
 * @param afterSeq - Only return events with seq > afterSeq (default 0 = all)
 */
export function getSessionEvents(sessionId: number, afterSeq: number = 0) {
  const params = new URLSearchParams({ after_seq: String(afterSeq) });
  return request<SessionEventLogEntry[]>(
    `/api/v1/ai-planning/sessions/${sessionId}/events?${params}`,
  );
}

export function listPlanningSessions() {
  return request<AIPlanningSessionSummary[]>("/api/v1/ai-planning/sessions");
}

export function deletePlanningSession(sessionId: number) {
  return request<void>(`/api/v1/ai-planning/sessions/${sessionId}`, {
    method: "DELETE",
  });
}

export function listSessionProjects(sessionId: number) {
  return request<ProjectSummaryInSession[]>(`/api/v1/ai-planning/sessions/${sessionId}/projects`);
}

export function linkProjectToSession(sessionId: number, payload: LinkProjectPayload) {
  return request<ProjectSummaryInSession>(`/api/v1/ai-planning/sessions/${sessionId}/projects`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function unlinkProjectFromSession(sessionId: number, projectId: number) {
  return request<void>(`/api/v1/ai-planning/sessions/${sessionId}/projects/${projectId}`, {
    method: "DELETE",
  });
}

export function createProjectInSession(sessionId: number, payload: CreateProjectInSessionPayload) {
  return request<ProjectSummaryInSession>(`/api/v1/ai-planning/sessions/${sessionId}/projects:create`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function saveAndExecuteDrafts(sessionId: number, draftIds: number[], execute: boolean = true) {
  return request<AIPlanningTurnResponse>(`/api/v1/ai-planning/sessions/${sessionId}/drafts:save-and-execute`, {
    method: "POST",
    body: JSON.stringify({ draft_ids: draftIds, execute }),
  });
}

export function sendPlanningMessage(sessionId: number, payload: SendPlanningMessagePayload) {
  return request<AIPlanningTurnResponse>(`/api/v1/ai-planning/sessions/${sessionId}/messages`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function generatePlanningDrafts(sessionId: number, payload: GeneratePlanningDraftsPayload) {
  return request<AIPlanningTurnResponse>(`/api/v1/ai-planning/sessions/${sessionId}/drafts:generate`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updatePlanningDraftStatus(draftId: number, payload: UpdatePlanningDraftStatusPayload) {
  return request<AIPlanningDraft>(`/api/v1/ai-planning/drafts/${draftId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deletePlanningDraft(draftId: number) {
  return request<void>(`/api/v1/ai-planning/drafts/${draftId}`, {
    method: "DELETE",
  });
}

export function getAISettings() {
  return request<AISettings>("/api/v1/settings/ai");
}

export function getAISettingsOverview() {
  return request<AISettingsOverview>("/api/v1/settings/ai/overview");
}

export function updateAISettings(payload: AISettingsUpdatePayload) {
  return request<AISettings>("/api/v1/settings/ai", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function executeCase(caseId: number, payload: CaseExecutionRequest) {
  return request<StoredCaseExecutionDetail>(`/api/v1/cases/${caseId}/execute`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function createCorrection(payload: CreateCorrectionPayload) {
  return request<StoredLocatorCorrection>("/api/v1/corrections", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getCorrections(params: {
  target_description?: string;
  page_url?: string;
  is_active?: boolean;
  limit?: number;
  offset?: number;
}) {
  const search = new URLSearchParams();
  if (params.target_description) {
    search.set("target_description", params.target_description);
  }
  if (params.page_url) {
    search.set("page_url", params.page_url);
  }
  if (typeof params.is_active === "boolean") {
    search.set("is_active", String(params.is_active));
  }
  if (params.limit) {
    search.set("limit", String(params.limit));
  }
  if (params.offset != null) {
    search.set("offset", String(params.offset));
  }
  const query = search.toString();
  return request<StoredLocatorCorrection[]>(`/api/v1/corrections${query ? `?${query}` : ""}`);
}

export function updateCorrectionState(correctionId: number, payload: UpdateCorrectionStatePayload) {
  return request<StoredLocatorCorrection>(`/api/v1/corrections/${correctionId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function batchUpdateCorrectionState(payload: BatchUpdateCorrectionStatePayload) {
  return request<StoredLocatorCorrection[]>("/api/v1/corrections/bulk", {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteCorrection(correctionId: number) {
  return request<void>(`/api/v1/corrections/${correctionId}`, { method: "DELETE" });
}

export function getCorrectionsOverview(window_days: OverviewWindowDays) {
  return request<LocatorCorrectionsOverview>(`/api/v1/corrections/overview?window_days=${window_days}`);
}

export function getCorrectionEvents(correctionId: number, params?: { limit?: number; offset?: number }) {
  const search = new URLSearchParams();
  if (params?.limit != null) {
    search.set("limit", String(params.limit));
  }
  if (params?.offset != null) {
    search.set("offset", String(params.offset));
  }
  const query = search.toString();
  return request<StoredLocatorCorrectionEvent[]>(
    `/api/v1/corrections/${correctionId}/events${query ? `?${query}` : ""}`,
  );
}

export function getExecutions(params: {
  project_id?: number;
  case_id?: number;
  status?: string;
  window_days?: OverviewWindowDays;
  failure_category?: string;
  failure_fingerprint?: string;
  limit?: number;
  offset?: number;
}) {
  const search = new URLSearchParams();
  if (params.project_id) {
    search.set("project_id", String(params.project_id));
  }
  if (params.case_id) {
    search.set("case_id", String(params.case_id));
  }
  if (params.status) {
    search.set("status", params.status);
  }
  if (params.window_days) {
    search.set("window_days", String(params.window_days));
  }
  if (params.failure_category) {
    search.set("failure_category", params.failure_category);
  }
  if (params.failure_fingerprint) {
    search.set("failure_fingerprint", params.failure_fingerprint);
  }
  if (params.limit) {
    search.set("limit", String(params.limit));
  }
  if (params.offset != null) {
    search.set("offset", String(params.offset));
  }
  return request<StoredCaseExecutionSummary[]>(`/api/v1/executions?${search.toString()}`);
}

export function getExecutionOverview(params: {
  scope_type?: ReportScopeType;
  project_id?: number;
  case_id?: number;
  window_days?: OverviewWindowDays;
  failure_fingerprint?: string;
}) {
  const search = new URLSearchParams();
  if (params.scope_type) {
    search.set("scope_type", params.scope_type);
  }
  if (params.project_id) {
    search.set("project_id", String(params.project_id));
  }
  if (params.case_id) {
    search.set("case_id", String(params.case_id));
  }
  if (params.window_days) {
    search.set("window_days", String(params.window_days));
  }
  if (params.failure_fingerprint) {
    search.set("failure_fingerprint", params.failure_fingerprint);
  }
  const query = search.toString();
  return request<ExecutionsOverview>(`/api/v1/executions/overview${query ? `?${query}` : ""}`);
}

export function getExecutionDetail(executionId: number) {
  return request<StoredCaseExecutionDetail>(`/api/v1/executions/${executionId}`);
}

export function deleteExecution(executionId: number) {
  return request<void>(`/api/v1/executions/${executionId}`, {
    method: "DELETE",
  });
}
