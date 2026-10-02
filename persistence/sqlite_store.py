from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Iterator, Mapping

from .repositories import (
    ApprovalRepository,
    CaseRepository,
    JourneyRepository,
    WorkerRunRepository,
)


def _json_dump(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), separators=(",", ":"), ensure_ascii=False, default=str)


def _json_load(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else {}


class _SQLiteBase:
    def __init__(self, db_path: str) -> None:
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class SQLiteWorkerRunRepository(_SQLiteBase, WorkerRunRepository):
    def save(self, scope_id: str, run: Mapping[str, Any]) -> None:
        run_id = str(run.get("runId") or "").strip()
        worker_id = str(run.get("workerId") or "").strip()
        if not run_id or not worker_id:
            raise ValueError("Worker run requires runId and workerId.")
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO worker_runs(
                    scope_id, run_id, worker_id, status, trigger_type,
                    started_at, completed_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_id, run_id) DO UPDATE SET
                    worker_id=excluded.worker_id,
                    status=excluded.status,
                    trigger_type=excluded.trigger_type,
                    started_at=excluded.started_at,
                    completed_at=excluded.completed_at,
                    payload_json=excluded.payload_json
                """,
                (
                    scope_id,
                    run_id,
                    worker_id,
                    str(run.get("status") or "unknown"),
                    str(run.get("triggerType") or "on_demand"),
                    str(run.get("startedAt") or ""),
                    str(run.get("completedAt") or ""),
                    _json_dump(run),
                ),
            )

    def get(self, scope_id: str, run_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT payload_json FROM worker_runs WHERE scope_id=? AND run_id=?",
                (scope_id, run_id),
            ).fetchone()
        return _json_load(row["payload_json"]) if row else None

    def latest(self, scope_id: str, worker_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM worker_runs
                WHERE scope_id=? AND worker_id=? AND status='completed'
                ORDER BY completed_at DESC, rowid DESC LIMIT 1
                """,
                (scope_id, worker_id),
            ).fetchone()
        return _json_load(row["payload_json"]) if row else None

    def list(self, scope_id: str, worker_id: str | None, limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.connection() as conn:
            if worker_id:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM worker_runs
                    WHERE scope_id=? AND worker_id=?
                    ORDER BY COALESCE(completed_at, started_at) DESC, rowid DESC LIMIT ?
                    """,
                    (scope_id, worker_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM worker_runs
                    WHERE scope_id=?
                    ORDER BY COALESCE(completed_at, started_at) DESC, rowid DESC LIMIT ?
                    """,
                    (scope_id, limit),
                ).fetchall()
        return [_json_load(row["payload_json"]) for row in rows]


class SQLiteCaseRepository(_SQLiteBase, CaseRepository):
    def _save_with_connection(
        self,
        conn: sqlite3.Connection,
        scope_id: str,
        case: Mapping[str, Any],
    ) -> None:
        case_id = str(case.get("caseId") or "").strip()
        if not case_id:
            raise ValueError("Business case requires caseId.")

        payload = deepcopy(dict(case))
        evidence_items = payload.pop("evidence", [])
        conn.execute(
            """
            INSERT INTO business_cases(
                scope_id, case_id, run_id, case_type, status,
                subject_id, subject_name, created_at, updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_id, case_id) DO UPDATE SET
                run_id=excluded.run_id,
                case_type=excluded.case_type,
                status=excluded.status,
                subject_id=excluded.subject_id,
                subject_name=excluded.subject_name,
                updated_at=excluded.updated_at,
                payload_json=excluded.payload_json
            """,
            (
                scope_id,
                case_id,
                str(case.get("createdFromRunId") or ""),
                str(case.get("caseType") or ""),
                str(case.get("caseStatus") or case.get("status") or "open"),
                str((case.get("subject") or {}).get("id") or case.get("customerId") or ""),
                str((case.get("subject") or {}).get("name") or case.get("customerName") or ""),
                str(case.get("createdAt") or ""),
                str(case.get("enrichedAt") or case.get("updatedAt") or case.get("createdAt") or ""),
                _json_dump(payload),
            ),
        )

        if isinstance(evidence_items, list):
            for evidence in evidence_items:
                if not isinstance(evidence, dict):
                    continue
                evidence_id = str(evidence.get("evidenceId") or "").strip()
                if not evidence_id:
                    continue
                conn.execute(
                    """
                    INSERT INTO evidence(
                        scope_id, evidence_id, case_id, record_type, status,
                        observed_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope_id, evidence_id) DO UPDATE SET
                        case_id=excluded.case_id,
                        record_type=excluded.record_type,
                        status=excluded.status,
                        observed_at=excluded.observed_at,
                        payload_json=excluded.payload_json
                    """,
                    (
                        scope_id,
                        evidence_id,
                        case_id,
                        str(evidence.get("recordType") or ""),
                        str(evidence.get("status") or ""),
                        str(evidence.get("observedAt") or ""),
                        _json_dump(evidence),
                    ),
                )

    def save(self, scope_id: str, case: Mapping[str, Any]) -> None:
        with self.connection() as conn:
            self._save_with_connection(conn, scope_id, case)

    def _inflate(self, conn: sqlite3.Connection, scope_id: str, row: sqlite3.Row) -> dict[str, Any]:
        payload = _json_load(row["payload_json"])
        evidence_rows = conn.execute(
            """
            SELECT payload_json FROM evidence
            WHERE scope_id=? AND case_id=?
            ORDER BY observed_at ASC, rowid ASC
            """,
            (scope_id, row["case_id"]),
        ).fetchall()
        payload["evidence"] = [_json_load(item["payload_json"]) for item in evidence_rows]
        return payload

    def get(self, scope_id: str, case_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT case_id, payload_json FROM business_cases WHERE scope_id=? AND case_id=?",
                (scope_id, case_id),
            ).fetchone()
            return self._inflate(conn, scope_id, row) if row else None

    def list(self, scope_id: str, status: str | None, limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.connection() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT case_id, payload_json FROM business_cases
                    WHERE scope_id=? AND status=?
                    ORDER BY updated_at DESC, rowid DESC LIMIT ?
                    """,
                    (scope_id, status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT case_id, payload_json FROM business_cases
                    WHERE scope_id=?
                    ORDER BY updated_at DESC, rowid DESC LIMIT ?
                    """,
                    (scope_id, limit),
                ).fetchall()
            return [self._inflate(conn, scope_id, row) for row in rows]


