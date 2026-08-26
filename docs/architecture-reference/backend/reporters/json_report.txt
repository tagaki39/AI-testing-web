"""Helpers to normalize execution reports."""

from __future__ import annotations

from app.schemas.executions import ExecutionReport, StepExecutionEvidence


def build_execution_report(
    *,
    status: str,
    steps: list[StepExecutionEvidence],
) -> ExecutionReport:
    return ExecutionReport(status=status, steps=steps)
