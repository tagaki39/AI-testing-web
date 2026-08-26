export type ExecutionStatus = "running" | "passed" | "failed" | "needs_intervention";
export type FailureCategory = "configuration" | "locator" | "assertion" | "navigation" | "network" | "runner";
export type OverviewWindowDays = 7 | 14 | 30;
export type ReportScopeType = "global" | "project" | "case";
export type DSLVariableType = "string" | "number" | "boolean" | "object" | "array";
export type CorrectionType = "css" | "xpath" | "test_id";
export type VLMModelFamily = "qwen-vl" | "gemini" | "gpt-4o" | "qwen2.5-vl" | "glm";
export type GenerateDslMode = "draft" | "strict_steps_only";
export type GenerateDslImportMode = "replace" | "steps_only" | "contracts_only";
export type GenerateDslBaseUrlSource = "ai_output" | "request" | "current_case" | "none";
export type DslGenerationRunStatus = "success" | "failed";
export type DslGenerationFeedbackStatus = "pending" | "accepted" | "rejected";
export type DslGenerationPromptVariant = "baseline_draft" | "rewrite_from_case" | "repair_steps" | "contracts_focus";
export type DslGenerationContextProfile = "blank_request" | "rewrite_from_case" | "repair_steps" | "contracts_focus";
export type DslGenerationRiskFlag =
  | "missing_name_fallback"
  | "base_url_backfilled"
  | "invalid_actions_repaired"
  | "invalid_steps_removed"
  | "invalid_contracts_removed"
  | "contracts_preserved_fallback";
export type DslGenerationRejectionReasonCode =
  | "wrong_actions"
  | "invalid_structure"
  | "context_mismatch"
  | "bad_contracts"
  | "other";
export type CorrectionEventType =
  | "created"
  | "activated"
  | "deactivated"
  | "tier0_hit"
  | "tier0_miss"
  | "auto_deactivated";
export type DSLVariableSource =
  | "latest_url"
  | "error_message"
  | "status"
  | "last_step_url"
  | "last_step_page_title"
  | "last_step_target"
  | "last_step_value"
  | "last_step_error_message";

export interface CurrentUser {
  id: number;
  email: string;
  display_name: string;
}

export interface LoginPayload {
  email: string;
  password: string;
}

export interface LogoutResponse {
  success: boolean;
}

export interface DSLStep {
  action: string;
  target?: string;
  value?: string;
  timeout_ms?: number;
  [key: string]: unknown;
}

export interface DSLCaseInputContract {
  name: string;
  context_key: string;
  value_type: DSLVariableType;
  required: boolean;
  description?: string | null;
}

export interface DSLCaseOutputContract {
  name: string;
  context_key: string;
  value_type: DSLVariableType;
  source?: DSLVariableSource | null;
  description?: string | null;
}

export interface StoredCaseSummary {
  id: number;
  project_id: number;
  name: string;
  description: string | null;
  base_url?: string | null;
  input_contract: DSLCaseInputContract[];
  output_contract: DSLCaseOutputContract[];
  steps: DSLStep[];
  created_by: number;
  updated_by: number;
  created_at: string;
  updated_at: string;
}

export interface StoredCaseDetail extends StoredCaseSummary {}

