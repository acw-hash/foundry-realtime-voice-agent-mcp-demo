from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp.exceptions import ToolError

from contoso_mcp.server import BankApp, MCP_PATH, build_app
from contoso_mcp.service import BankService, DEMO_CODE
from contoso_mcp.storage import SqliteStore, StorageError


ROOT = Path(__file__).resolve().parents[2]
MCP_TOKEN = "test-mcp-token-" + "a" * 32
ADMIN_TOKEN = "test-admin-token-" + "b" * 32


class ToolTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_and_failure_durations_do_not_log_arguments_or_results(self):
        service = MagicMock()
        app = BankApp(service, MCP_TOKEN, ADMIN_TOKEN)
        service.verify_caller_otp.return_value = {"ok": True, "customer": "private-customer"}
        with (
            patch("contoso_mcp.server.time.perf_counter", side_effect=[10.0, 10.025]),
            self.assertLogs("contoso_mcp", level="INFO") as logs,
        ):
            await app.mcp.call_tool("verify_caller_otp", {
                "session_id": "private-session", "otp": "private-otp",
            })
        self.assertIn("duration_ms=25.000", " ".join(logs.output))
        self.assertIn("ok=True", " ".join(logs.output))
        service.verify_caller_otp.assert_called_once_with("private-session", "private-otp")
        for secret in ("private-customer", "private-session", "private-otp"):
            self.assertNotIn(secret, " ".join(logs.output))
        service.start_call.side_effect = StorageError("private-storage-detail")
        with (
            patch("contoso_mcp.server.time.perf_counter", side_effect=[10.0, 10.05]),
            self.assertLogs("contoso_mcp", level="ERROR") as logs,
            self.assertRaises(ToolError),
        ):
            await app.mcp.call_tool("start_call", {})
        self.assertIn("completed=False", " ".join(logs.output))
        self.assertIn("duration_ms=50.000", " ".join(logs.output))
        self.assertNotIn("private-storage-detail", " ".join(logs.output))


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.service = BankService(
            SqliteStore(Path(self.temporary.name) / "state.db"),
            ROOT / "data" / "officers_20k.json",
            ROOT / "mcp_server" / "data" / "customers.json",
        )
        self.app = BankApp(self.service, MCP_TOKEN, ADMIN_TOKEN)
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.base = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(
            self.app, log_level="error", access_log=False, lifespan="on"
        ))
        self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        for _ in range(100):
            if self.server.started:
                break
            if self.task.done():
                await self.task
                self.fail("Server exited before startup")
            await asyncio.sleep(0.02)
        self.assertTrue(self.server.started)
        self.http = httpx.AsyncClient(base_url=self.base)

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        self.server.should_exit = True
        await asyncio.wait_for(self.task, timeout=10)
        self.socket.close()
        self.temporary.cleanup()

    async def test_authentication_and_public_health(self) -> None:
        self.assertEqual((await self.http.get("/healthz")).json(), {"status": "ok"})
        for method in ("GET", "POST", "DELETE"):
            response = await self.http.request(method, MCP_PATH)
            self.assertEqual(response.status_code, 401)
        response = await self.http.get("/api/otp/candidates",
                                       headers={"Authorization": "Bearer " + MCP_TOKEN})
        self.assertEqual(response.status_code, 401)
        response = await self.http.post(MCP_PATH,
                                        headers={"Authorization": "Bearer " + ADMIN_TOKEN})
        self.assertEqual(response.status_code, 401)
        with patch.object(self.service.store, "run", side_effect=StorageError("private detail")):
            self.assertEqual((await self.http.get("/readyz")).status_code, 503)
        self.assertEqual((await self.http.get("/readyz")).status_code, 200)

    async def test_otp_console_and_admin_apis(self) -> None:
        page = await self.http.get("/otp")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Contoso Bank", page.text)
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        self.assertEqual((await self.http.get("/otp.js")).status_code, 200)
        self.assertEqual((await self.http.post("/api/otp", json={})).status_code, 401)
        headers = {"Authorization": "Bearer " + ADMIN_TOKEN}
        candidates = await self.http.get("/api/otp/candidates", headers=headers)
        self.assertEqual(candidates.headers["cache-control"], "no-store")
        self.assertEqual(candidates.json()["permanent_demo"]["full_name"], "Avery Example")
        self.assertNotIn("Jeremy", candidates.text)
        issued = await self.http.post("/api/otp", headers=headers,
                                      json={"candidate_id": "cand-oakley-example"})
        self.assertEqual(issued.status_code, 200)
        self.assertRegex(issued.json()["otp"], r"^[0-9]{8}$")
        invalid = await self.http.post("/api/otp", headers=headers, content="{")
        self.assertEqual(invalid.status_code, 400)
        invalid = await self.http.post("/api/otp", headers=headers, json={"candidate_id": 1})
        self.assertEqual(invalid.status_code, 400)
        too_large = await self.http.post("/api/otp", headers=headers, content="x" * 4097)
        self.assertEqual(too_large.status_code, 413)
        with patch.object(self.service.store, "run", side_effect=StorageError("private detail")):
            unavailable = await self.http.post("/api/otp", headers=headers,
                                               json={"candidate_id": "cand-oakley-example"})
        self.assertEqual(unavailable.status_code, 503)
        self.assertNotIn("private detail", unavailable.text)

    async def flow(self, streams) -> None:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            inventory = await session.list_tools()
            expected = json.loads((ROOT / "config" / "agent.json").read_text())[
                "definition"
            ]["tools"][0]["allowed_tools"]
            self.assertEqual({tool.name for tool in inventory.tools}, set(expected))
            description = next(
                tool.description for tool in inventory.tools if tool.name == "search_loan_officer"
            )
            self.assertIn("full officer directory", description)
            self.assertNotIn("random", description)

            async def call(name, arguments):
                result = await session.call_tool(name, arguments)
                self.assertFalse(result.isError)
                value = json.loads(result.content[0].text)
                self.assertTrue(value["ok"], value)
                return value

            opened = await call("start_call", {})
            sid = opened["session_id"]
            verified = await call("verify_caller_otp", {"session_id": sid, "otp": DEMO_CODE})
            self.assertEqual(verified["customer"]["full_name"], "Avery Example")
            selected = await call("search_loan_officer",
                                  {"session_id": sid, "spoken_name": "Michael Brennan"})
            self.assertEqual(selected["matches"][0]["full_name"], "Michael Brennan")
            officer = selected["matches"][0]["officer_id"]
            await call("confirm_loan_officer", {"session_id": sid, "officer_id": officer})
            transferred = await call("transfer_to_officer",
                                     {"session_id": sid, "officer_id": officer})
            self.assertTrue(transferred["simulated"])

    async def test_streamable_http_complete_flow(self) -> None:
        async with streamablehttp_client(
            self.base + MCP_PATH, headers={"Authorization": "Bearer " + MCP_TOKEN},
            timeout=timedelta(seconds=10),
        ) as streams:
            await self.flow(streams)

    async def test_legacy_sse_complete_flow(self) -> None:
        async with sse_client(
            self.base + MCP_PATH, headers={"Authorization": "Bearer " + MCP_TOKEN},
        ) as streams:
            await self.flow(streams)

    async def test_deployment_probe_validates_generated_codes_and_both_transports(self) -> None:
        from test_deploy import deploy

        await deploy.probe(self.base, MCP_TOKEN, ADMIN_TOKEN)

    async def test_local_defaults_and_invalid_configuration(self) -> None:
        with patch.dict(os.environ, {
            "CONTOSO_MCP_TOKEN": MCP_TOKEN,
            "CONTOSO_OTP_ADMIN_TOKEN": ADMIN_TOKEN,
            "CONTOSO_STATE_BACKEND": "sqlite",
            "CONTOSO_SQLITE_PATH": str(Path(self.temporary.name) / "defaults.db"),
        }, clear=True):
            app = build_app()
            self.assertEqual(len(app.service.officers), 20_000)
            with patch.dict(os.environ, {"CONTOSO_STATE_BACKEND": "invalid"}):
                with self.assertRaisesRegex(ValueError, "must be sqlite or azure_table"):
                    build_app()
            with patch.dict(os.environ, {"CONTOSO_MCP_TOKEN": "short"}):
                with self.assertRaisesRegex(ValueError, "at least 32"):
                    build_app()
            with patch.dict(os.environ, {
                "CONTOSO_STATE_BACKEND": "azure_table",
                "CONTOSO_TABLE_ENDPOINT": "https://notazure.invalid/path.table.core.windows.net",
            }):
                with self.assertRaisesRegex(ValueError, "Azure Table HTTPS endpoint"):
                    build_app()
            with (
                patch.dict(os.environ, {
                    "CONTOSO_STATE_BACKEND": "azure_table",
                    "CONTOSO_TABLE_ENDPOINT": "https://stcontosomcpdevdemo.table.core.windows.net/",
                }),
                patch("contoso_mcp.server.DefaultAzureCredential"),
                patch("contoso_mcp.server.TableClient") as table,
                patch("contoso_mcp.server.AzureTableStore", return_value=self.service.store),
            ):
                build_app()
                self.assertEqual(
                    table.call_args.kwargs["endpoint"],
                    "https://stcontosomcpdevdemo.table.core.windows.net",
                )
        with self.assertRaisesRegex(ValueError, "different"):
            BankApp(self.service, MCP_TOKEN, MCP_TOKEN)


if __name__ == "__main__":
    unittest.main()
