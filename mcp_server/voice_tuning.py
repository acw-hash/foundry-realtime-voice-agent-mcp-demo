"""Prepare one reversible voice experiment; remote writes require explicit commands."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import deploy


EXPERIMENTS = ("silence-650", "silence-600", "speech-speed", "dialogue")
STYLE = (
    "\n\nDelivery guidance: Use a warm, calm, professional receptionist tone, "
    "natural contractions, and one short question at a time. Avoid redundant "
    "recaps and repeated acknowledgements. Do not add a spoken preamble before "
    "every tool call. Preserve all required wording, identity acknowledgements, "
    "name-and-title confirmation, language switching, and business gates above."
)
SCOPE = "https://ai.azure.com/.default"


def validate_agent(agent: dict[str, Any]) -> None:
    name = agent.get("name")
    definition = agent.get("definition")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise deploy.DeploymentError("Invalid voice agent name")
    if not isinstance(definition, dict) or definition.get("kind") != "voice":
        raise deploy.DeploymentError("Expected the existing Voice Agent type")
    tools = definition.get("tools")
    if (
        not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict)
        or tools[0].get("project_connection_id") != deploy.CONNECTION
        or not isinstance(tools[0].get("allowed_tools"), list)
        or not all(isinstance(tool, str) for tool in tools[0]["allowed_tools"])
        or set(tools[0]["allowed_tools"]) != deploy.TOOLS
    ):
        raise deploy.DeploymentError("Expected the customer-owned six-tool connection")


def experiment_path(experiment: str) -> tuple[str, ...]:
    if not isinstance(experiment, str):
        raise deploy.DeploymentError("Missing voice experiment")
    if experiment in {"silence-650", "silence-600"}:
        return ("audio", "input", "turn_detection", "silence_duration_ms")
    if experiment == "speech-speed":
        return ("audio", "output", "speed")
    if experiment == "dialogue":
        return ("instructions",)
    raise deploy.DeploymentError("Unknown voice experiment")


def value_at(definition: dict[str, Any], path: tuple[str, ...]) -> Any:
    value = definition
    for part in path[:-1]:
        value = value.get(part)
        if not isinstance(value, dict):
            raise deploy.DeploymentError("Voice configuration is missing a required object")
    return value.get(path[-1])


def tuned_definition(definition: dict[str, Any], experiment: str) -> dict[str, Any]:
    candidate = copy.deepcopy(definition)
    path = experiment_path(experiment)
    before = value_at(candidate, path)
    if experiment.startswith("silence-"):
        turn = candidate["audio"]["input"]["turn_detection"]
        if turn.get("type") != "azure_semantic_vad" or not isinstance(before, int):
            raise deploy.DeploymentError("Silence tuning requires the existing Azure semantic VAD")
        after = int(experiment.split("-")[1])
        if before <= after:
            raise deploy.DeploymentError("This experiment must reduce the existing silence setting")
    elif experiment == "speech-speed":
        if before not in (None, 1, 1.0):
            raise deploy.DeploymentError("Speech pacing experiment requires the default speed")
        after = 1.05
    else:
        if not isinstance(before, str) or not before or STYLE in before:
            raise deploy.DeploymentError("Instructions are missing or already contain this experiment")
        after = before + STYLE
    target = candidate
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = after
    return candidate


def agent_url(endpoint: str, name: str) -> str:
    return endpoint + "/voice_agents/" + quote(name, safe="") + "?api-version=v1"


def read_agent(endpoint: str, name: str) -> tuple[dict[str, Any], str, str | None]:
    metadata: dict[str, str] = {}
    observed = deploy.azure_request(
        agent_url(endpoint, name), scope=SCOPE, response_metadata=metadata,
    )
    versions = observed.get("versions")
    latest = versions.get("latest") if isinstance(versions, dict) else None
    if (
        not isinstance(latest, dict) or not isinstance(latest.get("definition"), dict)
        or not isinstance(latest.get("version"), (str, int))
    ):
        raise deploy.DeploymentError("Foundry did not return a versioned voice definition")
    agent = {
        "name": name, "description": observed.get("description", ""),
        "definition": latest["definition"],
    }
    validate_agent(agent)
    return agent, str(latest["version"]), metadata.get("etag")


def prepare(
    source: dict[str, Any], experiment: str, *, endpoint: str | None = None,
    version: str | None = None, etag: str | None = None,
) -> dict[str, Any]:
    validate_agent(source)
    candidate = tuned_definition(source["definition"], experiment)
    return {
        "schema": 1, "experiment": experiment, "endpoint": endpoint,
        "source": copy.deepcopy(source), "version": version, "etag": etag,
        "candidate_definition": candidate,
    }


def validate_state(state: dict[str, Any]) -> None:
    if not isinstance(state, dict):
        raise deploy.DeploymentError("Invalid voice tuning snapshot")
    source = state.get("source")
    if state.get("schema") != 1 or not isinstance(source, dict):
        raise deploy.DeploymentError("Invalid voice tuning snapshot")
    validate_agent(source)
    expected = tuned_definition(source["definition"], state.get("experiment"))
    if expected != state.get("candidate_definition"):
        raise deploy.DeploymentError("Snapshot changes exceed the single approved experiment")


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(state, indent=2) + "\n")


def preview(state: dict[str, Any]) -> None:
    validate_state(state)
    path = experiment_path(state["experiment"])
    before = value_at(state["source"]["definition"], path)
    after = value_at(state["candidate_definition"], path)
    print(f"Agent: {state['source']['name']}; experiment: {state['experiment']}")
    print(f"Provenance: {'live readback' if state['endpoint'] else 'local snapshot (not deployable)'}")
    if path == ("instructions",):
        print("instructions: append delivery guidance only:")
        print(STYLE.strip())
    else:
        print(f"{'.'.join(path)}: {before!r} -> {after!r}")
    print("Model, tools, greeting, language behavior, and unrelated settings are preserved.")


def check_current(
    state: dict[str, Any], endpoint: str, *, rollback: bool = False,
) -> tuple[dict[str, Any], str | None]:
    validate_state(state)
    if not state.get("endpoint") or state["endpoint"] != endpoint or not state.get("version"):
        raise deploy.DeploymentError("Prepare a fresh --read-live preview for this project first")
    current, version, etag = read_agent(endpoint, state["source"]["name"])
    expected = state["candidate_definition"] if rollback else state["source"]["definition"]
    if (
        deploy.normalized(current["definition"]) != deploy.normalized(expected)
        or current["description"] != state["source"].get("description", "")
        or (not rollback and version != state["version"])
    ):
        raise deploy.DeploymentError("Agent changed since preview; refusing to overwrite it")
    return current, etag


def state_digest(state: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def publish_candidate(state: dict[str, Any], endpoint: str, name: str) -> bool:
    check_current(state, endpoint)
    candidate = {**state["source"], "name": name, "definition": state["candidate_definition"]}
    validate_agent(candidate)
    if name == state["source"]["name"]:
        raise deploy.DeploymentError("A candidate must have a different agent name")
    try:
        read_agent(endpoint, name)
    except deploy.AzureRequestError as error:
        if error.status != 404:
            raise
    else:
        raise deploy.DeploymentError("Candidate name already exists; choose a fresh name")
    deploy.azure_request(
        endpoint + "/voice_agents?api-version=v1", method="POST", body=candidate,
        scope=SCOPE,
    )
    observed, version, etag = read_agent(endpoint, name)
    if deploy.normalized(observed["definition"]) != deploy.normalized(candidate["definition"]):
        raise deploy.DeploymentError("Candidate readback does not match; the source agent was not modified")
    verified = False
    if etag and not etag.startswith("W/") and etag != "*":
        try:
            deploy.azure_request(
                agent_url(endpoint, name), method="POST", body=candidate, scope=SCOPE,
                if_match='"voice-tuning-deliberately-stale"',
            )
        except deploy.AzureRequestError as error:
            if error.status != 412:
                raise
            verified = True
        else:
            raise deploy.DeploymentError(
                "Candidate created, but the service ignored a stale If-Match. "
                "Live updates are unsafe and remain blocked; the source agent was not modified."
            )
    print(f"Candidate {name} version {version} verified. Live agent and phone route unchanged.")
    if not verified:
        print("Conditional writes could not be verified; live apply/rollback remain blocked.")
    return verified


def apply(
    state: dict[str, Any], endpoint: str, *, rollback: bool = False,
    conditional_writes_verified: bool = False,
) -> None:
    if not conditional_writes_verified:
        raise deploy.DeploymentError("Verify conditional writes on an isolated candidate before any live update")
    current, etag = check_current(state, endpoint, rollback=rollback)
    if not etag or etag.startswith("W/") or etag == "*":
        raise deploy.DeploymentError(
            "No strong ETag: safe conditional updates are unavailable. "
            "Use an isolated candidate; do not overwrite the live agent."
        )
    desired = copy.deepcopy(current["definition"])
    path = experiment_path(state["experiment"])
    original = state["source"]["definition"] if rollback else state["candidate_definition"]
    target, origin = desired, original
    for part in path[:-1]:
        target, origin = target[part], origin[part]
    if path[-1] in origin:
        target[path[-1]] = origin[path[-1]]
    else:
        target.pop(path[-1], None)
    deploy.azure_request(
        agent_url(endpoint, current["name"]), method="POST",
        body={**current, "definition": desired}, scope=SCOPE, if_match=etag,
    )
    observed, version, _ = read_agent(endpoint, current["name"])
    if deploy.normalized(observed["definition"]) != deploy.normalized(desired):
        raise deploy.DeploymentError("Published readback did not match; retain snapshot for recovery")
    print(f"{'Rollback' if rollback else 'Tuning'} verified at version {version}. Audio acceptance still required.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--preview", action="store_true", help="Save a new immutable local experiment snapshot")
    operation.add_argument("--publish-candidate", action="store_true", help="APPROVAL REQUIRED: create an isolated remote agent")
    operation.add_argument("--apply", action="store_true", help="APPROVAL REQUIRED: conditionally update the live agent")
    operation.add_argument("--rollback", action="store_true", help="APPROVAL REQUIRED: conditionally restore the changed field")
    parser.add_argument("--state", type=Path, required=True, help="Local snapshot path; preview refuses to overwrite it")
    parser.add_argument("--experiment", choices=EXPERIMENTS)
    parser.add_argument("--read-live", action="store_true", help="Read current definition/version for a deployable preview")
    parser.add_argument("--candidate-name")
    parser.add_argument("--env-file", type=Path, default=deploy.ROOT / "config" / "deploy.env")
    args = parser.parse_args()
    if args.preview and not args.experiment:
        parser.error("--preview requires --experiment")
    if not args.preview and (args.read_live or args.experiment):
        parser.error("--read-live and --experiment apply only to --preview")
    if args.publish_candidate != bool(args.candidate_name):
        parser.error("--publish-candidate requires --candidate-name, used only with that operation")
    if args.preview and args.state.exists():
        raise deploy.DeploymentError("Snapshot already exists; choose a new --state path")
    endpoint = None
    if args.read_live or not args.preview:
        account = deploy.az(["account", "show"])
        if account["id"].lower() != deploy.SUBSCRIPTION:
            raise deploy.DeploymentError("Select the approved Azure subscription before continuing")
        endpoint, _ = deploy.foundry_context(args.env_file)
    if args.preview:
        source = json.loads(deploy.AGENT_FILE.read_text(encoding="utf-8"))
        version, etag = None, None
        if endpoint:
            source, version, etag = read_agent(endpoint, source["name"])
        state = prepare(source, args.experiment, endpoint=endpoint, version=version, etag=etag)
        preview(state)
        save_state(args.state, state)
        print(f"Snapshot saved to {args.state}. No remote changes made.")
    else:
        if endpoint is None:
            raise deploy.DeploymentError("A verified Foundry endpoint is required")
        state = json.loads(args.state.read_text(encoding="utf-8"))
        receipt_path = Path(str(args.state) + ".candidate.json")
        if args.publish_candidate:
            if receipt_path.exists():
                raise deploy.DeploymentError("Candidate receipt already exists; use a fresh experiment snapshot")
            verified = publish_candidate(state, endpoint, args.candidate_name)
            save_state(receipt_path, {
                "state_digest": state_digest(state), "candidate_name": args.candidate_name,
                "conditional_writes_verified": verified,
            })
        else:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict) or receipt.get("state_digest") != state_digest(state):
                raise deploy.DeploymentError("Candidate receipt does not match this experiment snapshot")
            apply(
                state, endpoint, rollback=args.rollback,
                conditional_writes_verified=receipt.get("conditional_writes_verified") is True,
            )


if __name__ == "__main__":
    try:
        main()
    except (deploy.DeploymentError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from None
