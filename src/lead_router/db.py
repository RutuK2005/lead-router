"""SQLite storage for leads and the delivery outbox (stdlib `sqlite3`, no ORM).

The key idea is the *transactional outbox*: a lead and the outbox rows describing where it
must be delivered are written in ONE transaction. Either both exist or neither does, so a
crash can never leave a saved lead that nobody will deliver.
"""

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from lead_router.models import Classification, Lead, LeadStatus, OutboxItem, OutboxStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    name TEXT, phone TEXT, email TEXT, city TEXT, interest TEXT, message TEXT,
    utm_source TEXT, utm_medium TEXT, utm_campaign TEXT, utm_content TEXT,
    raw_payload TEXT NOT NULL,
    issues TEXT NOT NULL,
    classification TEXT,
    intent TEXT,
    scored_by TEXT,
    status TEXT NOT NULL,
    duplicate_of TEXT,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone, received_at);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email, received_at);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id TEXT NOT NULL REFERENCES leads(id),
    destination TEXT NOT NULL,
    url TEXT NOT NULL,
    payload TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(status, next_attempt_at);
"""

LEAD_COLUMNS = (
    "id", "source", "name", "phone", "email", "city", "interest", "message",
    "utm_source", "utm_medium", "utm_campaign", "utm_content",
)


def _ts(value: datetime) -> str:
    # Fixed-width ISO strings (always with microseconds, always UTC) sort correctly as text,
    # which lets SQLite compare timestamps with plain < and >.
    return value.isoformat(timespec="microseconds")


class Database:
    """Thin repository over a SQLite file. Each method opens its own short connection."""

    def __init__(self, path: str):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside a transaction: commit on success, roll back on error."""
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:  # the connection context manager commits or rolls back
                yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        """Create tables and indexes if they don't exist yet."""
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # ---- leads ---------------------------------------------------------------------------

    def insert_lead(self, lead: Lead, destinations: Mapping[str, str], now: datetime) -> None:
        """Insert a lead plus one outbox row per destination, atomically."""
        values = [getattr(lead, column) for column in LEAD_COLUMNS]
        values += [
            json.dumps(lead.raw_payload),
            json.dumps(lead.issues),
            lead.classification.model_dump_json() if lead.classification else None,
            lead.classification.intent.value if lead.classification else None,
            lead.scored_by,
            lead.status.value,
            lead.duplicate_of,
            _ts(lead.received_at),
        ]
        payload = json.dumps(lead.outbound_payload())
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO leads ({', '.join(LEAD_COLUMNS)}, raw_payload, issues, "
                "classification, intent, scored_by, status, duplicate_of, received_at) "
                f"VALUES ({', '.join('?' * (len(LEAD_COLUMNS) + 8))})",
                values,
            )
            for name, url in destinations.items():
                conn.execute(
                    "INSERT INTO outbox (lead_id, destination, url, payload, idempotency_key, "
                    "status, next_attempt_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (lead.id, name, url, payload, f"{lead.id}:{name}",
                     OutboxStatus.PENDING.value, _ts(now), _ts(now)),
                )

    def find_duplicate(self, phone: str | None, email: str | None, since: datetime) -> str | None:
        """Return the id of an original (non-duplicate) lead with this phone or email."""
        if not phone and not email:
            return None
        row = None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM leads WHERE status != ? AND received_at >= ? "
                "AND ((? IS NOT NULL AND phone = ?) OR (? IS NOT NULL AND email = ?)) "
                "ORDER BY received_at LIMIT 1",
                (LeadStatus.DUPLICATE.value, _ts(since), phone, phone, email, email),
            ).fetchone()
        return row["id"] if row else None

    def get_lead(self, lead_id: str) -> Lead | None:
        """Fetch one lead by id."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return _row_to_lead(row) if row else None

    def list_leads(
        self,
        status: str | None = None,
        intent: str | None = None,
        source: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Lead], int]:
        """Return a page of leads (newest first) matching the filters, plus the total count."""
        clauses, params = [], []
        for column, value in (("status", status), ("intent", intent), ("source", source)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM leads {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM leads {where} ORDER BY received_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [_row_to_lead(row) for row in rows], total

    def refresh_lead_status(self, lead_id: str) -> LeadStatus | None:
        """Derive a lead's status from its outbox rows (dead > pending > delivered)."""
        with self.connect() as conn:
            statuses = {
                row["status"]
                for row in conn.execute("SELECT status FROM outbox WHERE lead_id = ?", (lead_id,))
            }
            if not statuses:
                return None
            if OutboxStatus.DEAD in statuses:
                new_status = LeadStatus.FAILED
            elif statuses == {OutboxStatus.DELIVERED}:
                new_status = LeadStatus.DELIVERED
            else:
                new_status = LeadStatus.PENDING
            conn.execute("UPDATE leads SET status = ? WHERE id = ?", (new_status.value, lead_id))
        return new_status

    # ---- outbox --------------------------------------------------------------------------

    def due_outbox(self, now: datetime, limit: int) -> list[OutboxItem]:
        """Pending outbox rows whose next attempt time has arrived, oldest first."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE status = ? AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at LIMIT ?",
                (OutboxStatus.PENDING.value, _ts(now), limit),
            ).fetchall()
        return [OutboxItem(**dict(row)) for row in rows]

    def outbox_for_lead(self, lead_id: str) -> list[OutboxItem]:
        """All outbox rows for a lead, in creation order."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE lead_id = ? ORDER BY id", (lead_id,)
            ).fetchall()
        return [OutboxItem(**dict(row)) for row in rows]

    def update_outbox(
        self,
        item_id: int,
        status: OutboxStatus,
        attempts: int,
        next_attempt_at: datetime,
        error: str | None,
        now: datetime,
    ) -> None:
        """Record the result of one delivery attempt."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE outbox SET status = ?, attempts = ?, next_attempt_at = ?, "
                "last_error = ?, updated_at = ? WHERE id = ?",
                (status.value, attempts, _ts(next_attempt_at), error, _ts(now), item_id),
            )

    def reset_outbox(self, lead_id: str, now: datetime) -> int:
        """Re-queue every outbox row of a lead for immediate delivery. Returns rows reset."""
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE outbox SET status = ?, attempts = 0, next_attempt_at = ?, "
                "last_error = NULL, updated_at = ? WHERE lead_id = ?",
                (OutboxStatus.PENDING.value, _ts(now), _ts(now), lead_id),
            )
            if cursor.rowcount:
                conn.execute(
                    "UPDATE leads SET status = ? WHERE id = ?",
                    (LeadStatus.PENDING.value, lead_id),
                )
        return cursor.rowcount

    # ---- metrics -------------------------------------------------------------------------

    def metrics(self) -> dict[str, Any]:
        """Simple counts grouped by lead status, intent, source and outbox status."""

        def grouped(conn: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
            rows = conn.execute(
                f"SELECT COALESCE({column}, 'unscored') AS k, COUNT(*) AS n "
                f"FROM {table} GROUP BY k"
            )
            return {row["k"]: row["n"] for row in rows}

        with self.connect() as conn:
            return {
                "leads_total": conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
                "leads_by_status": grouped(conn, "leads", "status"),
                "leads_by_intent": grouped(conn, "leads", "intent"),
                "leads_by_source": grouped(conn, "leads", "source"),
                "outbox_by_status": grouped(conn, "outbox", "status"),
            }


def _row_to_lead(row: sqlite3.Row) -> Lead:
    data = {column: row[column] for column in LEAD_COLUMNS}
    classification = row["classification"]
    return Lead(
        **data,
        raw_payload=json.loads(row["raw_payload"]),
        issues=json.loads(row["issues"]),
        classification=Classification.model_validate_json(classification)
        if classification
        else None,
        scored_by=row["scored_by"],
        status=LeadStatus(row["status"]),
        duplicate_of=row["duplicate_of"],
        received_at=datetime.fromisoformat(row["received_at"]),
    )
