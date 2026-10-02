from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkerControl:
    worker_id: str
    status: str
    updated_at: str
    paused_at: str | None = None
    resumed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workerId": self.worker_id,
            "status": self.status,
            "updatedAt": self.updated_at,
            "pausedAt": self.paused_at,
            "resumedAt": self.resumed_at,
        }


@dataclass(frozen=True)
class WorkerSchedule:
    schedule_id: str
    worker_id: str
    enabled: bool
    cadence: str
    time_local: str
    timezone: str
    day_of_week: int | None
    next_run_at: str
    created_at: str
    updated_at: str
    last_triggered_at: str | None = None
    scheduler_mode: str = "authenticated_session"

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheduleId": self.schedule_id,
            "workerId": self.worker_id,
            "enabled": self.enabled,
            "cadence": self.cadence,
            "timeLocal": self.time_local,
            "timezone": self.timezone,
            "dayOfWeek": self.day_of_week,
            "nextRunAt": self.next_run_at,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "lastTriggeredAt": self.last_triggered_at,
            "schedulerMode": self.scheduler_mode,
        }
