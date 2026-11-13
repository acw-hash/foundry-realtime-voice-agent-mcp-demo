from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from telephony_bridge import deploy


ROOT = Path(__file__).resolve().parents[2]
PROJECT = (
    f"/subscriptions/{deploy.hosting.SUBSCRIPTION}/resourceGroups/rg-contoso-foundry-example"
    "/providers/Microsoft.CognitiveServices/accounts/contoso-foundry-example/projects/contoso-project-example"
)


class DeploymentTests(unittest.TestCase):
    def test_deployment_requires_local_phone_settings_before_azure_calls(self):
        for values, expected in (
            ({}, "ACS_PHONE_NUMBER"),
            ({"ACS_PHONE_NUMBER": "not-a-number"}, "ACS_PHONE_NUMBER"),
            ({"ACS_PHONE_NUMBER": "+12025550100"}, "ACS_IMMUTABLE_ID"),
            ({"ACS_PHONE_NUMBER": "+12025550100", "ACS_IMMUTABLE_ID": "invalid"}, "ACS_IMMUTABLE_ID"),
            ({
                "ACS_PHONE_NUMBER": "+12025550100",
                "ACS_IMMUTABLE_ID": "00000000-0000-0000-0000-000000000000",
            }, "ACS_IMMUTABLE_ID"),
        ):
            with (
                self.subTest(values=values),
                patch.object(deploy.hosting, "az") as command,
                patch.object(deploy.hosting, "azure_request") as request,
                self.assertRaisesRegex(deploy.hosting.DeploymentError, expected),
            ):
                deploy.provision("image", "test-secret", values, agent_name="voice-agent")
            command.assert_not_called()
            request.assert_not_called()

    def test_template_requires_phone_settings_without_live_defaults(self):
        template = (ROOT / "infra" / "telephony.bicep").read_text()
        self.assertIn("param phoneNumber string\n", template)
        self.assertIn("param acsImmutableId string\n", template)
        self.assertNotIn("param phoneNumber string =", template)
        self.assertNotIn("param acsImmutableId string =", template)

    def test_deployment_supplies_local_phone_settings_as_parameters(self):
        values = {
            "AZURE_VOICE_AGENTS_ENDPOINT": "https://fixture.services.ai.azure.com/api/projects/proj",
            "ACS_PHONE_NUMBER": "+12025550100",
            "ACS_IMMUTABLE_ID": "11111111-2222-4333-8444-555555555555",
        }
        with (
            patch.object(deploy.hosting, "az", return_value={}),
            patch.object(deploy.hosting, "azure_request", return_value={"primaryKey": "fixture-key"}),
            patch.object(deploy, "AzureCliCredential"),
            patch.object(deploy, "ResourceManagementClient") as management,
            patch.object(deploy.hosting, "deployment_outputs", return_value={
                "phoneNumber": values["ACS_PHONE_NUMBER"],
            }),
        ):
            client = management.return_value.__enter__.return_value
            client.deployments.begin_create_or_update.return_value.done.return_value = True
            outputs = deploy.provision("image", "fixture-event-token", values, agent_name="voice-agent")
            parameters = client.deployments.begin_create_or_update.call_args.args[2]["properties"]["parameters"]
        self.assertEqual(parameters["phoneNumber"]["value"], values["ACS_PHONE_NUMBER"])
        self.assertEqual(parameters["acsImmutableId"]["value"], values["ACS_IMMUTABLE_ID"])
        self.assertEqual(parameters["foundryAgentName"]["value"], "voice-agent")
        self.assertEqual(parameters["acsAccessKey"]["value"], "fixture-key")
        self.assertEqual(parameters["eventGridToken"]["value"], "fixture-event-token")
        self.assertEqual(outputs, {"phoneNumber": values["ACS_PHONE_NUMBER"], "containerImage": "image"})

    def test_existing_phone_target_is_preserved_and_changes_are_explicit(self):
        current = {"properties": {"template": {"containers": [{
            "name": "voice-bridge",
            "env": [{"name": "FOUNDRY_AGENT_NAME", "value": "isolated-candidate"}],
        }]}}}
        with patch.object(deploy.hosting, "azure_request", return_value=current):
            self.assertEqual(deploy.selected_agent(), "isolated-candidate")
        with patch.object(deploy.hosting, "azure_request") as request:
            self.assertEqual(deploy.selected_agent("original-agent"), "original-agent")
            with self.assertRaises(deploy.hosting.DeploymentError):
                deploy.selected_agent("../invalid")
        request.assert_not_called()
        with patch.object(deploy.hosting, "azure_request", side_effect=deploy.hosting.AzureRequestError(403, "Forbidden")):
            with self.assertRaises(deploy.hosting.AzureRequestError):
                deploy.selected_agent()
        with patch.object(deploy.hosting, "azure_request", return_value={}):
            with self.assertRaises(deploy.hosting.DeploymentError):
                deploy.selected_agent()
        with patch.object(deploy.hosting, "azure_request", side_effect=deploy.hosting.AzureRequestError(404, "NotFound")):
            original = json.loads((ROOT / "config" / "agent.json").read_text())["name"]
            self.assertEqual(deploy.selected_agent(), original)

    def test_diagnostic_rejects_audio_from_the_wrong_phone_target(self):
        def respond(request):
            if request.url.path == "/healthz":
                return httpx.Response(200)
            if request.url.path == "/events":
                if "authorization" not in request.headers:
                    return httpx.Response(401)
                return httpx.Response(200, json={"validationResponse": "deployment-probe"})
            return httpx.Response(200, json={"audio_delivered": True, "agent_name": "wrong-agent"})

        client = httpx.AsyncClient(
            base_url="https://bridge.example", transport=httpx.MockTransport(respond)
        )
        with (
            patch.object(deploy.httpx, "AsyncClient", return_value=client),
            self.assertRaises(deploy.hosting.DeploymentError),
        ):
            asyncio.run(deploy.check({
                "bridgeUrl": "https://bridge.example", "foundryAgentName": "expected-agent",
            }, "test-secret"))

    def test_project_endpoint_and_scope_must_agree(self):
        values = {
            "AZURE_VOICE_AGENTS_ENDPOINT": (
                "https://contoso-foundry-example.services.ai.azure.com/api/projects/contoso-project-example"
            ),
            "AZURE_AI_PROJECT_ID": PROJECT,
        }
        self.assertEqual(deploy.project_context(values), PROJECT)
        for wrong in ("https://attacker.example/api/projects/contoso-project-example", values[
            "AZURE_VOICE_AGENTS_ENDPOINT"
        ].replace("contoso-project-example", "another-project")):
            with self.subTest(wrong=wrong), self.assertRaises(deploy.hosting.DeploymentError):
                deploy.project_context({**values, "AZURE_VOICE_AGENTS_ENDPOINT": wrong})

    def test_event_secret_is_reused_and_access_denial_never_rotates_it(self):
        secret = "s" * 64
        with patch.object(deploy.hosting, "azure_request", return_value={
            "value": [{"name": "eventgrid-token", "value": secret}],
        }):
            self.assertEqual(deploy.event_token(), secret)
        with (
            patch.object(deploy.hosting, "azure_request",
                         side_effect=deploy.hosting.AzureRequestError(403, "Forbidden")),
            patch.object(deploy.secrets, "token_hex") as generate,
            self.assertRaises(deploy.hosting.AzureRequestError),
        ):
            deploy.event_token()
        generate.assert_not_called()

    def test_foundry_role_is_limited_to_the_selected_project(self):
        principal = "11111111-2222-4333-8444-555555555555"
        with patch.object(deploy.hosting, "azure_request") as request:
            deploy.grant_foundry({"identityPrincipalId": principal}, PROJECT)
        url = request.call_args.args[0]
        self.assertTrue(url.startswith("https://management.azure.com" + PROJECT + "/providers/"))
        properties = request.call_args.kwargs["body"]["properties"]
        self.assertEqual(properties["principalId"], principal)
        self.assertEqual(properties["principalType"], "ServicePrincipal")
        self.assertTrue(properties["roleDefinitionId"].endswith("53ca6127-db72-4b80-b1b0-d745d6d5456d"))

    def test_rollout_gate_targets_bridge_not_mcp(self):
        with patch.object(deploy.hosting, "az", return_value=[{"properties": {
            "active": True, "healthState": "Healthy", "trafficWeight": 100,
            "template": {"containers": [{"image": "bridge-image"}]},
        }}]) as command:
            deploy.hosting.wait_for_revision("bridge-image", app_name=deploy.APP)
        arguments = command.call_args.args[0]
        self.assertEqual(arguments[arguments.index("--name") + 1], "ca-contoso-voice-bridge")

    def test_template_uses_only_existing_hosting_and_acs_and_secures_delivery(self):
        template = (ROOT / "infra" / "telephony.bicep").read_text()
        self.assertEqual(template.count("@secure()"), 2)
        self.assertIn("isSecret: true", template)
        self.assertIn("Microsoft.Communication.IncomingCall", template)
        self.assertIn("minReplicas: 1, maxReplicas: 1", template)
        self.assertIn("output foundryAgentName string = foundryAgentName", template)
        self.assertIn("cpu: json('0.25')", template)
        for resource in ("environment", "registry", "acs"):
            self.assertRegex(template, rf"resource {resource} '[^']+' existing")
        self.assertNotIn("otp-admin-token", template)
        self.assertNotIn("mcp-token", template)


if __name__ == "__main__":
    unittest.main()
