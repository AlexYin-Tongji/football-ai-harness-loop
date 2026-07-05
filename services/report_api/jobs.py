from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from services.report_api.domain import ConsumerReportRequest
from services.report_api.phase_registry import phase_label
from services.report_api.prediction import multiclass_brier, multiclass_log_loss


class JobEvent(BaseModel):
    id: int
    job_id: str
    status: str
    phase: str
    label: str
    progress: int = Field(ge=0, le=100)
    detail: str = ""
    payload: dict | None = None
    created_at: datetime


class WorkflowCheckpoint(BaseModel):
    job_id: str
    name: str
    payload: dict
    created_at: datetime


class JobView(BaseModel):
    id: str
    status: str
    phase: str
    progress: int = Field(ge=0, le=100)
    result: dict | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    events: list[JobEvent] = Field(default_factory=list, max_length=120)


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
                CREATE TABLE IF NOT EXISTS research_job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    label TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    payload_json TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES research_jobs(id)
                );
                CREATE TABLE IF NOT EXISTS story_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    story_key TEXT NOT NULL,
                    report_type TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    source_count INTEGER NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(story_key, report_type)
                );
                CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, name),
                    FOREIGN KEY(job_id) REFERENCES research_jobs(id)
                );
                """
            )

    def _recover_interrupted(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM research_jobs
                WHERE status='running'
                   OR (status='queued' AND phase IN ('queued', 'waiting_for_capacity'))
                """
            ).fetchall()
            now = datetime.now(UTC).isoformat()
            for row in rows:
                job_id = row["id"]
                connection.execute(
                    """
                    UPDATE research_jobs
                    SET status='queued', phase='waiting_for_resume', progress=3,
                        error=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (now, job_id),
                )
                self._event(
                    connection,
                    job_id,
                    status="queued",
                    phase="waiting_for_resume",
                    progress=3,
                    detail="服务重启后任务已放回恢复队列。",
                )

    def request_for_job(self, job_id: str) -> ConsumerReportRequest:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_json FROM research_jobs WHERE id=?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return ConsumerReportRequest.model_validate_json(row["request_json"])

    def list_resumable(self, limit: int = 20) -> list[JobView]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM research_jobs
                WHERE status='queued' AND phase='waiting_for_resume'
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (max(1, min(limit, 50)),),
            ).fetchall()
        return [self.get(row["id"]) for row in rows]

    def save_checkpoint(self, job_id: str, name: str, payload: dict) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workflow_checkpoints
                (job_id, name, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id, name) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    created_at=excluded.created_at
                """,
                (job_id, name, json.dumps(payload, ensure_ascii=False), now),
            )
            self._event(
                connection,
                job_id,
                status="running",
                phase=name,
                progress=0,
                detail=f"写入工作流检查点：{name}",
            )

    def latest_checkpoint(
        self, job_id: str, names: set[str] | None = None
    ) -> WorkflowCheckpoint | None:
        query = """
            SELECT * FROM workflow_checkpoints
            WHERE job_id=?
        """
        params: list[object] = [job_id]
        if names:
            placeholders = ",".join("?" for _ in names)
            query += f" AND name IN ({placeholders})"
            params.extend(sorted(names))
        query += " ORDER BY created_at DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        if row is None:
            return None
        return WorkflowCheckpoint(
            job_id=row["job_id"],
            name=row["name"],
            payload=json.loads(row["payload_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
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
            self._event(
                connection,
                job_id,
                status="queued",
                phase="queued",
                progress=0,
                detail="任务已进入本地持久化队列。",
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
        detail: str = "",
        payload: dict | None = None,
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
            self._event(
                connection,
                job_id,
                status=status,
                phase=phase,
                progress=progress,
                detail=detail or error or "",
                payload=payload,
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
            events=self.list_events(row["id"]),
        )

    def list_events(self, job_id: str, limit: int = 120) -> list[JobEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM research_job_events
                WHERE job_id=?
                ORDER BY id ASC
                LIMIT ?
                """,
                (job_id, max(1, min(limit, 200))),
            ).fetchall()
        return [
            JobEvent(
                id=row["id"],
                job_id=row["job_id"],
                status=row["status"],
                phase=row["phase"],
                label=row["label"],
                progress=row["progress"],
                detail=row["detail"],
                payload=json.loads(row["payload_json"])
                if row["payload_json"]
                else None,
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

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

    def recent_story_memory(self, report_type: str, limit: int = 12) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT title, summary, category, source_count, last_seen_at
                FROM story_memory
                WHERE report_type=?
                ORDER BY last_seen_at DESC
                LIMIT ?
                """,
                (report_type, max(1, min(limit, 30))),
            ).fetchall()
        notes = []
        for row in rows:
            notes.append(
                "昨日对比记忆："
                f"[{row['category']}] {row['title']}；{row['summary']}；"
                f"来源数 {row['source_count']}；上次出现 {row['last_seen_at']}。"
            )
        return notes

    def save_story_memory(self, report_type: str, result: dict) -> None:
        evidence = result.get("evidence") or []
        if not isinstance(evidence, list):
            return
        evidence_by_id = {
            str(item.get("id")): item for item in evidence if isinstance(item, dict)
        }
        report = ((result.get("report") or {}).get("report") or {})
        sections = report.get("sections") or []
        now = datetime.now(UTC).isoformat()
        rows: list[tuple[str, str, str, str, str, int, str, str]] = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            evidence_ids = [
                str(item)
                for item in section.get("evidence_ids", [])
                if str(item) in evidence_by_id
            ]
            if not evidence_ids:
                continue
            cluster_ids = [
                str(evidence_by_id[item].get("story_cluster_id") or "")
                for item in evidence_ids
            ]
            stable_cluster = next((item for item in cluster_ids if item), "")
            seed = stable_cluster or "|".join(sorted(evidence_ids))
            story_key = hashlib.sha256(seed.encode()).hexdigest()[:16]
            category = str(section.get("category") or "context")
            title = str(section.get("heading") or "未命名事件")[:200]
            summary = str(section.get("body") or "")[:500]
            rows.append(
                (
                    story_key,
                    report_type,
                    category,
                    title,
                    summary,
                    len(
                        {
                            evidence_by_id[item].get("source_name")
                            for item in evidence_ids
                        }
                    ),
                    json.dumps(evidence_ids, ensure_ascii=False),
                    now,
                )
            )
        if not rows:
            return
        with self._lock, self._connect() as connection:
            for row in rows[:20]:
                connection.execute(
                    """
                    INSERT INTO story_memory
                    (story_key, report_type, category, title, summary, source_count,
                     evidence_ids_json, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(story_key, report_type) DO UPDATE SET
                        category=excluded.category,
                        title=excluded.title,
                        summary=excluded.summary,
                        source_count=excluded.source_count,
                        evidence_ids_json=excluded.evidence_ids_json,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (*row, row[-1]),
                )

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

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        job_id: str,
        *,
        status: str,
        phase: str,
        progress: int,
        detail: str = "",
        payload: dict | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO research_job_events
            (job_id, status, phase, label, progress, detail, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                status,
                phase,
                phase_label(phase),
                max(0, min(100, progress)),
                detail,
                json.dumps(payload, ensure_ascii=False) if payload else None,
                datetime.now(UTC).isoformat(),
            ),
        )
