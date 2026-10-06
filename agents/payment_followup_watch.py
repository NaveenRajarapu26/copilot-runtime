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
        "id": "payments",
        "label": "Checking current payment activity",
        "toolId": "get_customer_payments",
        "sourceLabel": "Customer payments",
    },
    {
        "id": "credit-notes",
        "label": "Checking credit-note blockers",
        "toolId": "get_credit_notes",
        "sourceLabel": "Credit notes",
    },
    {
        "id": "tasks",
        "label": "Checking existing follow-up tasks",
        "toolId": "get_assigned_tasks",
        "sourceLabel": "Assigned tasks",
    },
    {
        "id": "evaluate",
        "label": "Identifying payment-status follow-up cases",
        "toolId": "",
        "sourceLabel": "",
    },
)


class PaymentFollowupWorkerError(RuntimeError):
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


def _customer_name_matches(row: Mapping[str, Any], customer: Mapping[str, Any]) -> bool:
    left = normalize(row.get("customerName"))
    right = normalize(customer.get("customerName"))
    return bool(left and right and left == right)


def _format_compact_currency(value: float, currency_code: str) -> str:
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


def _stable_case_id(customer: Mapping[str, Any]) -> str:
    customer_key = str(
        customer.get("customerId")
        or customer.get("customerNum")
        or normalize(customer.get("customerName"))
        or "unknown"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"flowone:payment-followup-watch:{customer_key}"))


