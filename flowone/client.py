from __future__ import annotations

from typing import Any

import httpx

from .context import FlowOneRequestContext


class FlowOneApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FlowOneClient:
    """
    Server-side equivalent of core-ui RestCoreClient for read-oriented worker tools.

    It forwards the logged-in user's Bearer token / X-ALGO-UP-ID and mirrors the
    existing legal-entity/GST context injection behavior. It intentionally does
    not refresh tokens or store credentials.
    """

    def __init__(
        self,
        context: FlowOneRequestContext,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.context = context
        self._transport = transport
        self._timeout = httpx.Timeout(
            connect=20.0,
            read=60.0,
            write=20.0,
            pool=20.0,
        )

    @staticmethod
    def _has_explicit_legal_entity(
        params: dict[str, Any],
        body: dict[str, Any],
    ) -> bool:
        return "legalEntityId" in params or "legalEntityId" in body

    @staticmethod
    def _has_explicit_gstin(
        params: dict[str, Any],
        body: dict[str, Any],
    ) -> bool:
        return any(key in params or key in body for key in ("gstin", "sgstin"))

    def _apply_context(
        self,
        *,
        method: str,
        params: dict[str, Any] | None,
        body: dict[str, Any] | None,
        skip_context: bool,
    ) -> tuple[dict[str, str], dict[str, Any], dict[str, Any] | None]:
        headers = self.context.upstream_headers()
        request_params = dict(params or {})
        request_body = dict(body) if isinstance(body, dict) else body

        if skip_context:
            return headers, request_params, request_body

        body_for_checks = request_body if isinstance(request_body, dict) else {}
        explicit_legal = self._has_explicit_legal_entity(request_params, body_for_checks)
        explicit_gstin = self._has_explicit_gstin(request_params, body_for_checks)

        context_payload: dict[str, str] = {}

        if self.context.legal_entity_id and not explicit_legal:
            headers["X-Legal-Entity-Id"] = self.context.legal_entity_id
            context_payload["legalEntityId"] = self.context.legal_entity_id

        if self.context.gst_number and not explicit_gstin:
            headers["X-GST-Number"] = self.context.gst_number
            context_payload["sgstin"] = self.context.gst_number

        method_name = method.lower()
        if method_name in {"get", "head", "delete"}:
            request_params = {**request_params, **context_payload}
        else:
            if isinstance(request_body, dict):
                request_body = {**context_payload, **request_body}
            elif request_body is None:
                request_body = context_payload

        return headers, request_params, request_body

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        skip_context: bool = False,
    ) -> dict[str, Any]:
        normalized_path = "/" + path.lstrip("/")
        url = f"{self.context.core_api_url}{normalized_path}"

        headers, request_params, request_body = self._apply_context(
            method=method,
            params=params,
            body=body,
            skip_context=skip_context,
        )

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method.upper(),
                    url,
                    headers=headers,
                    params=request_params or None,
                    json=request_body if method.lower() not in {"get", "head"} else None,
                )
        except httpx.HTTPError as error:
            raise FlowOneApiError("FlowOne API request failed.") from error

        if response.status_code == 401:
            raise FlowOneApiError(
                "The FlowOne session is no longer authorized. Refresh the FlowOne page/login and retry.",
                status_code=401,
            )

        if response.status_code == 403:
            raise FlowOneApiError(
                "FlowOne rejected this operation for the current user/context.",
                status_code=403,
            )

        if response.status_code >= 400:
            raise FlowOneApiError(
                f"FlowOne API returned HTTP {response.status_code}.",
                status_code=response.status_code,
            )

        try:
            data = response.json()
        except ValueError as error:
            raise FlowOneApiError("FlowOne API returned a non-JSON response.") from error

        if not isinstance(data, dict):
            raise FlowOneApiError("FlowOne API returned an unexpected response shape.")

        return data

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        skip_context: bool = False,
    ) -> dict[str, Any]:
        return await self.request_json(
            "GET",
            path,
            params=params,
            skip_context=skip_context,
        )

    async def post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        skip_context: bool = False,
    ) -> dict[str, Any]:
        return await self.request_json(
            "POST",
            path,
            body=body,
            skip_context=skip_context,
        )
