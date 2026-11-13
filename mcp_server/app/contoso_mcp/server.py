from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Callable

import uvicorn
from azure.data.tables import TableClient
from azure.identity import DefaultAzureCredential
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .service import BankService
from .storage import AzureTableStore, SqliteStore, StorageError, Store


LOGGER = logging.getLogger("contoso_mcp")
HERE = Path(__file__).resolve().parent
MCP_PATH = "/mcp/contoso-bank"
SessionId = Annotated[str, Field(description="Exact session_id returned by start_call.")]


def required_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if len(value) < 32:
        raise ValueError(f"{name} must contain at least 32 characters")
    return value


class BankApp:
    def __init__(
        self, service: BankService, mcp_token: str, admin_token: str,
        credential: DefaultAzureCredential | None = None,
    ) -> None:
        if min(len(mcp_token), len(admin_token)) < 32:
            raise ValueError("Both service credentials must have at least 32 characters")
        if secrets.compare_digest(mcp_token, admin_token):
            raise ValueError("MCP and administrator credentials must be different")
        self.service = service
        self.credential = credential
        self.mcp_authorization = ("Bearer " + mcp_token).encode()
        self.admin_authorization = ("Bearer " + admin_token).encode()
        self.mcp = FastMCP("contoso-bank-routing")
        self.mcp.settings.streamable_http_path = "/"
        self.mcp.settings.sse_path = "/"
        self.mcp.settings.message_path = "/messages/"
        hosts = [value.strip() for value in os.environ.get(
            "CONTOSO_ALLOWED_HOSTS", ""
        ).split(",") if value.strip()]
        self.mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(hosts), allowed_hosts=hosts
        )
        self._register_tools()
        self.streamable = self.mcp.streamable_http_app()
        self.sse = self.mcp.sse_app()

        @asynccontextmanager
        async def lifespan(app: Starlette):
            async with self.streamable.router.lifespan_context(self.streamable):
                try:
                    yield
                finally:
                    self.service.store.close()
                    if self.credential is not None:
                        self.credential.close()

        self.web = Starlette(
            routes=[
                Route("/healthz", self.health),
                Route("/readyz", self.ready),
                Route("/otp", self.console),
                Route("/otp.js", self.javascript),
                Route("/api/otp/candidates", self.list_candidates),
                Route("/api/otp", self.issue_otp, methods=["POST"]),
            ],
            lifespan=lifespan,
        )

    def _register_tools(self) -> None:
        def logged(name: str, action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
            started = time.perf_counter()
            result = None
            try:
                result = action()
                return result
            finally:
                duration = (time.perf_counter() - started) * 1000
                if result is None:
                    LOGGER.error("tool=%s completed=False duration_ms=%.3f", name, duration)
                else:
                    LOGGER.info(
                        "tool=%s completed=True ok=%s error=%s duration_ms=%.3f",
                        name, result["ok"], result.get("error", "-"), duration,
                    )

        @self.mcp.tool()
        def start_call() -> dict[str, Any]:
            """Open one Contoso Bank call. Call once, before requesting an OTP.

            Retain the returned session_id for every subsequent tool.
            """
            return logged("start_call", self.service.start_call)

        @self.mcp.tool()
        def verify_caller_otp(session_id: SessionId, otp: str) -> dict[str, Any]:
            """Verify only the access-code digits from the caller's latest turn.

            Spaces and dashes are allowed. The server remembers partial codes;
            incomplete codes do not consume one of the three complete attempts.
            Never repeat collected digits. Use digits_missing from the result.
            Successful verification returns the authoritative customer profile.
            """
            return logged("verify_caller_otp", lambda: self.service.verify_caller_otp(session_id, otp))

        @self.mcp.tool()
        def search_loan_officer(
            session_id: SessionId,
            spoken_name: str,
            also_heard: list[str] | None = None,
        ) -> dict[str, Any]:
            """Search the full officer directory by the requested name.

            Requires OTP authentication. Exact names and aliases take priority,
            followed by partial names and close spelling matches. Up to five
            candidates are returned; match_count includes candidates not shown.
            For multiple_matches, ask for a first or last name to narrow the
            search; never pick a person arbitrarily. Confirm a single returned
            name and title before proceeding. Unavailable officers stay visible.
            """
            return logged("search_loan_officer", lambda: self.service.search_loan_officer(
                session_id, spoken_name, also_heard
            ))

        @self.mcp.tool()
        def confirm_loan_officer(session_id: SessionId, officer_id: str) -> dict[str, Any]:
            """After an explicit caller yes, confirm the most recently returned officer."""
            return logged("confirm_loan_officer", lambda: self.service.confirm_loan_officer(
                session_id, officer_id
            ))

        @self.mcp.tool()
        def transfer_to_officer(session_id: SessionId, officer_id: str) -> dict[str, Any]:
            """Record a SIMULATED transfer to the explicitly confirmed officer.

            This closes the demo business record. It does not route a phone call
            or close the Voice Agent connection.
            """
            return logged("transfer_to_officer", lambda: self.service.transfer_to_officer(
                session_id, officer_id
            ))

        @self.mcp.tool()
        def end_call(session_id: SessionId, reason: str) -> dict[str, Any]:
            """Close a non-transferred call with authentication_failed,
            officer_not_found, caller_declined, caller_hung_up, or other.

            This records the outcome; the voice client owns transport closure.
            """
            return logged("end_call", lambda: self.service.end_call(session_id, reason))

    @staticmethod
    def json(value: dict[str, Any], status: int = 200) -> JSONResponse:
        return JSONResponse(value, status_code=status, headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })

    def authorized_admin(self, request: Request) -> bool:
        return secrets.compare_digest(
            request.headers.get("authorization", "").encode(), self.admin_authorization
        )

    async def health(self, request: Request) -> Response:
        return self.json({"status": "ok"})

    async def ready(self, request: Request) -> Response:
        try:
            await run_in_threadpool(self.service.store.run, lambda tx: tx.get("_health"))
        except StorageError:
            LOGGER.error("storage_readiness_failed")
            return self.json({"error": "storage_unavailable"}, 503)
        return self.json({"status": "ready"})

    async def console(self, request: Request) -> Response:
        return FileResponse(HERE / "otp.html", media_type="text/html", headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        })

    async def javascript(self, request: Request) -> Response:
        return FileResponse(HERE / "otp.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    async def list_candidates(self, request: Request) -> Response:
        if not self.authorized_admin(request):
            return self.json({"error": "unauthorized"}, 401)
        return self.json(self.service.candidates())

    async def issue_otp(self, request: Request) -> Response:
        if not self.authorized_admin(request):
            return self.json({"error": "unauthorized"}, 401)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return self.json({"error": "request_too_large"}, 413)
        try:
            document = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.json({"error": "invalid_json"}, 400)
        if not isinstance(document, dict) or not isinstance(document.get("candidate_id"), str):
            return self.json({"error": "candidate_id_required"}, 400)
        try:
            result = await run_in_threadpool(self.service.issue_otp, document["candidate_id"])
        except StorageError:
            LOGGER.error("otp_issuance_storage_failed")
            return self.json({"error": "storage_unavailable"}, 503)
        except ValueError:
            LOGGER.error("otp_allocation_failed")
            return self.json({"error": "otp_allocation_failed"}, 503)
        return self.json(result, 200 if result["ok"] else 400)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and (
            path == MCP_PATH or path.startswith(MCP_PATH + "/")
        ):
            headers = dict(scope.get("headers", []))
            if not secrets.compare_digest(
                headers.get(b"authorization", b""), self.mcp_authorization
            ):
                await self.json({"error": "unauthorized"}, 401)(scope, receive, send)
                return
            relative = path[len(MCP_PATH):] or "/"
            legacy = relative != "/" or (
                scope["method"] == "GET" and b"mcp-session-id" not in headers
            )
            await (self.sse if legacy else self.streamable)(
                {
                    **scope,
                    "path": relative,
                    "raw_path": relative.encode(),
                    "root_path": scope.get("root_path", "").rstrip("/") + MCP_PATH,
                },
                receive, send,
            )
            return
        await self.web(scope, receive, send)


def build_app() -> BankApp:
    mcp_token = required_secret("CONTOSO_MCP_TOKEN")
    admin_token = required_secret("CONTOSO_OTP_ADMIN_TOKEN")
    backend = os.environ.get("CONTOSO_STATE_BACKEND", "sqlite")
    credential = None
    store: Store
    if backend == "sqlite":
        store = SqliteStore(Path(os.environ.get(
            "CONTOSO_SQLITE_PATH", str(HERE.parents[1] / "state" / "contoso.db")
        )))
    elif backend == "azure_table":
        endpoint = os.environ.get("CONTOSO_TABLE_ENDPOINT", "").rstrip("/")
        if not re.fullmatch(r"https://[a-z0-9]{3,24}\.table\.core\.windows\.net", endpoint):
            raise ValueError("CONTOSO_TABLE_ENDPOINT must be an Azure Table HTTPS endpoint")
        credential = DefaultAzureCredential()
        store = AzureTableStore(TableClient(
            endpoint=endpoint,
            table_name=os.environ.get("CONTOSO_TABLE_NAME", "bankstate"),
            credential=credential,
        ))
    else:
        raise ValueError("CONTOSO_STATE_BACKEND must be sqlite or azure_table")
    data = Path(os.environ.get("CONTOSO_DATA_DIR", str(HERE.parents[2] / "data")))
    customers = Path(os.environ.get(
        "CONTOSO_CUSTOMERS_FILE", str(HERE.parents[1] / "data" / "customers.json")
    ))
    service = BankService(store, data / "officers_20k.json", customers)
    LOGGER.info("directory_loaded officers=%d backend=%s", len(service.officers), backend)
    return BankApp(service, mcp_token, admin_token, credential)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(
            logger_name="contoso_mcp",
            disable_offline_storage=True,
        )
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
