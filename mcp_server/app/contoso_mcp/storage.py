from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

from azure.core import MatchConditions
from azure.core.exceptions import (
    AzureError,
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.data.tables import TableClient, UpdateMode


Document = dict[str, Any]
T = TypeVar("T")


class StorageError(RuntimeError):
    pass


class Transaction(Protocol):
    def get(self, key: str) -> Document | None: ...
    def set(self, key: str, value: Document) -> None: ...
    def delete(self, key: str) -> None: ...


class Store(Protocol):
    def run(self, action: Callable[[Transaction], T]) -> T: ...
    def close(self) -> None: ...


class SqliteTransaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, key: str) -> Document | None:
        row = self.connection.execute(
            "SELECT payload FROM bankstate WHERE id = ?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key: str, value: Document) -> None:
        self.connection.execute(
            "INSERT INTO bankstate(id, payload) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
            (key, json.dumps(value, separators=(",", ":"))),
        )

    def delete(self, key: str) -> None:
        self.connection.execute("DELETE FROM bankstate WHERE id = ?", (key,))


class SqliteStore:
    """Durable local development backend; never selected implicitly in Azure."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS bankstate "
                    "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                )

    def run(self, action: Callable[[Transaction], T]) -> T:
        try:
            with closing(sqlite3.connect(self.path, timeout=15)) as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    return action(SqliteTransaction(connection))
        except sqlite3.Error as error:
            raise StorageError("Local state transaction failed") from error

    def close(self) -> None:
        pass


class TableTransaction:
    def __init__(self, client: TableClient, partition: str) -> None:
        self.client = client
        self.partition = partition
        self.pending: dict[str, Document | None] = {}

    def get(self, key: str) -> Document | None:
        if key in self.pending:
            return self.pending[key]
        try:
            entity = self.client.get_entity(self.partition, key)
        except ResourceNotFoundError:
            return None
        return json.loads(entity["payload"])

    def set(self, key: str, value: Document) -> None:
        self.pending[key] = value

    def delete(self, key: str) -> None:
        if self.get(key) is not None:
            self.pending[key] = None


class AzureTableStore:
    """Serialize small demo transactions through a partition-local revision CAS.

    Every writer must use this store: revision and business rows commit in one
    atomic Table Storage batch, including OTP consumption and caller identity.
    """

    PARTITION = "contoso-bank"

    def __init__(self, client: TableClient) -> None:
        self.client = client

    def run(self, action: Callable[[Transaction], T]) -> T:
        try:
            try:
                self.client.create_entity(
                    {
                        "PartitionKey": self.PARTITION,
                        "RowKey": "_revision",
                        "version": 0,
                    }
                )
            except ResourceExistsError:
                pass
            for _ in range(8):
                revision = self.client.get_entity(self.PARTITION, "_revision")
                transaction = TableTransaction(self.client, self.PARTITION)
                result = action(transaction)
                operations: list[Any] = [
                    (
                        "update",
                        {
                            "PartitionKey": self.PARTITION,
                            "RowKey": "_revision",
                            "version": revision["version"] + 1,
                        },
                        {
                            "mode": UpdateMode.REPLACE,
                            "etag": revision.metadata["etag"],
                            "match_condition": MatchConditions.IfNotModified,
                        },
                    )
                ]
                for key, value in transaction.pending.items():
                    entity = {"PartitionKey": self.PARTITION, "RowKey": key}
                    if value is None:
                        operations.append(("delete", entity))
                    else:
                        entity["payload"] = json.dumps(value, separators=(",", ":"))
                        operations.append(
                            ("upsert", entity, {"mode": UpdateMode.REPLACE})
                        )
                if len(operations) > 100:
                    raise StorageError("State transaction exceeds the atomic batch limit")
                try:
                    self.client.submit_transaction(operations)
                    return result
                except HttpResponseError as error:
                    if error.status_code not in {409, 412}:
                        raise
            raise StorageError("State transaction conflicted repeatedly; retry the request")
        except AzureError as error:
            raise StorageError("Azure Table Storage transaction failed") from error

    def close(self) -> None:
        self.client.close()