export interface PaginatedCases {
  items: StoredCaseSummary[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  has_next: boolean;
  has_prev: boolean;
}

export interface ProjectSummary {
  id: number;
  name: string;
  description: string | null;
}

export interface ReportPreference {
  scope_type: ReportScopeType;
  project_id: number | null;
  case_id: number | null;
  window_days: OverviewWindowDays;
}

export interface DSLCasePayload {
  name: string;
  description?: string | null;
  base_url?: string | null;
  input_contract: DSLCaseInputContract[];
  output_contract: DSLCaseOutputContract[];
  steps: DSLStep[];
}

export interface GenerateDslRequest {
  prompt: string;
  base_url?: string | null;
  actor_user_id: number;
  project_id?: number | null;
  case_id?: number | null;
  generation_mode?: GenerateDslMode;
  import_mode?: GenerateDslImportMode;
  current_case?: DSLCasePayload | null;
  current_steps?: DSLStep[] | null;
  current_input_contract?: DSLCaseInputContract[] | null;
  current_output_contract?: DSLCaseOutputContract[] | null;
  retry_from_generation_id?: number | null;
  retry_reason_code?: DslGenerationRejectionReasonCode | null;
  retry_note?: string | null;
  preserve_contracts?: boolean;
}

export interface GenerateDslMeta {
  model?: string | null;
  generation_mode: GenerateDslMode;
  import_mode: GenerateDslImportMode;
  prompt_variant: DslGenerationPromptVariant;
  context_profile: DslGenerationContextProfile;
  risk_flags: DslGenerationRiskFlag[];
  base_url_source: GenerateDslBaseUrlSource;
  base_url_backfilled: boolean;
  repaired_invalid_actions: number;
  removed_invalid_steps: number;
  removed_invalid_contracts: number;
  preserve_contracts_applied: boolean;
  used_current_case_context: boolean;
  used_current_steps_context: boolean;
}

export interface GenerateDslResponse {
  generation_id: number;
  case: DSLCasePayload;
  supported_actions: string[];
  warnings: string[];
  normalization_notes: string[];
  generation_meta: GenerateDslMeta;
}

export type AIPlanningSessionStatus =
  | "collecting"
  | "plan_ready"
  | "drafts_ready"
  | "reviewing"
  | "saving"
  | "executing"
  | "completed"
  | "closed"
  | "error";
export type AIPlanningDraftStatus = "generated" | "imported" | "rejected" | "failed";
export type AIPlanningNextAction = "ask_followup" | "review_plan" | "select_scenarios" | "drafts_generated";

export interface AIPlanningRequirements {
  app_under_test?: string | null;
  business_goal?: string | null;
  entry_url_or_page?: string | null;
  core_user_flow?: string | null;
  main_assertions: string[];
  test_data_or_account?: string | null;
  scope_limits?: string | null;
}

export interface AIPlanningTestDataRequirement {
  key: string;
  label: string;
  value_type: DSLVariableType;
  required: boolean;
  source_hint?: string | null;
}

export interface AIPlanningScenario {
  scenario_key: string;
  title: string;
  goal: string;
  preconditions: string[];
  priority: "high" | "medium" | "low";
  test_data_requirements: AIPlanningTestDataRequirement[];
  assertions: string[];
  draft_prompt: string;
}

export interface AIPlanningPlan {
  summary: string;
  assumptions: string[];
  risks: string[];
  scenarios: AIPlanningScenario[];
}

export interface AIPlanningToolCall {
  tool: string;
  params: Record<string, unknown>;
  result?: unknown;
  result_summary?: unknown;  // compressed summary for heavy tools
}

export interface ProjectSummaryInSession {
  id: number;
  name: string;
  description: string | null;
}

export interface AIPlanningSession {
  id: number;
  actor_user_id: number;
  projects: ProjectSummaryInSession[];
  case_id?: number | null;
  title?: string | null;
  status: AIPlanningSessionStatus;
  requirements: AIPlanningRequirements;
  plan?: AIPlanningPlan | null;
  missing_slots: string[];
  last_error_message?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AIPlanningMessage {
  id: number;
  session_id: number;
  role: "user" | "assistant";
  turn_type: "user" | "followup" | "plan" | "tool_call" | "system_error" | "streaming";
  content: string;
  structured_payload?: Record<string, unknown> | null;
  created_at: string;
}

export interface AIPlanningDraft {
  id: number;
  session_id: number;
  scenario_key: string;
  title: string;
  status: AIPlanningDraftStatus;
  dsl_generation_id?: number | null;
  dsl_case?: DSLCasePayload | null;
  warnings: string[];
  normalization_notes: string[];
  error_message?: string | null;
  created_at: string;
  updated_at: string;
}

export interface AIPlanningSessionDetail {
  session: AIPlanningSession;
  messages: AIPlanningMessage[];
  drafts: AIPlanningDraft[];
}

export interface AIPlanningSessionSummary {
  id: number;
  title: string | null;
  status: AIPlanningSessionStatus;
  projects: ProjectSummaryInSession[];
  created_at: string;
  updated_at: string;
}

export interface CreatePlanningSessionPayload {
  case_id?: number | null;
}

export interface SendPlanningMessagePayload {
  content: string;
}

export interface GeneratePlanningDraftsPayload {
  scenario_keys: string[];
  current_case?: DSLCasePayload | null;
  current_steps?: DSLStep[] | null;
  current_input_contract?: DSLCaseInputContract[] | null;
  current_output_contract?: DSLCaseOutputContract[] | null;
  preserve_contracts?: boolean;
}

export interface UpdatePlanningDraftStatusPayload {
  status: "imported" | "rejected";
}

export interface AIPlanningTurnResponse {
  assistant_message: string;
  session_status: AIPlanningSessionStatus;
  requirements: AIPlanningRequirements;
  missing_slots: string[];
  suggested_questions: string[];
  plan?: AIPlanningPlan | null;
  drafts: AIPlanningDraft[];
  next_action: AIPlanningNextAction;
  tool_calls?: AIPlanningToolCall[];
  saved_cases?: SavedCaseResult[];
  execution_summaries?: ExecutionSummaryResult[];
}

export interface SavedCaseResult {
  case_id: number;
  case_name: string;
  status: "saved";
}

export interface ExecutionSummaryResult {
  execution_id: number;
  case_id: number;
  case_name: string;
  status: "passed" | "failed" | "needs_intervention" | "error";
  total_steps: number;
  passed_steps: number;
  failed_steps: number;
  duration_ms: number | null;
  screenshot_url: string | null;
  report_url: string;
}

export interface DslGenerationFeedbackPayload {
  actor_user_id: number;
  feedback_status: "accepted" | "rejected";
  feedback_import_mode?: GenerateDslImportMode | null;
  rejection_reason_code?: DslGenerationRejectionReasonCode | null;
  feedback_note?: string | null;
}

export interface AISettings {
  enable_ai_dsl_generate: boolean;
  ai_dsl_timeout_ms: number;
  ai_dsl_base_url: string;
  ai_dsl_model?: string | null;
  ai_dsl_strict_mode: boolean;
  ai_dsl_allow_auto_repair: boolean;
  has_ai_dsl_api_key: boolean;
  enable_ai_visual_locate: boolean;
  ai_visual_timeout_ms: number;
  ai_visual_failure_threshold: number;
  ai_visual_cooldown_seconds: number;
  ai_visual_rate_limit_per_minute: number;
  vlm_base_url: string;
  vlm_model?: string | null;
  vlm_model_family: VLMModelFamily;
  has_vlm_api_key: boolean;
  enable_ai_planning?: boolean;
  ai_planning_model?: string | null;
  ai_planning_base_url?: string;
  ai_planning_timeout_ms?: number;
  ai_planning_max_react_rounds?: number;
  has_ai_planning_api_key?: boolean;
}

export interface AISettingsUpdatePayload {
  enable_ai_dsl_generate: boolean;
  ai_dsl_timeout_ms: number;
  ai_dsl_base_url: string;
  ai_dsl_model?: string | null;
  ai_dsl_strict_mode: boolean;
  ai_dsl_allow_auto_repair: boolean;
  ai_dsl_api_key?: string | null;
  clear_ai_dsl_api_key: boolean;
  enable_ai_visual_locate: boolean;
  ai_visual_timeout_ms: number;
  ai_visual_failure_threshold: number;
  ai_visual_cooldown_seconds: number;
  ai_visual_rate_limit_per_minute: number;
  vlm_base_url: string;
  vlm_model?: string | null;
  vlm_model_family: VLMModelFamily;
  vlm_api_key?: string | null;
  clear_vlm_api_key: boolean;
  enable_ai_planning?: boolean;
  ai_planning_model?: string | null;
  ai_planning_base_url?: string;
  ai_planning_timeout_ms?: number;
  ai_planning_max_react_rounds?: number;
  ai_planning_api_key?: string | null;
  clear_ai_planning_api_key?: boolean;
}

export interface AIDslGenerationStats {
  current_prompt_version: string;
  current_governance_focus_reasons: DslGenerationRejectionReasonCode[];
  prompt_version_observation_note: string;
  governance_focus_selection_note: string;
  total_requests: number;
  success_count: number;
  failure_count: number;
  accepted_count: number;
  rejected_count: number;
  pending_count: number;
  decision_coverage_rate: number;
  last_model?: string | null;
  last_error_type?: string | null;
  last_error_message?: string | null;
  last_24h_requests: number;
  last_24h_success_count: number;
  last_24h_failure_count: number;
  last_24h_auto_repair_rate: number;
  retry_requests: number;
  retry_accepted_count: number;
  retry_rejected_count: number;
  top_error_types: DslGenerationErrorTypeCount[];
  accepted_import_mode_breakdown: DslGenerationImportModeCount[];
  top_rejection_reasons: DslGenerationRejectionReasonCount[];
  prompt_variant_breakdown: DslGenerationPromptVariantBreakdown[];
  prompt_version_breakdown: DslGenerationPromptVersionBreakdown[];
  context_profile_breakdown: DslGenerationContextProfileBreakdown[];
  rejection_reason_by_variant: DslGenerationRejectionReasonByVariant[];
  model_outcome_breakdown: DslGenerationModelOutcome[];
  generation_mode_breakdown: DslGenerationModeBreakdown[];
  retry_acceptance_by_reason: DslGenerationRetryAcceptanceByReason[];
  current_governance_focus_breakdown: DslGenerationGovernanceFocusSummary[];
}

export interface AIVisualStats {
  locate_requests: number;
  locate_success_count: number;
  locate_failure_count: number;
  cache_hit_count: number;
  cache_miss_count: number;
  cache_invalidated_count: number;
  breaker_skip_count: number;
  rate_limited_skip_count: number;
  disabled_skip_count: number;
  avg_locate_latency_ms: number;
  max_locate_latency_ms: number;
}

export interface AISettingsOverview {
  ai_dsl_enabled: boolean;
  ai_dsl_model?: string | null;
  ai_dsl_strict_mode: boolean;
  ai_dsl_allow_auto_repair: boolean;
  generation_stats: AIDslGenerationStats;
  ai_visual_stats: AIVisualStats;
}

export interface DslGenerationErrorTypeCount {
  error_type: string;
  count: number;
}

export interface DslGenerationImportModeCount {
  import_mode: GenerateDslImportMode;
  count: number;
}

export interface DslGenerationRejectionReasonCount {
  rejection_reason_code: DslGenerationRejectionReasonCode;
  count: number;
}

export interface DslGenerationPromptVariantBreakdown {
  prompt_variant: DslGenerationPromptVariant;
  total_requests: number;
  success_count: number;
  accepted_count: number;
  rejected_count: number;
}

export interface DslGenerationPromptVersionBreakdown {
  prompt_version: string;
  total_requests: number;
  success_count: number;
  accepted_count: number;
  rejected_count: number;
  retry_requests: number;
  retry_accepted_count: number;
}

export interface DslGenerationContextProfileBreakdown {
  context_profile: DslGenerationContextProfile;
  total_requests: number;
  success_count: number;
  accepted_count: number;
  rejected_count: number;
}

export interface DslGenerationRejectionReasonByVariant {
  prompt_variant: DslGenerationPromptVariant;
  rejection_reason_code: DslGenerationRejectionReasonCode;
  count: number;
}

export interface DslGenerationModelOutcome {
  model_name?: string | null;
  total_requests: number;
  success_count: number;
  accepted_count: number;
  rejected_count: number;
}

export interface DslGenerationModeBreakdown {
  generation_mode: GenerateDslMode;
  total_requests: number;
  success_count: number;
  accepted_count: number;
  rejected_count: number;
}

export interface DslGenerationRetryAcceptanceByReason {
  rejection_reason_code: DslGenerationRejectionReasonCode;
  retry_requests: number;
  accepted_count: number;
  acceptance_rate: number;
}

export interface DslGenerationGovernanceFocusSummary {
  rejection_reason_code: DslGenerationRejectionReasonCode;
  rejected_count: number;
  affected_prompt_variants: number;
  retry_requests: number;
  retry_accepted_count: number;
  retry_acceptance_rate: number;
}

export interface StoredDslGenerationRunSummary {
  id: number;
  created_at: string;
  success: boolean;
  model_name?: string | null;
  generation_mode: GenerateDslMode;
  import_mode: GenerateDslImportMode;
  prompt_variant: DslGenerationPromptVariant;
  project_id?: number | null;
  case_id?: number | null;
  prompt_version: string;
  retry_from_generation_id?: number | null;
  retry_reason_code?: DslGenerationRejectionReasonCode | null;
  retry_note?: string | null;
  error_type?: string | null;
  error_message?: string | null;
  repaired_invalid_actions: number;
  removed_invalid_steps: number;
  removed_invalid_contracts: number;
  warnings_count: number;
  normalization_notes_count: number;
  prompt_preview: string;
  governance_focus_reasons: DslGenerationRejectionReasonCode[];
  risk_flags: DslGenerationRiskFlag[];
  feedback_status: DslGenerationFeedbackStatus;
  feedback_import_mode?: GenerateDslImportMode | null;
  rejection_reason_code?: DslGenerationRejectionReasonCode | null;
  feedback_recorded_at?: string | null;
}

export interface StoredDslGenerationRunDetail extends StoredDslGenerationRunSummary {
  request_base_url?: string | null;
  generated_case_json?: DSLCasePayload | null;
  warnings_json: string[];
  normalization_notes_json: string[];
  feedback_note?: string | null;
  context_profile: DslGenerationContextProfile;
  used_current_case_context: boolean;
  used_current_steps_context: boolean;
  preserve_contracts_requested: boolean;
  preserve_contracts_applied: boolean;
}

export interface CaseMutationPayload extends DSLCasePayload {
  project_id: number;
  actor_user_id: number;
}

export interface DSLValidationResult {
  valid: boolean;
  case: DSLCasePayload;
  supported_actions: string[];
}

export interface CaseExecutionRequest {
  actor_user_id: number;
  base_url?: string;
  input_values?: Record<string, string>;
}

export interface ViewportSnapshot {
  width: number;
  height: number;
}

export interface LocatorCandidateAttributes {
  aria_label?: string | null;
  placeholder?: string | null;
  data_testid?: string | null;
}

export interface LocatorCandidateEvidence {
  strategy: string;
  preview_text?: string | null;
  role?: string | null;
  attributes: LocatorCandidateAttributes;
  score: number;
  matched_rules: string[];
  rejected_reasons: string[];
  visible: boolean;
  enabled: boolean;
}

export interface LocatorTrace {
  target: string;
  match_strategy?: string | null;
  selection_reason?: string | null;
  candidates: LocatorCandidateEvidence[];
  selected_candidate?: LocatorCandidateEvidence | null;
  failure_reason?: string | null;
}

export interface DOMSummary {
  text_preview?: string | null;
  button_count: number;
  input_count: number;
  link_count: number;
}

export interface ConsoleEvent {
  level: "error" | "warning";
  text: string;
  source_url?: string | null;
  line_number?: number | null;
}

export interface NetworkEvent {
  url: string;
  method: string;
  status?: number | null;
  resource_type?: string | null;
  failure_text?: string | null;
}

export interface DOMElementSnapshot {
  tag: string;
  text?: string | null;
  role?: string | null;
  aria_label?: string | null;
  placeholder?: string | null;
  data_testid?: string | null;
  css_selector?: string | null;
  xpath?: string | null;
  rect?: { x: number; y: number; width: number; height: number } | null;
  visible: boolean;
  enabled: boolean;
}

export interface AILocateCandidate {
  center: [number, number];
  bbox: [number, number, number, number];
  confidence: number;
  raw_response?: string | null;
}

export interface InterventionRequest {
  screenshot_url?: string | null;
  page_url: string;
  target_description: string;
  dom_snapshot: DOMElementSnapshot[];
  ai_candidate?: AILocateCandidate | null;
  locator_trace?: LocatorTrace | null;
}

export interface StepExecutionEvidence {
  step_index: number;
  action: string;
  target?: string | null;
  value?: string | null;
  status: "passed" | "failed";
  duration_ms?: number | null;
  resolved_by?: string | null;
  locator_trace?: LocatorTrace | null;
  url?: string | null;
  page_title?: string | null;
  viewport?: ViewportSnapshot | null;
  dom_summary?: DOMSummary | null;
  console_events: ConsoleEvent[];
  network_events: NetworkEvent[];
  screenshot_path?: string | null;
  screenshot_url?: string | null;
  error_message?: string | null;
  intervention_request?: InterventionRequest | null;
}

export interface ExecutionReport {
  status: ExecutionStatus;
  steps: StepExecutionEvidence[];
}

export interface StoredCaseExecutionSummary {
  id: number;
  case_id: number;
  case_name: string;
  project_id: number;
  triggered_by: number;
  status: ExecutionStatus;
  error_message: string | null;
  started_at: string;
  finished_at: string | null;
  duration_ms?: number | null;
  total_steps: number;
  failed_step_index?: number | null;
  failure_category?: FailureCategory | null;
  failure_step_action?: string | null;
  latest_url?: string | null;
  latest_screenshot_url?: string | null;
}

export interface StoredCaseExecutionDetail extends StoredCaseExecutionSummary {
  report: ExecutionReport | null;
}

export interface CreateCorrectionPayload {
  page_url: string;
  target_description: string;
  correction_type: CorrectionType;
  correction_value: string;
  source_execution_id: number;
  created_by: number;
}

export interface UpdateCorrectionStatePayload {
  is_active: boolean;
}

export interface BatchUpdateCorrectionStatePayload {
  correction_ids: number[];
  is_active: boolean;
}

export interface StoredLocatorCorrection {
  id: number;
  page_url_pattern: string;
  target_description: string;
  correction_type: CorrectionType;
  correction_value: string;
  verified_count: number;
  consecutive_failures: number;
  is_active: boolean;
  source_execution_id: number | null;
  created_by: number;
  created_at: string;
  updated_at: string;
}

export interface LocatorCorrectionEventPoint {
  date: string;
  hit_count: number;
  miss_count: number;
}

export interface StoredLocatorCorrectionEvent {
  id: number;
  correction_id: number;
  event_type: CorrectionEventType;
  page_url_pattern: string;
  target_description: string;
  execution_id: number | null;
  verified_count_after: number;
  consecutive_failures_after: number;
  is_active_after: boolean;
  created_at: string;
}

export interface LocatorCorrectionsOverview {
  total_count: number;
  active_count: number;
  inactive_count: number;
  hit_count: number;
  miss_count: number;
  auto_deactivated_count: number;
  current_window_start?: string | null;
  current_window_end?: string | null;
  trend_points: LocatorCorrectionEventPoint[];
}

export interface FailureCategoryCount {
  category: FailureCategory;
  count: number;
}

export interface ExecutionAggregateSnapshot {
  total_count: number;
  passed_count: number;
  failed_count: number;
  running_count: number;
  pass_rate: number;
  avg_duration_ms: number;
}

export interface ExecutionWindowRange {
  start_date?: string | null;
  end_date?: string | null;
}

export interface ExecutionWindowComparison {
  total_count_delta: number;
  passed_count_delta: number;
  failed_count_delta: number;
  running_count_delta: number;
  pass_rate_delta: number;
  avg_duration_ms_delta: number;
}

export interface ExecutionTrendPoint {
  date: string;
  total_count: number;
  passed_count: number;
  failed_count: number;
  auto_completed_count: number;
  intervention_count: number;
  pass_rate: number;
  avg_duration_ms: number;
}

export interface FailureStepActionCount {
  action: string;
  count: number;
}

export interface TopFailedCase {
  case_id: number;
  case_name: string;
  failure_count: number;
  latest_execution_id: number;
  latest_failure_category?: FailureCategory | null;
}

export interface FailureRootCause {
  fingerprint: string;
  title: string;
  count: number;
  affected_case_count: number;
  latest_execution_id: number;
  latest_failure_category?: FailureCategory | null;
}

export interface ExecutionsOverview {
  scope_type: ReportScopeType;
  scope_project_id?: number | null;
  scope_case_id?: number | null;
  total_count: number;
  passed_count: number;
  failed_count: number;
  running_count: number;
  auto_completed_count: number;
  intervention_count: number;
  pass_rate: number;
  automation_rate: number;
  intervention_rate: number;
  avg_duration_ms: number;
  current_window_range?: ExecutionWindowRange | null;
  previous_window_range?: ExecutionWindowRange | null;
  previous_window_stats: ExecutionAggregateSnapshot;
  window_comparison: ExecutionWindowComparison;
  latest_failed_runs: StoredCaseExecutionSummary[];
  latest_intervention_runs: StoredCaseExecutionSummary[];
  failure_categories: FailureCategoryCount[];
  trend_points: ExecutionTrendPoint[];
  failure_step_actions: FailureStepActionCount[];
  top_failed_cases: TopFailedCase[];
  failure_root_causes: FailureRootCause[];
}

// ---------------------------------------------------------------------------
// Execution stream events (WebSocket)
// ---------------------------------------------------------------------------

export interface StatusStreamEvent {
  type: "status";
  phase: "thinking" | "generating" | "tool_calling" | "executing";
  message: string;
}

export interface TextChunkStreamEvent {
  type: "text_chunk";
  text: string;
  thinking?: boolean;
}

export interface ToolCallStartStreamEvent {
  type: "tool_call_start";
  tool: string;
  params?: Record<string, unknown>;
}

export interface ToolCallEndStreamEvent {
  type: "tool_call_end";
  tool: string;
  result?: unknown;
  result_summary?: unknown;  // compressed summary for heavy tools
}

export interface DraftGeneratingStreamEvent {
  type: "draft_generating";
  scenario_key: string;
  message: string;
}

export interface TurnCompleteStreamEvent {
  type: "turn_complete";
  session_status: string;
  payload: {
    assistant_message: string;
    missing_slots: string[];
    suggested_questions: string[];
    plan: Record<string, unknown> | null;
    tool_calls: Array<{ tool: string; params: Record<string, unknown> }>;
    todo_list: Array<{ item: string; status: string }>;
  };
}

export interface SaveProgressEvent {
  type: "save_progress";
  saved_count: number;
  total: number;
  case_name: string;
}

export interface CaseStartEvent {
  type: "case_start";
  case_id: number;
  case_name: string;
  total_steps: number;
}

export interface StepStartEvent {
  type: "step_start";
  case_id: number;
  step_index: number;
  action: string;
  target?: string | null;
  value?: string | null;
}

export interface StepCompleteEvent {
  type: "step_complete";
  case_id: number;
  step_index: number;
  action: string;
  status: "passed" | "failed";
  duration_ms: number;
}

export interface ExecutionSummaryStreamEvent {
  type: "execution_summary";
  message: string;
  structured_payload: {
    type: "execution_summary";
    saved_cases: SavedCaseResult[];
    execution_summaries: ExecutionSummaryResult[];
  };
}

export interface CancelledEvent {
  type: "cancelled";
}

export interface DoneEvent {
  type: "done";
}

export interface ErrorEvent {
  type: "error";
  message: string;
  error_type?: string;
  phase?: string;
  traceback?: string;
}

export type ExecutionStreamEvent =
  | StatusStreamEvent
  | TextChunkStreamEvent
  | ToolCallStartStreamEvent
  | ToolCallEndStreamEvent
  | DraftGeneratingStreamEvent
  | TurnCompleteStreamEvent
  | SaveProgressEvent
  | CaseStartEvent
  | StepStartEvent
  | StepCompleteEvent
  | ExecutionSummaryStreamEvent
  | CancelledEvent
  | DoneEvent
  | ErrorEvent;

export interface LinkProjectPayload {
  project_id: number;
}

export interface CreateProjectInSessionPayload {
  name: string;
  description?: string | null;
}
