from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx
from azure.identity import AzureCliCredential
from azure.core.exceptions import AzureError
from azure.mgmt.resource import ResourceManagementClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp_server import deploy as hosting
from debug_agent import load_env_file


GROUP = hosting.RESOURCE_GROUP
APP = "ca-contoso-voice-bridge"
APP_ID = (
    f"/subscriptions/{hosting.SUBSCRIPTION}/resourceGroups/{GROUP}"
    f"/providers/Microsoft.App/containerApps/{APP}"
)
ACS_ID = (
    f"/subscriptions/{hosting.SUBSCRIPTION}/resourceGroups/{GROUP}"
    "/providers/Microsoft.Communication/communicationServices/acs-contoso-bank-demo"
)
OUTPUT_FILE = ROOT / "config" / "telephony.deployment.json"
DEPLOYMENT = "contoso-voice-bridge"


def selected_agent(requested: str | None = None) -> str:
    if requested is None:
        try:
            current = hosting.azure_request(
                f"https://management.azure.com{APP_ID}?api-version=2025-01-01"
            )
        except hosting.AzureRequestError as error:
            if error.status != 404:
                raise
            requested = json.loads((ROOT / "config" / "agent.json").read_text())["name"]
        else:
            containers = current.get("properties", {}).get("template", {}).get("containers", [])
            matches = [
                variable.get("value")
                for container in containers if container.get("name") == "voice-bridge"
                for variable in container.get("env", [])
                if variable.get("name") == "FOUNDRY_AGENT_NAME"
            ]
            if len(matches) != 1:
                raise hosting.DeploymentError("Existing bridge has no unique Foundry agent target")
            requested = matches[0]
    if not isinstance(requested, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", requested):
        raise hosting.DeploymentError("Invalid Foundry agent name")
    return requested


def project_context(values: dict[str, str]) -> str:
    endpoint = values.get("AZURE_VOICE_AGENTS_ENDPOINT", "").rstrip("/")
    project_id = values.get("AZURE_AI_PROJECT_ID", "")
    match = re.fullmatch(
        r"https://([a-z0-9-]+)\.services\.ai\.azure\.com/api/projects/([A-Za-z0-9._-]+)",
        endpoint,
    )
    expected = (
        rf"/subscriptions/{hosting.SUBSCRIPTION}/resourceGroups/rg-contoso-foundry-example/"
        rf"providers/Microsoft\.CognitiveServices/accounts/{match[1]}/projects/{match[2]}"
        if match else ""
    )
    if not expected or not re.fullmatch(expected, project_id, re.IGNORECASE):
        raise hosting.DeploymentError("Foundry endpoint and project ARM ID must match the approved project scope")
    return project_id


def event_token() -> str:
    try:
        document = hosting.azure_request(
            f"https://management.azure.com{APP_ID}/listSecrets?api-version=2025-01-01",
            method="POST", body={},
        )
    except hosting.AzureRequestError as error:
        if error.status == 404:
            return secrets.token_hex(32)
        raise
    for item in document.get("value", []):
        if item.get("name") == "eventgrid-token" and len(item.get("value", "")) >= 32:
            return item["value"]
    raise hosting.DeploymentError("Existing bridge is missing its Event Grid secret; refusing implicit rotation")


def provision(
    image: str, secret: str, values: dict[str, str], *, agent_name: str,
) -> dict[str, str]:
    phone_number = values.get("ACS_PHONE_NUMBER", "")
    immutable_id = values.get("ACS_IMMUTABLE_ID", "")
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone_number):
        raise hosting.DeploymentError("Set ACS_PHONE_NUMBER to your owned number in E.164 format")
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        immutable_id, re.IGNORECASE,
    ) or immutable_id == "00000000-0000-0000-0000-000000000000":
        raise hosting.DeploymentError("Set ACS_IMMUTABLE_ID to your ACS resource's immutable ID")
    template = hosting.az([
        "bicep", "build", "--file", str(ROOT / "infra" / "telephony.bicep"), "--stdout",
    ])
    keys = hosting.azure_request(
        f"https://management.azure.com{ACS_ID}/listKeys?api-version=2025-09-01",
        method="POST", body={},
    )
    parameters = {
        "containerImage": image, "eventGridToken": secret, "acsAccessKey": keys["primaryKey"],
        "foundryProjectEndpoint": values["AZURE_VOICE_AGENTS_ENDPOINT"].rstrip("/"),
        "foundryAgentName": agent_name,
        "phoneNumber": phone_number,
        "acsImmutableId": immutable_id,
    }
    with AzureCliCredential() as credential, ResourceManagementClient(
        credential, hosting.SUBSCRIPTION, logging_enable=False,
    ) as client:
        poller = client.deployments.begin_create_or_update(
            GROUP, DEPLOYMENT, {"properties": {
                "mode": "Incremental", "template": template,
                "parameters": {key: {"value": value} for key, value in parameters.items()},
            }},
        )
        for attempt in range(60):
            if poller.done():
                break
            print("Waiting for the bridge deployment...", flush=True)
            time.sleep(20)
        else:
            raise hosting.DeploymentPending("Bridge deployment is still running; use --resume, not --deploy")
        outputs = hosting.deployment_outputs(poller.result())
    return {**outputs, "containerImage": image}


