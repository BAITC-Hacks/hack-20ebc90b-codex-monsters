"""Small, transactional JSON artifact store for the single-coordinator MVP.

The SQLite connection is owned by an operation, never shared between threads.
``transaction()`` groups domain validation and writes under BEGIN IMMEDIATE;
use it for approval/export and idempotent job creation. No network actions occur
inside this module. Quantities should already be serialized to decimal strings.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterator

Payload = dict[str, Any]
Mutation = Callable[[Payload], Payload]


class StorageError(Exception):
    """Base error suitable for mapping onto a domain/API error."""


class NotFoundError(StorageError):
    pass


class AlreadyExistsError(StorageError):
    pass


class VersionConflictError(StorageError):
    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(f"Expected version {expected}; current version is {actual}")


class IdempotencyConflictError(StorageError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode(payload: Payload) -> str:
    # Reject NaN/Infinity instead of persisting invalid JSON.
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, item_id)
);
CREATE TABLE IF NOT EXISTS item_versions (
    kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (kind, item_id, version)
);
CREATE TABLE IF NOT EXISTS idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, idempotency_key)
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_item_idx ON audit_events(kind, item_id, sequence);
"""


class StoreTransaction:
    """Methods are valid only while the owning Store.transaction() is open."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get_item(self, kind: str, item_id: str, version: int | None = None) -> Payload | None:
        if version is None:
            row = self.connection.execute(
                "SELECT payload FROM items WHERE kind=? AND item_id=?", (kind, item_id)
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT payload FROM item_versions WHERE kind=? AND item_id=? AND version=?",
                (kind, item_id, version),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_items(self, kind: str) -> list[Payload]:
        rows = self.connection.execute(
            "SELECT payload FROM items WHERE kind=? ORDER BY item_id", (kind,)
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def create_item(self, kind: str, item_id: str, payload: Payload) -> Payload:
        if not kind or not item_id:
            raise ValueError("kind and item_id must be nonempty")
        value = deepcopy(payload)
        value["version"] = 1
        timestamp = _now()
        encoded = _encode(value)
        try:
            self.connection.execute(
                "INSERT INTO items VALUES (?, ?, ?, ?, ?)",
                (kind, item_id, 1, encoded, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise AlreadyExistsError(f"{kind}/{item_id} already exists") from exc
        self.connection.execute(
            "INSERT INTO item_versions VALUES (?, ?, ?, ?, ?)",
            (kind, item_id, 1, encoded, timestamp),
        )
        return value

    def update_item(
        self, kind: str, item_id: str, payload: Payload, expected_version: int,
        bump_version: bool = True,
    ) -> Payload:
        current = self.get_item(kind, item_id)
        if current is None:
            raise NotFoundError(f"{kind}/{item_id} does not exist")
        if current["version"] != expected_version:
            raise VersionConflictError(expected_version, current["version"])
        value = deepcopy(payload)
        value["version"] = expected_version + int(bump_version)
        encoded = _encode(value)
        timestamp = _now()
        cursor = self.connection.execute(
            "UPDATE items SET version=?,payload=?,updated_at=? "
            "WHERE kind=? AND item_id=? AND version=?",
            (value["version"], encoded, timestamp, kind, item_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise VersionConflictError(expected_version, current["version"])
        if bump_version:
            self.connection.execute(
                "INSERT INTO item_versions VALUES (?, ?, ?, ?, ?)",
                (kind, item_id, value["version"], encoded, timestamp),
            )
        return value

    def mutate_item(
        self,
        kind: str,
        item_id: str,
        expected_version: int,
        mutation: Mutation,
        audit_event: Payload | None = None,
        bump_version: bool = True,
    ) -> Payload:
        current = self.get_item(kind, item_id)
        if current is None:
            raise NotFoundError(f"{kind}/{item_id} does not exist")
        if current["version"] != expected_version:
            raise VersionConflictError(expected_version, current["version"])
        updated = mutation(deepcopy(current))
        if not isinstance(updated, dict):
            raise TypeError("mutation must return a dictionary")
        value = self.update_item(kind, item_id, updated, expected_version, bump_version)
        if audit_event is not None:
            event = deepcopy(audit_event)
            event.setdefault("item_version", value["version"])
            self.append_audit(kind, item_id, event.pop("event_type", "updated"), event)
        return value

    def mutate_proposal(
        self,
        proposal_id: str,
        expected_version: int,
        mutation: Mutation,
        audit_event: Payload | None = None,
        bump_version: bool = True,
    ) -> Payload:
        return self.mutate_item(
            "proposals", proposal_id, expected_version, mutation, audit_event, bump_version
        )

    def get_idempotency(self, scope: str, key: str) -> Payload | None:
        row = self.connection.execute(
            "SELECT request_hash,result_id,created_at FROM idempotency "
            "WHERE scope=? AND idempotency_key=?", (scope, key)
        ).fetchone()
        if row is None:
            return None
        return {"scope": scope, "key": key, "request_hash": row[0], "result_id": row[1],
                "created_at": row[2]}

    def reserve_idempotency(
        self, scope: str, key: str, request_hash: str, result_id: str
    ) -> Payload:
        if not all((scope, key, request_hash, result_id)):
            raise ValueError("Idempotency scope, key, hash and result ID must be nonempty")
        existing = self.get_idempotency(scope, key)
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflictError("Idempotency key was used with another request")
            return {"created": False, "result_id": existing["result_id"]}
        self.connection.execute(
            "INSERT INTO idempotency VALUES (?, ?, ?, ?, ?)",
            (scope, key, request_hash, result_id, _now()),
        )
        return {"created": True, "result_id": result_id}

    def append_audit(
        self, kind: str, item_id: str, event_type: str, payload: Payload
    ) -> Payload:
        audit_id = str(uuid.uuid4())
        timestamp = _now()
        self.connection.execute(
            "INSERT INTO audit_events(audit_id,kind,item_id,event_type,payload,created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (audit_id, kind, item_id, event_type, _encode(payload), timestamp),
        )
        return {"audit_id": audit_id, "kind": kind, "item_id": item_id,
                "event_type": event_type, "payload": deepcopy(payload), "created_at": timestamp}

    def list_audit(self, kind: str, item_id: str) -> list[Payload]:
        rows = self.connection.execute(
            "SELECT audit_id,event_type,payload,created_at FROM audit_events "
            "WHERE kind=? AND item_id=? ORDER BY sequence", (kind, item_id)
        ).fetchall()
        return [{"audit_id": row[0], "kind": kind, "item_id": item_id,
                 "event_type": row[1], "payload": json.loads(row[2]), "created_at": row[3]}
                for row in rows]


class Store:
    """Durable store; multiple threads/processes use SQLite's write serialization.

    ``version`` is server-managed and normally increments for every mutation.
    Use ``bump_version=False`` only for status-only approval transitions: order
    content retains its business version and immutable historical payload stays
    as it was at creation; current status and audit history record approval.
    Domain callbacks are responsible for preserving content in that operation.
    Historical payloads and audit events are immutable.
    For a multi-item action, call only ``tx`` methods inside ``transaction()``;
    nesting a Store write would try to acquire a second write lock.
    """

    def __init__(self, db_path: str | Path, busy_timeout_ms: int = 10_000):
        self.busy_timeout_ms = busy_timeout_ms
        self._anchor: sqlite3.Connection | None = None
        # Shared-cache memory databases return SQLITE_LOCKED immediately rather
        # than honouring busy_timeout. Serialize their operations so they retain
        # the same transactional behaviour as the file-backed WAL store.
        self._memory_lock = RLock() if str(db_path) == ":memory:" else None
        if str(db_path) == ":memory:":
            self.db_path = f"file:ekt-{uuid.uuid4()}?mode=memory&cache=shared"
            self._uri = True
            self._anchor = self._connect()
        else:
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(path)
            self._uri = False
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path, timeout=self.busy_timeout_ms / 1000,
            isolation_level=None, uri=self._uri,
        )
        connection.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self._memory_lock if self._memory_lock is not None else nullcontext():
            connection = self._connect()
            try:
                yield connection
            finally:
                connection.close()

    @contextmanager
    def transaction(self) -> Iterator[StoreTransaction]:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield StoreTransaction(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def get_item(self, kind: str, item_id: str, version: int | None = None) -> Payload | None:
        with self._connection() as connection:
            return StoreTransaction(connection).get_item(kind, item_id, version)

    def list_items(self, kind: str) -> list[Payload]:
        with self._connection() as connection:
            return StoreTransaction(connection).list_items(kind)

    def create_item(self, kind: str, item_id: str, payload: Payload) -> Payload:
        with self.transaction() as tx:
            return tx.create_item(kind, item_id, payload)

    def update_item(
        self, kind: str, item_id: str, payload: Payload, expected_version: int,
        bump_version: bool = True,
    ) -> Payload:
        with self.transaction() as tx:
            return tx.update_item(kind, item_id, payload, expected_version, bump_version)

    def mutate_item(
        self, kind: str, item_id: str, expected_version: int, mutation: Mutation,
        audit_event: Payload | None = None,
        bump_version: bool = True,
    ) -> Payload:
        with self.transaction() as tx:
            return tx.mutate_item(kind, item_id, expected_version, mutation, audit_event, bump_version)

    def mutate_proposal(
        self, proposal_id: str, expected_version: int, mutation: Mutation,
        audit_event: Payload | None = None,
        bump_version: bool = True,
    ) -> Payload:
        return self.mutate_item(
            "proposals", proposal_id, expected_version, mutation, audit_event, bump_version
        )

    def reserve_idempotency(
        self, scope: str, key: str, request_hash: str, result_id: str
    ) -> Payload:
        with self.transaction() as tx:
            return tx.reserve_idempotency(scope, key, request_hash, result_id)

    def get_idempotency(self, scope: str, key: str) -> Payload | None:
        with self._connection() as connection:
            return StoreTransaction(connection).get_idempotency(scope, key)

    def append_audit(self, kind: str, item_id: str, event_type: str, payload: Payload) -> Payload:
        with self.transaction() as tx:
            return tx.append_audit(kind, item_id, event_type, payload)

    def list_audit(self, kind: str, item_id: str) -> list[Payload]:
        with self._connection() as connection:
            return StoreTransaction(connection).list_audit(kind, item_id)

    def recover_running_jobs(self, kinds: tuple[str, ...] = ("jobs", "runs", "scenarios")) -> int:
        """Fail interrupted work on coordinator startup; never auto-rerun side effects."""
        count = 0
        with self.transaction() as tx:
            for kind in kinds:
                # Read keys from SQL: domain IDs may be job_id, run_id or simply id.
                rows = tx.connection.execute(
                    "SELECT item_id,payload FROM items WHERE kind=?", (kind,)
                ).fetchall()
                for item_id, encoded in rows:
                    payload = json.loads(encoded)
                    if payload.get("status") not in {"running", "queued"}:
                        continue
                    previous_status = payload["status"]
                    payload.update({
                        "status": "failed", "stage": "interrupted",
                        "updated_at": _now(),
                        "error": {"code": "WORKER_RESTARTED",
                                  "message": "Processing was interrupted; start a new run to retry.",
                                  "details": {"previous_status": previous_status}, "retryable": True},
                    })
                    tx.update_item(kind, item_id, payload, payload["version"])
                    tx.append_audit(kind, item_id, "interrupted_on_restart", {
                        "previous_status": previous_status, "retryable": True,
                    })
                    count += 1
        return count

    def close(self) -> None:
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
