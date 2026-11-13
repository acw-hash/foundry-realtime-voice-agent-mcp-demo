#!/usr/bin/env python3
"""Explicit deployment/check/cutover commands for the customer-owned MCP."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client


ROOT = Path(__file__).resolve().parent.parent
SUBSCRIPTION = os.environ.get("AZURE_SUBSCRIPTION_ID", "").lower()
REGION = "eastus2"
RESOURCE_GROUP = "rg-contoso-bank-mcp"
APP_NAME = "ca-contosomcp-private-demo"
DEPLOYMENT_NAME = "contoso-bank-mcp-private"
APP_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}"
    f"/providers/Microsoft.App/containerApps/{APP_NAME}"
)
OUTPUT_FILE = ROOT / "config" / "contoso-mcp.deployment.json"
AGENT_FILE = ROOT / "config" / "agent.contoso-mcp.json"
CONNECTION = "contoso-bank-mcp"
TOOLS = {
    "start_call", "verify_caller_otp", "search_loan_officer",
    "confirm_loan_officer", "transfer_to_officer", "end_call",
}


class DeploymentError(RuntimeError):
    pass


class DeploymentPending(DeploymentError):
    pass


def validate_subscription() -> None:
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        SUBSCRIPTION, re.IGNORECASE,
    ) or SUBSCRIPTION == "00000000-0000-0000-0000-000000000000":
        raise DeploymentError("Set AZURE_SUBSCRIPTION_ID to your approved subscription ID")


class AzureRequestError(DeploymentError):
    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(f"Azure request failed: HTTP {status}, code={code}")


def az(arguments: list[str], *, parse: bool = True) -> Any:
    executable = shutil.which("az")
    if executable is None:
        raise DeploymentError("Azure CLI is required")
    result = subprocess.run(
        [executable, *arguments, "--only-show-errors", "--output", "json"],
        cwd=ROOT, capture_output=True, text=True, timeout=1200, check=False,
    )
    if result.returncode:
        raise DeploymentError(f"Azure CLI {arguments[0]} failed: {result.stderr.strip()}")
    return json.loads(result.stdout) if parse else None


def azure_request(
    url: str, *, method: str = "GET", body: dict[str, Any] | None = None,
    scope: str = "https://management.azure.com/.default",
    if_match: str | None = None,
    response_metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    token = az(["account", "get-access-token", "--scope", scope])["accessToken"]
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    if scope == "https://ai.azure.com/.default":
        headers["Foundry-Features"] = "VoiceAgents=V1Preview"
    if if_match is not None:
        headers["If-Match"] = if_match
    request = urllib.request.Request(
        url, method=method, headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if response_metadata is not None:
                etag = response.headers.get("ETag")
                if etag:
                    response_metadata["etag"] = etag
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read())
            detail = payload.get("error", {}) if isinstance(payload, dict) else {}
            inner = detail.get("innererror", {}) if isinstance(detail, dict) else {}
            code = str(
                inner.get("code") or detail.get("code", "unknown")
            ) if isinstance(inner, dict) and isinstance(detail, dict) else "unknown"
        except json.JSONDecodeError:
            code = "unknown"
        raise AzureRequestError(error.code, code) from None
    except urllib.error.URLError as error:
        raise DeploymentError(f"Could not reach Azure: {error.reason}") from error


def app_tokens(*, create: bool = False) -> dict[str, str]:
    """Read encrypted platform secrets through authorized ARM, never via Key Vault."""
    url = f"https://management.azure.com{APP_ID}/listSecrets?api-version=2025-01-01"
    try:
        response = azure_request(url, method="POST", body={})
    except AzureRequestError as error:
        if error.status == 404 and create:
            return {"mcp-token": secrets.token_hex(32), "otp-admin-token": secrets.token_hex(32)}
        raise
    values = {
        item["name"]: item["value"]
        for item in response.get("value", [])
        if item.get("name") in {"mcp-token", "otp-admin-token"} and isinstance(item.get("value"), str)
    }
    if set(values) != {"mcp-token", "otp-admin-token"} or any(len(value) < 32 for value in values.values()):
        raise DeploymentError("Existing app secrets are missing or invalid; refusing to rotate them implicitly")
    if secrets.compare_digest(values["mcp-token"], values["otp-admin-token"]):
        raise DeploymentError("MCP and OTP administrator secrets must be different")
    return values


def provision(image: str, tokens: dict[str, str], *, preview: bool = False) -> dict[str, str]:
    from azure.core.exceptions import AzureError
    from azure.identity import AzureCliCredential
    from azure.mgmt.resource import ResourceManagementClient

    template = az(["bicep", "build", "--file", str(ROOT / "infra" / "demo.bicep"), "--stdout"])
    properties = {
        "mode": "Incremental",
        "template": template,
        "parameters": {
            "containerImage": {"value": image},
            "mcpToken": {"value": tokens["mcp-token"]},
            "otpAdminToken": {"value": tokens["otp-admin-token"]},
        },
    }
    # SDK keeps secureString values out of files, CLI arguments, and printed output.
    with AzureCliCredential() as credential, ResourceManagementClient(
        credential, SUBSCRIPTION, logging_enable=False
    ) as client:
        try:
            if preview:
                result = client.deployments.begin_what_if(
                    RESOURCE_GROUP, DEPLOYMENT_NAME, {"properties": properties}
                ).result(timeout=1200)
                if result.status != "Succeeded":
                    raise DeploymentError("Azure what-if did not succeed")
                for change in result.changes or []:
                    if change.change_type == "Delete":
                        raise DeploymentError("What-if proposed a deletion; deployment blocked")
                    prefix = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}/".lower()
                    if not change.resource_id.lower().startswith(prefix):
                        raise DeploymentError("What-if contains a resource outside the approved group")
                    print(f"What-if: {change.change_type} {change.resource_id}")
                return {}
            poller = client.deployments.begin_create_or_update(
                RESOURCE_GROUP, DEPLOYMENT_NAME, {"properties": properties}
            )
            for _ in range(80):
                if poller.done():
                    break
                print("Azure deployment is running...")
                time.sleep(15)
            if not poller.done():
                raise DeploymentPending(
                    "Azure is still provisioning. The existing deployment was not cancelled. "
                    "Run --resume to wait and verify it without rebuilding or deploying again."
                )
            return deployment_outputs(poller.result())
        except AzureError as error:
            raise DeploymentError(
                f"Azure template operation failed ({type(error).__name__}); "
                "inspect the deployment operations in Azure Portal"
            ) from None


def deployment_outputs(result: Any) -> dict[str, str]:
    properties = getattr(result, "properties", None)
    state = getattr(properties, "provisioning_state", None)
    if state in {"Running", "Accepted", "Creating", "Updating"} or properties is None:
        raise DeploymentPending("Azure deployment is still running; use --resume rather than redeploying")
    outputs = getattr(properties, "outputs", None)
    if state != "Succeeded" or not outputs:
        raise DeploymentError(
            f"Azure template deployment ended with state={state}; inspect its operations in Azure Portal"
        )
    return {name: output["value"] for name, output in outputs.items()}


def resume() -> dict[str, str]:
    from azure.identity import AzureCliCredential
    from azure.mgmt.resource import ResourceManagementClient

    with AzureCliCredential() as credential, ResourceManagementClient(
        credential, SUBSCRIPTION, logging_enable=False
    ) as client:
        for _ in range(40):
            result = client.deployments.get(RESOURCE_GROUP, DEPLOYMENT_NAME)
            try:
                outputs = deployment_outputs(result)
                break
            except DeploymentPending:
                print("Existing Azure deployment is still running; no new deployment started.")
                time.sleep(30)
        else:
            raise DeploymentPending("Azure deployment remains pending; no duplicate deployment was started")
    save_and_check(outputs)
    return outputs


def validate_base_url(base_url: str) -> str:
    if not re.fullmatch(r"https://[a-z0-9.-]+\.azurecontainerapps\.io", base_url):
        raise DeploymentError("Expected an HTTPS Azure Container Apps base URL")
    return base_url


def make_agent(base_url: str, *, source: dict[str, Any] | None = None) -> dict[str, Any]:
    base_url = validate_base_url(base_url)
    agent = (
        copy.deepcopy(source) if source is not None
        else json.loads((ROOT / "config" / "agent.json").read_text(encoding="utf-8"))
    )
    definition = agent["definition"]
    if definition.get("kind") != "voice" or "handoff" in definition:
        raise DeploymentError("Expected the existing flat Voice Agent definition")
    bindings = definition.get("tools", [])
    if len(bindings) != 1 or set(bindings[0].get("allowed_tools", [])) != TOOLS:
        raise DeploymentError("The existing agent does not have the required six-tool contract")
    bindings[0].update(
        server_label="contoso-bank-routing",
        server_url=base_url + "/mcp/contoso-bank",
        project_connection_id=CONNECTION,
    )
    definition["instructions"] = (ROOT / "mcp_server" / "agent_instructions.txt").read_text()
    agent["description"] = (
        "Contoso Bank broker support: OTP verification, full-directory name lookup, "
        "explicit confirmation, and simulated transfer using a customer-owned MCP."
    )
    return agent


def normalized(definition: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(definition)
    audio = value.get("audio", {})
    vad = audio.get("input", {}).get("turn_detection", {})
    if vad.get("remove_filler_words") is False:
        vad.pop("remove_filler_words")
    output = audio.get("output", {})
    if isinstance(output.get("voice"), str) and isinstance(output.get("voice_type"), str):
        output["voice"] = {"type": output.pop("voice_type"), "name": output["voice"]}
    return value


async def probe(base_url: str, mcp_token: str, admin_token: str) -> None:
    headers = {"Authorization": "Bearer " + mcp_token}
    for transport in (streamablehttp_client, sse_client):
        async with httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": "Bearer " + admin_token},
            timeout=30,
        ) as admin:
            candidates = await admin.get("/api/otp/candidates")
            if candidates.status_code != 200:
                raise DeploymentError("OTP administrator authentication failed")
            demo = candidates.json().get("permanent_demo", {})
            if demo.get("full_name") != "Avery Example":
                raise DeploymentError("The permanent fictional demo identity is incorrect")
            issuance = await admin.post("/api/otp", json={"candidate_id": demo["candidate_id"]})
            if issuance.status_code != 200:
                raise DeploymentError("OTP issuance failed")
            code = issuance.json()["otp"]
        async with transport(base_url + "/mcp/contoso-bank", headers=headers) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                inventory = await session.list_tools()
                if {tool.name for tool in inventory.tools} != TOOLS:
                    raise DeploymentError("MCP tool inventory does not match the six-tool agent")
                async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    result = await session.call_tool(name, arguments)
                    if result.isError:
                        raise DeploymentError(f"MCP {name} returned a tool error")
                    return json.loads(result.content[0].text)

                opened = await call("start_call", {})
                sid = opened.get("session_id")
                if not opened.get("ok") or not sid:
                    raise DeploymentError("MCP durable call creation failed")
                verified = await call("verify_caller_otp", {"session_id": sid, "otp": code})
                if not verified.get("authenticated") or verified.get("customer", {}).get("full_name") != "Avery Example":
                    raise DeploymentError("Generated OTP did not authenticate its bound demo identity")
                missing = await call("search_loan_officer", {"session_id": sid, "spoken_name": "Qzxv Plmok"})
                if not missing.get("ok") or missing.get("status") != "no_match" or missing.get("matches"):
                    raise DeploymentError("Unknown officer incorrectly matched the directory")
                ambiguous = await call("search_loan_officer", {"session_id": sid, "spoken_name": "Brennan"})
                if not ambiguous.get("ok") or ambiguous.get("status") != "multiple_matches":
                    raise DeploymentError("Ambiguous officer names did not require clarification")
                selected_ids = set()
                for requested in ("Michael Brennan", "Priya Raghavan"):
                    found = await call("search_loan_officer", {"session_id": sid, "spoken_name": requested})
                    matches = found.get("matches", [])
                    if (
                        not found.get("ok") or found.get("status") != "one_match"
                        or len(matches) != 1 or matches[0].get("full_name") != requested
                        or found.get("match_type") != "exact"
                    ):
                        raise DeploymentError("Requested officer was not matched exactly")
                    officer_id = matches[0]["officer_id"]
                    selected_ids.add(officer_id)
                if len(selected_ids) != 2:
                    raise DeploymentError("Different officer names resolved to the same person")
                confirmed = await call("confirm_loan_officer", {"session_id": sid, "officer_id": officer_id})
                if not confirmed.get("confirmed"):
                    raise DeploymentError("Officer confirmation failed")
                transferred = await call("transfer_to_officer", {"session_id": sid, "officer_id": officer_id})
                if not transferred.get("transferred") or not transferred.get("simulated"):
                    raise DeploymentError("Simulated transfer failed")
                replay_call = await call("start_call", {})
                replay_sid = replay_call["session_id"]
                replay = await call("verify_caller_otp", {"session_id": replay_sid, "otp": code})
                if replay.get("authenticated") or replay.get("status") != "consumed":
                    raise DeploymentError("Consumed OTP replay was not rejected")
                ended = await call(
                    "end_call", {"session_id": replay_sid, "reason": "other"}
                )
                if not ended.get("ended"):
                    raise DeploymentError("MCP call closure failed")
    print("MCP probes passed: six-tool inventory, generated OTP and replay protection, exact-name lookup, ambiguity, no-match, simulated transfer, both HTTP transports")


def check(outputs: dict[str, str]) -> None:
    base = validate_base_url(outputs["mcpBaseUrl"])
    for path in ("/healthz", "/readyz", "/otp"):
        with urllib.request.urlopen(base + path, timeout=30) as response:
            if response.status != 200:
                raise DeploymentError(f"{path} did not return HTTP 200")
    try:
        urllib.request.urlopen(base + "/mcp/contoso-bank", timeout=15)
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise DeploymentError(f"Unauthenticated MCP returned HTTP {error.code}") from error
    else:
        raise DeploymentError("MCP accepted an unauthenticated request")
    tokens = app_tokens()
    asyncio.run(probe(base, tokens["mcp-token"], tokens["otp-admin-token"]))


def foundry_context(env_file: Path) -> tuple[str, str]:
    values = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        parsed = shlex.split(value, comments=True)
        if not separator or len(parsed) != 1:
            raise DeploymentError("Project environment file must contain KEY=VALUE assignments")
        values[key] = parsed[0]
    endpoint = values.get("AZURE_VOICE_AGENTS_ENDPOINT", "").rstrip("/")
    project = values.get("AZURE_AI_PROJECT_ID", "")
    match = re.fullmatch(
        r"https://([a-z0-9-]+)\.services\.ai\.azure\.com/api/projects/([A-Za-z0-9._-]+)",
        endpoint,
    )
    expected_id = (
        rf"/subscriptions/{SUBSCRIPTION}/resourceGroups/[^/]+/providers/"
        rf"Microsoft\.CognitiveServices/accounts/{match[1]}/projects/{match[2]}"
        if match else ""
    )
    if not expected_id or not re.fullmatch(expected_id, project, flags=re.IGNORECASE):
        raise DeploymentError("Foundry project endpoint, ARM ID, and selected subscription must match")
    return endpoint, project


def cutover(outputs: dict[str, str], env_file: Path) -> None:
    endpoint, project = foundry_context(env_file)
    check(outputs)
    agent = make_agent(outputs["mcpBaseUrl"])
    token = app_tokens()["mcp-token"]
    azure_request(
        f"https://management.azure.com{project}/connections/{CONNECTION}?api-version=2025-06-01",
        method="PUT",
        body={"properties": {
            "category": "RemoteTool", "target": agent["definition"]["tools"][0]["server_url"],
            "authType": "CustomKeys", "credentials": {"keys": {"Authorization": "Bearer " + token}},
        }},
    )
    url = endpoint + "/voice_agents/" + agent["name"] + "?api-version=v1"
    existing = None
    try:
        existing = azure_request(url, scope="https://ai.azure.com/.default")
    except AzureRequestError as error:
        if error.status != 404:
            raise
    current = existing["versions"]["latest"]["definition"] if existing else None
    if current is not None:
        agent = make_agent(outputs["mcpBaseUrl"], source={**agent, "definition": current})
    if current is None or normalized(current) != normalized(agent["definition"]):
        azure_request(
            url if existing else endpoint + "/voice_agents?api-version=v1",
            method="POST", body=agent, scope="https://ai.azure.com/.default",
        )
    observed = azure_request(url, scope="https://ai.azure.com/.default")
    if normalized(observed["versions"]["latest"]["definition"]) != normalized(agent["definition"]):
        raise DeploymentError("Published agent readback does not match the customer-owned definition")
    AGENT_FILE.write_text(json.dumps(agent, indent=2) + "\n", encoding="utf-8")
    print("Foundry connection and voice agent verified. Original agent.json was not overwritten.")


def deploy() -> dict[str, str]:
    tokens = app_tokens(create=True)
    tag = "contoso-bank-mcp:" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    print("Building the MCP image in the existing Azure Container Registry...")
    az([
        "acr", "build", "--subscription", SUBSCRIPTION,
        "--registry", "crcontosomcpdevdemo", "--image", tag,
        "--file", "mcp_server/Dockerfile", "--no-logs", str(ROOT),
    ], parse=False)
    image = "crcontosomcpdevdemo.azurecr.io/" + tag
    print("Deploying the VNet-connected MCP with encrypted platform secrets...")
    outputs = provision(image, tokens)
    outputs["containerImage"] = image
    save_and_check(outputs)
    return outputs


def wait_for_revision(image: str, *, app_name: str = APP_NAME) -> None:
    for attempt in range(30):
        revisions = az([
            "containerapp", "revision", "list", "--subscription", SUBSCRIPTION,
            "--name", app_name, "--resource-group", RESOURCE_GROUP,
        ])
        for revision in revisions:
            properties = revision.get("properties", {})
            containers = properties.get("template", {}).get("containers", [])
            if (
                properties.get("active") and properties.get("healthState") == "Healthy"
                and properties.get("trafficWeight") == 100
                and any(container.get("image") == image for container in containers)
            ):
                return
        if attempt < 29:
            print("Waiting for the new MCP image to be healthy and receive all traffic...", flush=True)
            time.sleep(10)
    raise DeploymentPending("The new MCP image is not yet healthy with 100% traffic")


def save_and_check(outputs: dict[str, str]) -> None:
    OUTPUT_FILE.write_text(json.dumps(outputs, indent=2) + "\n", encoding="utf-8")
    if outputs.get("containerImage"):
        wait_for_revision(outputs["containerImage"])
    for attempt in range(20):
        try:
            check(outputs)
            break
        except (urllib.error.URLError, TimeoutError, AzureRequestError):
            if attempt == 19:
                raise
            print("Waiting for application readiness and role propagation...")
            time.sleep(15)
    AGENT_FILE.write_text(json.dumps(make_agent(outputs["mcpBaseUrl"]), indent=2) + "\n")
    print("MCP deployment verified. OTP console: " + outputs["mcpBaseUrl"] + "/otp")
    print("Retrieve otp-admin-token from Container Apps > Secrets in Azure Portal; do not share it in chat.")
    print("The current Foundry Voice Agent was not changed. --cutover is a separate operation.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--deploy", action="store_true", help="Provision hosting, build, deploy and verify; no Foundry changes")
    group.add_argument("--check", action="store_true", help="Verify an existing customer-owned deployment")
    group.add_argument("--cutover", action="store_true", help="Explicitly switch the existing Voice Agent to the verified MCP")
    group.add_argument("--preview", action="store_true", help="Read-only Azure what-if; no resource changes or image build")
    group.add_argument("--resume", action="store_true", help="Wait for and verify the existing deployment; never rebuild or redeploy")
    parser.add_argument("--env-file", type=Path, default=ROOT / "config" / "deploy.env")
    args = parser.parse_args()
    validate_subscription()
    account = az(["account", "show"])
    if account["id"].lower() != SUBSCRIPTION:
        raise DeploymentError("Azure CLI is not using the approved subscription; select it before continuing")
    if args.resume:
        resume()
    elif args.preview:
        provision("crcontosomcpdevdemo.azurecr.io/contoso-bank-mcp:preview", app_tokens(create=True), preview=True)
    elif args.deploy:
        deploy()
    else:
        if not OUTPUT_FILE.exists():
            raise DeploymentError("No saved MCP deployment outputs; deploy the server first")
        outputs = json.loads(OUTPUT_FILE.read_text())
        if args.check:
            check(outputs)
        else:
            cutover(outputs, args.env_file)


if __name__ == "__main__":
    try:
        main()
    except DeploymentPending as error:
        print(f"PENDING: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    except (DeploymentError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from None
