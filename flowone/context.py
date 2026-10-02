from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

from fastapi import Request


class FlowOneContextError(ValueError):
    """Raised when the browser did not supply a safe executable FlowOne context."""


def _clean(value: str | None) -> str:
    return str(value or "").strip()


def _resolve_core_api_url(request: Request) -> str:
    """
    Prefer a server-owned URL when configured. For the current localhost phase,
    fall back to the exact core API URL selected by the existing FlowOne UI.

    Production should configure FLOWONE_CORE_API_URL (or a tenant/environment
    resolver) so the runtime does not trust a browser-selected upstream host.
    """

    configured = _clean(os.getenv("FLOWONE_CORE_API_URL"))
    candidate = configured or _clean(request.headers.get("X-FlowOne-Core-Api-Url"))

    if not candidate:
        raise FlowOneContextError(
            "FlowOne core API URL is missing. Configure FLOWONE_CORE_API_URL "
            "or use the updated core-ui auth bridge."
        )

    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise FlowOneContextError("FlowOne core API URL is invalid.")
    if parsed.username or parsed.password:
        raise FlowOneContextError("FlowOne core API URL must not contain credentials.")

    return candidate.rstrip("/")


@dataclass(frozen=True)
class FlowOneRequestContext:
    """Execution-only context. Never add this object to an LLM prompt/state."""

    authorization: str = field(repr=False)
    core_api_url: str
    company_id: str
    actor_key: str | None = field(default=None, repr=False)
    user_pool_id: str | None = None
    legal_entity_id: str | None = None
    gst_number: str | None = None
    portfolio_id: str | None = None
    user_scope_access: str = ""

    @classmethod
    def from_request(cls, request: Request) -> "FlowOneRequestContext":
        authorization = _clean(request.headers.get("Authorization"))
        if not authorization.lower().startswith("bearer "):
            raise FlowOneContextError(
                "The FlowOne login token was not forwarded to the local agent runtime."
            )

        bearer_value = authorization[7:].strip()
        if not bearer_value:
            raise FlowOneContextError("The FlowOne login token is empty.")

        company_id = _clean(request.headers.get("X-FlowOne-Company-Id"))
        if not company_id:
            raise FlowOneContextError(
                "The current FlowOne company context was not forwarded to the agent runtime."
            )

        return cls(
            authorization=f"Bearer {bearer_value}",
            core_api_url=_resolve_core_api_url(request),
            company_id=company_id,
            actor_key=_clean(request.headers.get("X-FlowOne-Actor-Key")) or None,
            user_pool_id=_clean(request.headers.get("X-ALGO-UP-ID")) or None,
            legal_entity_id=_clean(request.headers.get("X-Legal-Entity-Id")) or None,
            gst_number=_clean(request.headers.get("X-GST-Number")) or None,
            portfolio_id=_clean(request.headers.get("X-FlowOne-Portfolio-Id")) or None,
            user_scope_access=_clean(request.headers.get("X-FlowOne-User-Scope")),
        )


    def persistence_scope_id(self) -> str:
        """Stable, non-secret isolation key for backend state.

        The raw actor key (currently derived from the signed-in FlowOne user +
        legal entity by core-ui) is never written to the database. Only this
        SHA-256 digest is persisted.
        """

        actor = self.actor_key or self.user_pool_id or "flowone-user"
        raw = "|".join(
            [
                actor,
                self.company_id,
                self.legal_entity_id or "",
                self.portfolio_id or "",
                self.user_scope_access or "",
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def upstream_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": self.authorization,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.user_pool_id:
            headers["X-ALGO-UP-ID"] = self.user_pool_id
        return headers

    def legal_context_payload(self) -> dict[str, str]:
        payload: dict[str, str] = {}
        if self.legal_entity_id:
            payload["legalEntityId"] = self.legal_entity_id
        if self.gst_number:
            payload["sgstin"] = self.gst_number
        return payload
