from __future__ import annotations

import asyncio
import unittest

import app as app_module
from agents import PaymentFollowupWatchWorker, resolve_worker_access
from flowone.daily_ar_tools import DailyArToolResult


class FakeTools:
    def __init__(self, data: dict[str, list[dict]]) -> None:
        self.data = data

    async def run_tool(self, tool_id: str) -> DailyArToolResult:
        return DailyArToolResult(tool_id, list(self.data.get(tool_id, [])))


class FlowOneFeature7Tests(unittest.TestCase):
    def capabilities(self) -> dict[str, bool]:
        return {
            "canViewCollections": True,
            "canViewCustomers": True,
            "canViewPayments": True,
            "canViewCreditNotes": True,
            "canViewTasks": True,
            "canManageCollections": True,
        }

    def test_payment_watch_requires_payment_visibility(self) -> None:
        caps = self.capabilities()
        caps["canViewPayments"] = False
        access = resolve_worker_access("payment_followup_watch", caps)
        assert access is not None
        self.assertFalse(access.allowed)
        self.assertIn("customer payments", str(access.reason).lower())

    def test_watch_filters_payment_and_credit_note_evidence(self) -> None:
        caps = self.capabilities()
        access = resolve_worker_access("payment_followup_watch", caps)
        assert access is not None and access.allowed

        worker = PaymentFollowupWatchWorker(
            tools=FakeTools({}),
            access=access,
            capabilities=caps,
            currency_code="INR",
        )
        customers = [
            {"customerId": "1", "customerName": "Needs Followup", "customerNum": "C1", "pastDueInvBal": 1000, "dso": 20},
            {"customerId": "2", "customerName": "Has Payment", "customerNum": "C2", "pastDueInvBal": 2000, "dso": 30},
            {"customerId": "3", "customerName": "Has Credit", "customerNum": "C3", "pastDueInvBal": 3000, "dso": 40},
        ]
        payments = [
            {"paymentId": "p1", "customerName": "Has Payment", "paymentAmount": 500, "paymentDate": "2026-10-01"},
        ]
        credits = [
            {"creditNoteId": "cr1", "customerName": "Has Credit", "claimAmount": 100},
        ]
        cases, stats = worker.build_followup_cases(
            customers,
            payments,
            credits,
            [],
            run_id="run-1",
            observed_at="2026-10-02T12:00:00Z",
            source_statuses={
                "get_overdue_customers": "completed",
                "get_customer_payments": "completed",
                "get_credit_notes": "completed",
                "get_assigned_tasks": "completed",
            },
        )
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["customerName"], "Needs Followup")
        self.assertTrue(cases[0]["followupReady"])
        self.assertEqual(cases[0]["caseType"], "payment_followup_watch")
        self.assertEqual(stats["excludedWithPaymentEvidence"], 1)
        self.assertEqual(stats["excludedWithCreditNoteEvidence"], 1)

    def test_watch_continues_conservatively_without_credit_note_permission(self) -> None:
        caps = self.capabilities()
        caps["canViewCreditNotes"] = False
        access = resolve_worker_access("payment_followup_watch", caps)
        assert access is not None and access.allowed

        worker = PaymentFollowupWatchWorker(
            tools=FakeTools({}),
            access=access,
            capabilities=caps,
            currency_code="USD",
        )
        cases, _ = worker.build_followup_cases(
            [{"customerId": "1", "customerName": "A", "pastDueInvBal": 100, "dso": 5}],
            [],
            [],
            [],
            run_id="run-2",
            observed_at="2026-10-02T12:00:00Z",
            source_statuses={
                "get_overdue_customers": "completed",
                "get_customer_payments": "completed",
                "get_credit_notes": "skipped",
                "get_assigned_tasks": "completed",
            },
        )
        self.assertEqual(len(cases), 1)
        self.assertFalse(cases[0]["followupReady"])
        credit_evidence = next(item for item in cases[0]["evidence"] if item["recordType"] == "credit_note")
        self.assertEqual(credit_evidence["status"], "unavailable")

    def test_stream_run_returns_payment_watch_worker_run(self) -> None:
        caps = self.capabilities()
        access = resolve_worker_access("payment_followup_watch", caps)
        assert access is not None and access.allowed
        tools = FakeTools(
            {
                "get_overdue_customers": [
                    {"customerId": "1", "customerName": "A", "pastDueInvBal": 100, "dso": 5}
                ],
                "get_customer_payments": [],
                "get_credit_notes": [],
                "get_assigned_tasks": [],
            }
        )
        worker = PaymentFollowupWatchWorker(
            tools=tools,
            access=access,
            capabilities=caps,
            currency_code="USD",
        )

        async def collect():
            return [event async for event in worker.stream_run(run_id="watch-run", trigger_type="schedule")]

        events = asyncio.run(collect())
        result = next(event["run"] for event in events if event.get("kind") == "result")
        self.assertEqual(result["workerId"], "payment_followup_watch")
        self.assertEqual(result["triggerType"], "schedule")
        self.assertEqual(result["caseCount"], 1)
        self.assertEqual(result["sourceCounts"]["payments"], 0)

    def test_known_route_recognizes_payment_watch(self) -> None:
        result = app_module.route_known_request("Run Payment Follow-up Watch")
        self.assertEqual(
            result,
            {"intent": "RUN_WORKER", "workerId": "payment_followup_watch"},
        )


if __name__ == "__main__":
    unittest.main()
