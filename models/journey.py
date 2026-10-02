from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class JourneyRecord:
    """Canonical persisted journey envelope.

    The payload intentionally remains flexible so the current collection
    follow-up journey can migrate without forcing future workers into that
    exact schema.
    """

    journey_id: str
    journey_type: str
    title: str
    status: str
    created_at: str
    updated_at: str
    subject: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)
    case_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "journeyId": self.journey_id,
            "journeyType": self.journey_type,
            "title": self.title,
            "status": self.status,
            "caseId": self.case_id,
            "subject": dict(self.subject),
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "payload": dict(self.payload),
        }
