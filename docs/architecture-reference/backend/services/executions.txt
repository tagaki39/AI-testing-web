"""Case execution services."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, UTC
from hashlib import sha1
import hashlib
import re
from threading import Event
from typing import Generator, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.locators.ai_visual import reset_ai_visual_runtime_state
from app.locators.corrections import SQLAlchemyCorrectionStore
from app.models import TestCase, TestCaseRun, User
from app.reporters import build_execution_report
from app.runners import RunnerExecutionError, RunnerInterventionError, execute_case_with_playwright
from app.runners.playwright_runner import RunnerCancelledError, StepStreamEvent, execute_case_with_playwright_streaming
from app.schemas.dsl import DSLCase, GotoStep
from app.schemas.executions import (
    CaseExecutionRequest,
    ExecutionAggregateSnapshot,
    ExecutionTrendPoint,
    ExecutionsOverview,
    FailureCategoryCount,
    FailureRootCause,
    FailureStepActionCount,
    ExecutionWindowComparison,
    ExecutionWindowRange,
    ReportScopeType,
    StoredCaseExecutionDetail,
    StoredCaseExecutionSummary,
    StepExecutionEvidence,
    TopFailedCase,
)
from app.services.cases import EntityNotFoundError

FAILURE_CATEGORY_ORDER = [
    "configuration",
    "locator",
    "assertion",
    "navigation",
    "network",
    "runner",
]
LATEST_FAILED_RUNS_LIMIT = 5
TOP_FAILED_CASES_LIMIT = 5
FAILURE_ROOT_CAUSES_LIMIT = 10


@dataclass(frozen=True)
class ExecutionFailureDescriptor:
    fingerprint: str | None = None
    title: str | None = None


def execute_case(session: Session, case_id: int, payload: CaseExecutionRequest) -> StoredCaseExecutionDetail:
    record = session.get(TestCase, case_id)
    if record is None:
        raise EntityNotFoundError(f"Case {case_id} not found.")
    _ensure_user_exists(session, payload.actor_user_id)
    return _execute_case_record(session, record, payload)


def execute_case_streaming(
    session: Session,
    case_id: int,
    payload: CaseExecutionRequest,
    *,
    cancel_event: Event | None = None,
) -> Generator[StepStreamEvent, None, StoredCaseExecutionDetail]:
    """Stream step events for a case execution.

    Yields :class:`StepStreamEvent` objects and returns the persisted
    :class:`StoredCaseExecutionDetail` via ``StopIteration.value``.
    """
    record = session.get(TestCase, case_id)
    if record is None:
        raise EntityNotFoundError(f"Case {case_id} not found.")
    _ensure_user_exists(session, payload.actor_user_id)

    normalized_case = DSLCase.model_validate(record.dsl)
    execution = TestCaseRun(
        case_id=record.id,
        project_id=record.project_id,
        triggered_by=payload.actor_user_id,
        status="running",
    )
    session.add(execution)
    session.commit()
    session.refresh(execution)

    effective_base_url = payload.base_url or normalized_case.base_url
    missing_base_url_error = _build_missing_base_url_error(normalized_case, effective_base_url)

    # Merge input_contract defaults with user-provided input_values
    merged_input_values: dict[str, str] = {}
    for contract in normalized_case.input_contract:
        if contract.value is not None:
            merged_input_values[contract.context_key] = contract.value
    if payload.input_values:
        merged_input_values.update(payload.input_values)

    try:
        if missing_base_url_error is not None:
            report = build_execution_report(status="failed", steps=[_with_artifact_url(missing_base_url_error)])
            execution.status = "failed"
            execution.report = report.model_dump(mode="json")
            execution.error_message = missing_base_url_error.error_message
        else:
            reset_ai_visual_runtime_state()
            step_results = yield from execute_case_with_playwright_streaming(
                case=normalized_case,
                execution_id=execution.id,
                base_url=effective_base_url,
                cancel_event=cancel_event,
                correction_store=SQLAlchemyCorrectionStore(session),
                input_values=merged_input_values,
            )
            step_results = [_with_artifact_url(step) for step in step_results]
            has_failure = any(s.status in ("failed", "cascade_blocked") for s in step_results)
            report = build_execution_report(
                status="failed" if has_failure else "passed",
                steps=step_results,
            )
            execution.status = "failed" if has_failure else "passed"
            execution.report = report.model_dump(mode="json")
            execution.error_message = None
    except RunnerInterventionError as exc:
        step_results = [_with_artifact_url(step) for step in exc.step_results]
        report = build_execution_report(status="failed", steps=step_results)
        execution.status = "needs_intervention"
        execution.report = report.model_dump(mode="json")
        execution.error_message = str(exc)
    except RunnerExecutionError as exc:
        step_results = [_with_artifact_url(step) for step in exc.step_results]
        report = build_execution_report(status="failed", steps=step_results)
        execution.status = "failed"
        execution.report = report.model_dump(mode="json")
        execution.error_message = str(exc)
    except RunnerCancelledError:
        execution.status = "failed"
        execution.error_message = "Execution cancelled by user."
        raise
    execution.finished_at = datetime.now(UTC).replace(tzinfo=None)
    session.add(execution)
    session.commit()
    session.refresh(execution)
    return _to_execution_detail(session, execution, case_name=record.name)


def _execute_case_record(
    session: Session,
    record: TestCase,
    payload: CaseExecutionRequest,
    *,
    case_override: DSLCase | None = None,
    precomputed_error: StepExecutionEvidence | None = None,
) -> StoredCaseExecutionDetail:
    normalized_case = case_override or DSLCase.model_validate(record.dsl)

    execution = TestCaseRun(
        case_id=record.id,
        project_id=record.project_id,
        triggered_by=payload.actor_user_id,
        status="running",
    )
    session.add(execution)
    session.commit()
    session.refresh(execution)

    effective_base_url = payload.base_url or normalized_case.base_url
    missing_base_url_error = _build_missing_base_url_error(normalized_case, effective_base_url)

    # Merge input_contract defaults with user-provided input_values
    merged_input_values: dict[str, str] = {}
    for contract in normalized_case.input_contract:
        if contract.value is not None:
            merged_input_values[contract.context_key] = contract.value
    if payload.input_values:
        merged_input_values.update(payload.input_values)

    try:
        if precomputed_error is not None:
            report = build_execution_report(status="failed", steps=[_with_artifact_url(precomputed_error)])
            execution.status = "failed"
            execution.report = report.model_dump(mode="json")
            execution.error_message = precomputed_error.error_message
        elif missing_base_url_error is not None:
            report = build_execution_report(status="failed", steps=[missing_base_url_error])
            execution.status = "failed"
            execution.report = report.model_dump(mode="json")
            execution.error_message = missing_base_url_error.error_message
        else:
            reset_ai_visual_runtime_state()
            step_results = execute_case_with_playwright(
                case=normalized_case,
                execution_id=execution.id,
                base_url=effective_base_url,
                correction_store=SQLAlchemyCorrectionStore(session),
                input_values=merged_input_values,
            )
            step_results = [_with_artifact_url(step) for step in step_results]
            has_failure = any(s.status in ("failed", "cascade_blocked") for s in step_results)
            report = build_execution_report(
                status="failed" if has_failure else "passed",
                steps=step_results,
            )
            execution.status = "failed" if has_failure else "passed"
            execution.report = report.model_dump(mode="json")
            execution.error_message = None
    except RunnerInterventionError as exc:
        step_results = [_with_artifact_url(step) for step in exc.step_results]
        report = build_execution_report(status="failed", steps=step_results)
        execution.status = "needs_intervention"
        execution.report = report.model_dump(mode="json")
        execution.error_message = str(exc)
    except RunnerExecutionError as exc:
        step_results = [_with_artifact_url(step) for step in exc.step_results]
        report = build_execution_report(status="failed", steps=step_results)
        execution.status = "failed"
        execution.report = report.model_dump(mode="json")
        execution.error_message = str(exc)
    execution.finished_at = datetime.now(UTC).replace(tzinfo=None)
    session.add(execution)
    session.commit()
    session.refresh(execution)
    return _to_execution_detail(session, execution, case_name=record.name)


def list_case_executions(session: Session, case_id: int) -> list[StoredCaseExecutionSummary]:
    case = session.get(TestCase, case_id)
    if case is None:
        raise EntityNotFoundError(f"Case {case_id} not found.")

    statement = (
        select(TestCaseRun)
        .where(TestCaseRun.case_id == case_id)
        .order_by(TestCaseRun.started_at.desc(), TestCaseRun.id.desc())
    )
    records = session.scalars(statement).all()
    return [_to_execution_summary(record, case_name=case.name) for record in records]


def list_executions(
    session: Session,
    *,
    project_id: int | None = None,
    case_id: int | None = None,
    status: str | None = None,
    window_days: int | None = None,
    failure_category: str | None = None,
    failure_fingerprint: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[StoredCaseExecutionSummary]:
    requires_post_filtering = (
        failure_category is not None or failure_fingerprint is not None or window_days is not None
    )
    rows = _list_execution_rows(
        session,
        project_id=project_id,
        case_id=case_id,
        status=status,
        limit=limit,
        offset=offset,
        apply_pagination=not requires_post_filtering,
    )

    if not requires_post_filtering:
        return [_to_execution_summary(record, case_name=case_name) for record, case_name in rows]

    filtered: list[StoredCaseExecutionSummary] = []
    for record, case_name in rows:
        report = _normalize_report(record.report)
        summary = _to_execution_summary(record, case_name=case_name, report=report)
        failure_descriptor = _describe_failure(summary=summary, report=report, error_message=record.error_message)
        if failure_category is not None and summary.failure_category != failure_category:
            continue
        if failure_fingerprint is not None and failure_descriptor.fingerprint != failure_fingerprint:
            continue
        filtered.append(summary)
    filtered = _filter_summaries_by_window(filtered, window_days)
    return filtered[offset : offset + limit]


def get_executions_overview(
    session: Session,
    *,
    scope_type: ReportScopeType | None = None,
    project_id: int | None = None,
    case_id: int | None = None,
    window_days: int | None = None,
    failure_fingerprint: str | None = None,
) -> ExecutionsOverview:
    resolved_scope_type, resolved_project_id, resolved_case_id = _resolve_report_scope(
        scope_type=scope_type,
        project_id=project_id,
        case_id=case_id,
    )
    rows = _list_execution_rows(
        session,
        project_id=resolved_project_id,
        case_id=resolved_case_id,
        status=None,
        limit=None,
        offset=0,
        apply_pagination=False,
    )
    summary_entries: list[tuple[StoredCaseExecutionSummary, ExecutionFailureDescriptor]] = []
    for record, case_name in rows:
        report = _normalize_report(record.report)
        summary = _to_execution_summary(record, case_name=case_name, report=report)
        failure_descriptor = _describe_failure(summary=summary, report=report, error_message=record.error_message)
        if failure_fingerprint is not None and failure_descriptor.fingerprint != failure_fingerprint:
            continue
        summary_entries.append((summary, failure_descriptor))

    current_window_range = _build_window_range(window_days)
    previous_window_range = _build_previous_window_range(window_days)
    filtered_entries = _filter_entries_by_window(summary_entries, window_days)
    filtered_summaries = [summary for summary, _ in filtered_entries]
    previous_entries = (
        _filter_entries_by_range(summary_entries, previous_window_range)
        if previous_window_range is not None
        else []
    )
    previous_summaries = [summary for summary, _ in previous_entries]
    current_stats = _build_aggregate_snapshot(filtered_summaries)
    previous_stats = _build_aggregate_snapshot(previous_summaries)
    window_comparison = (
        _build_window_comparison(current_stats, previous_stats)
        if previous_window_range is not None
        else ExecutionWindowComparison()
    )
    category_counter = Counter(
        item.failure_category
        for item in filtered_summaries
        if item.status in {"failed", "needs_intervention"} and item.failure_category is not None
    )

    return ExecutionsOverview(
        scope_type=resolved_scope_type,
        scope_project_id=resolved_project_id,
        scope_case_id=resolved_case_id,
        total_count=current_stats.total_count,
        passed_count=current_stats.passed_count,
        failed_count=current_stats.failed_count,
        running_count=current_stats.running_count,
        auto_completed_count=_count_auto_completed(filtered_summaries),
        intervention_count=_count_intervention(filtered_summaries),
        pass_rate=current_stats.pass_rate,
        automation_rate=_compute_automation_rate(filtered_summaries),
        intervention_rate=_compute_intervention_rate(filtered_summaries),
        avg_duration_ms=current_stats.avg_duration_ms,
        current_window_range=current_window_range,
        previous_window_range=previous_window_range,
        previous_window_stats=previous_stats,
        window_comparison=window_comparison,
        latest_failed_runs=[
            item for item in filtered_summaries if item.status in {"failed", "needs_intervention"}
        ][:LATEST_FAILED_RUNS_LIMIT],
        latest_intervention_runs=[
            item for item in filtered_summaries if item.status == "needs_intervention"
        ][:LATEST_FAILED_RUNS_LIMIT],
        failure_categories=[
            FailureCategoryCount(category=category, count=category_counter.get(category, 0))
            for category in FAILURE_CATEGORY_ORDER
        ],
        trend_points=_build_trend_points(filtered_summaries, window_days),
        failure_step_actions=_build_failure_step_actions(filtered_summaries),
        top_failed_cases=_build_top_failed_cases(filtered_summaries),
        failure_root_causes=_build_failure_root_causes(filtered_entries),
    )


def get_case_execution(session: Session, execution_id: int) -> StoredCaseExecutionDetail | None:
    record = session.get(TestCaseRun, execution_id)
    if record is None:
        return None
    case = session.get(TestCase, record.case_id)
    case_name = case.name if case is not None else f"Case {record.case_id}"
    return _to_execution_detail(session, record, case_name=case_name)


def delete_execution(session: Session, execution_id: int) -> None:
    record = session.get(TestCaseRun, execution_id)
    if record is None:
        raise EntityNotFoundError(f"Execution {execution_id} not found.")
    session.delete(record)
    session.commit()


def _list_execution_rows(
    session: Session,
    *,
    project_id: int | None,
    case_id: int | None,
    status: str | None,
    limit: int | None,
    offset: int,
    apply_pagination: bool,
):
    statement = (
        select(TestCaseRun, TestCase.name)
        .join(TestCase, TestCase.id == TestCaseRun.case_id)
        .order_by(TestCaseRun.started_at.desc(), TestCaseRun.id.desc())
    )
    if project_id is not None:
        statement = statement.where(TestCaseRun.project_id == project_id)
    if case_id is not None:
        statement = statement.where(TestCaseRun.case_id == case_id)
    if status is not None:
        statement = statement.where(TestCaseRun.status == status)
    if apply_pagination and limit is not None:
        statement = statement.offset(offset).limit(limit)
    return session.execute(statement).all()


def _ensure_user_exists(session: Session, user_id: int) -> None:
    if session.get(User, user_id) is None:
        raise EntityNotFoundError(f"User {user_id} not found.")


def _build_window_range(window_days: int | None) -> ExecutionWindowRange | None:
    if window_days is None:
        return None

    end_date = datetime.now(UTC).date()
    start_date = end_date - timedelta(days=window_days - 1)
    return ExecutionWindowRange(start_date=start_date, end_date=end_date)


def _build_previous_window_range(window_days: int | None) -> ExecutionWindowRange | None:
    current_window = _build_window_range(window_days)
    if current_window is None or current_window.start_date is None:
        return None

    end_date = current_window.start_date - timedelta(days=1)
    start_date = end_date - timedelta(days=window_days - 1)
    return ExecutionWindowRange(start_date=start_date, end_date=end_date)


def _filter_summaries_by_window(
    summaries: list[StoredCaseExecutionSummary],
    window_days: int | None,
) -> list[StoredCaseExecutionSummary]:
    return _filter_summaries_by_range(summaries, _build_window_range(window_days))


def _filter_summaries_by_range(
    summaries: list[StoredCaseExecutionSummary],
    window_range: ExecutionWindowRange | None,
) -> list[StoredCaseExecutionSummary]:
    if window_range is None or window_range.start_date is None or window_range.end_date is None:
        return summaries

    return [
        summary
        for summary in summaries
        if window_range.start_date <= summary.started_at.date() <= window_range.end_date
    ]


def _filter_entries_by_window(
    entries: list[tuple[StoredCaseExecutionSummary, ExecutionFailureDescriptor]],
    window_days: int | None,
) -> list[tuple[StoredCaseExecutionSummary, ExecutionFailureDescriptor]]:
    return _filter_entries_by_range(entries, _build_window_range(window_days))


def _filter_entries_by_range(
    entries: list[tuple[StoredCaseExecutionSummary, ExecutionFailureDescriptor]],
    window_range: ExecutionWindowRange | None,
) -> list[tuple[StoredCaseExecutionSummary, ExecutionFailureDescriptor]]:
    if window_range is None or window_range.start_date is None or window_range.end_date is None:
        return entries

    return [
        entry
        for entry in entries
        if window_range.start_date <= entry[0].started_at.date() <= window_range.end_date
    ]


def _build_missing_base_url_error(case: DSLCase, base_url: str | None) -> StepExecutionEvidence | None:
    if base_url:
        return None

    for index, step in enumerate(case.steps):
        if step.action != "goto":
            continue
        goto_step = cast(GotoStep, step)
        if _is_absolute_url(goto_step.value):
            continue
        return StepExecutionEvidence(
            step_index=index,
            action="goto",
            value=goto_step.value,
            status="failed",
            duration_ms=0,
            error_message="Relative goto step requires case.base_url or execution request base_url.",
        )
    return None


def _is_absolute_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _to_execution_summary(
    record: TestCaseRun,
    *,
    case_name: str,
    report=None,
) -> StoredCaseExecutionSummary:
    report = _normalize_report(record.report) if report is None else report
    failed_step = _derive_failed_step(report)
    return StoredCaseExecutionSummary(
        id=record.id,
        case_id=record.case_id,
        case_name=case_name,
        project_id=record.project_id,
        triggered_by=record.triggered_by,
        status=record.status,
        error_message=record.error_message,
        started_at=record.started_at,
        finished_at=record.finished_at,
        duration_ms=_derive_duration_ms(record.started_at, record.finished_at),
        total_steps=len(report.steps) if report is not None else 0,
        failed_step_index=failed_step.step_index if failed_step is not None else None,
        failure_category=_derive_failure_category(report, record.error_message),
        failure_step_action=failed_step.action if failed_step is not None else None,
        latest_url=_derive_latest_url(report),
        latest_screenshot_url=_derive_latest_screenshot_url(report),
    )


def _to_execution_detail(session: Session, record: TestCaseRun, *, case_name: str) -> StoredCaseExecutionDetail:
    report = _normalize_report(record.report)
    summary = _to_execution_summary(record, case_name=case_name, report=report)
    return StoredCaseExecutionDetail(
        **summary.model_dump(),
        report=report,
    )


def _normalize_report(report: dict | None):
    if report is None:
        return None
    steps = [_with_artifact_url(StepExecutionEvidence.model_validate(step)) for step in report.get("steps", [])]
    return build_execution_report(status=report["status"], steps=steps)


def _with_artifact_url(step: StepExecutionEvidence) -> StepExecutionEvidence:
    if step.screenshot_url or not step.screenshot_path:
        return step

    normalized = step.screenshot_path.replace("\\", "/").lstrip("/")
    screenshot_url = f"/{normalized}" if normalized.startswith("artifacts/") else None
    return step.model_copy(update={"screenshot_url": screenshot_url})


def _derive_duration_ms(started_at: datetime, finished_at: datetime | None) -> int | None:
    if finished_at is None:
        return None
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def _derive_failed_step(report) -> StepExecutionEvidence | None:
    if report is None:
        return None
    for step in report.steps:
        if step.status == "failed":
            return step
    return None


def _derive_failure_category(report, error_message: str | None) -> str | None:
    failed_step = _derive_failed_step(report)
    if failed_step is None:
        if error_message and _is_configuration_error(error_message):
            return "configuration"
        if error_message:
            return "runner"
        return None

    step_error_message = failed_step.error_message or error_message
    if step_error_message and _is_configuration_error(step_error_message):
        return "configuration"
    if failed_step.locator_trace and failed_step.locator_trace.failure_reason:
        return "locator"
    if failed_step.action.startswith("assert_"):
        return "assertion"
    if failed_step.action == "goto":
        return "navigation"
    if any(event.failure_text or (event.status is not None and event.status >= 400) for event in failed_step.network_events):
        return "network"
    return "runner"


def _describe_failure(
    *,
    summary: StoredCaseExecutionSummary,
    report,
    error_message: str | None,
) -> ExecutionFailureDescriptor:
    if summary.status not in {"failed", "needs_intervention"}:
        return ExecutionFailureDescriptor()

    title = _derive_failure_title(
        report,
        error_message=error_message,
        failure_category=summary.failure_category,
        failure_step_action=summary.failure_step_action,
    )
    fingerprint = _build_failure_fingerprint(
        failure_category=summary.failure_category,
        failure_step_action=summary.failure_step_action,
        title=title,
    )
    return ExecutionFailureDescriptor(fingerprint=fingerprint, title=title)


def _is_configuration_error(error_message: str) -> bool:
    return "Relative goto step requires" in error_message or "case.base_url" in error_message


def _derive_failure_title(
    report,
    *,
    error_message: str | None,
    failure_category: str | None,
    failure_step_action: str | None,
) -> str:
    failed_step = _derive_failed_step(report)
    raw_title = failed_step.error_message if failed_step is not None and failed_step.error_message else error_message
    normalized_title = _normalize_error_message(raw_title)
    if normalized_title:
        return normalized_title

    category_label = failure_category or "runner"
    action_label = failure_step_action or "unknown"
    return f"{category_label}:{action_label}"


def _normalize_error_message(error_message: str | None) -> str | None:
    if not error_message:
        return None

    normalized = re.sub(r"\s+", " ", error_message).strip()
    return normalized or None


def _build_failure_fingerprint(
    *,
    failure_category: str | None,
    failure_step_action: str | None,
    title: str,
) -> str:
    source = "|".join(
        [
            failure_category or "unknown",
            failure_step_action or "unknown",
            title.casefold(),
        ]
    )
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


def _derive_latest_url(report) -> str | None:
    if report is None:
        return None
    for step in reversed(report.steps):
        if step.url:
            return step.url
    return None


def _derive_latest_screenshot_url(report) -> str | None:
    if report is None:
        return None
    for step in reversed(report.steps):
        if step.screenshot_url:
            return step.screenshot_url
    return None


def _build_aggregate_snapshot(summaries: list[StoredCaseExecutionSummary]) -> ExecutionAggregateSnapshot:
    passed_count = sum(1 for item in summaries if item.status == "passed")
    failed_count = sum(1 for item in summaries if item.status in {"failed", "needs_intervention"})
    running_count = sum(1 for item in summaries if item.status == "running")
    finished_total = passed_count + failed_count
    durations = [item.duration_ms for item in summaries if item.status != "running" and item.duration_ms is not None]
    return ExecutionAggregateSnapshot(
        total_count=len(summaries),
        passed_count=passed_count,
        failed_count=failed_count,
        running_count=running_count,
        pass_rate=round(passed_count / finished_total, 4) if finished_total else 0,
        avg_duration_ms=int(sum(durations) / len(durations)) if durations else 0,
    )


def _build_window_comparison(
    current: ExecutionAggregateSnapshot,
    previous: ExecutionAggregateSnapshot,
) -> ExecutionWindowComparison:
    return ExecutionWindowComparison(
        total_count_delta=current.total_count - previous.total_count,
        passed_count_delta=current.passed_count - previous.passed_count,
        failed_count_delta=current.failed_count - previous.failed_count,
        running_count_delta=current.running_count - previous.running_count,
        pass_rate_delta=round(current.pass_rate - previous.pass_rate, 4),
        avg_duration_ms_delta=current.avg_duration_ms - previous.avg_duration_ms,
    )


def _build_trend_points(
    summaries: list[StoredCaseExecutionSummary],
    window_days: int | None,
) -> list[ExecutionTrendPoint]:
    daily_buckets: dict[date, list[StoredCaseExecutionSummary]] = {}
    for summary in summaries:
        bucket = summary.started_at.date()
        daily_buckets.setdefault(bucket, []).append(summary)

    dates = _build_trend_dates(daily_buckets, window_days)
    trend_points: list[ExecutionTrendPoint] = []
    for bucket in dates:
        items = daily_buckets.get(bucket, [])
        passed_count = sum(1 for item in items if item.status == "passed")
        failed_count = sum(1 for item in items if item.status in {"failed", "needs_intervention"})
        auto_completed_count = sum(1 for item in items if item.status in {"passed", "failed"})
        intervention_count = sum(1 for item in items if item.status == "needs_intervention")
        finished_total = passed_count + failed_count
        durations = [item.duration_ms for item in items if item.status != "running" and item.duration_ms is not None]
        trend_points.append(
            ExecutionTrendPoint(
                date=bucket,
                total_count=len(items),
                passed_count=passed_count,
                failed_count=failed_count,
                auto_completed_count=auto_completed_count,
                intervention_count=intervention_count,
                pass_rate=round(passed_count / finished_total, 4) if finished_total else 0,
                avg_duration_ms=int(sum(durations) / len(durations)) if durations else 0,
            )
        )
    return trend_points


def _count_auto_completed(summaries: list[StoredCaseExecutionSummary]) -> int:
    return sum(1 for item in summaries if item.status in {"passed", "failed"})


def _count_intervention(summaries: list[StoredCaseExecutionSummary]) -> int:
    return sum(1 for item in summaries if item.status == "needs_intervention")


def _compute_automation_rate(summaries: list[StoredCaseExecutionSummary]) -> float:
    completed_total = sum(1 for item in summaries if item.status in {"passed", "failed", "needs_intervention"})
    if completed_total == 0:
        return 0.0
    return round(_count_auto_completed(summaries) / completed_total, 4)


def _compute_intervention_rate(summaries: list[StoredCaseExecutionSummary]) -> float:
    completed_total = sum(1 for item in summaries if item.status in {"passed", "failed", "needs_intervention"})
    if completed_total == 0:
        return 0.0
    return round(_count_intervention(summaries) / completed_total, 4)


def _resolve_report_scope(
    *,
    scope_type: ReportScopeType | None,
    project_id: int | None,
    case_id: int | None,
) -> tuple[ReportScopeType, int | None, int | None]:
    resolved_scope = scope_type
    if resolved_scope is None:
        if case_id is not None:
            resolved_scope = "case"
        elif project_id is not None:
            resolved_scope = "project"
        else:
            resolved_scope = "global"

    if resolved_scope == "global":
        return "global", None, None
    if resolved_scope == "project":
        return "project", project_id, None
    return "case", project_id, case_id


def _build_trend_dates(
    daily_buckets: dict[date, list[StoredCaseExecutionSummary]],
    window_days: int | None,
) -> list[date]:
    if window_days is not None:
        today = datetime.now(UTC).date()
        start = today - timedelta(days=window_days - 1)
        return [start + timedelta(days=offset) for offset in range(window_days)]
    return sorted(daily_buckets.keys())


def _build_failure_step_actions(
    summaries: list[StoredCaseExecutionSummary],
) -> list[FailureStepActionCount]:
    action_counter = Counter(
        item.failure_step_action
        for item in summaries
        if item.status in {"failed", "needs_intervention"} and item.failure_step_action
    )
    return [
        FailureStepActionCount(action=action, count=count)
        for action, count in sorted(action_counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _build_failure_root_causes(
    entries: list[tuple[StoredCaseExecutionSummary, ExecutionFailureDescriptor]],
) -> list[FailureRootCause]:
    grouped: dict[str, list[tuple[StoredCaseExecutionSummary, ExecutionFailureDescriptor]]] = {}
    for summary, descriptor in entries:
        if summary.status not in {"failed", "needs_intervention"} or descriptor.fingerprint is None or descriptor.title is None:
            continue
        grouped.setdefault(descriptor.fingerprint, []).append((summary, descriptor))

    root_causes: list[FailureRootCause] = []
    for fingerprint, group_entries in grouped.items():
        latest_summary, latest_descriptor = max(group_entries, key=lambda item: (item[0].started_at, item[0].id))
        root_causes.append(
            FailureRootCause(
                fingerprint=fingerprint,
                title=latest_descriptor.title or fingerprint,
                count=len(group_entries),
                affected_case_count=len({summary.case_id for summary, _ in group_entries}),
                latest_execution_id=latest_summary.id,
                latest_failure_category=latest_summary.failure_category,
            )
        )

    return sorted(
        root_causes,
        key=lambda item: (
            -item.count,
            -item.affected_case_count,
            -item.latest_execution_id,
            item.title,
        ),
    )[:FAILURE_ROOT_CAUSES_LIMIT]


def _build_top_failed_cases(
    summaries: list[StoredCaseExecutionSummary],
) -> list[TopFailedCase]:
    failed_summaries = [item for item in summaries if item.status in {"failed", "needs_intervention"}]
    grouped: dict[int, list[StoredCaseExecutionSummary]] = {}
    for summary in failed_summaries:
        grouped.setdefault(summary.case_id, []).append(summary)

    ranked_cases: list[TopFailedCase] = []
    for case_id, case_summaries in grouped.items():
        latest_summary = max(case_summaries, key=lambda item: (item.started_at, item.id))
        ranked_cases.append(
            TopFailedCase(
                case_id=case_id,
                case_name=latest_summary.case_name,
                failure_count=len(case_summaries),
                latest_execution_id=latest_summary.id,
                latest_failure_category=latest_summary.failure_category,
            )
        )

    return sorted(
        ranked_cases,
        key=lambda item: (
            -item.failure_count,
            -next(
                summary.started_at.timestamp()
                for summary in grouped[item.case_id]
                if summary.id == item.latest_execution_id
            ),
            -item.latest_execution_id,
        ),
    )[:TOP_FAILED_CASES_LIMIT]
