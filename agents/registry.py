from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


CapabilityMap = Mapping[str, Any]


@dataclass(frozen=True)
class WorkerDefinition:
    id: str
    label: str
    description: str
    required_any_capabilities: tuple[str, ...]
    tool_ids: tuple[str, ...]
    trigger: str
    output_resource: str
    human_approval_mode: str
    execution_mode: str


@dataclass(frozen=True)
class WorkerAccess:
    allowed: bool
    worker: WorkerDefinition
    permitted_tools: tuple[str, ...]
    denied_tools: tuple[str, ...]
    reason: str | None = None


FLOWONE_TOOL_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "get_overdue_customers": (
        "canViewCollections",
        "canViewCustomers",
    ),
    "get_unpaid_invoices": ("canViewInvoices",),
    "get_customer_payments": ("canViewPayments",),
    "get_credit_notes": ("canViewCreditNotes",),
    "get_assigned_tasks": ("canViewTasks",),
}


FLOWONE_WORKER_REGISTRY: dict[str, WorkerDefinition] = {
    "daily_ar_priorities": WorkerDefinition(
        id="daily_ar_priorities",
        label="Daily AR Priorities",
        description=(
            "Correlates current overdue exposure, invoices, payments, "
            "credit notes and assigned tasks into prioritized AR cases."
        ),
        required_any_capabilities=(
            "canViewCollections",
            "canViewCustomers",
        ),
        tool_ids=(
            "get_overdue_customers",
            "get_unpaid_invoices",
            "get_customer_payments",
            "get_credit_notes",
            "get_assigned_tasks",
        ),
        trigger="on_demand",
        output_resource="daily_ar_priorities_result",
        human_approval_mode="external_actions_only",
        # Feature 3: sequencing, FlowOne tool execution and result construction
        # are owned by the FastAPI runtime. React renders worker events/results.
        execution_mode="backend",
    ),
}


def _capability_enabled(capabilities: CapabilityMap, name: str) -> bool:
    return bool(capabilities.get(name))


def get_worker(worker_id: str) -> WorkerDefinition | None:
    return FLOWONE_WORKER_REGISTRY.get(worker_id)


def resolve_worker_access(
    worker_id: str,
    capabilities: CapabilityMap,
) -> WorkerAccess | None:
    worker = get_worker(worker_id)
    if worker is None:
        return None

    worker_allowed = any(
        _capability_enabled(capabilities, capability)
        for capability in worker.required_any_capabilities
    )

    if not worker_allowed:
        return WorkerAccess(
            allowed=False,
            worker=worker,
            permitted_tools=(),
            denied_tools=worker.tool_ids,
            reason=(
                "The current FlowOne permission set does not allow this "
                "worker to inspect customer collection exposure."
            ),
        )

    permitted_tools: list[str] = []
    denied_tools: list[str] = []

    for tool_id in worker.tool_ids:
        required = FLOWONE_TOOL_CAPABILITIES.get(tool_id, ())
        if not required or any(
            _capability_enabled(capabilities, capability)
            for capability in required
        ):
            permitted_tools.append(tool_id)
        else:
            denied_tools.append(tool_id)

    # Overdue/customer exposure is the minimum source required to produce
    # Daily AR cases. Optional evidence sources may be denied without blocking
    # the worker, matching the current stable POC behavior.
    if "get_overdue_customers" not in permitted_tools:
        return WorkerAccess(
            allowed=False,
            worker=worker,
            permitted_tools=tuple(permitted_tools),
            denied_tools=tuple(denied_tools),
            reason=(
                "Daily AR Priorities cannot run because overdue customer "
                "exposure is not available to the current user."
            ),
        )

    return WorkerAccess(
        allowed=True,
        worker=worker,
        permitted_tools=tuple(permitted_tools),
        denied_tools=tuple(denied_tools),
    )
