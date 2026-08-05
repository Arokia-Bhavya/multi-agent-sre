"""
Persistent store for auto-triaged incidents (Milestone 6 extension).

Originally an in-memory-only `IncidentStore` living in
`ai_platform/webui/auto_responder.py`. Moved here and given SQLite-backed
durability so incident/RCA history survives process restarts and can be
queried by the copilot itself (see `get_incident_history` in
`copilot_tools.py`), not just displayed live in the web UI's Incidents
panel while the process happens to still be running.

Lives under `ai_platform/tools/` (rather than `ai_platform/webui/`) so both
`chat.py`'s CLI and `server.py`'s web backend can import it directly,
without either one importing from the other — `ai_platform/tools` is
already on `sys.path` for both entry points.

SQLite (not a server-based database) because this remains a single-process
demo platform per the rest of the codebase's own conventions (conversation
memory is also in-process) — a file the process owns is enough durability
without adding an external service to run and operate.
"""

import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


# Default location: ai_platform/webui/incidents.db, computed relative to
# this file so it resolves the same regardless of whether the process is
# launched from the repo root, ai_platform/copilot, or ai_platform/webui.
DEFAULT_DB_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "webui", "incidents.db")
)


@dataclass
class IncidentRecord:
    """
    One auto-triaged incident, keyed by the Alertmanager alert fingerprint.

    `thread_id` is a dedicated copilot conversation thread for this incident
    (separate from any browser chat session), so its investigation and any
    follow-up approval exchange don't collide with a human's own chat.
    """

    fingerprint: str
    alert_name: str
    service: Optional[str]
    severity: str
    thread_id: str
    status: str = "investigating"  # investigating -> awaiting_approval|completed|error ; or resolved
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    content: str = ""
    steps: List[Dict[str, Any]] = field(default_factory=list)
    pending_action: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "alert_name": self.alert_name,
            "service": self.service,
            "severity": self.severity,
            "thread_id": self.thread_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "content": self.content,
            "steps": self.steps,
            "pending_action": self.pending_action,
            "error": self.error,
        }

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> "IncidentRecord":
        return cls(
            fingerprint=row["fingerprint"],
            alert_name=row["alert_name"],
            service=row["service"],
            severity=row["severity"],
            thread_id=row["thread_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            content=row["content"] or "",
            steps=json.loads(row["steps"]) if row["steps"] else [],
            pending_action=json.loads(row["pending_action"]) if row["pending_action"] else None,
            error=row["error"],
        )


class IncidentStore:
    """
    Thread-safe registry of auto-triaged incidents, keyed by Alertmanager
    fingerprint, backed by SQLite for durability across process restarts.

    All reads (get / get_by_thread / list / query_history) are served from
    an in-memory dict that's kept in sync on every write. The dict is the
    source of truth while the process is running — SQLite exists purely so
    everything can be reloaded the next time the process starts, rather
    than needing every read to round-trip through the database.

    Pass `db_path=":memory:"` (the default) for a purely in-process,
    non-durable store — e.g. in tests, or anywhere persistence isn't
    wanted. Pass a real file path (see `DEFAULT_DB_PATH`) for durability.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                fingerprint TEXT PRIMARY KEY,
                alert_name TEXT NOT NULL,
                service TEXT,
                severity TEXT,
                thread_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                content TEXT,
                steps TEXT,
                pending_action TEXT,
                error TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remediation_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_name TEXT NOT NULL,
                desired_replicas INTEGER NOT NULL,
                reason TEXT,
                status TEXT NOT NULL,
                detail TEXT,
                timestamp TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

        self._by_fingerprint: Dict[str, IncidentRecord] = {}
        for row in self._conn.execute("SELECT * FROM incidents"):
            record = IncidentRecord._from_row(row)
            self._by_fingerprint[record.fingerprint] = record

    def _persist(self, record: IncidentRecord) -> None:
        """Write-through: upsert the current in-memory record into SQLite."""
        self._conn.execute(
            """
            INSERT INTO incidents (
                fingerprint, alert_name, service, severity, thread_id, status,
                created_at, updated_at, content, steps, pending_action, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                alert_name=excluded.alert_name,
                service=excluded.service,
                severity=excluded.severity,
                thread_id=excluded.thread_id,
                status=excluded.status,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at,
                content=excluded.content,
                steps=excluded.steps,
                pending_action=excluded.pending_action,
                error=excluded.error
            """,
            (
                record.fingerprint,
                record.alert_name,
                record.service,
                record.severity,
                record.thread_id,
                record.status,
                record.created_at,
                record.updated_at,
                record.content,
                json.dumps(record.steps),
                json.dumps(record.pending_action) if record.pending_action is not None else None,
                record.error,
            ),
        )
        self._conn.commit()

    def get(self, fingerprint: str) -> Optional[IncidentRecord]:
        with self._lock:
            return self._by_fingerprint.get(fingerprint)

    def get_by_thread(self, thread_id: str) -> Optional[IncidentRecord]:
        with self._lock:
            for record in self._by_fingerprint.values():
                if record.thread_id == thread_id:
                    return record
        return None

    def create(self, fingerprint: str, alert_name: str, service: Optional[str], severity: str) -> IncidentRecord:
        with self._lock:
            record = IncidentRecord(
                fingerprint=fingerprint,
                alert_name=alert_name,
                service=service,
                severity=severity,
                thread_id=f"incident-{uuid.uuid4()}",
            )
            self._by_fingerprint[fingerprint] = record
            self._persist(record)
            return record

    def update(self, fingerprint: str, **fields: Any) -> Optional[IncidentRecord]:
        with self._lock:
            record = self._by_fingerprint.get(fingerprint)
            if record is None:
                return None
            for key, value in fields.items():
                setattr(record, key, value)
            record.updated_at = datetime.now(timezone.utc).isoformat()
            self._persist(record)
            return record

    def list(self) -> List[IncidentRecord]:
        with self._lock:
            records = list(self._by_fingerprint.values())
        return sorted(records, key=lambda r: r.created_at, reverse=True)

    def query_history(
        self, service: str = "", since_hours: float = 168, limit: int = 20
    ) -> List[IncidentRecord]:
        """
        Past incidents matching a service (substring match, case-insensitive;
        empty string matches all) within the last `since_hours` hours
        (default 168 = 7 days), newest first, capped at `limit`.

        Backs the `get_incident_history` copilot tool — this is what makes
        "what's happened with checkout this week" answerable instead of
        only ever-current-state questions.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

        def _created_at(record: IncidentRecord) -> datetime:
            try:
                dt = datetime.fromisoformat(record.created_at)
            except ValueError:
                return datetime.min.replace(tzinfo=timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        with self._lock:
            records = list(self._by_fingerprint.values())

        records = [r for r in records if _created_at(r) >= cutoff]
        if service:
            records = [r for r in records if r.service and service.lower() in r.service.lower()]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    def log_remediation(
        self,
        service_name: str,
        desired_replicas: int,
        reason: str,
        status: str,
        detail: Optional[str] = None,
    ) -> None:
        """
        Append an entry to the remediation audit trail.

        Called from every exit path of the `remediate_scale_deployment`
        copilot tool (`copilot_tools.py`) — `status` is one of "blocked"
        (rejected by a guardrail before a human ever saw it), "rejected"
        (a human declined it), or "approved" (a human approved it and it
        was applied). This is a straight append — the audit trail is never
        edited or deleted, so it stays a trustworthy record of every
        remediation attempt regardless of outcome.
        """
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO remediation_audit
                    (service_name, desired_replicas, reason, status, detail, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    service_name,
                    desired_replicas,
                    reason,
                    status,
                    detail,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._conn.commit()

    def query_remediation_audit(self, service_name: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        """
        Past remediation attempts (blocked, rejected, or approved), newest
        first, optionally filtered to a service (substring match,
        case-insensitive).
        """
        fetch_limit = limit * 10 if service_name else limit  # overfetch to allow for filtering below
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM remediation_audit ORDER BY id DESC LIMIT ?",
                (fetch_limit,),
            ).fetchall()

        results = [dict(row) for row in rows]
        if service_name:
            results = [r for r in results if service_name.lower() in r["service_name"].lower()]
        return results[:limit]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
