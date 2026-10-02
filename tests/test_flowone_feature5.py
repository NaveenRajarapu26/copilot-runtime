from __future__ import annotations

import os
import tempfile
import unittest

from persistence import SQLiteStateStore


class FlowOneFeature5Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "agent_state.db")
        self.store = SQLiteStateStore(self.db_path)
        self.scope_a = "scope-a"
        self.scope_b = "scope-b"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def sample_case(self) -> dict:
        return {
            "caseId": "case-1",
            "caseType": "ar_collection_priority",
            "caseStatus": "open",
            "createdByWorker": "daily_ar_priorities",
            "createdFromRunId": "run-1",
            "createdAt": "2026-10-02T10:00:00Z",
            "customerId": "customer-1",
            "customerName": "Alpha Traders",
            "subject": {
                "type": "customer",
                "id": "customer-1",
                "name": "Alpha Traders",
            },
            "evidence": [
                {
                    "evidenceId": "case-1:exposure",
                    "sourceSystem": "FlowOne",
                    "sourceType": "flowone_tool",
                    "toolId": "get_overdue_customers",
                    "recordType": "customer_exposure",
                    "status": "observed",
                    "scope": "bulk_scan",
                    "observedAt": "2026-10-02T10:01:00Z",
                    "summary": "Past due exposure observed.",
                    "recordIds": ["customer-1"],
                    "facts": {"pastDueInvBal": 1000},
                }
            ],
        }

    def sample_run(self) -> dict:
        case = self.sample_case()
        return {
            "schemaVersion": "1.0",
            "runType": "worker_run",
            "runId": "run-1",
            "workerId": "daily_ar_priorities",
            "workerLabel": "Daily AR Priorities",
            "status": "completed",
            "triggerType": "on_demand",
            "startedAt": "2026-10-02T10:00:00Z",
            "completedAt": "2026-10-02T10:02:00Z",
            "caseCount": 1,
            "caseIds": ["case-1"],
            "toolExecutions": [],
            "runMetadata": {},
            "cases": [case],
            "sourceCounts": {
                "overdueCustomers": 1,
                "unpaidInvoices": 0,
                "payments": 0,
                "creditNotes": 0,
                "tasks": 0,
            },
            "skippedSources": [],
            "note": "test",
        }

    def test_worker_run_case_and_evidence_survive_store_reopen(self) -> None:
        self.store.persist_worker_result(self.scope_a, self.sample_run())

        reopened = SQLiteStateStore(self.db_path)
        run = reopened.worker_runs.latest(self.scope_a, "daily_ar_priorities")
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run["runId"], "run-1")

        case = reopened.cases.get(self.scope_a, "case-1")
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(case["customerName"], "Alpha Traders")
        self.assertEqual(len(case["evidence"]), 1)
        self.assertEqual(case["evidence"][0]["status"], "observed")

    def test_scope_isolation_prevents_cross_user_reads(self) -> None:
        self.store.persist_worker_result(self.scope_a, self.sample_run())
        self.assertIsNone(
            self.store.worker_runs.latest(self.scope_b, "daily_ar_priorities")
        )
        self.assertIsNone(self.store.cases.get(self.scope_b, "case-1"))

    def test_journey_and_approval_lifecycle_is_durable(self) -> None:
        journey = {
            "id": "journey-1",
            "type": "collection_followup",
            "title": "Alpha Traders Follow-up",
            "customerId": "customer-1",
            "customerName": "Alpha Traders",
            "status": "approval_pending",
            "createdAt": "2026-10-02T11:00:00Z",
            "updatedAt": "2026-10-02T11:00:00Z",
        }
        self.store.journeys.save(self.scope_a, journey)
        self.store.approvals.save(
            self.scope_a,
            {
                "journeyId": "journey-1",
                "actionKey": "collection_followup",
                "status": "pending",
                "requestedAt": "2026-10-02T11:00:00Z",
                "updatedAt": "2026-10-02T11:00:00Z",
            },
        )

        approval = self.store.approvals.decide(
            self.scope_a,
            "journey-1",
            "approved_demo",
            "2026-10-02T11:05:00Z",
            {"productionActionExecuted": False},
        )
        self.assertIsNotNone(approval)
        assert approval is not None
        self.assertEqual(approval["status"], "approved_demo")

        journey["status"] = "approved_demo"
        journey["updatedAt"] = "2026-10-02T11:05:00Z"
        self.store.journeys.save(self.scope_a, journey)

        reopened = SQLiteStateStore(self.db_path)
        restored_journey = reopened.journeys.latest_active(
            self.scope_a,
            "collection_followup",
        )
        self.assertIsNotNone(restored_journey)
        assert restored_journey is not None
        self.assertEqual(restored_journey["status"], "approved_demo")
        restored_approval = reopened.approvals.get_for_journey(
            self.scope_a,
            "journey-1",
        )
        self.assertIsNotNone(restored_approval)
        assert restored_approval is not None
        self.assertEqual(restored_approval["status"], "approved_demo")

    def test_cancelled_journey_is_not_restored_as_active(self) -> None:
        journey = {
            "id": "journey-cancel",
            "type": "collection_followup",
            "title": "Cancelled",
            "status": "approval_pending",
            "createdAt": "2026-10-02T11:00:00Z",
            "updatedAt": "2026-10-02T11:00:00Z",
        }
        self.store.journeys.save(self.scope_a, journey)
        self.store.journeys.cancel(
            self.scope_a,
            "journey-cancel",
            "2026-10-02T11:01:00Z",
        )
        self.assertIsNone(
            self.store.journeys.latest_active(
                self.scope_a,
                "collection_followup",
            )
        )


if __name__ == "__main__":
    unittest.main()