def grant_foundry(outputs: dict[str, str], project_id: str) -> None:
    role = f"/subscriptions/{hosting.SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/53ca6127-db72-4b80-b1b0-d745d6d5456d"
    assignment_id = str(uuid5(NAMESPACE_URL, project_id.lower() + outputs["identityPrincipalId"]))
    hosting.azure_request(
        f"https://management.azure.com{project_id}/providers/Microsoft.Authorization/roleAssignments/{assignment_id}?api-version=2022-04-01",
        method="PUT", body={"properties": {
            "roleDefinitionId": role, "principalId": outputs["identityPrincipalId"],
            "principalType": "ServicePrincipal",
        }},
    )
    print("Bridge identity has Foundry User on the existing project only.", flush=True)


async def check(outputs: dict[str, str], secret: str) -> None:
    async with httpx.AsyncClient(base_url=outputs["bridgeUrl"], timeout=100) as client:
        response = await client.get("/healthz")
        if response.status_code != 200:
            raise hosting.DeploymentError("Bridge health probe failed")
        rejected = await client.post("/events", json=[])
        if rejected.status_code != 401:
            raise hosting.DeploymentError("Unauthenticated event delivery was not rejected")
        headers = {"Authorization": "Bearer " + secret}
        validation = await client.post("/events", headers=headers, json=[{
            "eventType": "Microsoft.EventGrid.SubscriptionValidationEvent",
            "data": {"validationCode": "deployment-probe"},
        }])
        if validation.status_code != 200 or validation.json() != {"validationResponse": "deployment-probe"}:
            raise hosting.DeploymentError("Authenticated Event Grid validation failed")
        for attempt in range(18):
            response = await client.post("/check", headers=headers)
            if response.status_code == 200 and response.json().get("audio_delivered") is True:
                if (
                    outputs.get("foundryAgentName")
                    and response.json().get("agent_name") != outputs["foundryAgentName"]
                ):
                    raise hosting.DeploymentError("Bridge diagnostic reached a different Foundry agent")
                print("Bridge verified: authenticated event delivery and managed-identity Foundry session with audio.", flush=True)
                return
            if attempt < 17:
                print("Waiting for bridge readiness and project permission propagation...", flush=True)
                await asyncio.sleep(10)
        raise hosting.DeploymentError("Bridge could not establish a Foundry session with audio")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy only the ACS-to-Foundry media bridge")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--deploy", action="store_true")
    group.add_argument("--check", action="store_true")
    group.add_argument("--resume", action="store_true")
    parser.add_argument("--agent-name", help="APPROVAL REQUIRED: explicitly change the phone target with --deploy")
    args = parser.parse_args()
    if args.agent_name and not args.deploy:
        parser.error("--agent-name is supported only with --deploy")
    if args.agent_name:
        selected_agent(args.agent_name)
    hosting.validate_subscription()
    account = hosting.az(["account", "show"])
    if account["id"].lower() != hosting.SUBSCRIPTION:
        raise hosting.DeploymentError("Azure CLI subscription does not match the approved demo")
    values = load_env_file(ROOT / "config" / "deploy.env")
    project_id = project_context(values)
    secret = event_token()
    if args.deploy:
        agent_name = selected_agent(args.agent_name)
        tag = "contoso-voice-bridge:" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        print("Building the isolated bridge image in the existing registry...", flush=True)
        hosting.az([
            "acr", "build", "--subscription", hosting.SUBSCRIPTION,
            "--registry", "crcontosomcpdevdemo", "--image", tag,
            "--file", "telephony_bridge/Dockerfile", "--no-logs", str(ROOT),
        ], parse=False)
        outputs = provision(
            "crcontosomcpdevdemo.azurecr.io/" + tag, secret, values, agent_name=agent_name,
        )
        OUTPUT_FILE.write_text(json.dumps(outputs, indent=2) + "\n", encoding="utf-8")
        grant_foundry(outputs, project_id)
    elif args.resume:
        with AzureCliCredential() as credential, ResourceManagementClient(
            credential, hosting.SUBSCRIPTION, logging_enable=False,
        ) as client:
            result = client.deployments.get(GROUP, DEPLOYMENT)
        outputs = hosting.deployment_outputs(result)
        OUTPUT_FILE.write_text(json.dumps(outputs, indent=2) + "\n", encoding="utf-8")
        grant_foundry(outputs, project_id)
    else:
        outputs = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    hosting.wait_for_revision(outputs["containerImage"], app_name=APP)
    asyncio.run(check(outputs, secret))
    print(f"Dial {outputs['phoneNumber']} for real-phone acceptance testing.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except AzureError as error:
        print(
            f"Azure bridge operation failed ({type(error).__name__}); inspect deployment {DEPLOYMENT} in {GROUP}.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except (hosting.DeploymentError, httpx.HTTPError, OSError, ValueError) as error:
        print(f"Bridge deployment/check failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
