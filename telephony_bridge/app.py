from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse
from uuid import UUID

import jwt
import uvicorn
from azure.communication.callautomation import (
    AudioFormat,
    MediaStreamingAudioChannelType,
    MediaStreamingContentType,
    MediaStreamingOptions,
    StreamingTransportType,
)
from azure.communication.callautomation.aio import CallAutomationClient
from azure.core.credentials import AzureKeyCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .media import MediaError, relay, timed_stage


LOGGER = logging.getLogger("contoso_telephony")
TOKEN_SCOPE = "https://ai.azure.com/.default"
ACS_ISSUER = "https://acscallautomation.communication.azure.com"
ACS_KEYS = ACS_ISSUER + "/calling/keys"


@dataclass(frozen=True)
class Settings:
    public_url: str
    project_endpoint: str
    agent_name: str
    acs_endpoint: str
    acs_resource_id: str
    acs_immutable_id: str
    phone_number: str
    event_token: str

    def __post_init__(self) -> None:
        for name in ("public_url", "project_endpoint", "acs_endpoint"):
            parsed = urlparse(getattr(self, name))
            if (
                parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
            ):
                raise ValueError(f"{name} must be a plain HTTPS endpoint")
        if not re.fullmatch(r"/api/projects/[A-Za-z0-9._-]+", urlparse(self.project_endpoint).path):
            raise ValueError("project_endpoint must identify one Foundry project")
        if urlparse(self.public_url).path not in {"", "/"}:
            raise ValueError("public_url must not contain a path")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", self.agent_name):
            raise ValueError("agent_name is invalid")
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", self.phone_number):
            raise ValueError("phone_number must be E.164")
        if len(self.event_token) < 32:
            raise ValueError("event_token must contain at least 32 characters")
        UUID(self.acs_immutable_id)
        if "/providers/microsoft.communication/communicationservices/" not in self.acs_resource_id.lower():
            raise ValueError("acs_resource_id must identify the approved ACS resource")

    @classmethod
    def from_env(cls) -> Settings:
        fields = {
            "public_url": "BRIDGE_PUBLIC_URL",
            "project_endpoint": "FOUNDRY_PROJECT_ENDPOINT",
            "agent_name": "FOUNDRY_AGENT_NAME",
            "acs_endpoint": "ACS_ENDPOINT",
            "acs_resource_id": "ACS_RESOURCE_ID",
            "acs_immutable_id": "ACS_IMMUTABLE_ID",
            "phone_number": "ACS_PHONE_NUMBER",
            "event_token": "EVENTGRID_TOKEN",
        }
        values = {}
        for name, variable in fields.items():
            value = os.environ.get(variable, "").strip()
            if not value:
                raise ValueError(f"{variable} is required")
            values[name] = value.rstrip("/") if name.endswith(("url", "endpoint")) else value
        return cls(**values)

    def voice_url(self, session_id: str) -> str:
        return (
            self.project_endpoint.replace("https://", "wss://", 1)
            + f"/agents/{quote(self.agent_name, safe='')}/endpoint/protocols/voice"
            + f"?api-version=v1&agent_session_id={quote(session_id, safe='')}"
        )


class CallbackValidator:
    def __init__(self, audience: str) -> None:
        self.audience = audience
        self.keys = jwt.PyJWKClient(ACS_KEYS, cache_jwk_set=True, lifespan=300)

    def verify(self, token: str) -> None:
        key = self.keys.get_signing_key_from_jwt(token)
        jwt.decode(
            token, key.key, algorithms=["RS256"], audience=self.audience,
            issuer=ACS_ISSUER, options={"require": ["exp", "iss", "aud"]},
        )

    async def __call__(self, token: str) -> None:
        await asyncio.to_thread(self.verify, token)


@dataclass
class Call:
    token: str
    event_id: str
    call_id: str = ""
    attached: bool = False
    disconnected: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    watchdog: asyncio.Task[None] | None = None
    started_at: float = field(default_factory=time.perf_counter)


