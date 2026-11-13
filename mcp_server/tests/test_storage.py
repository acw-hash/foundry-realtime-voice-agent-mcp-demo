from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableEntity

from contoso_mcp.storage import AzureTableStore, StorageError
from contoso_mcp.service import BankService


class FakeTable:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.version = 0
        self.conflict_once = False
        self.fail_status: int | None = None
        self.commits: list[list[Any]] = []

    def create_entity(self, entity: dict[str, Any]) -> None:
        if entity["RowKey"] in self.rows:
            raise ResourceExistsError("already exists")
        self.rows[entity["RowKey"]] = copy.deepcopy(entity)

    def get_entity(self, partition: str, key: str) -> TableEntity:
        if key not in self.rows:
            raise ResourceNotFoundError("not found")
        entity = TableEntity(copy.deepcopy(self.rows[key]))
        entity.metadata["etag"] = str(self.version)
        return entity

    def submit_transaction(self, operations: list[Any]) -> None:
        if self.conflict_once or self.fail_status:
            error = HttpResponseError("injected storage failure")
            error.status_code = self.fail_status or 412
            self.conflict_once = False
            raise error
        if operations[0][2]["etag"] != str(self.version):
            error = HttpResponseError("conflict")
            error.status_code = 412
            raise error
        rows = copy.deepcopy(self.rows)
        for operation in operations:
            kind, entity, *_ = operation
            if kind == "delete":
                del rows[entity["RowKey"]]
            else:
                rows[entity["RowKey"]] = copy.deepcopy(entity)
        self.rows = rows
        self.version += 1
        self.commits.append(operations)

    def close(self) -> None:
        pass


class TableStoreTests(unittest.TestCase):
    def test_candidate_consumption_and_call_identity_share_a_cloud_transaction(self) -> None:
        client = FakeTable()
        root = Path(__file__).resolve().parents[2]

        def service():
            return BankService(
                AzureTableStore(client),
                root / "data" / "officers_20k.json",
                root / "mcp_server" / "data" / "customers.json",
                code_factory=lambda: "12341234",
            )

        first = service()
        first.issue_otp("cand-oakley-example")
        sid = first.start_call()["session_id"]
        second = service()
        result = second.verify_caller_otp(sid, "12341234")
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["customer"]["full_name"], "Oakley Example")
        keys = {operation[1]["RowKey"] for operation in client.commits[-1]}
        self.assertIn(sid, keys)
        self.assertTrue(any(key.startswith("otp_") for key in keys))
        self.assertEqual(first.verify_caller_otp(first.start_call()["session_id"], "12341234")["status"],
                         "consumed")

    def test_atomic_batch_includes_revision_and_all_business_rows(self) -> None:
        client = FakeTable()
        store = AzureTableStore(client)

        def write(tx):
            tx.set("otp_digest", {"consumed": True})
            tx.set("finance_session", {"authenticated": True})
            return "committed"

        self.assertEqual(store.run(write), "committed")
        self.assertEqual(len(client.commits[0]), 3)
        self.assertEqual({operation[1]["PartitionKey"] for operation in client.commits[0]},
                         {store.PARTITION})
        self.assertIn("match_condition", client.commits[0][0][2])
        self.assertTrue(store.run(lambda tx: tx.get("finance_session"))["authenticated"])

    def test_conflict_retries_whole_action_and_fails_explicitly_for_other_errors(self) -> None:
        client = FakeTable()
        client.conflict_once = True
        store = AzureTableStore(client)
        runs = []

        def write(tx):
            runs.append(True)
            tx.set("value", {"count": len(runs)})
            return len(runs)

        self.assertEqual(store.run(write), 2)
        self.assertEqual(json.loads(client.rows["value"]["payload"])["count"], 2)
        client.fail_status = 403
        with self.assertRaisesRegex(StorageError, "Azure Table Storage"):
            store.run(write)

    def test_failed_callback_does_not_commit_any_rows(self) -> None:
        client = FakeTable()
        store = AzureTableStore(client)

        def write(tx):
            tx.set("one", {"value": 1})
            raise ValueError("invalid operation")

        with self.assertRaises(ValueError):
            store.run(write)
        self.assertNotIn("one", client.rows)
        self.assertEqual(client.commits, [])


if __name__ == "__main__":
    unittest.main()
