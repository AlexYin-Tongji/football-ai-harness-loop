from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from services.report_api.domain import ConsumerReportRequest
from services.report_api.prediction import multiclass_brier, multiclass_log_loss


class JobView(BaseModel):
    id: str
    status: str
    phase: str
    progress: int = Field(ge=0, le=100)
    result: dict | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class PersistentJobStore:
    """Small durable execution ledger for the local beta.

    The schema intentionally stores normalized requests and generated artifacts,
    never provider credentials, full articles, or hidden reasoning.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()
        self._recover_interrupted()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS research_jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prediction_outcomes (
                    job_id TEXT PRIMARY KEY,
                    outcome TEXT NOT NULL,
                    brier_score REAL NOT NULL,
                    log_loss REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES research_jobs(id)
                );
                """
            )

    def _recover_interrupted(self) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE research_jobs
                SET status='failed', phase='interrupted', progress=100,
                    error='服务重启中断了任务，请重新生成', updated_at=?
                WHERE status IN ('queued', 'running')
                """,
                (now,),
            )

    def create(self, request: ConsumerReportRequest) -> JobView:
        now = datetime.now(UTC)
        job_id = str(uuid4())
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO research_jobs
                (id, status, phase, progress, request_json, created_at, updated_at)
                VALUES (?, 'queued', 'queued', 0, ?, ?, ?)
                """,
                (job_id, request.model_dump_json(), now.isoformat(), now.isoformat()),
            )
            self._audit(connection, "create", "research_job", job_id, "accepted")
        return self.get(job_id)

    def update(
        self,
        job_id: str,
        *,
        status: str,
        phase: str,
        progress: int,
        result: dict | None = None,
        error: str | None = None,
    ) -> JobView:
        now = datetime.now(UTC)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE research_jobs
                SET status=?, phase=?, progress=?, result_json=?, error=?, updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    phase,
                    progress,
                    json.dumps(result, ensure_ascii=False) if result else None,
                    error,
                    now.isoformat(),
                    job_id,
                ),
            )
            if status in {"completed", "failed"}:
                self._audit(connection, "finish", "research_job", job_id, status)
        return self.get(job_id)

    def get(self, job_id: str) -> JobView:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM research_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return JobView(
            id=row["id"],
            status=row["status"],
            phase=row["phase"],
            progress=row["progress"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_recent(self, limit: int = 50) -> list[JobView]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM research_jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [self.get(row["id"]) for row in rows]

    def overview(self) -> dict:
        with self._connect() as connection:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count "
                    "FROM research_jobs GROUP BY status"
                ).fetchall()
            }
            calibration = connection.execute(
                """
                SELECT COUNT(*) AS samples,
                       AVG(brier_score) AS brier_score,
                       AVG(log_loss) AS log_loss
                FROM prediction_outcomes
                """
            ).fetchone()
        return {
            "jobs": counts,
            "calibration": {
                "samples": calibration["samples"],
                "brier_score": calibration["brier_score"],
                "log_loss": calibration["log_loss"],
            },
        }

    def record_prediction_outcome(self, job_id: str, outcome: str) -> dict:
        outcome_index = {"home": 0, "draw": 1, "away": 2}.get(outcome)
        if outcome_index is None:
            raise ValueError("outcome must be home, draw or away")
        job = self.get(job_id)
        if job.status != "completed" or not job.result:
            raise ValueError("completed prediction job is required")
        prediction = job.result["report"]["report"].get("prediction")
        if not prediction:
            raise ValueError("job does not contain a prediction")
        probabilities = (
            float(prediction["home_win"]),
            float(prediction["draw"]),
            float(prediction["away_win"]),
        )
        brier = multiclass_brier(probabilities, outcome_index)
        log_loss = multiclass_log_loss(probabilities, outcome_index)
        now = datetime.now(UTC)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO prediction_outcomes
                (job_id, outcome, brier_score, log_loss, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO NOTHING
                """,
                (job_id, outcome, brier, log_loss, now.isoformat()),
            )
            if cursor.rowcount == 0:
                self._audit(
                    connection, "record_result", "prediction", job_id, "duplicate"
                )
                raise ValueError(
                    "prediction outcome already recorded; use a correction workflow"
                )
            self._audit(
                connection, "record_result", "prediction", job_id, "accepted"
            )
        return {
            "job_id": job_id,
            "outcome": outcome,
            "brier_score": round(brier, 6),
            "log_loss": round(log_loss, 6),
        }

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        action: str,
        object_type: str,
        object_id: str,
        outcome: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_logs
            (action, object_type, object_id, outcome, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (action, object_type, object_id, outcome, datetime.now(UTC).isoformat()),
        )
