from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Mapping

from flowone import DailyArToolService, FlowOneApiError
from models import (
    BusinessCase,
    CaseRecommendation,
    EvidenceItem,
    ToolExecution,
    WorkerRun,
    collect_record_ids,
)
from .registry import WorkerAccess


STEP_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "id": "overdue",
        "label": "Checking overdue customer exposure",
        "toolId": "get_overdue_customers",
        "sourceLabel": "Customer exposure",
    },
    {
        "id": "invoices",
        "label": "Checking open sales invoices",
        "toolId": "get_unpaid_invoices",
        "sourceLabel": "Sales invoices",
    },
    {
        "id": "payments",
        "label": "Checking recent customer payments",
        "toolId": "get_customer_payments",
        "sourceLabel": "Customer payments",
    },
    {
        "id": "credit-notes",
        "label": "Checking credit-note activity",
        "toolId": "get_credit_notes",
        "sourceLabel": "Credit notes",
    },
    {
        "id": "tasks",
        "label": "Checking assigned workflow tasks",
        "toolId": "get_assigned_tasks",
        "sourceLabel": "Assigned tasks",
    },
    {
        "id": "prioritize",
        "label": "Building prioritized customer cases",
        "toolId": "",
        "sourceLabel": "",
    },
)


class DailyArWorkerError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _customer_matches(row: Mapping[str, Any], customer: Mapping[str, Any]) -> bool:
    customer_id = str(customer.get("customerId") or "").strip()
    row_id = str(row.get("customerId") or "").strip()
    if customer_id and row_id and customer_id == row_id:
        return True

    left = normalize(row.get("customerName"))
    right = normalize(customer.get("customerName"))
    return bool(left and right and left == right)


def _customer_name_matches(row: Mapping[str, Any], customer: Mapping[str, Any]) -> bool:
    left = normalize(row.get("customerName"))
    right = normalize(customer.get("customerName"))
    return bool(left and right and left == right)


def _format_compact_currency(value: float, currency_code: str) -> str:
    """Formatting for explanations only; numeric values remain authoritative."""

    code = (currency_code or "USD").upper()
    symbols = {
        "INR": "₹",
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
        "AUD": "A$",
        "CAD": "C$",
        "JPY": "¥",
    }
    symbol = symbols.get(code, f"{code} ")
    amount = abs(float(value or 0))
    sign = "-" if value < 0 else ""

    if code == "INR":
        if amount >= 10_000_000:
            return f"{sign}{symbol}{amount / 10_000_000:.2f}Cr"
        if amount >= 100_000:
            return f"{sign}{symbol}{amount / 100_000:.2f}L"
        if amount >= 1_000:
            return f"{sign}{symbol}{amount / 1_000:.1f}K"
    else:
        if amount >= 1_000_000_000:
            return f"{sign}{symbol}{amount / 1_000_000_000:.2f}B"
        if amount >= 1_000_000:
            return f"{sign}{symbol}{amount / 1_000_000:.2f}M"
        if amount >= 1_000:
            return f"{sign}{symbol}{amount / 1_000:.1f}K"

    return f"{sign}{symbol}{amount:,.2f}"


