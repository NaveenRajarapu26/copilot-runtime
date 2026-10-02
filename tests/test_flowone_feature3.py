from __future__ import annotations

import unittest

from agents import DailyArPrioritiesWorker, resolve_worker_access
from flowone.daily_ar_tools import DailyArToolResult


class FakeDailyArTools:
    def __init__(self) -> None:
        self.records = {
            "get_overdue_customers": [
                {
                    "customerId": "c1",
                    "customerName": "Alpha Traders",
                    "customerNum": "A-1",
                    "pastDueInvBal": 1_500_000,
                    "dso": 75,
                },
                {
                    "customerId": "c2",
                    "customerName": "Beta Stores",
                    "customerNum": "B-1",
                    "pastDueInvBal": 500_000,
                    "dso": 40,
                },
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
            "get_customer_payments": [
                {
                    "paymentId": "p1",
                    "customerId": "c1",
                    "customerName": "Alpha Traders",
                    "paymentDate": "2026-10-01",
                    "paymentAmount": 100_000,
                }
            ],
            "get_credit_notes": [
                {
                    "creditNoteId": "cn1",
                    "customerId": "c2",
                    "customerName": "Beta Stores",
                    "claimAmount": 25_000,
                    "approvedAmount": 20_000,
                }
            ],
            "get_assigned_tasks": [
                {"taskName": "Alpha Traders collection follow-up", "description": ""}
            ],
        }

    async def run_tool(self, tool_id: str) -> DailyArToolResult:
        return DailyArToolResult(tool_id, list(self.records[tool_id]))


class FlowOneFeature3Tests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_worker_builds_daily_ar_result(self) -> None:
        capabilities = {
            "canViewCollections": True,
            "canViewCustomers": True,
            "canViewInvoices": True,
            "canViewPayments": True,
            "canViewCreditNotes": True,
            "canViewTasks": True,
            "canManageCollections": True,
        }
        access = resolve_worker_access("daily_ar_priorities", capabilities)
        self.assertIsNotNone(access)
        assert access is not None
        self.assertTrue(access.allowed)

        worker = DailyArPrioritiesWorker(
            tools=FakeDailyArTools(),  # type: ignore[arg-type]
            access=access,
            capabilities=capabilities,
            currency_code="INR",
        )

        streamed = []
        async for event in worker.stream_run(run_id="run-1", started_at="2026-10-01T10:00:00Z"):
            streamed.append(event)

        result_event = next(event for event in streamed if event["kind"] == "result")
        run = result_event["run"]

        self.assertEqual(run["workerId"], "daily_ar_priorities")
        self.assertEqual(run["runId"], "run-1")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["sourceCounts"]["overdueCustomers"], 2)
        self.assertEqual(run["sourceCounts"]["unpaidInvoices"], 1)
        self.assertEqual(run["sourceCounts"]["payments"], 1)
        self.assertEqual(run["sourceCounts"]["creditNotes"], 1)
        self.assertEqual(run["sourceCounts"]["tasks"], 1)

        self.assertEqual(run["cases"][0]["customerName"], "Alpha Traders")
        self.assertEqual(run["cases"][0]["openInvoiceCount"], 1)
        self.assertEqual(run["cases"][0]["recentPaymentCount"], 1)
        self.assertEqual(run["cases"][0]["matchingTaskCount"], 1)
        self.assertTrue(run["cases"][0]["canPrepareFollowup"])

        step_statuses = [
            (event.get("id"), event.get("status"))
            for event in streamed
            if event.get("kind") == "step"
        ]
        self.assertIn(("overdue", "running"), step_statuses)
        self.assertIn(("overdue", "done"), step_statuses)
        self.assertIn(("prioritize", "done"), step_statuses)

    async def test_permission_denied_optional_sources_are_skipped(self) -> None:
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
        assert access is not None
        self.assertTrue(access.allowed)

        worker = DailyArPrioritiesWorker(
            tools=FakeDailyArTools(),  # type: ignore[arg-type]
            access=access,
            capabilities=capabilities,
            currency_code="USD",
        )

        streamed = []
        async for event in worker.stream_run(run_id="run-2"):
            streamed.append(event)

        run = next(event["run"] for event in streamed if event["kind"] == "result")
        self.assertEqual(run["sourceCounts"]["overdueCustomers"], 2)
        self.assertEqual(run["sourceCounts"]["unpaidInvoices"], 0)
        self.assertEqual(
            set(run["skippedSources"]),
            {"Sales invoices", "Customer payments", "Credit notes", "Assigned tasks"},
        )
        self.assertFalse(run["cases"][0]["canPrepareFollowup"])


if __name__ == "__main__":
    unittest.main()
