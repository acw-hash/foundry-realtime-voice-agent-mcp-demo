from __future__ import annotations

import copy
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("contoso_deploy", ROOT / "mcp_server" / "deploy.py")
assert SPEC is not None and SPEC.loader is not None
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class DeployConfigurationTests(unittest.TestCase):
    def test_subscription_must_be_explicit_before_any_azure_command(self) -> None:
        for value in ("", "<subscription-id>", "00000000-0000-0000-0000-000000000000"):
            with (
                self.subTest(value=value),
                patch.object(deploy, "SUBSCRIPTION", value),
                patch.object(deploy, "az") as command,
                patch("sys.argv", ["deploy.py", "--check"]),
                self.assertRaisesRegex(deploy.DeploymentError, "AZURE_SUBSCRIPTION_ID"),
            ):
                deploy.main()
            command.assert_not_called()
        with patch.object(deploy, "SUBSCRIPTION", "11111111-2222-4333-8444-555555555555"):
            deploy.validate_subscription()

    def test_opt_in_config_preserves_voice_settings_and_original_file(self) -> None:
        path = ROOT / "config" / "agent.json"
        original_bytes = path.read_bytes()
        original = json.loads(original_bytes)
        changed = deploy.make_agent("https://contoso.example.eastus2.azurecontainerapps.io")
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(changed["name"], original["name"])
        for key in ("kind", "model", "model_type", "audio", "greeting", "store", "tool_choice"):
            self.assertEqual(changed["definition"][key], original["definition"][key])
        tool = changed["definition"]["tools"][0]
        self.assertEqual(set(tool["allowed_tools"]), deploy.TOOLS)
        self.assertEqual(tool["project_connection_id"], "contoso-bank-mcp")
        self.assertTrue(tool["server_url"].endswith("/mcp/contoso-bank"))
        instructions = changed["definition"]["instructions"]
        self.assertIn("Search the directory", instructions)
        self.assertIn("For multiple_matches", instructions)
        self.assertIn("Do not volunteer that this is a demo", instructions)
        self.assertIn("I've recorded", instructions)
        self.assertIn("If the result has simulated=true", instructions)
        self.assertNotIn("Tell the caller briefly that this demo", instructions)
        self.assertNotIn("random demo directory placeholder", instructions)
        self.assertIn("Speak English by default", changed["definition"]["instructions"])

    def test_existing_live_voice_settings_are_preserved_without_mutating_the_source(self) -> None:
        source = json.loads((ROOT / "config" / "agent.json").read_text())
        definition = source["definition"]
        definition["sub_protocol"] = "realtime"
        definition["audio"]["input"]["turn_detection"]["threshold"] = 0.6
        definition["store"] = False
        before = copy.deepcopy(source)
        changed = deploy.make_agent(
            "https://contoso.example.eastus2.azurecontainerapps.io", source=source
        )
        self.assertEqual(source, before)
        for key in definition:
            if key not in {"instructions", "tools"}:
                self.assertEqual(changed["definition"][key], definition[key])

    def test_cutover_builds_from_the_live_definition_not_the_rollback_template(self) -> None:
        source = json.loads((ROOT / "config" / "agent.json").read_text())
        source["definition"]["sub_protocol"] = "realtime"
        source["definition"]["audio"]["input"]["turn_detection"]["threshold"] = 0.6
        outputs = {"mcpBaseUrl": "https://contoso.example.eastus2.azurecontainerapps.io"}
        expected = deploy.make_agent(outputs["mcpBaseUrl"], source=source)
        existing = {"versions": {"latest": {"definition": source["definition"], "version": "4"}}}
        observed = {"versions": {"latest": {"definition": expected["definition"], "version": "5"}}}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            env_file = path / "deploy.env"
            env_file.write_text(
                "AZURE_VOICE_AGENTS_ENDPOINT=https://fixture.services.ai.azure.com/api/projects/proj\n"
                f"AZURE_AI_PROJECT_ID=/subscriptions/{deploy.SUBSCRIPTION}/resourceGroups/fixture/"
                "providers/Microsoft.CognitiveServices/accounts/fixture/projects/proj\n"
            )
            agent_file = path / "agent.json"
            with (
                patch.object(deploy, "check"),
                patch.object(deploy, "app_tokens", return_value={"mcp-token": "a" * 64}),
                patch.object(deploy, "azure_request", side_effect=[{}, existing, {}, observed]) as request,
                patch.object(deploy, "AGENT_FILE", agent_file),
                patch("builtins.print"),
            ):
                deploy.cutover(outputs, env_file)
            published = request.call_args_list[2].kwargs["body"]
            self.assertEqual(published["definition"], expected["definition"])
            self.assertEqual(json.loads(agent_file.read_text())["definition"], expected["definition"])

    def test_cutover_rejects_non_azure_endpoint_before_credentials_are_used(self) -> None:
        for url in (
            "http://example.azurecontainerapps.io", "https://attacker.example",
            "https://x.azurecontainerapps.io/other", "https://x.azurecontainerapps.io?secret=x",
        ):
            with self.subTest(url=url), self.assertRaises(deploy.DeploymentError):
                deploy.make_agent(url)

    def test_normalization_matches_existing_deployment_contract(self) -> None:
        original = json.loads((ROOT / "config" / "agent.json").read_text())["definition"]
        observed = copy.deepcopy(original)
        output = observed["audio"]["output"]
        output["voice_type"] = output["voice"]["type"]
        output["voice"] = output["voice"]["name"]
        observed["audio"]["input"]["turn_detection"]["remove_filler_words"] = False
        self.assertEqual(deploy.normalized(original), deploy.normalized(observed))

    def test_deployment_uses_platform_secrets_without_key_vault_or_cutover(self) -> None:
        outputs = {
            "resourceGroupName": "rg-contoso-bank-mcp",
            "registryName": "crcontosomcpdevdemo",
            "registryLoginServer": "crcontosomcpdevdemo.azurecr.io",
            "mcpBaseUrl": "https://contoso.example.eastus2.azurecontainerapps.io",
        }
        calls = []

        def command(arguments, *, parse=True):
            calls.append(arguments)
            if arguments[:2] == ["acr", "build"]:
                return None
            self.fail(f"Unexpected CLI command: {arguments}")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            with (
                patch.object(deploy, "az", side_effect=command),
                patch.object(deploy, "app_tokens", return_value={
                    "mcp-token": "a" * 64, "otp-admin-token": "b" * 64,
                }),
                patch.object(deploy, "provision", return_value=outputs) as provision,
                patch.object(deploy, "check"),
                patch.object(deploy, "wait_for_revision") as wait_for_revision,
                patch("builtins.print"),
                patch.object(deploy, "cutover") as cutover,
                patch.object(deploy, "OUTPUT_FILE", path / "outputs.json"),
                patch.object(deploy, "AGENT_FILE", path / "agent.json"),
            ):
                deploy.deploy()
                cutover.assert_not_called()
        provision.assert_called_once()
        self.assertTrue(provision.call_args.args[0].startswith("crcontosomcpdevdemo.azurecr.io/"))
        wait_for_revision.assert_called_once_with(provision.call_args.args[0])
        self.assertEqual(provision.call_args.args[1]["mcp-token"], "a" * 64)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("a" * 64, str(calls))
        self.assertNotIn("b" * 64, str(calls))

    def test_revision_gate_waits_for_the_requested_image_to_receive_all_traffic(self) -> None:
        def revision(image: str, traffic: int) -> list[dict]:
            return [{"properties": {
                "active": True, "healthState": "Healthy", "trafficWeight": traffic,
                "template": {"containers": [{"image": image}]},
            }}]

        with (
            patch.object(deploy, "az", side_effect=[
                revision("old-image", 100), revision("new-image", 0), revision("new-image", 100),
            ]) as command,
            patch.object(deploy.time, "sleep") as sleep,
            patch("builtins.print"),
        ):
            deploy.wait_for_revision("new-image")
        self.assertEqual(command.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_access_denial_does_not_generate_replacement_secrets(self) -> None:
        with (
            patch.object(deploy, "azure_request",
                         side_effect=deploy.AzureRequestError(403, "ForbiddenByConnection")) as request,
            patch.object(deploy.secrets, "token_hex") as generate,
        ):
            with self.assertRaisesRegex(deploy.AzureRequestError, "ForbiddenByConnection"):
                deploy.app_tokens(create=True)
            self.assertEqual(request.call_count, 1)
            generate.assert_not_called()

    def test_new_app_generates_two_independent_credentials_only_when_not_found(self) -> None:
        with (
            patch.object(deploy, "azure_request", side_effect=deploy.AzureRequestError(404, "NotFound")),
        ):
            values = deploy.app_tokens(create=True)
            self.assertEqual(set(values), {"mcp-token", "otp-admin-token"})
            self.assertEqual(len(values["mcp-token"]), 64)
            self.assertNotEqual(values["mcp-token"], values["otp-admin-token"])
            with self.assertRaises(deploy.AzureRequestError):
                deploy.app_tokens()

    def test_existing_platform_secrets_are_reused_and_missing_values_fail(self) -> None:
        values = {"mcp-token": "a" * 64, "otp-admin-token": "b" * 64}
        response = {"value": [{"name": key, "value": value} for key, value in values.items()]}
        with patch.object(deploy, "azure_request", return_value=response):
            self.assertEqual(deploy.app_tokens(create=True), values)
        with patch.object(deploy, "azure_request", return_value={"value": []}):
            with self.assertRaisesRegex(deploy.DeploymentError, "refusing to rotate"):
                deploy.app_tokens(create=True)

    def test_secure_parameters_remain_in_memory_and_existing_resources_are_referenced(self) -> None:
        bicep = (ROOT / "infra" / "demo.bicep").read_text()
        self.assertEqual(bicep.count("@secure()"), 2)
        self.assertIn("privateDnsZoneId: zone.id", bicep)
        self.assertIn("groupIds: ['table']", bicep)
        self.assertNotIn("keyVaultUrl", bicep)
        self.assertNotIn("Microsoft.KeyVault", bicep)
        for resource in ("identity", "registry", "storage", "logs", "insights"):
            self.assertRegex(bicep, rf"resource {resource} '[^']+' existing")
        self.assertIn("cpu: json('0.5')", bicep)

    def test_sdk_uses_incremental_template_deployment_with_secure_parameters(self) -> None:
        client = MagicMock()
        response = client.deployments.begin_create_or_update.return_value.result.return_value
        response.properties.provisioning_state = "Succeeded"
        response.properties.outputs = {"mcpBaseUrl": {"value": "https://fixture.azurecontainerapps.io"}}
        client.deployments.begin_create_or_update.return_value.done.return_value = True
        manager = MagicMock()
        manager.__enter__.return_value = client
        tokens = {"mcp-token": "a" * 64, "otp-admin-token": "b" * 64}
        with (
            patch.object(deploy, "az", return_value={"resources": []}),
            patch("azure.mgmt.resource.ResourceManagementClient", return_value=manager),
            patch("azure.identity.AzureCliCredential"),
        ):
            result = deploy.provision("fixture-image", tokens)
        self.assertEqual(result, {"mcpBaseUrl": "https://fixture.azurecontainerapps.io"})
        arguments = client.deployments.begin_create_or_update.call_args.args
        self.assertEqual(arguments[:2], (deploy.RESOURCE_GROUP, deploy.DEPLOYMENT_NAME))
        properties = arguments[2]["properties"]
        self.assertEqual(properties["mode"], "Incremental")
        self.assertEqual(properties["parameters"]["mcpToken"]["value"], "a" * 64)
        self.assertEqual(properties["parameters"]["otpAdminToken"]["value"], "b" * 64)

    def test_preview_rejects_deletions_and_never_invokes_deployment(self) -> None:
        client = MagicMock()
        result = client.deployments.begin_what_if.return_value.result.return_value
        result.status = "Succeeded"
        change = MagicMock()
        change.change_type = "Delete"
        result.changes = [change]
        manager = MagicMock()
        manager.__enter__.return_value = client
        with (
            patch.object(deploy, "az", return_value={"resources": []}),
            patch("azure.mgmt.resource.ResourceManagementClient", return_value=manager),
            patch("azure.identity.AzureCliCredential"),
        ):
            with self.assertRaisesRegex(deploy.DeploymentError, "deletion"):
                deploy.provision("fixture-image", {
                    "mcp-token": "a" * 64, "otp-admin-token": "b" * 64,
                }, preview=True)
        client.deployments.begin_create_or_update.assert_not_called()

    def test_missing_or_running_sdk_result_is_pending_not_attribute_error(self) -> None:
        for result in (None, type("Result", (), {"properties": None})()):
            with self.subTest(result=result), self.assertRaises(deploy.DeploymentPending):
                deploy.deployment_outputs(result)
        result = MagicMock()
        result.properties.provisioning_state = "Running"
        with self.assertRaises(deploy.DeploymentPending):
            deploy.deployment_outputs(result)

    def test_resume_only_reads_existing_deployment_then_verifies(self) -> None:
        client = MagicMock()
        running = MagicMock()
        running.properties.provisioning_state = "Running"
        succeeded = MagicMock()
        succeeded.properties.provisioning_state = "Succeeded"
        succeeded.properties.outputs = {"mcpBaseUrl": {"value": "https://fixture.azurecontainerapps.io"}}
        client.deployments.get.side_effect = [running, succeeded]
        manager = MagicMock()
        manager.__enter__.return_value = client
        with (
            patch("azure.mgmt.resource.ResourceManagementClient", return_value=manager),
            patch("azure.identity.AzureCliCredential"),
            patch.object(deploy, "save_and_check") as verify,
            patch.object(deploy, "az") as command,
            patch.object(deploy.time, "sleep"),
            patch("builtins.print"),
        ):
            deploy.resume()
        command.assert_not_called()
        client.deployments.begin_create_or_update.assert_not_called()
        verify.assert_called_once_with({"mcpBaseUrl": "https://fixture.azurecontainerapps.io"})

    def test_azure_error_preserves_network_inner_code_without_secret_values(self) -> None:
        error = urllib.error.HTTPError(
            "https://fixture.vault.azure.net/secrets/mcp-token", 403, "Forbidden", {},
            io.BytesIO(json.dumps({
                "error": {"code": "Forbidden", "innererror": {"code": "ForbiddenByConnection"}}
            }).encode()),
        )
        with (
            patch.object(deploy, "az", return_value={"accessToken": "fixture-only"}),
            patch.object(deploy.urllib.request, "urlopen", side_effect=error),
        ):
            with self.assertRaises(deploy.AzureRequestError) as raised:
                deploy.azure_request("https://fixture.vault.azure.net/secrets/mcp-token")
            self.assertEqual(raised.exception.code, "ForbiddenByConnection")


if __name__ == "__main__":
    unittest.main()
