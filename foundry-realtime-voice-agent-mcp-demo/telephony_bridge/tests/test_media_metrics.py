from __future__ import annotations

import asyncio
import base64
import json
import unittest
from unittest.mock import AsyncMock, patch

from telephony_bridge.media import VoiceMetrics, send_audio, timed_stage


AUDIO = base64.b64encode(b"\x00\x01" * 480).decode()


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.metrics = VoiceMetrics("call-test", clock=lambda: self.now)

    def stopped(self):
        self.metrics.observe({
            "type": "input_audio_buffer.speech_stopped", "item_id": "user-1",
        })

    def audio(self):
        self.metrics.observe({
            "type": "response.output_audio.delta", "response_id": "response-1",
            "item_id": "assistant-1", "delta": "private-audio",
        })

    def parent(self, parent="user-1"):
        self.metrics.observe({
            "type": "conversation.item.created", "previous_item_id": parent,
            "item": {"id": "assistant-1", "type": "message", "role": "assistant"},
        })

    def done(self, status="completed"):
        self.metrics.observe({
            "type": "response.done", "response": {"id": "response-1", "status": status},
        })

    def test_only_first_audio_of_correlated_completed_turn_is_measured(self):
        self.stopped()
        self.now = 10.125
        self.audio()
        self.now = 12.0
        self.audio()
        self.parent()
        with self.assertLogs("contoso_telephony", level="INFO") as logs:
            self.done()
        self.assertIn("stage=speech_stop_event_to_first_audio", logs.output[0])
        self.assertIn("duration_ms=125.000", logs.output[0])
        self.assertNotIn("private-audio", " ".join(logs.output))
        self.assertFalse(self.metrics.responses)
        self.assertFalse(self.metrics.stops)
        self.assertFalse(self.metrics.parents)

    def test_greetings_tool_results_cancellations_and_unrelated_turns_are_not_samples(self):
        for scenario in ("greeting", "tool", "cancelled", "unrelated"):
            with self.subTest(scenario=scenario):
                self.setUp()
                if scenario != "greeting":
                    self.stopped()
                self.audio()
                self.parent("tool-result" if scenario == "tool" else "other" if scenario == "unrelated" else "user-1")
                with self.assertLogs("contoso_telephony", level="INFO") as logs:
                    self.done("cancelled" if scenario == "cancelled" else "completed")
                self.assertIn("timing_unavailable", logs.output[0])
                self.assertNotIn("duration_ms", logs.output[0])

    def test_missing_and_malformed_optional_ids_do_not_break_audio(self):
        with self.assertLogs("contoso_telephony", level="INFO") as logs:
            self.metrics.observe({"type": "response.audio.delta", "delta": "private-audio"})
            self.metrics.observe({"type": "response.audio.delta", "delta": "private-audio"})
            self.metrics.observe({"type": "response.done", "response": {"id": []}})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("reason=missing_audio_ids", logs.output[0])
        self.assertNotIn("private-audio", logs.output[0])

    def test_timing_state_is_bounded_and_first_forwarded_audio_is_logged_once(self):
        for index in range(100):
            self.metrics.observe({
                "type": "input_audio_buffer.speech_stopped", "item_id": str(index),
            })
        self.assertEqual(len(self.metrics.stops), 64)
        self.now = 10.25
        with self.assertLogs("contoso_telephony", level="INFO") as logs:
            self.metrics.forwarded_audio()
            self.metrics.forwarded_audio()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("duration_ms=250.000", logs.output[0])

    def test_stage_duration_on_success_failure_and_cancellation_does_not_hide_errors(self):
        for error in (None, ValueError("private-error"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                with patch("telephony_bridge.media.time.perf_counter", side_effect=[10.0, 10.025]):
                    with self.assertLogs("contoso_telephony", level="INFO") as logs:
                        if error is None:
                            with timed_stage("test", "call-test"):
                                pass
                        else:
                            with self.assertRaises(type(error)):
                                with timed_stage("test", "call-test"):
                                    raise error
                self.assertIn("duration_ms=25.000", logs.output[0])
                self.assertIn(f"completed={error is None}", logs.output[0])
                self.assertNotIn("private-error", logs.output[0])


class ForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_preserve_media_order_and_stop_audio(self):
        events = [
            {"type": "response.audio.delta", "delta": AUDIO},
            {"type": "input_audio_buffer.speech_started"},
            {"type": "response.output_audio.delta", "delta": AUDIO},
        ]

        async def voice():
            for event in events:
                yield json.dumps(event)

        acs = AsyncMock()
        with self.assertLogs("contoso_telephony", level="INFO") as logs:
            await send_audio(acs, voice(), VoiceMetrics("call-test"))
        self.assertEqual([call.args[0] for call in acs.send_json.await_args_list], [
            {"kind": "AudioData", "audioData": {"data": AUDIO}},
            {"kind": "StopAudio", "stopAudio": {}},
            {"kind": "AudioData", "audioData": {"data": AUDIO}},
        ])
        self.assertIn("speech_start_event_to_stop_audio_forwarded", " ".join(logs.output))
        self.assertNotIn(AUDIO, " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
