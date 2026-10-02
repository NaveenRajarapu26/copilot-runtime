from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .evidence import EvidenceItem


@dataclass(frozen=True)
class CaseRecommendation:
    action_key: str
    label: str
    description: str
    external_impact: bool
    requires_human_approval: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "actionKey": self.action_key,
            "label": self.label,
            "description": self.description,
            "externalImpact": self.external_impact,
            "requiresHumanApproval": self.requires_human_approval,
        }


@dataclass(frozen=True)
class BusinessCase:
    """Canonical shared case contract for current and future FlowOne workers."""

    case_id: str
    case_type: str
    status: str
    created_by_worker: str
    created_from_run_id: str
    created_at: str
    subject: Mapping[str, Any]
    summary: str
    reasons: Sequence[str]
    recommendation: CaseRecommendation
    evidence: Sequence[EvidenceItem]
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "caseId": self.case_id,
            "caseType": self.case_type,
            "caseStatus": self.status,
            "createdByWorker": self.created_by_worker,
            "createdFromRunId": self.created_from_run_id,
            "createdAt": self.created_at,
            "subject": dict(self.subject),
            "caseSummary": self.summary,
            "caseReasons": list(self.reasons),
            "recommendation": self.recommendation.to_dict(),
            "evidence": [item.to_dict() for item in self.evidence],
            "tags": list(self.tags),
            "caseMetadata": dict(self.metadata),
        }
