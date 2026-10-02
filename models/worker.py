from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ToolExecution:
    tool_id: str
    status: str
    started_at: str
    completed_at: str
    record_count: int
    detail: str = ""
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "toolId": self.tool_id,
            "status": self.status,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "recordCount": self.record_count,
            "detail": self.detail,
        }
        if self.error_code:
            payload["errorCode"] = self.error_code
        return payload


@dataclass(frozen=True)
class WorkerRun:
    """Canonical worker-run envelope shared by every FlowOne agent/worker."""

    run_id: str
    worker_id: str
    worker_label: str
    status: str
    trigger_type: str
    started_at: str
    completed_at: str
    case_ids: Sequence[str]
    tool_executions: Sequence[ToolExecution]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "runType": "worker_run",
            "runId": self.run_id,
            "workerId": self.worker_id,
            "workerLabel": self.worker_label,
            "status": self.status,
            "triggerType": self.trigger_type,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "caseCount": len(self.case_ids),
            "caseIds": list(self.case_ids),
            "toolExecutions": [item.to_dict() for item in self.tool_executions],
            "runMetadata": dict(self.metadata),
        }
