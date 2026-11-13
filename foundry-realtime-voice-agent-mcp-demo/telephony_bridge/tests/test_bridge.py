from __future__ import annotations

import asyncio
import base64
import json
import time
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.websockets import WebSocket

from telephony_bridge.app import ACS_ISSUER, Bridge, CallbackValidator, Settings, main
from telephony_bridge.media import MediaError, pcm_payload


RESOURCE_ID = (
    "/subscriptions/test/resourceGroups/demo/providers/"
    "Microsoft.Communication/communicationServices/test"
)
SETTINGS = Settings(
    public_url="https://bridge.example",
    project_endpoint="https://foundry.example/api/projects/project",
    agent_name="voice-agent",
    acs_endpoint="https://test.communication.azure.com",
    acs_resource_id=RESOURCE_ID,
    acs_immutable_id="11111111-2222-4333-8444-555555555555",
    phone_number="+12025550100",
    event_token="event-test-" + "a" * 40,
)
AUDIO = base64.b64encode(b"\x00\x01" * 480).decode()


def incoming(event_id="event-1"):
    return [{
        "id": event_id, "topic": RESOURCE_ID,
        "eventType": "Microsoft.Communication.IncomingCall",
        "data": {
            "incomingCallContext": "private-incoming-context",
            "to": {"kind": "phoneNumber", "phoneNumber": {"value": SETTINGS.phone_number}},
        },
    }]


