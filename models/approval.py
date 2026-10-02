from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ApprovalRecord:
    """Auditable human/policy approval request for an agent-prepared action."""

    approval_id: str
    journey_id: str
    action_key: str
    status: str
    requested_at: str
    updated_at: str
    subject: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)
    decided_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "approvalId": self.approval_id,
            "journeyId": self.journey_id,
            "actionKey": self.action_key,
            "status": self.status,
            "subject": dict(self.subject),
            "requestedAt": self.requested_at,
            "updatedAt": self.updated_at,
            "decidedAt": self.decided_at,
            "payload": dict(self.payload),
        }