class PaymentFollowupWatchWorker:
    """
    Deterministic condition/watch worker.

    A payment-status follow-up case is produced only when:
    - the customer is currently overdue, and
    - no matching payment is present in the current permitted payment result set, and
    - no matching credit-note blocker is present when credit-note evidence is available.

    No company-specific amount/age threshold is invented.
    """

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

    def _build_evidence(
        self,
        *,
        case_id: str,
        observed_at: str,
        customer: Mapping[str, Any],
        matching_payments: list[dict[str, Any]],
        matching_credit_notes: list[dict[str, Any]],
        matching_tasks: list[dict[str, Any]],
        source_statuses: Mapping[str, str],
    ) -> list[EvidenceItem]:
        past_due = _number(customer.get("pastDueInvBal"))
        dso = _number(customer.get("dso"))
        customer_record_id = str(
            customer.get("customerId")
            or customer.get("customerNum")
            or customer.get("customerName")
            or ""
        )

        evidence: list[EvidenceItem] = [
            EvidenceItem(
                evidence_id=f"{case_id}:exposure",
                source_system="FlowOne",
                source_type="flowone_tool",
                tool_id="get_overdue_customers",
                record_type="customer_exposure",
                status="observed",
                scope="watch_scan",
                observed_at=observed_at,
                summary=(
                    "Current FlowOne exposure reports "
                    f"{_format_compact_currency(past_due, self.currency_code)} past due "
                    f"and {round(dso)} DSO days."
                ),
                record_ids=(customer_record_id,) if customer_record_id else (),
                facts={
                    "pastDueInvBal": past_due,
                    "dso": dso,
                    "currencyCode": self.currency_code,
                },
            )
        ]

        def add_source_evidence(
            *,
            tool_id: str,
            record_type: str,
            records: list[dict[str, Any]],
            id_keys: tuple[str, ...],
            observed_summary: str,
            no_match_summary: str,
        ) -> None:
            source_status = source_statuses.get(tool_id, "pending")
            if source_status == "skipped":
                status = "unavailable"
                summary = f"{record_type.replace('_', ' ').title()} evidence is not available to the current permission set."
            elif source_status == "error":
                status = "error"
                summary = f"{record_type.replace('_', ' ').title()} lookup failed; absence of evidence is not assumed."
            elif records:
                status = "observed"
                summary = observed_summary
            else:
                status = "no_match"
                summary = no_match_summary

            evidence.append(
                EvidenceItem(
                    evidence_id=f"{case_id}:{record_type}",
                    source_system="FlowOne",
                    source_type="flowone_tool",
                    tool_id=tool_id,
                    record_type=record_type,
                    status=status,
                    scope="watch_scan",
                    observed_at=observed_at,
                    summary=summary,
                    record_ids=collect_record_ids(records, id_keys),
                    facts={"recordCount": len(records)},
                )
            )

        add_source_evidence(
            tool_id="get_customer_payments",
            record_type="payment",
            records=matching_payments,
            id_keys=("paymentId", "receiptReferenceNum"),
            observed_summary=f"{len(matching_payments)} matching payment record(s) are present in the current payment result set.",
            no_match_summary="No matching payment record was returned in the current payment result set.",
        )
        add_source_evidence(
            tool_id="get_credit_notes",
            record_type="credit_note",
            records=matching_credit_notes,
            id_keys=("creditNoteId", "creditNoteNum"),
            observed_summary=f"{len(matching_credit_notes)} matching credit-note record(s) are present in the current scope.",
            no_match_summary="No matching credit-note blocker was returned in the current credit-note result set.",
        )
        add_source_evidence(
            tool_id="get_assigned_tasks",
            record_type="task",
            records=matching_tasks,
            id_keys=("id", "taskId", "claimTaskId"),
            observed_summary=f"{len(matching_tasks)} assigned task(s) reference this customer.",
            no_match_summary="No assigned task text matched this customer in the current task result.",
        )
        return evidence

    def build_followup_cases(
        self,
        overdue_customers: list[dict[str, Any]],
        payments: list[dict[str, Any]],
        credit_notes: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        *,
        run_id: str,
        observed_at: str,
        source_statuses: Mapping[str, str],
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        cases: list[dict[str, Any]] = []
        stats = {
            "excludedWithPaymentEvidence": 0,
            "excludedWithCreditNoteEvidence": 0,
            "candidates": 0,
        }
        can_manage_collections = bool(self.capabilities.get("canManageCollections"))
        credit_evidence_reliable = source_statuses.get("get_credit_notes") == "completed"

        for customer in overdue_customers:
            matching_payments = [
                row for row in payments if _customer_name_matches(row, customer)
            ]
            matching_credit_notes = [
                row for row in credit_notes if _customer_name_matches(row, customer)
            ]
            customer_key = normalize(customer.get("customerName"))
            matching_tasks: list[dict[str, Any]] = []
            for task in tasks:
                task_text = normalize(
                    f"{task.get('taskName', '')} {task.get('description', '')}"
                )
                if customer_key and customer_key in task_text:
                    matching_tasks.append(task)

            # This watch is intentionally narrower than Daily AR. Current
            # payment evidence means a payment-status follow-up is not created;
            # the account may instead require reconciliation/review later.
            if matching_payments:
                stats["excludedWithPaymentEvidence"] += 1
                continue

            # A current credit note is treated as a blocker rather than a
            # collection follow-up candidate. A dedicated credit/dispute worker
            # will own that path in a later feature.
            if matching_credit_notes:
                stats["excludedWithCreditNoteEvidence"] += 1
                continue

            past_due = _number(customer.get("pastDueInvBal"))
            dso = _number(customer.get("dso"))
            reasons = [
                "Customer is currently overdue in FlowOne: "
                f"{_format_compact_currency(past_due, self.currency_code)} past due."
            ]
            if dso > 0:
                reasons.append(f"Current DSO: {round(dso)} days.")
            reasons.append(
                "No matching payment was found in the current permitted payment result set."
            )

            if credit_evidence_reliable:
                reasons.append(
                    "No matching credit-note blocker was found in the current credit-note result set."
                )
            else:
                reasons.append(
                    "Credit-note blocker evidence is unavailable or incomplete, so outreach readiness is not assumed."
                )

            if matching_tasks:
                reasons.append(
                    f"{len(matching_tasks)} existing assigned task(s) reference this customer."
                )

            followup_ready = bool(can_manage_collections and credit_evidence_reliable)
            recommended_next_step = (
                "Review the account and prepare a payment-status follow-up. Any customer-facing execution remains human/policy gated."
                if followup_ready
                else "Review the account and confirm missing blocker/permission context before any customer outreach."
            )

            case_id = _stable_case_id(customer)
            evidence = self._build_evidence(
                case_id=case_id,
                observed_at=observed_at,
                customer=customer,
                matching_payments=matching_payments,
                matching_credit_notes=matching_credit_notes,
                matching_tasks=matching_tasks,
                source_statuses=source_statuses,
            )

            business_case = BusinessCase(
                case_id=case_id,
                case_type="payment_followup_watch",
                status="open",
                created_by_worker="payment_followup_watch",
                created_from_run_id=run_id,
                created_at=observed_at,
                subject={
                    "type": "customer",
                    "id": str(customer.get("customerId") or ""),
                    "name": str(customer.get("customerName") or ""),
                    "reference": str(customer.get("customerNum") or ""),
                },
                summary=(
                    f"{str(customer.get('customerName') or 'Customer')} is overdue with no matching payment evidence in the current watch scan."
                ),
                reasons=reasons,
                recommendation=CaseRecommendation(
                    action_key=(
                        "prepare_collection_followup"
                        if followup_ready
                        else "review_payment_status"
                    ),
                    label=(
                        "Prepare payment-status follow-up"
                        if followup_ready
                        else "Review payment status"
                    ),
                    description=recommended_next_step,
                    external_impact=False,
                    requires_human_approval=followup_ready,
                ),
                evidence=evidence,
                tags=("accounts_receivable", "payments", "watch", "followup"),
                metadata={
                    "currencyCode": self.currency_code,
                    "pastDueInvBal": past_due,
                    "dso": dso,
                    "followupReady": followup_ready,
                    "matchingTaskCount": len(matching_tasks),
                    "watchClassification": "payment_status_followup_candidate",
                },
            )

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
                "openInvoiceCount": 0,
                "openInvoiceBalance": 0.0,
                "recentPaymentCount": 0,
                "latestPaymentDate": "",
                "recentPaymentAmount": 0.0,
                "creditNoteCount": 0,
                "creditNoteAmount": 0.0,
                "matchingTaskCount": len(matching_tasks),
                "reasons": reasons,
                "recommendedNextStep": recommended_next_step,
                "canPrepareFollowup": followup_ready,
                "invoices": [],
                "payments": [],
                "creditNotes": [],
                "tasks": matching_tasks[:10],
                "evidenceMode": "bulk_scan",
                "followupReady": followup_ready,
                "watchClassification": "payment_status_followup_candidate",
            }
            case_payload.update(business_case.to_dict())
            cases.append(case_payload)

        cases.sort(
            key=lambda case: (
                -_number(case.get("pastDueInvBal")),
                -_number(case.get("dso")),
            )
        )
        cases = cases[:10]
        stats["candidates"] = len(cases)
        return cases, stats

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
            "get_customer_payments": "payment records reviewed",
            "get_credit_notes": "credit-note records reviewed",
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
                completed_at = utc_now_iso()
                detail = f"{result.count} {detail_labels[tool_id]}"
                tool_executions.append(
                    ToolExecution(
                        tool_id=tool_id,
                        status="completed",
                        started_at=tool_started,
                        completed_at=completed_at,
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
                completed_at = utc_now_iso()
                essential = tool_id in {
                    "get_overdue_customers",
                    "get_customer_payments",
                }
                detail = (
                    "Required watch evidence lookup failed"
                    if essential
                    else "Optional watch evidence lookup failed; worker continued conservatively"
                )
                tool_executions.append(
                    ToolExecution(
                        tool_id=tool_id,
                        status="error",
                        started_at=tool_started,
                        completed_at=completed_at,
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
                if essential:
                    raise PaymentFollowupWorkerError(
                        "Payment Follow-up Watch could not complete because required FlowOne overdue/payment evidence was unavailable."
                    ) from error

        evaluate = STEP_DEFINITIONS[-1]
        yield {
            "kind": "step",
            "id": evaluate["id"],
            "label": evaluate["label"],
            "status": "running",
        }

        observed_at = utc_now_iso()
        cases, evaluation_stats = self.build_followup_cases(
            source_records["get_overdue_customers"],
            source_records["get_customer_payments"],
            source_records["get_credit_notes"],
            source_records["get_assigned_tasks"],
            run_id=run_id,
            observed_at=observed_at,
            source_statuses=source_statuses,
        )

        yield {
            "kind": "step",
            "id": evaluate["id"],
            "label": evaluate["label"],
            "status": "done",
            "detail": f"{len(cases)} payment-status follow-up cases prepared",
        }

        completed = utc_now_iso()
        source_counts = {
            "overdueCustomers": len(source_records["get_overdue_customers"]),
            "unpaidInvoices": 0,
            "payments": len(source_records["get_customer_payments"]),
            "creditNotes": len(source_records["get_credit_notes"]),
            "tasks": len(source_records["get_assigned_tasks"]),
        }
        note = (
            "Payment Follow-up Watch creates a case only for currently overdue accounts with no matching payment evidence and no observed credit-note blocker in the current permitted result sets. No company-specific amount or age threshold is invented."
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
                "watchRule": "overdue_without_matching_payment_or_credit_note",
                **evaluation_stats,
            },
        )

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
