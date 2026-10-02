from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping


class WorkerRunRepository(ABC):
    @abstractmethod
    def save(self, scope_id: str, run: Mapping[str, Any]) -> None: ...

    @abstractmethod
    def get(self, scope_id: str, run_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def latest(self, scope_id: str, worker_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def list(self, scope_id: str, worker_id: str | None, limit: int) -> list[dict[str, Any]]: ...


class CaseRepository(ABC):
    @abstractmethod
    def save(self, scope_id: str, case: Mapping[str, Any]) -> None: ...

    @abstractmethod
    def get(self, scope_id: str, case_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def list(self, scope_id: str, status: str | None, limit: int) -> list[dict[str, Any]]: ...


class JourneyRepository(ABC):
    @abstractmethod
    def save(self, scope_id: str, journey: Mapping[str, Any]) -> None: ...

    @abstractmethod
    def get(self, scope_id: str, journey_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def latest_active(self, scope_id: str, journey_type: str | None = None) -> dict[str, Any] | None: ...

    @abstractmethod
    def list(self, scope_id: str, limit: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    def cancel(self, scope_id: str, journey_id: str, updated_at: str) -> dict[str, Any] | None: ...


class ApprovalRepository(ABC):
    @abstractmethod
    def save(self, scope_id: str, approval: Mapping[str, Any]) -> None: ...

    @abstractmethod
    def get(self, scope_id: str, approval_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def get_for_journey(self, scope_id: str, journey_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def decide(
        self,
        scope_id: str,
        journey_id: str,
        status: str,
        decided_at: str,
        decision_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    def list(self, scope_id: str, status: str | None, limit: int) -> list[dict[str, Any]]: ...


class WorkerControlRepository(ABC):
    @abstractmethod
    def save(self, scope_id: str, control: Mapping[str, Any]) -> None: ...

    @abstractmethod
    def get(self, scope_id: str, worker_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def list(self, scope_id: str) -> list[dict[str, Any]]: ...


class WorkerScheduleRepository(ABC):
    @abstractmethod
    def save(self, scope_id: str, schedule: Mapping[str, Any]) -> None: ...

    @abstractmethod
    def get(self, scope_id: str, worker_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def list(self, scope_id: str) -> list[dict[str, Any]]: ...