class SQLiteJourneyRepository(_SQLiteBase, JourneyRepository):
    def save(self, scope_id: str, journey: Mapping[str, Any]) -> None:
        journey_id = str(journey.get("id") or journey.get("journeyId") or "").strip()
        if not journey_id:
            raise ValueError("Journey requires id.")
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO journeys(
                    scope_id, journey_id, journey_type, status, subject_id,
                    subject_name, created_at, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_id, journey_id) DO UPDATE SET
                    journey_type=excluded.journey_type,
                    status=excluded.status,
                    subject_id=excluded.subject_id,
                    subject_name=excluded.subject_name,
                    updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    scope_id,
                    journey_id,
                    str(journey.get("type") or journey.get("journeyType") or ""),
                    str(journey.get("status") or ""),
                    str(journey.get("customerId") or (journey.get("subject") or {}).get("id") or ""),
                    str(journey.get("customerName") or (journey.get("subject") or {}).get("name") or ""),
                    str(journey.get("createdAt") or ""),
                    str(journey.get("updatedAt") or journey.get("createdAt") or ""),
                    _json_dump(journey),
                ),
            )

    def get(self, scope_id: str, journey_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT payload_json FROM journeys WHERE scope_id=? AND journey_id=?",
                (scope_id, journey_id),
            ).fetchone()
        return _json_load(row["payload_json"]) if row else None

    def latest_active(self, scope_id: str, journey_type: str | None = None) -> dict[str, Any] | None:
        params: list[Any] = [scope_id]
        type_clause = ""
        if journey_type:
            type_clause = " AND journey_type=?"
            params.append(journey_type)
        with self.connection() as conn:
            row = conn.execute(
                f"""
                SELECT payload_json FROM journeys
                WHERE scope_id=? AND status NOT IN ('cancelled', 'rejected'){type_clause}
                ORDER BY updated_at DESC, rowid DESC LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        return _json_load(row["payload_json"]) if row else None

    def list(self, scope_id: str, limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM journeys
                WHERE scope_id=? ORDER BY updated_at DESC, rowid DESC LIMIT ?
                """,
                (scope_id, limit),
            ).fetchall()
        return [_json_load(row["payload_json"]) for row in rows]

    def cancel(self, scope_id: str, journey_id: str, updated_at: str) -> dict[str, Any] | None:
        journey = self.get(scope_id, journey_id)
        if not journey:
            return None
        journey["status"] = "cancelled"
        journey["updatedAt"] = updated_at
        self.save(scope_id, journey)
        return journey


