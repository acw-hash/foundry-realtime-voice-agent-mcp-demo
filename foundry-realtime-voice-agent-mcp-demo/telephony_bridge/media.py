from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection


LOGGER = logging.getLogger("contoso_telephony")
T = TypeVar("T")


class MediaError(RuntimeError):
    pass


@contextmanager
def timed_stage(stage: str, call_id: str) -> Iterator[None]:
    started = time.perf_counter()
    completed = False
    try:
        yield
        completed = True
    finally:
        LOGGER.info(
            "voice_stage stage=%s call_id=%s completed=%s duration_ms=%.3f",
            stage, call_id, completed, (time.perf_counter() - started) * 1000,
        )


class VoiceMetrics:
    def __init__(
        self, call_id: str, *, clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.call_id = call_id
        self.clock = clock
        self.started = clock()
        self.first_audio = False
        self.stops: dict[str, float] = {}
        self.parents: dict[str, str] = {}
        self.responses: dict[str, tuple[str, float]] = {}
        self.missing_audio_ids = False

    @staticmethod
    def remember(mapping: dict[str, T], key: str, value: T) -> None:
        if len(mapping) >= 64 and key not in mapping:
            mapping.pop(next(iter(mapping)))
        mapping[key] = value

    def observe(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "input_audio_buffer.speech_stopped":
            item_id = event.get("item_id")
            if isinstance(item_id, str) and item_id:
                self.remember(self.stops, item_id, self.clock())
        elif kind == "conversation.item.created":
            item, parent = event.get("item"), event.get("previous_item_id")
            if (
                isinstance(item, dict) and item.get("type") == "message"
                and item.get("role") == "assistant" and isinstance(parent, str)
                and isinstance(item.get("id"), str)
            ):
                self.remember(self.parents, item["id"], parent)
        elif kind in {"response.audio.delta", "response.output_audio.delta"}:
            response_id, item_id = event.get("response_id"), event.get("item_id")
            if isinstance(response_id, str) and isinstance(item_id, str):
                if response_id not in self.responses:
                    self.remember(self.responses, response_id, (item_id, self.clock()))
            elif not self.missing_audio_ids:
                self.missing_audio_ids = True
                LOGGER.info("voice_turn_timing_unavailable call_id=%s reason=missing_audio_ids", self.call_id)
        elif kind == "response.done":
            response = event.get("response")
            if not isinstance(response, dict):
                return
            self.missing_audio_ids = False
            response_id = response.get("id")
            if not isinstance(response_id, str):
                return
            audio = self.responses.pop(response_id, None)
            if audio is None:
                return
            item_id, first_audio = audio
            parent = self.parents.pop(item_id, None)
            stopped = self.stops.pop(parent, None) if parent is not None else None
            if response.get("status") != "completed":
                LOGGER.info("voice_turn_timing_unavailable call_id=%s reason=not_completed", self.call_id)
            elif stopped is None or first_audio < stopped:
                LOGGER.info("voice_turn_timing_unavailable call_id=%s reason=uncorrelated_audio", self.call_id)
            else:
                LOGGER.info(
                    "voice_stage stage=speech_stop_event_to_first_audio call_id=%s "
                    "completed=True duration_ms=%.3f",
                    self.call_id, (first_audio - stopped) * 1000,
                )

    def forwarded_audio(self) -> None:
        if not self.first_audio:
            self.first_audio = True
            LOGGER.info(
                "voice_stage stage=session_connected_to_first_audio_forwarded "
                "call_id=%s completed=True duration_ms=%.3f",
                self.call_id, (self.clock() - self.started) * 1000,
            )


def pcm_payload(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1_048_576:
        raise MediaError("Invalid audio payload")
    try:
        decoded = base64.b64decode(value, validate=True)
    except binascii.Error as error:
        raise MediaError("Invalid base64 audio") from error
    if not decoded or len(decoded) % 2:
        raise MediaError("Audio must contain complete PCM16 samples")
    return value


async def receive_audio(acs: WebSocket, voice: ClientConnection) -> None:
    try:
        while True:
            packet = await acs.receive_json()
            if not isinstance(packet, dict):
                raise MediaError("Invalid ACS media event")
            kind = packet.get("kind")
            if kind == "AudioData":
                audio = packet.get("audioData")
                if not isinstance(audio, dict):
                    raise MediaError("Missing ACS audio data")
                data = audio.get("data")
                if not data and audio.get("silent") is True:
                    continue
                await voice.send(json.dumps({
                    "type": "input_audio_buffer.append", "audio": pcm_payload(data),
                }))
            elif kind == "AudioMetadata":
                raise MediaError("Unexpected second media metadata event")
            elif kind not in {"DtmfData"}:
                raise MediaError("Unsupported ACS media event")
    except WebSocketDisconnect:
        return


async def send_audio(
    acs: WebSocket, voice: ClientConnection, metrics: VoiceMetrics,
) -> None:
    async for raw in voice:
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise MediaError("Invalid Foundry event")
        metrics.observe(event)
        kind = event.get("type")
        if kind == "error":
            raise MediaError("Foundry rejected the voice session")
        if kind in {"response.audio.delta", "response.output_audio.delta"}:
            await acs.send_json({
                "kind": "AudioData",
                "audioData": {"data": pcm_payload(event.get("delta"))},
            })
            metrics.forwarded_audio()
        elif kind == "input_audio_buffer.speech_started":
            with timed_stage("speech_start_event_to_stop_audio_forwarded", metrics.call_id):
                await acs.send_json({"kind": "StopAudio", "stopAudio": {}})
        elif kind == "response.done":
            response = event.get("response")
            if isinstance(response, dict) and response.get("status") in {"failed", "incomplete"}:
                raise MediaError("Foundry could not complete its response")


async def relay(
    acs: WebSocket, voice: ClientConnection, stopped: asyncio.Event, *, call_id: str = "",
) -> None:
    tasks = [
        asyncio.create_task(receive_audio(acs, voice)),
        asyncio.create_task(send_audio(acs, voice, VoiceMetrics(call_id))),
        asyncio.create_task(stopped.wait()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            raise result
