from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import app as app_module
from persistence import SQLiteStateStore


class FlowOneFeature6Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "agent_state.db")
        self.original_store = app_module.STATE_STORE
        app_module.STATE_STORE = SQLiteStateStore(self.db_path)
        self.client = TestClient(app_module.app)
        self.headers = {
            "Authorization": "Bearer local-test-token",
            "X-FlowOne-Company-Id": "company-1",
            "X-FlowOne-Actor-Key": "user@example.com|entity-1",
            "X-Legal-Entity-Id": "entity-1",
            "X-FlowOne-Portfolio-Id": "101",
            "X-FlowOne-User-Scope": "ALL",
            "X-FlowOne-Core-Api-Url": "http://localhost:9999/core",
        }

    def tearDown(self) -> None:
        app_module.STATE_STORE = self.original_store
        self.tempdir.cleanup()

    def test_worker_control_defaults_active_and_persists_pause_resume(self) -> None:
        response = self.client.get(
            "/state/workers/daily_ar_priorities/control",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["control"]["status"], "active")

        paused = self.client.put(
            "/state/workers/daily_ar_priorities/control",
            headers=self.headers,
            json={"status": "paused"},
        )
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["control"]["status"], "paused")

        reopened = SQLiteStateStore(self.db_path)
        scope_id = app_module.FlowOneRequestContext(
            authorization="Bearer local-test-token",
            core_api_url="http://localhost:9999/core",
            company_id="company-1",
            actor_key="user@example.com|entity-1",
            legal_entity_id="entity-1",
            portfolio_id="101",
            user_scope_access="ALL",
        ).persistence_scope_id()
        self.assertEqual(
            reopened.worker_controls.get(scope_id, "daily_ar_priorities")["status"],
            "paused",
        )

        resumed = self.client.put(
            "/state/workers/daily_ar_priorities/control",
            headers=self.headers,
            json={"status": "active"},
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["control"]["status"], "active")

    def test_schedule_is_persisted_and_has_next_run(self) -> None:
        response = self.client.put(
            "/state/workers/daily_ar_priorities/schedule",
            headers=self.headers,
            json={
                "schedule": {
                    "enabled": True,
                    "cadence": "weekdays",
                    "timeLocal": "06:30",
                    "timezone": "America/Detroit",
                }
            },
        )
        self.assertEqual(response.status_code, 200)
        schedule = response.json()["schedule"]
        self.assertTrue(schedule["enabled"])
        self.assertEqual(schedule["cadence"], "weekdays")
        self.assertEqual(schedule["timeLocal"], "06:30")
        self.assertTrue(schedule["nextRunAt"].endswith("Z"))
        self.assertEqual(schedule["schedulerMode"], "authenticated_session")

        restored = self.client.get(
            "/state/workers/daily_ar_priorities/schedule",
            headers=self.headers,
        )
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["schedule"]["nextRunAt"], schedule["nextRunAt"])

    def test_due_schedule_claim_advances_next_run_and_paused_worker_does_not_claim(self) -> None:
        self.client.put(
            "/state/workers/daily_ar_priorities/schedule",
            headers=self.headers,
            json={
                "schedule": {
                    "enabled": True,
                    "cadence": "daily",
                    "timeLocal": "06:00",
                    "timezone": "UTC",
                }
            },
        )

        context = app_module.FlowOneRequestContext(
            authorization="Bearer local-test-token",
            core_api_url="http://localhost:9999/core",
            company_id="company-1",
            actor_key="user@example.com|entity-1",
            legal_entity_id="entity-1",
            portfolio_id="101",
            user_scope_access="ALL",
        )
        scope_id = context.persistence_scope_id()
        schedule = app_module.STATE_STORE.worker_schedules.get(
            scope_id,
            "daily_ar_priorities",
        )
        assert schedule is not None
        schedule["nextRunAt"] = "2020-01-01T00:00:00Z"
        app_module.STATE_STORE.worker_schedules.save(scope_id, schedule)

        claimed = self.client.post(
            "/state/workers/daily_ar_priorities/schedule/claim-due",
            headers=self.headers,
        )
        self.assertEqual(claimed.status_code, 200)
        body = claimed.json()
        self.assertTrue(body["due"])
        self.assertEqual(body["triggerType"], "schedule")
        self.assertGreater(
            app_module._parse_utc_iso(body["schedule"]["nextRunAt"]),
            datetime.now(timezone.utc),
        )

        self.client.put(
            "/state/workers/daily_ar_priorities/control",
            headers=self.headers,
            json={"status": "paused"},
        )
        schedule = app_module.STATE_STORE.worker_schedules.get(
            scope_id,
            "daily_ar_priorities",
        )
        assert schedule is not None
        schedule["nextRunAt"] = "2020-01-01T00:00:00Z"
        app_module.STATE_STORE.worker_schedules.save(scope_id, schedule)

        blocked = self.client.post(
            "/state/workers/daily_ar_priorities/schedule/claim-due",
            headers=self.headers,
        )
        self.assertEqual(blocked.status_code, 200)
        self.assertFalse(blocked.json()["due"])
        self.assertEqual(blocked.json()["reason"], "worker_paused")

    def test_weekly_next_run_respects_day_and_clock(self) -> None:
        after = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)  # Friday
        next_run = app_module.calculate_next_schedule_run(
            cadence="weekly",
            time_local="08:00",
            timezone_name="UTC",
            day_of_week=0,  # Monday
            after_utc=after,
        )
        self.assertEqual(next_run, "2026-10-05T08:00:00Z")


if __name__ == "__main__":
    unittest.main()
