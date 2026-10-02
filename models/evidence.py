from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class EvidenceItem:
    """One auditable observation used by a FlowOne business case."""

    evidence_id: str
    source_system: str
    source_type: str
    tool_id: str
    record_type: str
    status: str
    scope: str
    observed_at: str
    summary: str
    record_ids: tuple[str, ...] = ()
    facts: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidenceId": self.evidence_id,
            "sourceSystem": self.source_system,
            "sourceType": self.source_type,
            "toolId": self.tool_id,
            "recordType": self.record_type,
            "status": self.status,
            "scope": self.scope,
            "observedAt": self.observed_at,
            "summary": self.summary,
            "recordIds": list(self.record_ids),
            "facts": dict(self.facts),
        }


def collect_record_ids(
    records: Sequence[Mapping[str, Any]],
    candidate_keys: Sequence[str],
    *,
    limit: int = 20,
) -> tuple[str, ...]:
    values: list[str] = []
    for record in records:
        found = ""
        for key in candidate_keys:
            value = str(record.get(key) or "").strip()
            if value:
                found = value
                break
        if found and found not in values:
            values.append(found)
        if len(values) >= limit:
            break
    return tuple(values)
