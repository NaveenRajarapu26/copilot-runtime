from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Mapping

from flowone import DailyArToolService, FlowOneApiError
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
    """
    Backend-only explanatory formatting. Numeric values stay authoritative in the
    result payload and are formatted by the FlowOne UI for cards/tables.
    """

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

    def build_priority_cases(
        self,
        overdue_customers: list[dict[str, Any]],
        invoices: list[dict[str, Any]],
        payments: list[dict[str, Any]],
        credit_notes: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
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

            cases.append(
                {
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
                    "recommendedNextStep": (
                        "Review the account context and prepare a collection follow-up "
                        "if the evidence supports outreach."
                        if can_manage_collections
                        else "Review the account context and route any required follow-up "
                        "to a user with collection-management permission."
                    ),
                    "canPrepareFollowup": can_manage_collections,
                    "invoices": matching_invoices[:20],
                    "payments": matching_payments[:20],
                    "creditNotes": matching_credit_notes[:20],
                    "tasks": matching_tasks[:10],
                    "evidenceMode": "bulk_scan",
                }
            )

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

        for step in STEP_DEFINITIONS[:-1]:
            step_id = step["id"]
            label = step["label"]
            tool_id = step["toolId"]
            source_label = step["sourceLabel"]

            if tool_id in denied:
                skipped_sources.append(source_label)
                yield {
                    "kind": "step",
                    "id": step_id,
                    "label": label,
                    "status": "skipped",
                    "detail": "Not available to the current permission set",
                }
                continue

            if tool_id not in permitted:
                continue

            yield {
                "kind": "step",
                "id": step_id,
                "label": label,
                "status": "running",
            }

            try:
                result = await self.tools.run_tool(tool_id)
                source_records[tool_id] = result.records
                detail_labels = {
                    "get_overdue_customers": "scoped overdue customer records",
                    "get_unpaid_invoices": "open invoice records",
                    "get_customer_payments": "recent payment records",
                    "get_credit_notes": "credit-note records",
                    "get_assigned_tasks": "assigned task records",
                }
                yield {
                    "kind": "step",
                    "id": step_id,
                    "label": label,
                    "status": "done",
                    "detail": f"{result.count} {detail_labels[tool_id]}",
                }
            except (FlowOneApiError, KeyError) as error:
                if tool_id == "get_overdue_customers":
                    yield {
                        "kind": "step",
                        "id": step_id,
                        "label": label,
                        "status": "error",
                        "detail": "Customer exposure lookup failed",
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
                yield {
                    "kind": "step",
                    "id": step_id,
                    "label": label,
                    "status": "error",
                    "detail": error_details.get(tool_id, "FlowOne lookup failed"),
                }

        prioritize = STEP_DEFINITIONS[-1]
        yield {
            "kind": "step",
            "id": prioritize["id"],
            "label": prioritize["label"],
            "status": "running",
        }

        cases = self.build_priority_cases(
            source_records["get_overdue_customers"],
            source_records["get_unpaid_invoices"],
            source_records["get_customer_payments"],
            source_records["get_credit_notes"],
            source_records["get_assigned_tasks"],
        )

        yield {
            "kind": "step",
            "id": prioritize["id"],
            "label": prioritize["label"],
            "status": "done",
            "detail": f"{len(cases)} priority cases prepared",
        }

        completed = utc_now_iso()
        run = {
            "workerId": "daily_ar_priorities",
            "runId": run_id,
            "status": "completed",
            "startedAt": started,
            "completedAt": completed,
            "cases": cases,
            "sourceCounts": {
                "overdueCustomers": len(source_records["get_overdue_customers"]),
                "unpaidInvoices": len(source_records["get_unpaid_invoices"]),
                "payments": len(source_records["get_customer_payments"]),
                "creditNotes": len(source_records["get_credit_notes"]),
                "tasks": len(source_records["get_assigned_tasks"]),
            },
            "skippedSources": skipped_sources,
            "note": (
                "POC prioritization is transparent: cases are ordered by current "
                "past-due exposure, then DSO, then open-invoice balance. No company "
                "policy threshold is invented."
            ),
        }

        yield {"kind": "result", "run": run}
