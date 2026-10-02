from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .client import FlowOneClient


@dataclass(frozen=True)
class DailyArToolResult:
    tool_id: str
    records: list[dict[str, Any]]

    @property
    def count(self) -> int:
        return len(self.records)


class DailyArToolService:
    """Real FlowOne read tools used by the Daily AR worker migration."""

    def __init__(self, client: FlowOneClient) -> None:
        self.client = client
        self.context = client.context

    @staticmethod
    def _list_at(payload: dict[str, Any], *path: str) -> list[Any]:
        value: Any = payload
        for key in path:
            if not isinstance(value, dict):
                return []
            value = value.get(key)
        return value if isinstance(value, list) else []

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _string(value: Any) -> str:
        return str(value if value is not None else "")

    @staticmethod
    def _portfolio_value(value: str | None) -> int | str:
        raw = str(value or "").strip()
        if not raw:
            return 0
        try:
            return int(raw)
        except ValueError:
            return raw

    async def get_overdue_customers(self) -> DailyArToolResult:
        payload = await self.client.post(
            "/dashboard/riskAcctSummary",
            {
                "companyId": self.context.company_id,
                "portfolioId": self._portfolio_value(self.context.portfolio_id),
                "userScopeAccess": self.context.user_scope_access,
            },
        )

        rows = self._list_at(payload, "data")
        records = [
            {
                "customerId": self._string(item.get("customerId")),
                "customerName": self._string(item.get("customerName")),
                "customerNum": self._string(item.get("customerNum")),
                "pastDueInvBal": self._number(item.get("pastDueInvBal")),
                "dso": self._number(item.get("dso")),
            }
            for item in rows
            if isinstance(item, dict)
        ]
        records = [row for row in records if row["customerName"] and row["dso"] != 0]
        records.sort(key=lambda row: (-row["pastDueInvBal"], -row["dso"]))
        return DailyArToolResult("get_overdue_customers", records)

    async def get_unpaid_invoices(self) -> DailyArToolResult:
        payload = await self.client.post(
            "/dashboard/invoice/v1/findAll",
            {
                "pageNumber": 0,
                "pageSize": 100,
                "globalSearch": "",
                "invStatus": "OPEN",
                "userScopeAccess": self.context.user_scope_access,
                "portfolioId": self._portfolio_value(self.context.portfolio_id),
                "companyId": self.context.company_id,
            },
        )

        rows = self._list_at(payload, "data", "list")
        records: list[dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            customer = item.get("customer") if isinstance(item.get("customer"), dict) else {}
            record = {
                "invoiceId": self._string(item.get("invoiceId") or item.get("id")),
                "invoiceNum": self._string(item.get("formatNumber") or item.get("invoiceNum")),
                "customerId": self._string(item.get("customerId")),
                "customerName": self._string(item.get("customerName") or customer.get("name")),
                "customerNum": self._string(item.get("customerNum")),
                "balance": self._number(item.get("balance")),
                "amount": self._number(item.get("amount") or item.get("amountWithTax")),
                "dueDate": self._string(item.get("dueDate")),
                "status": self._string(
                    item.get("invoiceStatus") or item.get("invStatus") or item.get("status") or "OPEN"
                ),
            }
            if record["balance"] > 0:
                records.append(record)

        records.sort(key=lambda row: -row["balance"])
        return DailyArToolResult("get_unpaid_invoices", records)

    async def get_customer_payments(self) -> DailyArToolResult:
        payload = await self.client.post(
            "/payments/getAllPayments",
            {
                "companyId": self.context.company_id,
                "customerId": "",
                "pageSize": 100,
                "pageNumber": 0,
                "globalSearch": "",
                "portfolioId": self._portfolio_value(self.context.portfolio_id),
                "userScopeAccess": self.context.user_scope_access,
            },
        )

        rows = self._list_at(payload, "data", "list")
        records: list[dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            customer_details = (
                item.get("customerDetails") if isinstance(item.get("customerDetails"), dict) else {}
            )
            customer = item.get("customer") if isinstance(item.get("customer"), dict) else {}
            records.append(
                {
                    "paymentId": self._string(
                        item.get("id") or item.get("paymentId") or item.get("paymentRequestId")
                    ),
                    "receiptReferenceNum": self._string(
                        item.get("formatNumber")
                        or item.get("receiptReferenceNum")
                        or item.get("receiptNum")
                    ),
                    "customerId": self._string(
                        item.get("customerId")
                        or customer_details.get("customerId")
                        or customer_details.get("id")
                        or customer.get("customerId")
                        or customer.get("id")
                    ),
                    "customerName": self._string(
                        customer_details.get("name") or item.get("customerName") or customer.get("name")
                    ),
                    "customerNum": self._string(
                        item.get("customerNum")
                        or customer_details.get("customerNum")
                        or customer.get("customerNum")
                    ),
                    "paymentDate": self._string(item.get("paymentDate") or item.get("paidOn")),
                    "paymentAmount": self._number(
                        item.get("paymentAmount") or item.get("totalAmountPaid") or item.get("amount")
                    ),
                    "status": self._string(item.get("transactionStatus") or item.get("status")),
                    "source": self._string(item.get("source") or item.get("modeofPayment")),
                }
            )

        records.sort(key=lambda row: row["paymentDate"], reverse=True)
        return DailyArToolResult("get_customer_payments", records)

    async def get_credit_notes(self) -> DailyArToolResult:
        payload = await self.client.post(
            "/returnMgt/getCrNoteList",
            {
                "page": 0,
                "pageSize": 100,
                "searchKeyword": "",
                "userScopeAccess": self.context.user_scope_access,
                "portfolioId": self._portfolio_value(self.context.portfolio_id),
                "companyId": self.context.company_id,
            },
        )

        rows = self._list_at(payload, "data", "list")
        records: list[dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            customer = item.get("customer") if isinstance(item.get("customer"), dict) else {}
            category = (
                item.get("claimCategoryConfig")
                if isinstance(item.get("claimCategoryConfig"), dict)
                else {}
            )
            records.append(
                {
                    "creditNoteId": self._string(item.get("claimId") or item.get("id")),
                    "creditNoteNum": self._string(item.get("formatNumber") or item.get("claimCode")),
                    "customerId": self._string(
                        item.get("customerId") or customer.get("customerId") or customer.get("id")
                    ),
                    "customerName": self._string(
                        item.get("customerName") or customer.get("name") or customer.get("customerName")
                    ),
                    "customerNum": self._string(item.get("customerNum") or customer.get("customerNum")),
                    "claimAmount": self._number(item.get("claimAmount") or item.get("amount")),
                    "approvedAmount": self._number(item.get("approvedAmount")),
                    "createdDate": self._string(item.get("createdDate")),
                    "status": self._string(item.get("status") or item.get("flowStatus")),
                    "categoryName": self._string(item.get("categoryName") or category.get("name")),
                }
            )

        records.sort(key=lambda row: row["createdDate"], reverse=True)
        return DailyArToolResult("get_credit_notes", records)

    async def get_assigned_tasks(self) -> DailyArToolResult:
        payload = await self.client.get("/dashboard/tasks")
        rows = self._list_at(payload, "data", "claimTaskItems")
        records = [dict(item) for item in rows if isinstance(item, dict)]
        return DailyArToolResult("get_assigned_tasks", records)

    async def run_tool(self, tool_id: str) -> DailyArToolResult:
        dispatch: dict[str, Callable[[], Awaitable[DailyArToolResult]]] = {
            "get_overdue_customers": self.get_overdue_customers,
            "get_unpaid_invoices": self.get_unpaid_invoices,
            "get_customer_payments": self.get_customer_payments,
            "get_credit_notes": self.get_credit_notes,
            "get_assigned_tasks": self.get_assigned_tasks,
        }
        handler = dispatch.get(tool_id)
        if handler is None:
            raise KeyError(f"Unknown Daily AR tool: {tool_id}")
        return await handler()