class Bridge:
    def __init__(
        self, settings: Settings, acs: CallAutomationClient, credential: AsyncTokenCredential,
        *, validate_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.acs = acs
        self.credential = credential
        self.validate_callback = validate_callback or CallbackValidator(settings.acs_immutable_id)
        self.calls: dict[str, Call] = {}
        self.seen: OrderedDict[str, None] = OrderedDict()

        @asynccontextmanager
        async def lifespan(app: Starlette):
            try:
                yield
            finally:
                for call in list(self.calls.values()):
                    call.stopped.set()
                    await self.finish(call)
                await self.acs.close()
                await self.credential.close()

        self.web = Starlette(routes=[
            Route("/healthz", self.health),
            Route("/readyz", self.ready),
            Route("/check", self.check_voice, methods=["POST"]),
            Route("/events", self.events, methods=["POST"]),
            Route("/callbacks/{token}", self.callbacks, methods=["POST"]),
            WebSocketRoute("/media/{token}", self.media),
        ], lifespan=lifespan)

    async def __call__(self, scope, receive, send) -> None:
        await self.web(scope, receive, send)

    async def health(self, request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def ready(self, request: Request) -> Response:
        try:
            await self.credential.get_token(TOKEN_SCOPE)
        except AzureError as error:
            LOGGER.error("identity_not_ready error_type=%s", type(error).__name__)
            return JSONResponse({"error": "identity_not_ready"}, 503)
        return JSONResponse({"status": "ready"})

    def authorized(self, request: Request) -> bool:
        return secrets.compare_digest(
            request.headers.get("authorization", "").encode(),
            ("Bearer " + self.settings.event_token).encode(),
        )

    async def check_voice(self, request: Request) -> Response:
        if not self.authorized(request):
            return JSONResponse({"error": "unauthorized"}, 401)
        try:
            access = await self.credential.get_token(TOKEN_SCOPE)
            async with asyncio.timeout(90), connect(
                self.settings.voice_url("acs-check-" + secrets.token_hex(8)),
                additional_headers={
                    "Authorization": "Bearer " + access.token,
                    "Foundry-Features": "VoiceAgents=V1Preview",
                }, open_timeout=20, max_size=2_097_152,
            ) as voice:
                audio = 0
                async for raw in voice:
                    event = json.loads(raw)
                    if event.get("type") == "error":
                        raise MediaError("Foundry rejected the diagnostic session")
                    if event.get("type") in {"response.audio.delta", "response.output_audio.delta"}:
                        audio += len(event.get("delta", ""))
                    if event.get("type") == "response.done" and audio:
                        return JSONResponse({
                            "status": "ready", "audio_delivered": True,
                            "agent_name": self.settings.agent_name,
                        })
                raise MediaError("Foundry closed before delivering audio")
        except (AzureError, MediaError, WebSocketException, OSError, ValueError, TimeoutError) as error:
            LOGGER.error("voice_check_failed error_type=%s", type(error).__name__)
            return JSONResponse({"error": "voice_check_failed"}, 503)

    @staticmethod
    async def body(request: Request) -> list[dict[str, Any]]:
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 65_536:
                raise ValueError("event_body_too_large")
        value = json.loads(content)
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list) or not value or not all(isinstance(item, dict) for item in value):
            raise ValueError("invalid_event_body")
        return value

    async def events(self, request: Request) -> Response:
        if not self.authorized(request):
            LOGGER.warning("event_delivery_unauthorized")
            return JSONResponse({"error": "unauthorized"}, 401)
        try:
            events = await self.body(request)
        except ValueError:
            return JSONResponse({"error": "invalid_event_body"}, 400)
        for event in events:
            kind, data = event.get("eventType"), event.get("data")
            if not isinstance(data, dict):
                return JSONResponse({"error": "invalid_event_data"}, 400)
            if kind == "Microsoft.EventGrid.SubscriptionValidationEvent":
                code = data.get("validationCode")
                if not isinstance(code, str) or not code:
                    return JSONResponse({"error": "invalid_validation_code"}, 400)
                return JSONResponse({"validationResponse": code})
            if kind != "Microsoft.Communication.IncomingCall":
                LOGGER.warning("unexpected_event_type")
                return JSONResponse({"error": "unexpected_event_type"}, 400)
            if str(event.get("topic", "")).lower() != self.settings.acs_resource_id.lower():
                LOGGER.warning("unexpected_event_source")
                return JSONResponse({"error": "unexpected_event_source"}, 403)
            destination = data.get("to", {})
            number = destination.get("phoneNumber", {}) if isinstance(destination, dict) else {}
            if not isinstance(number, dict) or number.get("value") != self.settings.phone_number:
                LOGGER.warning("unexpected_called_number")
                return JSONResponse({"error": "unexpected_called_number"}, 403)
            event_id, context = event.get("id"), data.get("incomingCallContext")
            if not isinstance(event_id, str) or not event_id or not isinstance(context, str) or not context:
                return JSONResponse({"error": "missing_call_context"}, 400)
            if event_id in self.seen:
                continue
            call = Call(secrets.token_urlsafe(32), event_id)
            self.calls[call.token] = call
            self.seen[event_id] = None
            try:
                with timed_stage("acs_answer", call.event_id):
                    result = await self.acs.answer_call(
                        context, self.settings.public_url + "/callbacks/" + call.token,
                        media_streaming=MediaStreamingOptions(
                            transport_url=self.settings.public_url.replace("https://", "wss://", 1)
                            + "/media/" + call.token,
                            transport_type=StreamingTransportType.WEBSOCKET,
                            content_type=MediaStreamingContentType.AUDIO,
                            audio_channel_type=MediaStreamingAudioChannelType.UNMIXED,
                            audio_format=AudioFormat.PCM24_K_MONO,
                            start_media_streaming=True, enable_bidirectional=True,
                        ),
                        logging_enable=False,
                    )
                if not result.call_connection_id:
                    raise MediaError("ACS returned no call connection")
                call.call_id = result.call_connection_id
                call.ready.set()
                call.watchdog = asyncio.create_task(self.await_media(call))
                LOGGER.info("call_answered call_id=%s", call.call_id)
            except (AzureError, MediaError) as error:
                call.stopped.set()
                call.ready.set()
                self.calls.pop(call.token, None)
                self.seen.pop(event_id, None)
                LOGGER.error("call_answer_failed error_type=%s", type(error).__name__)
                return JSONResponse({"error": "call_answer_failed"}, 503)
            while len(self.seen) > 1024:
                self.seen.popitem(last=False)
        return JSONResponse({"accepted": True})

    async def await_media(self, call: Call) -> None:
        await asyncio.sleep(45)
        if not call.attached:
            LOGGER.error("media_connection_timeout call_id=%s", call.call_id)
            call.stopped.set()
            await self.finish(call)

    async def callbacks(self, request: Request) -> Response:
        header = request.headers.get("authorization") or request.headers.get("authentication", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            LOGGER.warning("callback_missing_authentication")
            return JSONResponse({"error": "unauthorized"}, 401)
        try:
            await self.validate_callback(token)
        except jwt.PyJWKClientConnectionError:
            LOGGER.error("callback_signing_keys_unavailable")
            return JSONResponse({"error": "signing_keys_unavailable"}, 503)
        except (jwt.InvalidTokenError, jwt.PyJWKClientError):
            LOGGER.warning("callback_invalid_authentication")
            return JSONResponse({"error": "unauthorized"}, 401)
        call = self.calls.get(request.path_params["token"])
        if call is None:
            LOGGER.info("callback_for_closed_call")
            return Response(status_code=204)
        try:
            events = await self.body(request)
        except ValueError:
            return JSONResponse({"error": "invalid_event_body"}, 400)
        for event in events:
            data = event.get("data", {})
            if not isinstance(data, dict) or (
                call.call_id and data.get("callConnectionId") != call.call_id
            ):
                LOGGER.warning("callback_call_mismatch")
                return JSONResponse({"error": "call_mismatch"}, 403)
            kind = event.get("type") or event.get("eventType")
            if kind == "Microsoft.Communication.CallDisconnected":
                call.disconnected = True
                call.stopped.set()
                await self.finish(call)
            elif kind == "Microsoft.Communication.MediaStreamingFailed":
                LOGGER.error("acs_media_failed call_id=%s", call.call_id)
                call.stopped.set()
                await self.finish(call)
        return Response(status_code=204)

    async def media(self, websocket: WebSocket) -> None:
        call = self.calls.get(websocket.path_params["token"])
        if call is None or call.attached or call.stopped.is_set():
            LOGGER.warning("media_connection_rejected")
            await websocket.close(code=1008)
            return
        call.attached = True
        await websocket.accept()
        close_code = 1000
        try:
            with timed_stage("media_handshake", call.call_id):
                await asyncio.wait_for(call.ready.wait(), 30)
                if call.stopped.is_set() or websocket.headers.get("x-ms-call-connection-id") != call.call_id:
                    raise MediaError("Media connection does not match the active call")
                metadata = await asyncio.wait_for(websocket.receive_json(), 15)
                if not isinstance(metadata, dict) or metadata.get("kind") != "AudioMetadata":
                    raise MediaError("Missing ACS media metadata")
                audio = metadata.get("audioMetadata", {})
                if not isinstance(audio, dict) or (
                    audio.get("encoding") != "PCM" or audio.get("sampleRate") != 24_000
                    or audio.get("channels") != 1
                ):
                    raise MediaError("ACS must stream PCM16 mono at 24 kHz")
            LOGGER.info(
                "voice_stage stage=incoming_event_to_media_attached call_id=%s "
                "completed=True duration_ms=%.3f",
                call.call_id, (time.perf_counter() - call.started_at) * 1000,
            )
            with timed_stage("credential_acquisition", call.call_id):
                access = await self.credential.get_token(TOKEN_SCOPE)
            async with AsyncExitStack() as stack:
                with timed_stage("foundry_connect", call.call_id):
                    voice = await stack.enter_async_context(connect(
                        self.settings.voice_url("acs-" + call.call_id),
                        additional_headers={
                            "Authorization": "Bearer " + access.token,
                            "Foundry-Features": "VoiceAgents=V1Preview",
                        },
                        open_timeout=30, max_size=2_097_152, ping_timeout=None,
                    ))
                LOGGER.info("voice_session_connected call_id=%s", call.call_id)
                await relay(websocket, voice, call.stopped, call_id=call.call_id)
        except WebSocketDisconnect:
            LOGGER.info("caller_media_closed call_id=%s", call.call_id)
        except (AzureError, MediaError, WebSocketException, OSError, ValueError, TimeoutError) as error:
            close_code = 1011
            LOGGER.error("voice_bridge_failed call_id=%s error_type=%s", call.call_id, type(error).__name__)
        finally:
            call.stopped.set()
            await self.finish(call)
            if (
                websocket.application_state == WebSocketState.CONNECTED
                and websocket.client_state == WebSocketState.CONNECTED
            ):
                await websocket.close(code=close_code)

    async def finish(self, call: Call) -> None:
        if self.calls.pop(call.token, None) is None:
            return
        if call.watchdog and call.watchdog is not asyncio.current_task():
            call.watchdog.cancel()
            await asyncio.gather(call.watchdog, return_exceptions=True)
        if call.call_id and not call.disconnected:
            try:
                await self.acs.get_call_connection(call.call_id).hang_up(is_for_everyone=True)
            except ResourceNotFoundError:
                LOGGER.info("call_already_closed call_id=%s", call.call_id)
            except AzureError as error:
                LOGGER.error("call_hangup_failed call_id=%s error_type=%s", call.call_id, type(error).__name__)
        LOGGER.info("call_cleaned_up call_id=%s", call.call_id)


def build_app() -> Bridge:
    settings = Settings.from_env()
    key = os.environ.get("ACS_ACCESS_KEY", "").strip()
    if len(key) < 32:
        raise ValueError("ACS_ACCESS_KEY is required")
    credential = DefaultAzureCredential()
    acs = CallAutomationClient(settings.acs_endpoint, AzureKeyCredential(key), logging_enable=False)
    return Bridge(settings, acs, credential)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(
        build_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
        access_log=False, log_level="warning", ws_max_size=1_048_576,
    )


if __name__ == "__main__":
    main()