class Voice:
    def __init__(self):
        self.events = asyncio.Queue()
        self.input = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.events.get()
        if value is None:
            raise StopAsyncIteration
        return json.dumps(value)

    async def send(self, value):
        await self.input.put(json.loads(value))


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.acs = MagicMock()
        self.acs.answer_call = AsyncMock(return_value=SimpleNamespace(call_connection_id="call-1"))
        self.acs.close = AsyncMock()
        self.acs.get_call_connection.return_value.hang_up = AsyncMock()
        self.credential = MagicMock()
        self.credential.get_token = AsyncMock(return_value=AccessToken("test-token", int(time.time()) + 3600))
        self.credential.close = AsyncMock()
        self.validate = AsyncMock()
        self.bridge = Bridge(SETTINGS, self.acs, self.credential, validate_callback=self.validate)
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.bridge), base_url=SETTINGS.public_url
        )
        self.headers = {"Authorization": "Bearer " + SETTINGS.event_token}

    async def asyncTearDown(self):
        for call in list(self.bridge.calls.values()):
            call.stopped.set()
            await self.bridge.finish(call)
        await self.http.aclose()

    async def open_call(self, event_id="event-1"):
        response = await self.http.post("/events", headers=self.headers, json=incoming(event_id))
        self.assertEqual(response.status_code, 200, response.text)
        return next(call for call in self.bridge.calls.values() if call.event_id == event_id)

    def socket(self, call, *, call_id="call-1", rate=24_000):
        input_queue, output_queue = asyncio.Queue(), asyncio.Queue()
        for item in (
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": json.dumps({
                "kind": "AudioMetadata",
                "audioMetadata": {"encoding": "PCM", "sampleRate": rate, "channels": 1},
            })},
        ):
            input_queue.put_nowait(item)
        scope = {
            "type": "websocket", "path_params": {"token": call.token},
            "headers": [(b"x-ms-call-connection-id", call_id.encode())],
        }
        return WebSocket(scope, input_queue.get, output_queue.put), input_queue, output_queue

    async def test_event_authentication_and_health(self):
        self.assertEqual((await self.http.get("/healthz")).json(), {"status": "ok"})
        self.assertEqual((await self.http.get("/readyz")).status_code, 200)
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            self.assertEqual((await self.http.post(
                "/events", headers=headers, json=incoming()
            )).status_code, 401)
        self.acs.answer_call.assert_not_called()
        self.credential.get_token.side_effect = ClientAuthenticationError("sensitive details")
        with self.assertLogs("contoso_telephony", level="ERROR") as logs:
            response = await self.http.get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("sensitive details", " ".join(logs.output))

    async def test_event_grid_validation_and_malformed_requests(self):
        response = await self.http.post("/events", headers=self.headers, json=[{
            "eventType": "Microsoft.EventGrid.SubscriptionValidationEvent",
            "data": {"validationCode": "validation-123"},
        }])
        self.assertEqual(response.json(), {"validationResponse": "validation-123"})
        for content in ('{', '[]', '"not an event"', "x" * 65_537):
            result = await self.http.post("/events", headers=self.headers, content=content)
            self.assertEqual(result.status_code, 400)

    async def test_wrong_resource_and_number_are_rejected(self):
        for field in ("topic", "number"):
            event = incoming()
            if field == "topic":
                event[0]["topic"] = RESOURCE_ID + "-other"
            else:
                event[0]["data"]["to"]["phoneNumber"]["value"] = "+12025550101"
            self.assertEqual((await self.http.post(
                "/events", headers=self.headers, json=event
            )).status_code, 403)
        self.acs.answer_call.assert_not_called()

    async def test_answer_is_idempotent_and_uses_bidirectional_24khz(self):
        call = await self.open_call()
        await self.open_call()
        self.acs.answer_call.assert_awaited_once()
        options = self.acs.answer_call.call_args.kwargs["media_streaming"]
        self.assertTrue(options.enable_bidirectional)
        self.assertTrue(options.start_media_streaming)
        self.assertEqual(options.audio_format, "pcm24KMono")
        self.assertTrue(options.transport_url.endswith("/media/" + call.token))
        self.assertNotIn(SETTINGS.event_token, options.transport_url)

    async def test_answer_error_is_explicit_and_does_not_log_context(self):
        self.acs.answer_call.side_effect = HttpResponseError("private-incoming-context")
        with self.assertLogs("contoso_telephony", level="ERROR") as logs:
            result = await self.http.post("/events", headers=self.headers, json=incoming())
        self.assertEqual(result.status_code, 503)
        self.assertFalse(self.bridge.calls)
        self.assertFalse(self.bridge.seen)
        self.assertNotIn("private-incoming-context", " ".join(logs.output))

    async def test_callback_authentication_and_call_correlation(self):
        call = await self.open_call()
        path = "/callbacks/" + call.token
        self.assertEqual((await self.http.post(path, json=[])).status_code, 401)
        headers = {"Authorization": "Bearer synthetic-jwt"}
        self.validate.side_effect = jwt.InvalidTokenError("invalid")
        self.assertEqual((await self.http.post(path, headers=headers, json=[])).status_code, 401)
        self.validate.side_effect = None
        mismatch = await self.http.post(path, headers=headers, json=[{
            "type": "Microsoft.Communication.CallDisconnected",
            "data": {"callConnectionId": "other-call"},
        }])
        self.assertEqual(mismatch.status_code, 403)
        self.assertFalse(call.stopped.is_set())
        ended = await self.http.post(path, headers=headers, json=[{
            "type": "Microsoft.Communication.CallDisconnected",
            "data": {"callConnectionId": call.call_id},
        }])
        self.assertEqual(ended.status_code, 204)
        self.assertTrue(call.stopped.is_set())
        self.assertFalse(self.bridge.calls)
        self.acs.get_call_connection.return_value.hang_up.assert_not_awaited()

    async def test_unknown_media_connection_is_rejected(self):
        output = asyncio.Queue()
        socket = WebSocket(
            {"type": "websocket", "path_params": {"token": "unknown"}, "headers": []},
            AsyncMock(), output.put,
        )
        await self.bridge.media(socket)
        self.assertEqual((await output.get())["code"], 1008)

    async def test_wrong_audio_rate_and_wrong_call_id_fail_closed(self):
        for rate, call_id in ((16_000, "call-1"), (24_000, "wrong-call")):
            call = await self.open_call(str(rate) + call_id)
            socket, _, output = self.socket(call, rate=rate, call_id=call_id)
            with self.assertLogs("contoso_telephony", level="ERROR"):
                await self.bridge.media(socket)
            self.assertEqual((await output.get())["type"], "websocket.accept")
            self.assertEqual((await output.get())["code"], 1011)
            self.assertNotIn(call.token, self.bridge.calls)

    async def test_real_wire_audio_greeting_interruption_and_disconnect(self):
        call = await self.open_call()
        socket, input_queue, output = self.socket(call)
        voice = Voice()

        @asynccontextmanager
        async def connection(*args, **kwargs):
            self.assertIn("agent_session_id=acs-call-1", args[0])
            self.assertEqual(kwargs["additional_headers"]["Authorization"], "Bearer test-token")
            yield voice

        with patch("telephony_bridge.app.connect", connection):
            task = asyncio.create_task(self.bridge.media(socket))
            self.assertEqual((await asyncio.wait_for(output.get(), 2))["type"], "websocket.accept")
            await voice.events.put({"type": "response.output_audio.delta", "delta": AUDIO})
            greeting = await asyncio.wait_for(output.get(), 2)
            self.assertEqual(json.loads(greeting["text"]), {
                "kind": "AudioData", "audioData": {"data": AUDIO},
            })
            await input_queue.put({"type": "websocket.receive", "text": json.dumps({
                "kind": "AudioData", "audioData": {"data": AUDIO},
            })})
            caller = await asyncio.wait_for(voice.input.get(), 2)
            self.assertEqual(caller, {"type": "input_audio_buffer.append", "audio": AUDIO})
            await voice.events.put({"type": "input_audio_buffer.speech_started"})
            stopped = await asyncio.wait_for(output.get(), 2)
            self.assertEqual(json.loads(stopped["text"]), {"kind": "StopAudio", "stopAudio": {}})
            await input_queue.put({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(task, 2)
        self.assertFalse(self.bridge.calls)
        self.acs.get_call_connection.return_value.hang_up.assert_awaited_once_with(is_for_everyone=True)

    async def test_calls_and_session_urls_are_independent(self):
        first = await self.open_call("first")
        self.acs.answer_call.return_value = SimpleNamespace(call_connection_id="call-2")
        second = await self.open_call("second")
        self.assertNotEqual(first.token, second.token)
        self.assertNotEqual(
            SETTINGS.voice_url("acs-" + first.call_id), SETTINGS.voice_url("acs-" + second.call_id)
        )
        first.stopped.set()
        await self.bridge.finish(first)
        self.assertIn(second.token, self.bridge.calls)
        self.assertFalse(second.stopped.is_set())

    async def test_voice_diagnostic_is_protected(self):
        self.assertEqual((await self.http.post("/check")).status_code, 401)

    async def test_voice_diagnostic_identifies_the_agent_that_produced_audio(self):
        voice = Voice()
        await voice.events.put({"type": "response.output_audio.delta", "delta": AUDIO})
        await voice.events.put({"type": "response.done"})

        @asynccontextmanager
        async def connection(*args, **kwargs):
            yield voice

        with patch("telephony_bridge.app.connect", connection):
            response = await self.http.post("/check", headers=self.headers)
        self.assertEqual(response.json(), {
            "status": "ready", "audio_delivered": True, "agent_name": SETTINGS.agent_name,
        })


class ValidationTests(unittest.TestCase):
    def test_server_disables_access_and_websocket_url_logging(self):
        with patch("telephony_bridge.app.build_app"), patch("telephony_bridge.app.uvicorn.run") as run:
            main()
        self.assertFalse(run.call_args.kwargs["access_log"])
        self.assertEqual(run.call_args.kwargs["log_level"], "warning")

    def test_audio_validation_requires_complete_base64_pcm16(self):
        self.assertEqual(pcm_payload(AUDIO), AUDIO)
        for value in (None, "", "!!!", base64.b64encode(b"\x01").decode()):
            with self.subTest(value=value), self.assertRaises(MediaError):
                pcm_payload(value)

    def test_invalid_configuration_is_not_silently_defaulted(self):
        for fields in (
            {"public_url": "http://insecure.example"},
            {"project_endpoint": "https://example.com/not-a-project"},
            {"phone_number": "555-1234"},
            {"event_token": "short"},
            {"acs_immutable_id": "not-a-guid"},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(SETTINGS, **fields)

    def test_callback_jwt_checks_signature_issuer_audience_and_expiry(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        validator = CallbackValidator(SETTINGS.acs_immutable_id)
        claims = {
            "iss": ACS_ISSUER, "aud": SETTINGS.acs_immutable_id,
            "exp": int(time.time()) + 300,
        }
        with patch.object(validator.keys, "get_signing_key_from_jwt",
                          return_value=SimpleNamespace(key=key.public_key())):
            validator.verify(jwt.encode(claims, key, algorithm="RS256"))
            for overrides in (
                {"iss": "https://attacker.example"}, {"aud": "wrong-resource"}, {"exp": 1},
            ):
                with self.subTest(overrides=overrides), self.assertRaises(jwt.InvalidTokenError):
                    validator.verify(jwt.encode({**claims, **overrides}, key, algorithm="RS256"))


if __name__ == "__main__":
    unittest.main()