class DailyArPrioritiesWorker:
    """Deterministic Daily AR worker. No LLM is required for prioritization."""

    def __init__(
        self,
        *,
        tools: DailyArToolService,
        access: WorkerAccess,
        capabilities: Mapping[str, Any],
        currency_code: str = "USD",
    ) -> None:
        self.tools = tools
        self.access = access
        self.capabilities = capabilities
        self.currency_code = currency_code or "USD"

    @property
    def steps(self) -> list[dict[str, str]]:
        return [
            {"id": step["id"], "label": step["label"]}
            for step in STEP_DEFINITIONS
        ]

    def _source_available(self, tool_id: str) -> bool:
        return tool_id in set(self.access.permitted_tools)

    def _build_case_evidence(
        self,
        *,
        case_id: str,
        observed_at: str,
        customer: Mapping[str, Any],
        matching_invoices: list[dict[str, Any]],
        matching_payments: list[dict[str, Any]],
        matching_credit_notes: list[dict[str, Any]],
        matching_tasks: list[dict[str, Any]],
        open_invoice_balance: float,
        recent_payment_amount: float,
        credit_note_amount: float,
        source_statuses: Mapping[str, str],
    ) -> list[EvidenceItem]:
        evidence: list[EvidenceItem] = []
        past_due = _number(customer.get("pastDueInvBal"))
        dso = _number(customer.get("dso"))
        customer_record_id = str(
            customer.get("customerId")
            or customer.get("customerNum")
            or customer.get("customerName")
            or ""
        )

        evidence.append(
            EvidenceItem(
                evidence_id=f"{case_id}:exposure",
                source_system="FlowOne",
                source_type="flowone_tool",
                tool_id="get_overdue_customers",
                record_type="customer_exposure",
                status="observed",
                scope="bulk_scan",
                observed_at=observed_at,
                summary=(
                    "Current FlowOne customer exposure reported "
                    f"{_format_compact_currency(past_due, self.currency_code)} past due "
                    f"and {round(dso)} DSO days."
                ),
                record_ids=(customer_record_id,) if customer_record_id else (),
                facts={
                    "pastDueInvBal": past_due,
                    "dso": dso,
                    "customerId": str(customer.get("customerId") or ""),
                    "customerNum": str(customer.get("customerNum") or ""),
                },
            )
        )

        source_specs: tuple[
            tuple[str, str, list[dict[str, Any]], tuple[str, ...], dict[str, Any]], ...
        ] = (
            (
                "get_unpaid_invoices",
                "invoice",
                matching_invoices,
                ("invoiceId", "invoiceNum", "formatNumber", "id"),
                {
                    "recordCount": len(matching_invoices),
                    "openInvoiceBalance": open_invoice_balance,
                },
            ),
            (
                "get_customer_payments",
                "payment",
                matching_payments,
                ("paymentId", "receiptReferenceNum", "id"),
                {
                    "recordCount": len(matching_payments),
                    "recentPaymentAmount": recent_payment_amount,
                },
            ),
            (
                "get_credit_notes",
                "credit_note",
                matching_credit_notes,
                ("creditNoteId", "creditNoteNum", "id"),
                {
                    "recordCount": len(matching_credit_notes),
                    "creditNoteAmount": credit_note_amount,
                },
            ),
            (
                "get_assigned_tasks",
                "task",
                matching_tasks,
                ("taskId", "id", "taskName"),
                {"recordCount": len(matching_tasks)},
            ),
        )

        labels = {
            "get_unpaid_invoices": "invoice",
            "get_customer_payments": "payment",
            "get_credit_notes": "credit-note",
            "get_assigned_tasks": "assigned task",
        }

        for tool_id, record_type, records, id_keys, facts in source_specs:
            available = self._source_available(tool_id)
            source_status = source_statuses.get(tool_id, "unknown")
            if not available or source_status == "skipped":
                status = "unavailable"
                summary = (
                    f"{labels[tool_id].capitalize()} evidence was not available to the "
                    "current FlowOne permission set."
                )
            elif source_status == "error":
                status = "error"
                summary = (
                    f"The FlowOne {labels[tool_id]} lookup failed during this worker run; "
                    "absence of records must not be treated as negative evidence."
                )
            elif records:
                status = "observed"
                summary = (
                    f"{len(records)} matching {labels[tool_id]} "
                    f"{'record' if len(records) == 1 else 'records'} found in the current FlowOne scope."
                )
            else:
                status = "no_match"
                summary = (
                    f"No matching {labels[tool_id]} records were found in the current FlowOne scope."
                )

            evidence.append(
                EvidenceItem(
                    evidence_id=f"{case_id}:{record_type}",
                    source_system="FlowOne",
                    source_type="flowone_tool",
                    tool_id=tool_id,
                    record_type=record_type,
                    status=status,
                    scope="bulk_scan",
                    observed_at=observed_at,
                    summary=summary,
                    record_ids=collect_record_ids(records, id_keys),
                    facts=facts,
                )
            )

        return evidence

    def build_priority_cases(
        self,
        overdue_customers: list[dict[str, Any]],
        invoices: list[dict[str, Any]],
        payments: list[dict[str, Any]],
        credit_notes: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        *,
        run_id: str,
        observed_at: str,
        source_statuses: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        cases: list[dict[str, Any]] = []
        can_view_payments = bool(self.capabilities.get("canViewPayments"))
        can_manage_collections = bool(self.capabilities.get("canManageCollections"))

        for customer in overdue_customers:
            matching_invoices = [
                row for row in invoices if _customer_matches(row, customer)
            ]
            # Preserve the stable POC correlation behavior: invoice rows may
            # match by customer ID or normalized name, while bulk payment and
            # credit-note scans are correlated by normalized customer name.
            matching_payments = [
                row for row in payments if _customer_name_matches(row, customer)
            ]
            matching_credit_notes = [
                row for row in credit_notes if _customer_name_matches(row, customer)
            ]

            customer_key = normalize(customer.get("customerName"))
            matching_tasks = []
            for task in tasks:
                task_text = normalize(
                    f"{task.get('taskName', '')} {task.get('description', '')}"
                )
                if customer_key and customer_key in task_text:
                    matching_tasks.append(task)

            open_invoice_balance = sum(
                _number(invoice.get("balance")) for invoice in matching_invoices
            )
            recent_payment_amount = sum(
                _number(payment.get("paymentAmount")) for payment in matching_payments
            )
            credit_note_amount = sum(
                _number(note.get("approvedAmount") or note.get("claimAmount"))
                for note in matching_credit_notes
            )

            past_due = _number(customer.get("pastDueInvBal"))
            dso = _number(customer.get("dso"))
            reasons: list[str] = [
                "Past-due exposure in current FlowOne data: "
                f"{_format_compact_currency(past_due, self.currency_code)}."
            ]

            if dso > 0:
                reasons.append(f"Current DSO: {round(dso)} days.")

            if matching_invoices:
                noun = "invoice" if len(matching_invoices) == 1 else "invoices"
                reasons.append(
                    f"{len(matching_invoices)} open {noun} found in the current invoice "
                    "result set, totaling "
                    f"{_format_compact_currency(open_invoice_balance, self.currency_code)} "
                    "remaining balance."
                )

            if can_view_payments:
                if matching_payments:
                    noun = "record" if len(matching_payments) == 1 else "records"
                    reasons.append(
                        f"{len(matching_payments)} matching recent payment {noun} found "
                        "in the current payment result set."
                    )
                else:
                    reasons.append(
                        "No matching payment was found in the current recent-payment result set."
                    )

            if matching_credit_notes:
                noun = "record" if len(matching_credit_notes) == 1 else "records"
                reasons.append(
                    f"{len(matching_credit_notes)} credit-note {noun} found in the current scope."
                )

            if matching_tasks:
                noun = "task" if len(matching_tasks) == 1 else "tasks"
                reasons.append(
                    f"{len(matching_tasks)} assigned {noun} reference this customer."
                )

            recommended_next_step = (
                "Review the account context and prepare a collection follow-up "
                "if the evidence supports outreach."
                if can_manage_collections
                else "Review the account context and route any required follow-up "
                "to a user with collection-management permission."
            )

            case_id = str(uuid.uuid4())
            evidence = self._build_case_evidence(
                case_id=case_id,
                observed_at=observed_at,
                customer=customer,
                matching_invoices=matching_invoices,
                matching_payments=matching_payments,
                matching_credit_notes=matching_credit_notes,
                matching_tasks=matching_tasks,
                open_invoice_balance=open_invoice_balance,
                recent_payment_amount=recent_payment_amount,
                credit_note_amount=credit_note_amount,
                source_statuses=source_statuses,
            )

            business_case = BusinessCase(
                case_id=case_id,
                case_type="ar_collection_priority",
                status="open",
                created_by_worker="daily_ar_priorities",
                created_from_run_id=run_id,
                created_at=observed_at,
                subject={
                    "type": "customer",
                    "id": str(customer.get("customerId") or ""),
                    "name": str(customer.get("customerName") or ""),
                    "reference": str(customer.get("customerNum") or ""),
                },
                summary=(
                    f"{str(customer.get('customerName') or 'Customer')} has "
                    f"{_format_compact_currency(past_due, self.currency_code)} past due"
                    + (f" with {round(dso)} DSO days." if dso > 0 else ".")
                ),
                reasons=reasons,
                recommendation=CaseRecommendation(
                    action_key=(
                        "prepare_collection_followup"
                        if can_manage_collections
                        else "review_and_route"
                    ),
                    label=(
                        "Prepare collection follow-up"
                        if can_manage_collections
                        else "Review and route"
                    ),
                    description=recommended_next_step,
                    external_impact=False,
                    # Follow-up preparation is internal, but any eventual
                    # customer-facing execution remains human/policy gated.
                    requires_human_approval=can_manage_collections,
                ),
                evidence=evidence,
                tags=("accounts_receivable", "collections", "daily_ar"),
                metadata={
                    "currencyCode": self.currency_code,
                    "pastDueInvBal": past_due,
                    "dso": dso,
                    "openInvoiceCount": len(matching_invoices),
                    "recentPaymentCount": len(matching_payments),
                    "creditNoteCount": len(matching_credit_notes),
                    "matchingTaskCount": len(matching_tasks),
                },
            )

            # Compatibility fields are intentionally preserved so the stable
            # React work surface can render Feature 4 without a redesign.
            case_payload: dict[str, Any] = {
                "id": str(
                    customer.get("customerId")
                    or customer.get("customerNum")
                    or customer.get("customerName")
                    or ""
                ),
                "customerId": str(customer.get("customerId") or ""),
                "customerName": str(customer.get("customerName") or ""),
                "customerNum": str(customer.get("customerNum") or ""),
                "pastDueInvBal": past_due,
                "dso": dso,
                "openInvoiceCount": len(matching_invoices),
                "openInvoiceBalance": open_invoice_balance,
                "recentPaymentCount": len(matching_payments),
                "latestPaymentDate": str(
                    (matching_payments[0] if matching_payments else {}).get(
                        "paymentDate", ""
                    )
                    or ""
                ),
                "recentPaymentAmount": recent_payment_amount,
                "creditNoteCount": len(matching_credit_notes),
                "creditNoteAmount": credit_note_amount,
                "matchingTaskCount": len(matching_tasks),
                "reasons": reasons,
                "recommendedNextStep": recommended_next_step,
                "canPrepareFollowup": can_manage_collections,
                "invoices": matching_invoices[:20],
                "payments": matching_payments[:20],
                "creditNotes": matching_credit_notes[:20],
                "tasks": matching_tasks[:10],
                "evidenceMode": "bulk_scan",
            }
            case_payload.update(business_case.to_dict())
            cases.append(case_payload)

        cases.sort(
            key=lambda case: (
                -_number(case.get("pastDueInvBal")),
                -_number(case.get("dso")),
                -_number(case.get("openInvoiceBalance")),
            )
        )
        return cases[:10]

    async def stream_run(
        self,
        *,
        run_id: str,
        started_at: str | None = None,
        trigger_type: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        started = started_at or utc_now_iso()
        permitted = set(self.access.permitted_tools)
        denied = set(self.access.denied_tools)

        source_records: dict[str, list[dict[str, Any]]] = {
            "get_overdue_customers": [],
            "get_unpaid_invoices": [],
            "get_customer_payments": [],
            "get_credit_notes": [],
            "get_assigned_tasks": [],
        }
        skipped_sources: list[str] = []
        tool_executions: list[ToolExecution] = []
        source_statuses: dict[str, str] = {
            tool_id: "pending" for tool_id in source_records
        }

        detail_labels = {
            "get_overdue_customers": "scoped overdue customer records",
            "get_unpaid_invoices": "open invoice records",
            "get_customer_payments": "recent payment records",
            "get_credit_notes": "credit-note records",
            "get_assigned_tasks": "assigned task records",
        }

        for step in STEP_DEFINITIONS[:-1]:
            step_id = step["id"]
            label = step["label"]
            tool_id = step["toolId"]
            source_label = step["sourceLabel"]

            if tool_id in denied:
                skipped_sources.append(source_label)
                source_statuses[tool_id] = "skipped"
                now = utc_now_iso()
                detail = "Not available to the current permission set"
                tool_executions.append(
                    ToolExecution(
                        tool_id=tool_id,
                        status="skipped",
                        started_at=now,
                        completed_at=now,
                        record_count=0,
                        detail=detail,
                        error_code="permission_denied",
                    )
                )
                yield {
                    "kind": "step",
                    "id": step_id,
                    "label": label,
                    "status": "skipped",
                    "detail": detail,
                }
                continue

            if tool_id not in permitted:
                continue

            tool_started = utc_now_iso()
            yield {
                "kind": "step",
                "id": step_id,
                "label": label,
                "status": "running",
            }

            try:
                result = await self.tools.run_tool(tool_id)
                source_records[tool_id] = result.records
                source_statuses[tool_id] = "completed"
                tool_completed = utc_now_iso()
                detail = f"{result.count} {detail_labels[tool_id]}"
                tool_executions.append(
                    ToolExecution(
                        tool_id=tool_id,
                        status="completed",
                        started_at=tool_started,
                        completed_at=tool_completed,
                        record_count=result.count,
                        detail=detail,
                    )
                )
                yield {
                    "kind": "step",
                    "id": step_id,
                    "label": label,
                    "status": "done",
                    "detail": detail,
                }
            except (FlowOneApiError, KeyError) as error:
                source_statuses[tool_id] = "error"
                tool_completed = utc_now_iso()
                if tool_id == "get_overdue_customers":
                    detail = "Customer exposure lookup failed"
                    tool_executions.append(
                        ToolExecution(
                            tool_id=tool_id,
                            status="error",
                            started_at=tool_started,
                            completed_at=tool_completed,
                            record_count=0,
                            detail=detail,
                            error_code="flowone_lookup_failed",
                        )
                    )
                    yield {
                        "kind": "step",
                        "id": step_id,
                        "label": label,
                        "status": "error",
                        "detail": detail,
                    }
                    raise DailyArWorkerError(
                        "Daily AR Priorities could not complete because the current "
                        "FlowOne customer-exposure lookup failed."
                    ) from error

                error_details = {
                    "get_unpaid_invoices": "Invoice lookup failed; worker continued without invoice context",
                    "get_customer_payments": "Payment lookup failed; worker continued without payment context",
                    "get_credit_notes": "Credit-note lookup failed; worker continued without this context",
                    "get_assigned_tasks": "Task lookup failed; worker continued without task context",
                }
                detail = error_details.get(tool_id, "FlowOne lookup failed")
                tool_executions.append(
                    ToolExecution(
                        tool_id=tool_id,
                        status="error",
                        started_at=tool_started,
                        completed_at=tool_completed,
                        record_count=0,
                        detail=detail,
                        error_code="flowone_lookup_failed",
                    )
                )
                yield {
                    "kind": "step",
                    "id": step_id,
                    "label": label,
                    "status": "error",
                    "detail": detail,
                }

        prioritize = STEP_DEFINITIONS[-1]
        yield {
            "kind": "step",
            "id": prioritize["id"],
            "label": prioritize["label"],
            "status": "running",
        }

        observed_at = utc_now_iso()
        cases = self.build_priority_cases(
            source_records["get_overdue_customers"],
            source_records["get_unpaid_invoices"],
            source_records["get_customer_payments"],
            source_records["get_credit_notes"],
            source_records["get_assigned_tasks"],
            run_id=run_id,
            observed_at=observed_at,
            source_statuses=source_statuses,
        )

        yield {
            "kind": "step",
            "id": prioritize["id"],
            "label": prioritize["label"],
            "status": "done",
            "detail": f"{len(cases)} priority cases prepared",
        }

        completed = utc_now_iso()
        source_counts = {
            "overdueCustomers": len(source_records["get_overdue_customers"]),
            "unpaidInvoices": len(source_records["get_unpaid_invoices"]),
            "payments": len(source_records["get_customer_payments"]),
            "creditNotes": len(source_records["get_credit_notes"]),
            "tasks": len(source_records["get_assigned_tasks"]),
        }
        note = (
            "POC prioritization is transparent: cases are ordered by current "
            "past-due exposure, then DSO, then open-invoice balance. No company "
            "policy threshold is invented."
        )

        canonical_run = WorkerRun(
            run_id=run_id,
            worker_id=self.access.worker.id,
            worker_label=self.access.worker.label,
            status="completed",
            trigger_type=trigger_type or self.access.worker.trigger,
            started_at=started,
            completed_at=completed,
            case_ids=[str(case.get("caseId") or "") for case in cases],
            tool_executions=tool_executions,
            metadata={
                "sourceCounts": source_counts,
                "skippedSources": skipped_sources,
                "prioritization": "past_due_then_dso_then_open_invoice_balance",
            },
        )

        # Preserve the stable Feature 3 payload while adding the canonical
        # worker-run envelope that future workers will share.
        run = canonical_run.to_dict()
        run.update(
            {
                "cases": cases,
                "sourceCounts": source_counts,
                "skippedSources": skipped_sources,
                "note": note,
            }
        )

        yield {"kind": "result", "run": run}