class SQLiteApprovalRepository(_SQLiteBase, ApprovalRepository):
    def save(self, scope_id: str, approval: Mapping[str, Any]) -> None:
        journey_id = str(approval.get("journeyId") or "").strip()
        if not journey_id:
            raise ValueError("Approval requires journeyId.")
        approval_id = str(approval.get("approvalId") or f"{journey_id}:collection_followup").strip()
        payload = dict(approval)
        payload["approvalId"] = approval_id
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO approvals(
                    scope_id, approval_id, journey_id, action_key, status,
                    requested_at, updated_at, decided_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_id, approval_id) DO UPDATE SET
                    journey_id=excluded.journey_id,
                    action_key=excluded.action_key,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    decided_at=excluded.decided_at,
                    payload_json=excluded.payload_json
                """,
                (
                    scope_id,
                    approval_id,
                    journey_id,
                    str(approval.get("actionKey") or "collection_followup"),
                    str(approval.get("status") or "pending"),
                    str(approval.get("requestedAt") or ""),
                    str(approval.get("updatedAt") or approval.get("requestedAt") or ""),
                    approval.get("decidedAt"),
                    _json_dump(payload),
                ),
            )

    def get(self, scope_id: str, approval_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT payload_json FROM approvals WHERE scope_id=? AND approval_id=?",
                (scope_id, approval_id),
            ).fetchone()
        return _json_load(row["payload_json"]) if row else None

    def get_for_journey(self, scope_id: str, journey_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM approvals
                WHERE scope_id=? AND journey_id=? ORDER BY rowid DESC LIMIT 1
                """,
                (scope_id, journey_id),
            ).fetchone()
        return _json_load(row["payload_json"]) if row else None

    def decide(
        self,
        scope_id: str,
        journey_id: str,
        status: str,
        decided_at: str,
        decision_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        approval = self.get_for_journey(scope_id, journey_id)
        if not approval:
            return None
        approval["status"] = status
        approval["decidedAt"] = decided_at
        approval["updatedAt"] = decided_at
        if decision_payload:
            approval["decision"] = dict(decision_payload)
        self.save(scope_id, approval)
        return approval

    def list(self, scope_id: str, status: str | None, limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.connection() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM approvals
                    WHERE scope_id=? AND status=?
                    ORDER BY updated_at DESC, rowid DESC LIMIT ?
                    """,
                    (scope_id, status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM approvals
                    WHERE scope_id=?
                    ORDER BY updated_at DESC, rowid DESC LIMIT ?
                    """,
                    (scope_id, limit),
                ).fetchall()
        return [_json_load(row["payload_json"]) for row in rows]


class SQLiteStateStore(_SQLiteBase):
    """Local durable implementation behind repository interfaces.

    Production can replace these repositories without changing worker logic or
    UI contracts. SQLite is intentionally used only as the localhost durable
    store for this phase.
    """

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self._initialize_schema()
        self.worker_runs = SQLiteWorkerRunRepository(self.db_path)
        self.cases = SQLiteCaseRepository(self.db_path)
        self.journeys = SQLiteJourneyRepository(self.db_path)
        self.approvals = SQLiteApprovalRepository(self.db_path)

    def _initialize_schema(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS worker_runs(
                    scope_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    trigger_type TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(scope_id, run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_worker_runs_scope_worker_time
                    ON worker_runs(scope_id, worker_id, completed_at);

                CREATE TABLE IF NOT EXISTS business_cases(
                    scope_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    run_id TEXT,
                    case_type TEXT,
                    status TEXT,
                    subject_id TEXT,
                    subject_name TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(scope_id, case_id)
                );
                CREATE INDEX IF NOT EXISTS idx_cases_scope_status_time
                    ON business_cases(scope_id, status, updated_at);

                CREATE TABLE IF NOT EXISTS evidence(
                    scope_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    record_type TEXT,
                    status TEXT,
                    observed_at TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(scope_id, evidence_id)
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_scope_case
                    ON evidence(scope_id, case_id, observed_at);

                CREATE TABLE IF NOT EXISTS journeys(
                    scope_id TEXT NOT NULL,
                    journey_id TEXT NOT NULL,
                    journey_type TEXT,
                    status TEXT,
                    subject_id TEXT,
                    subject_name TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(scope_id, journey_id)
                );
                CREATE INDEX IF NOT EXISTS idx_journeys_scope_time
                    ON journeys(scope_id, updated_at);

                CREATE TABLE IF NOT EXISTS approvals(
                    scope_id TEXT NOT NULL,
                    approval_id TEXT NOT NULL,
                    journey_id TEXT NOT NULL,
                    action_key TEXT,
                    status TEXT,
                    requested_at TEXT,
                    updated_at TEXT,
                    decided_at TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(scope_id, approval_id)
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_scope_status_time
                    ON approvals(scope_id, status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_approvals_scope_journey
                    ON approvals(scope_id, journey_id);
                """
            )

    def persist_worker_result(self, scope_id: str, run: Mapping[str, Any]) -> None:
        self.worker_runs.save(scope_id, run)
        for case in run.get("cases", []) if isinstance(run.get("cases"), list) else []:
            if isinstance(case, dict) and case.get("caseId"):
                self.cases.save(scope_id, case)
