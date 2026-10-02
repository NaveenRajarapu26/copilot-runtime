from __future__ import annotations

import json
import unittest

import httpx

from flowone import DailyArToolService, FlowOneClient, FlowOneRequestContext


class FlowOneFeature2Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.context = FlowOneRequestContext(
            authorization="Bearer test-token",
            core_api_url="https://flowone.example/core",
            company_id="company-1",
            user_pool_id="pool-1",
            legal_entity_id="1105",
            gst_number="GST-1",
            portfolio_id="7",
            user_scope_access="PORTFOLIO",
        )

    async def test_client_mirrors_rest_core_context(self) -> None:
        captured: dict[str, object] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"data": []})

        client = FlowOneClient(self.context, transport=httpx.MockTransport(handler))
        await client.post("/dashboard/riskAcctSummary", {"companyId": "company-1"})

        headers = captured["headers"]
        body = captured["body"]
        self.assertEqual(headers["authorization"], "Bearer test-token")
        self.assertEqual(headers["x-algo-up-id"], "pool-1")
        self.assertEqual(headers["x-legal-entity-id"], "1105")
        self.assertEqual(headers["x-gst-number"], "GST-1")
        self.assertEqual(body["legalEntityId"], "1105")
        self.assertEqual(body["sgstin"], "GST-1")
        self.assertEqual(body["companyId"], "company-1")

    async def test_daily_ar_tools_use_existing_flowone_endpoints(self) -> None:
        paths: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path.endswith("/dashboard/riskAcctSummary"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "customerId": "c1",
                                "customerName": "Alpha",
                                "customerNum": "A-1",
                                "pastDueInvBal": 100,
                                "dso": 50,
                            }
                        ]
                    },
                )
            if request.url.path.endswith("/dashboard/invoice/v1/findAll"):
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "list": [
                                {
                                    "invoiceId": "i1",
                                    "customerId": "c1",
                                    "customerName": "Alpha",
                                    "balance": 80,
                                }
                            ]
                        }
                    },
                )
            if request.url.path.endswith("/payments/getAllPayments"):
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "list": [
                                {
                                    "id": "p1",
                                    "customerId": "c1",
                                    "customerName": "Alpha",
                                    "paymentAmount": 25,
                                    "paymentDate": "2026-10-01",
                                }
                            ]
                        }
                    },
                )
            if request.url.path.endswith("/returnMgt/getCrNoteList"):
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "list": [
                                {
                                    "claimId": "cn1",
                                    "customerId": "c1",
                                    "customerName": "Alpha",
                                    "claimAmount": 10,
                                }
                            ]
                        }
                    },
                )
            if request.url.path.endswith("/dashboard/tasks"):
                return httpx.Response(
                    200,
                    json={"data": {"claimTaskItems": [{"taskName": "Alpha follow-up"}]}},
                )
            return httpx.Response(404, json={})

        client = FlowOneClient(self.context, transport=httpx.MockTransport(handler))
        service = DailyArToolService(client)

        counts = {}
        for tool_id in (
            "get_overdue_customers",
            "get_unpaid_invoices",
            "get_customer_payments",
            "get_credit_notes",
            "get_assigned_tasks",
        ):
            counts[tool_id] = (await service.run_tool(tool_id)).count

        self.assertEqual(counts, {
            "get_overdue_customers": 1,
            "get_unpaid_invoices": 1,
            "get_customer_payments": 1,
            "get_credit_notes": 1,
            "get_assigned_tasks": 1,
        })
        self.assertTrue(any(path.endswith("/dashboard/riskAcctSummary") for path in paths))
        self.assertTrue(any(path.endswith("/dashboard/invoice/v1/findAll") for path in paths))
        self.assertTrue(any(path.endswith("/payments/getAllPayments") for path in paths))
        self.assertTrue(any(path.endswith("/returnMgt/getCrNoteList") for path in paths))
        self.assertTrue(any(path.endswith("/dashboard/tasks") for path in paths))


if __name__ == "__main__":
    unittest.main()
