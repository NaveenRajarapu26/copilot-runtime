from __future__ import annotations

import unittest

from agents import DailyArPrioritiesWorker, resolve_worker_access
from flowone import FlowOneApiError
from flowone.daily_ar_tools import DailyArToolResult


class Feature4Tools:
    def __init__(self, *, fail_tool: str | None = None) -> None:
        self.fail_tool = fail_tool
        self.records = {
            "get_overdue_customers": [
                {
                    "customerId": "c1",
                    "customerName": "Alpha Traders",
                    "customerNum": "A-1",
                    "pastDueInvBal": 1_500_000,
                    "dso": 75,
                }
            ],
            "get_unpaid_invoices": [
                {
                    "invoiceId": "i1",
                    "customerId": "c1",
                    "customerName": "Alpha Traders",
                    "balance": 1_000_000,
                    "amount": 1_000_000,
                }
            ],
            "get_customer_payments": [],
            "get_credit_notes": [],
            "get_assigned_tasks": [
                {
                    "taskName": "Alpha Traders collection follow-up",
                    "description": "Review account",
                }
            ],
        }

    async def run_tool(self, tool_id: str) -> DailyArToolResult:
        if tool_id == self.fail_tool:
            raise FlowOneApiError("simulated failure")
        return DailyArToolResult(tool_id, list(self.records[tool_id]))


class FlowOneFeature4Tests(unittest.IsolatedAsyncioTestCase):
    def full_capabilities(self) -> dict[str, bool]:
        return {
            "canViewCollections": True,
            "canViewCustomers": True,
            "canViewInvoices": True,
            "canViewPayments": True,
            "canViewCreditNotes": True,
            "canViewTasks": True,
            "canManageCollections": True,
        }

    async def test_worker_run_and_case_evidence_contract(self) -> None:
        capabilities = self.full_capabilities()
        access = resolve_worker_access("daily_ar_priorities", capabilities)
        assert access is not None and access.allowed

        worker = DailyArPrioritiesWorker(
            tools=Feature4Tools(),  # type: ignore[arg-type]
            access=access,
            capabilities=capabilities,
            currency_code="INR",
        )

        events = []
        async for event in worker.stream_run(
            run_id="run-feature4",
            started_at="2026-10-02T10:00:00Z",
        ):
            events.append(event)

        run = next(event["run"] for event in events if event["kind"] == "result")

        # Canonical WorkerRun fields.
        self.assertEqual(run["schemaVersion"], "1.0")
        self.assertEqual(run["runType"], "worker_run")
        self.assertEqual(run["workerId"], "daily_ar_priorities")
        self.assertEqual(run["workerLabel"], "Daily AR Priorities")
        self.assertEqual(run["triggerType"], "on_demand")
        self.assertEqual(run["caseCount"], 1)
        self.assertEqual(len(run["caseIds"]), 1)
        self.assertEqual(len(run["toolExecutions"]), 5)
        self.assertTrue(
            all(item["status"] == "completed" for item in run["toolExecutions"])
        )

        case = run["cases"][0]

        # Canonical BusinessCase fields.
        self.assertEqual(case["caseId"], run["caseIds"][0])
        self.assertEqual(case["caseType"], "ar_collection_priority")
        self.assertEqual(case["caseStatus"], "open")
        self.assertEqual(case["createdByWorker"], "daily_ar_priorities")
        self.assertEqual(case["createdFromRunId"], "run-feature4")
        self.assertEqual(case["subject"]["id"], "c1")
        self.assertEqual(case["recommendation"]["actionKey"], "prepare_collection_followup")
        self.assertEqual(len(case["evidence"]), 5)

        evidence_by_type = {item["recordType"]: item for item in case["evidence"]}
        self.assertEqual(evidence_by_type["customer_exposure"]["status"], "observed")
        self.assertEqual(evidence_by_type["invoice"]["status"], "observed")
        self.assertEqual(evidence_by_type["payment"]["status"], "no_match")
        self.assertEqual(evidence_by_type["credit_note"]["status"], "no_match")
        self.assertEqual(evidence_by_type["task"]["status"], "observed")
        self.assertEqual(evidence_by_type["invoice"]["recordIds"], ["i1"])

        # Stable Feature 3 compatibility fields remain available.
        self.assertEqual(case["customerName"], "Alpha Traders")
        self.assertEqual(case["openInvoiceCount"], 1)
        self.assertTrue(case["canPrepareFollowup"])

    async def test_permission_denied_sources_are_explicit_evidence(self) -> None:
        capabilities = {
            "canViewCollections": True,
            "canViewCustomers": True,
            "canViewInvoices": False,
            "canViewPayments": False,
            "canViewCreditNotes": False,
            "canViewTasks": False,
            "canManageCollections": False,
        }
        access = resolve_worker_access("daily_ar_priorities", capabilities)
        assert access is not None and access.allowed

        worker = DailyArPrioritiesWorker(
            tools=Feature4Tools(),  # type: ignore[arg-type]
            access=access,
            capabilities=capabilities,
        )

        events = []
        async for event in worker.stream_run(run_id="run-denied"):
            events.append(event)
        run = next(event["run"] for event in events if event["kind"] == "result")

        skipped = {
            item["toolId"]: item
            for item in run["toolExecutions"]
            if item["status"] == "skipped"
        }
        self.assertEqual(len(skipped), 4)

        evidence = {
            item["recordType"]: item for item in run["cases"][0]["evidence"]
        }
        self.assertEqual(evidence["invoice"]["status"], "unavailable")
        self.assertEqual(evidence["payment"]["status"], "unavailable")
        self.assertEqual(evidence["credit_note"]["status"], "unavailable")
        self.assertEqual(evidence["task"]["status"], "unavailable")

    async def test_failed_optional_source_is_not_treated_as_no_match(self) -> None:
        capabilities = self.full_capabilities()
        access = resolve_worker_access("daily_ar_priorities", capabilities)
        assert access is not None and access.allowed

        worker = DailyArPrioritiesWorker(
            tools=Feature4Tools(fail_tool="get_customer_payments"),  # type: ignore[arg-type]
            access=access,
            capabilities=capabilities,
        )

        events = []
        async for event in worker.stream_run(run_id="run-error"):
            events.append(event)
        run = next(event["run"] for event in events if event["kind"] == "result")

        payment_execution = next(
            item
            for item in run["toolExecutions"]
            if item["toolId"] == "get_customer_payments"
        )
        self.assertEqual(payment_execution["status"], "error")

        payment_evidence = next(
            item
            for item in run["cases"][0]["evidence"]
            if item["recordType"] == "payment"
        )
        self.assertEqual(payment_evidence["status"], "error")
        self.assertIn("must not be treated as negative evidence", payment_evidence["summary"])


if __name__ == "__main__":
    unittest.main()
