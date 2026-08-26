"""Schemas for AI planning sessions and drafts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.dsl import DSLCase, DSLCaseInputContract, DSLCaseOutputContract, DSLModel, DSLStep, DSLVariableType


AIPlanningSessionStatus = Literal["collecting", "plan_ready", "drafts_ready", "reviewing", "saving", "executing", "completed", "closed", "error"]
AIPlanningMessageRole = Literal["user", "assistant"]
AIPlanningMessageTurnType = Literal["user", "followup", "plan", "tool_call", "system_error", "explorer_result", "judge_verdict"]
AIPlanningDraftStatus = Literal["generated", "imported", "rejected", "failed"]
AIPlanningNextAction = Literal["ask_followup", "review_plan", "select_scenarios", "drafts_generated"]


class ProjectSummaryInSession(DSLModel):
    """Minimal project info returned within session schemas."""
    id: int = Field(ge=1)
    name: str
    description: str | None = None


class LinkProjectRequest(DSLModel):
    project_id: int = Field(ge=1)


class CreateProjectInSessionRequest(DSLModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)


class AIPlanningRequirements(DSLModel):
    app_under_test: str | None = Field(default=None, max_length=500)
    business_goal: str | None = Field(default=None, max_length=1000)
    entry_url_or_page: str | None = Field(default=None, max_length=500)
    core_user_flow: str | None = Field(default=None, max_length=2000)
    main_assertions: list[str] = Field(default_factory=list)
    test_data_or_account: str | None = Field(default=None, max_length=1000)
    scope_limits: str | None = Field(default=None, max_length=1000)
    test_context: dict[str, Any] | None = Field(default=None, description="Persistent execution context: last_run_status, failures, root cause, regression scope.")


class AIPlanningTodoItem(DSLModel):
    item: str = Field(min_length=1, max_length=500)
    status: Literal["done", "in_progress", "pending", "failed", "skipped"] = "pending"


class AIPlanningToolCall(DSLModel):
    tool: str = Field(min_length=1, max_length=100)
    params: dict[str, Any] = Field(default_factory=dict)
    result: Any = None


class AIPlanningTestDataRequirement(DSLModel):
    key: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    value_type: DSLVariableType
    required: bool = True
    source_hint: str | None = Field(default=None, max_length=200)


class AIPlanningFlowStep(DSLModel):
    """Atomic flow step linking user workflow to explored page elements."""
    step_index: int
    scenario_key: str = Field(default="", max_length=100)
    session_id: int = Field(default=0, ge=0)
    action: str = Field(min_length=1, max_length=50)
    target: str | None = Field(default=None, max_length=500)
    value: str | None = Field(default=None, max_length=1000)
    trigger: str | None = Field(default=None, max_length=50)
    expected_result: str | None = Field(default=None, max_length=1000)
    page_url: str | None = Field(default=None, max_length=1000)
    page_state: str | None = Field(default=None, max_length=10)
    element_indices: list[int] | None = None
    element_target_keywords: list[str] | None = None


class AIPlanningScenarioVariable(DSLModel):
    """Cross-segment variable declared by the planning agent as naming authority.

    The planning agent emits one entry per ``${context_key}`` that will appear
    in DSL steps across page states.  Each downstream segment prompt receives
    this list verbatim so all segments use the same context_key spelling.
    """
    context_key: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="snake_case identifier, referenced as ${context_key} in DSL steps.",
    )
    description: str = Field(min_length=1, max_length=200, description="Human-readable purpose, e.g. '商品A名称'.")
    source: Literal["input", "captured"] = Field(
        default="captured",
        description="'input' = provided via input_contract at execution; 'captured' = populated at runtime by capture_text.",
    )
    capture_in_state: str | None = Field(
        default=None,
        max_length=10,
        description="For source='captured': page_state (S0/S1/...) where capture_text must run. Ignored for source='input'.",
    )


class AIPlanningScenario(DSLModel):
    """Test scenario — lean 4-field output, rest optional for backward compat."""
    scenario_key: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    draft_prompt: str = Field(min_length=1)
    priority: Literal["high", "medium", "low"] = "medium"
    goal: str | None = Field(default=None, max_length=1000)
    preconditions: list[str] = Field(default_factory=list)
    test_data_requirements: list[AIPlanningTestDataRequirement] = Field(default_factory=list)
    assertions: list[str] = Field(default_factory=list)
    page_elements: str | None = Field(default=None)
    flow_steps: list[AIPlanningFlowStep] = Field(default_factory=list)
    variables: list[AIPlanningScenarioVariable] = Field(
        default_factory=list,
        description="Cross-segment variable dictionary. Segments use these context_keys as the naming authority.",
    )


class AIPlanningPlan(DSLModel):
    summary: str = Field(min_length=1, max_length=2000)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    scenarios: list[AIPlanningScenario] = Field(default_factory=list)


class AIPlanningSession(DSLModel):
    id: int = Field(ge=1)
    actor_user_id: int = Field(ge=1)
    case_id: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, max_length=200)
    status: AIPlanningSessionStatus
    requirements: AIPlanningRequirements = Field(default_factory=AIPlanningRequirements)
    plan: AIPlanningPlan | None = None
    missing_slots: list[str] = Field(default_factory=list)
    last_error_message: str | None = Field(default=None, max_length=4000)
    created_at: datetime
    updated_at: datetime
    projects: list[ProjectSummaryInSession] = Field(default_factory=list)


class AIPlanningSessionSummary(DSLModel):
    id: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=200)
    status: AIPlanningSessionStatus
    created_at: datetime
    updated_at: datetime
    projects: list[ProjectSummaryInSession] = Field(default_factory=list)


class AIPlanningMessage(DSLModel):
    id: int = Field(ge=1)
    session_id: int = Field(ge=1)
    role: AIPlanningMessageRole
    turn_type: AIPlanningMessageTurnType
    content: str = Field(min_length=1)
    structured_payload: dict[str, Any] | None = None
    created_at: datetime


class AIPlanningDraft(DSLModel):
    id: int = Field(ge=1)
    session_id: int = Field(ge=1)
    scenario_key: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    status: AIPlanningDraftStatus
    dsl_generation_id: int | None = Field(default=None, ge=1)
    dsl_case: DSLCase | None = None
    warnings: list[str] = Field(default_factory=list)
    normalization_notes: list[str] = Field(default_factory=list)
    error_message: str | None = Field(default=None, max_length=4000)
    created_at: datetime
    updated_at: datetime


class AIPlanningSessionDetail(DSLModel):
    session: AIPlanningSession
    messages: list[AIPlanningMessage] = Field(default_factory=list)
    drafts: list[AIPlanningDraft] = Field(default_factory=list)


class CreateAIPlanningSessionRequest(DSLModel):
    case_id: int | None = Field(default=None, ge=1)
    project_id: int | None = Field(default=None, ge=1)


class AIPlanningMessageCreateRequest(DSLModel):
    content: str = Field(min_length=1, max_length=4000)


class GenerateAIPlanningDraftsRequest(DSLModel):
    scenario_keys: list[str] = Field(min_length=1)
    current_case: DSLCase | None = None
    current_steps: list[DSLStep] | None = None
    current_input_contract: list[DSLCaseInputContract] | None = None
    current_output_contract: list[DSLCaseOutputContract] | None = None
    preserve_contracts: bool = False


class UpdateAIPlanningDraftStatusRequest(DSLModel):
    status: Literal["imported", "rejected"]


class SavedCaseResult(DSLModel):
    case_id: int = Field(ge=1)
    case_name: str
    status: Literal["saved"] = "saved"


class ExecutionSummaryResult(DSLModel):
    execution_id: int = Field(ge=1)
    case_id: int = Field(ge=1)
    case_name: str
    status: Literal["passed", "failed", "needs_intervention", "error"]
    total_steps: int
    passed_steps: int
    failed_steps: int
    duration_ms: int | None = None
    screenshot_url: str | None = None
    report_url: str


class FailureDetail(DSLModel):
    case_name: str = Field(min_length=1)
    step_index: int = Field(ge=0)
    action: str = Field(min_length=1)
    target: str | None = None
    error_message: str | None = None
    suspected_cause: str = Field(min_length=1)
    cause_probability: Literal["high", "medium", "low"] = "medium"


class CaseAnalysisResult(DSLModel):
    case_id: int = Field(ge=1)
    case_name: str = Field(min_length=1)
    status: str = Field(min_length=1)
    passed_steps: int = Field(ge=0)
    total_steps: int = Field(ge=0)
    failure_summary: str | None = None


class ExecutionAnalysis(DSLModel):
    conclusion: Literal["all_passed", "partial", "all_failed"] = "all_passed"
    case_results: list[CaseAnalysisResult] = Field(default_factory=list)
    failure_details: list[FailureDetail] = Field(default_factory=list)
    suspected_root_cause: str | None = None
    impact_scope: str | None = None
    recommended_action: Literal["targeted_retest", "regression", "manual", "done"] = "done"
    recommended_scope: str | None = None


class AIPlanningTurnResponse(DSLModel):
    assistant_message: str = Field(min_length=1)
    session_status: AIPlanningSessionStatus
    requirements: AIPlanningRequirements = Field(default_factory=AIPlanningRequirements)
    missing_slots: list[str] = Field(default_factory=list)
    suggested_questions: list[str] = Field(default_factory=list)
    plan: AIPlanningPlan | None = None
    drafts: list[AIPlanningDraft] = Field(default_factory=list)
    next_action: AIPlanningNextAction
    tool_calls: list[AIPlanningToolCall] = Field(default_factory=list)
    saved_cases: list[SavedCaseResult] = Field(default_factory=list)
    execution_summaries: list[ExecutionSummaryResult] = Field(default_factory=list)
    execution_analysis: ExecutionAnalysis | None = None
    todo_list: list[AIPlanningTodoItem] = Field(default_factory=list)
