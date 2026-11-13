#!/usr/bin/env python3
"""Run a text-only session against the Contoso Bank Agent in a customer Foundry project."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

try:
    from websockets.asyncio.client import connect
    from websockets.exceptions import WebSocketException
except ImportError as error:
    raise SystemExit(
        "Missing dependency 'websockets'. Run: "
        "python3 -m pip install -r requirements.txt"
    ) from error


ROOT = Path(__file__).resolve().parent
AGENT_CONFIG = ROOT / "config" / "agent.json"
DEFAULT_ENV_FILE = ROOT / "config" / "deploy.env"
API_VERSION = "v1"
TOKEN_SCOPE = "https://ai.azure.com/.default"
VOICE_AGENT_FEATURE = "VoiceAgents=V1Preview"
ENV_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
TEXT_DELTA_EVENTS = {
    "response.audio_transcript.delta",
    "response.output_audio_transcript.delta",
    "response.output_text.delta",
}
def load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(
            f"configuration file not found: {path}; "
            "copy config/deploy.env.example to config/deploy.env"
        )

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_ASSIGNMENT.fullmatch(line)
        if match is None:
            raise ValueError(f"{path}:{line_number}: expected KEY=VALUE")
        try:
            parsed = shlex.split(match.group(2), comments=True, posix=True)
        except ValueError as error:
            raise ValueError(f"{path}:{line_number}: {error}") from error
        if len(parsed) != 1:
            raise ValueError(f"{path}:{line_number}: value must be one string")
        values[match.group(1)] = parsed[0]
    return values


def load_agent_name() -> str:
    with AGENT_CONFIG.open(encoding="utf-8") as stream:
        document = json.load(stream)
    name = document.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{AGENT_CONFIG} has no Agent name")
    return name


def validate_endpoint(endpoint: str) -> str:
    value = endpoint.rstrip("/")
    parsed = urlparse(value)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or len(parts) < 3
        or parts[-2] != "projects"
    ):
        raise ValueError(
            "AZURE_VOICE_AGENTS_ENDPOINT must be an HTTPS URL ending in "
            "/api/projects/<project-name>"
        )
    if "<" in value or ">" in value:
        raise ValueError("AZURE_VOICE_AGENTS_ENDPOINT still contains a placeholder")
    return value


def realtime_url(
    endpoint: str, agent_name: str, agent_session_id: str
) -> str:
    parsed = urlparse(endpoint)
    path = (
        f"{parsed.path.rstrip('/')}/agents/{quote(agent_name, safe='')}"
        "/endpoint/protocols/voice"
    )
    query = (
        f"api-version={quote(API_VERSION, safe='')}"
        f"&agent_session_id={quote(agent_session_id, safe='')}"
    )
    return urlunparse(("wss", parsed.netloc, path, "", query, ""))


def azure_access_token() -> str:
    result = subprocess.run(
        [
            "az.cmd",
            "account",
            "get-access-token",
            "--scope",
            TOKEN_SCOPE,
            "--query",
            "accessToken",
            "--output",
            "tsv",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Azure CLI error"
        raise RuntimeError(f"could not obtain Foundry access token: {detail}")
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("Azure CLI returned an empty Foundry access token")
    return token


def wire_event(event_type: str, **fields: Any) -> str:
    return json.dumps({"type": event_type, **fields}, separators=(",", ":"))


def text_input(text: str) -> str:
    return wire_event(
        "conversation.item.create",
        item={
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    )


def error_message(event: dict[str, Any]) -> str | None:
    if event.get("type") != "error":
        return None
    error = event.get("error")
    if not isinstance(error, dict):
        return "Voice Agent returned an unknown error"
    code = error.get("code") or error.get("type") or "unknown"
    message = error.get("message") or "No message provided"
    return f"Voice Agent error [{code}]: {message}"


def response_output_types(event: dict[str, Any]) -> set[str]:
    response = event.get("response")
    output = response.get("output") if isinstance(response, dict) else None
    if not isinstance(output, list):
        return set()
    return {
        str(item.get("type"))
        for item in output
        if isinstance(item, dict) and item.get("type")
    }


class EventPrinter:
    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose
        self._line_open = False

    def finish_line(self) -> None:
        if self._line_open:
            print()
            self._line_open = False

    def show(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type") or "<unknown>")
        message = error_message(event)
        if message:
            self.finish_line()
            raise RuntimeError(message)

        if kind in TEXT_DELTA_EVENTS:
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                if not self._line_open:
                    print("agent> ", end="", flush=True)
                    self._line_open = True
                print(delta, end="", flush=True)
            return

        if ".mcp_call." in kind:
            self.finish_line()
            name = event.get("name") or event.get("tool_name")
            if name:
                print(f"mcp> {name} ({kind.rsplit('.', 1)[-1]})")
            elif self.verbose:
                print(f"event> {kind}")
            return

        if kind == "response.done":
            response = event.get("response")
            output = response.get("output") if isinstance(response, dict) else None
            if isinstance(output, list):
                for item in output:
                    if not isinstance(item, dict) or item.get("type") != "mcp_call":
                        continue
                    self.finish_line()
                    print(
                        f"mcp> {item.get('name', 'unknown')} "
                        f"({item.get('status', 'completed')})"
                    )

        if self.verbose and kind not in {
            "response.audio.delta",
            "response.output_audio.delta",
        }:
            self.finish_line()
            print(f"event> {kind}")


async def receive_until(
    websocket: Any,
    printer: EventPrinter,
    *,
    phase: str,
    timeout_seconds: float,
    wait_for_session: bool = False,
) -> dict[str, Any]:
    while True:
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=timeout_seconds)
        except asyncio.TimeoutError as error:
            printer.finish_line()
            raise RuntimeError(
                f"timed out after {timeout_seconds:g}s waiting for {phase}"
            ) from error

        try:
            event = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeError("Voice Agent returned invalid JSON") from error
        if not isinstance(event, dict):
            raise RuntimeError("Voice Agent event must be a JSON object")

        printer.show(event)
        kind = event.get("type")
        if wait_for_session and kind == "session.updated":
            printer.finish_line()
            return event
        if not wait_for_session and kind == "response.done":
            printer.finish_line()
            response = event.get("response")
            response = response if isinstance(response, dict) else {}
            status = response.get("status")
            if status not in {None, "completed"}:
                raise RuntimeError(
                    f"Voice Agent response ended with status={status!r}: "
                    f"{response.get('status_details')!r}"
                )
            if response_output_types(event) & {"function_call", "mcp_call"}:
                continue
            return event


async def send_turn(
    websocket: Any,
    printer: EventPrinter,
    text: str,
    timeout_seconds: float,
) -> None:
    value = text.strip()
    if not value:
        raise ValueError("message cannot be empty")
    await websocket.send(text_input(value))
    await websocket.send(wire_event("response.create"))
    await receive_until(
        websocket,
        printer,
        phase="Agent response",
        timeout_seconds=timeout_seconds,
    )


async def run(args: argparse.Namespace) -> None:
    file_values = load_env_file(args.env_file)
    endpoint = validate_endpoint(file_values.get("AZURE_VOICE_AGENTS_ENDPOINT", ""))
    agent_name = load_agent_name()
    session_id = f"local-debug-{uuid.uuid4().hex}"
    url = realtime_url(endpoint, agent_name, session_id)
    token = azure_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Foundry-Features": VOICE_AGENT_FEATURE,
    }
    printer = EventPrinter(args.verbose)

    print(f"connecting agent={agent_name} agent_session_id={session_id}")
    async with connect(
        url,
        additional_headers=headers,
        max_size=None,
        ping_timeout=None,
        open_timeout=args.timeout,
    ) as websocket:
        await receive_until(
            websocket,
            printer,
            phase="session readiness",
            timeout_seconds=args.timeout,
            wait_for_session=True,
        )
        print("session=ready")

        await receive_until(
            websocket,
            printer,
            phase="opening response",
            timeout_seconds=args.timeout,
        )

        if args.connect_only:
            print("connect_smoke=passed")
            return

        if args.message:
            for message in args.message:
                print(f"you> {message}")
                await send_turn(websocket, printer, message, args.timeout)
            print(f"message_smoke=passed turns={len(args.message)}")
            return

        print("Enter a text turn. Use /quit to close the session.")
        while True:
            try:
                message = await asyncio.to_thread(input, "you> ")
            except EOFError:
                print()
                return
            if message.strip().lower() in {"/quit", "/exit"}:
                return
            if not message.strip():
                continue
            await send_turn(websocket, printer, message, args.timeout)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Text-only terminal client for the published Contoso Bank Voice Agent."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="customer Foundry project settings (default: config/deploy.env)",
    )
    parser.add_argument(
        "--message",
        action="append",
        help="send one turn non-interactively; repeat for multiple turns",
    )
    parser.add_argument(
        "--connect-only",
        action="store_true",
        help="connect, receive the opening response, and exit",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="seconds to wait for each session/response phase (default: 60)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print additional protocol event types (audio payloads stay hidden)",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.connect_only and args.message:
        parser.error("--connect-only cannot be combined with --message")
    return args


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nclosed by user", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, WebSocketException) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
