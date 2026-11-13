from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server import deploy, voice_tuning as tuning


ENDPOINT = "https://fixture.services.ai.azure.com/api/projects/proj"


def source_agent():
    return deploy.make_agent("https://fixture.eastus2.azurecontainerapps.io")


def live_state(experiment="silence-650"):
    return tuning.prepare(
        source_agent(), experiment, endpoint=ENDPOINT, version="5", etag='"v5"',
    )


class VoiceTuningTests(unittest.TestCase):
    def test_experiments_change_only_the_declared_field_without_mutating_the_source(self):
        for experiment in tuning.EXPERIMENTS:
            with self.subTest(experiment=experiment):
                source = source_agent()
                before = copy.deepcopy(source)
                state = tuning.prepare(source, experiment)
                candidate = copy.deepcopy(state["candidate_definition"])
                path = tuning.experiment_path(experiment)
                target, original = candidate, source["definition"]
                for part in path[:-1]:
                    target, original = target[part], original[part]
                if path[-1] in original:
                    target[path[-1]] = original[path[-1]]
                else:
                    target.pop(path[-1])
                self.assertEqual(candidate, source["definition"])
                self.assertEqual(source, before)
                tuning.validate_state(state)

    def test_speed_supports_both_existing_voice_shapes_and_preserves_unknown_settings(self):
        for legacy in (False, True):
            source = source_agent()
            output = source["definition"]["audio"]["output"]
            if legacy:
                output["voice_type"] = output["voice"]["type"]
                output["voice"] = output["voice"]["name"]
            source["definition"]["future_setting"] = {"preserve": True}
            tuned = tuning.prepare(source, "speech-speed")["candidate_definition"]
            self.assertEqual(tuned["audio"]["output"], {**output, "speed": 1.05})
            self.assertEqual(tuned["future_setting"], {"preserve": True})

    def test_dialogue_preserves_all_original_instructions_and_required_wording(self):
        source = source_agent()
        state = tuning.prepare(source, "dialogue")
        instructions = state["candidate_definition"]["instructions"]
        self.assertEqual(instructions, source["definition"]["instructions"] + tuning.STYLE)
        self.assertIn("Identity verified", instructions)
        self.assertIn("If the result has simulated=true", instructions)
        self.assertIn("Only after an explicit caller yes", instructions)

    def test_rejects_wrong_agent_backend_tampering_and_incompatible_settings(self):
        for change in ("kind", "tools", "vad", "speed"):
            source = source_agent()
            if change == "kind":
                source["definition"]["kind"] = "prompt"
            elif change == "tools":
                source["definition"]["tools"][0]["project_connection_id"] = "other"
            elif change == "vad":
                source["definition"]["audio"]["input"]["turn_detection"]["type"] = "server_vad"
            else:
                source["definition"]["audio"]["output"]["speed"] = 1.2
            with self.subTest(change=change), self.assertRaises(deploy.DeploymentError):
                tuning.prepare(source, "speech-speed" if change == "speed" else "silence-650")
        state = live_state()
        state["candidate_definition"]["tools"] = []
        with self.assertRaises(deploy.DeploymentError):
            tuning.validate_state(state)
        with self.assertRaises(deploy.DeploymentError):
            tuning.validate_state([])

    def test_offline_preview_never_calls_azure_or_overwrites_a_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "preview.json"
            source = Path(temporary) / "source.json"
            source.write_text(json.dumps(source_agent()))
            arguments = ["voice_tuning", "--preview", "--experiment", "silence-650", "--state", str(state)]
            with (
                patch.object(deploy, "AGENT_FILE", source),
                patch.object(deploy, "az") as az,
                patch.object(deploy, "azure_request") as request,
                patch("sys.argv", arguments),
                patch("builtins.print"),
            ):
                tuning.main()
                original = state.read_bytes()
                with self.assertRaises(deploy.DeploymentError):
                    tuning.main()
            az.assert_not_called()
            request.assert_not_called()
            self.assertEqual(state.read_bytes(), original)
            self.assertIsNone(json.loads(original)["endpoint"])

    def test_local_or_wrong_project_snapshots_cannot_publish(self):
        for state, endpoint in (
            (tuning.prepare(source_agent(), "silence-650"), ENDPOINT),
            (live_state(), ENDPOINT + "-other"),
        ):
            with (
                patch.object(tuning, "read_agent") as read,
                self.assertRaises(deploy.DeploymentError),
            ):
                tuning.check_current(state, endpoint)
            read.assert_not_called()

    def test_refuses_remote_drift_even_when_only_version_or_unknown_field_changes(self):
        state = live_state()
        for change in ("version", "definition", "description"):
            current = copy.deepcopy(state["source"])
            version = "6" if change == "version" else "5"
            if change == "definition":
                current["definition"]["future"] = True
            if change == "description":
                current["description"] = "changed"
            with (
                self.subTest(change=change),
                patch.object(tuning, "read_agent", return_value=(current, version, '"v6"')),
                patch.object(deploy, "azure_request") as request,
                self.assertRaises(deploy.DeploymentError),
            ):
                tuning.apply(state, ENDPOINT, conditional_writes_verified=True)
            request.assert_not_called()

    def test_apply_requires_a_strong_etag_and_sends_a_conditional_write(self):
        state = live_state()
        for etag in (None, 'W/"v5"', "*"):
            with (
                self.subTest(etag=etag),
                patch.object(tuning, "read_agent", return_value=(state["source"], "5", etag)),
                patch.object(deploy, "azure_request") as request,
                self.assertRaises(deploy.DeploymentError),
            ):
                tuning.apply(state, ENDPOINT, conditional_writes_verified=True)
            request.assert_not_called()
        observed = {**state["source"], "definition": state["candidate_definition"]}
        with (
            patch.object(tuning, "read_agent", side_effect=[
                (state["source"], "5", '"v5"'), (observed, "6", '"v6"'),
            ]),
            patch.object(deploy, "azure_request") as request,
            patch("builtins.print"),
        ):
            tuning.apply(state, ENDPOINT, conditional_writes_verified=True)
        self.assertEqual(request.call_args.kwargs["if_match"], '"v5"')
        self.assertEqual(request.call_args.kwargs["body"]["definition"], state["candidate_definition"])

    def test_conflict_and_failed_readback_are_explicit(self):
        state = live_state()
        with (
            patch.object(tuning, "read_agent", return_value=(state["source"], "5", '"v5"')),
            patch.object(deploy, "azure_request", side_effect=deploy.AzureRequestError(412, "PreconditionFailed")),
            self.assertRaises(deploy.AzureRequestError),
        ):
            tuning.apply(state, ENDPOINT, conditional_writes_verified=True)
        with (
            patch.object(tuning, "read_agent", return_value=(state["source"], "5", '"v5"')),
            patch.object(deploy, "azure_request"),
            self.assertRaises(deploy.DeploymentError),
        ):
            tuning.apply(state, ENDPOINT, conditional_writes_verified=True)

    def test_rollback_restores_absent_speed_without_reverting_tools_or_other_settings(self):
        state = live_state("speech-speed")
        current = {**state["source"], "definition": state["candidate_definition"]}
        with (
            patch.object(tuning, "read_agent", side_effect=[
                (current, "6", '"v6"'), (state["source"], "7", '"v7"'),
            ]),
            patch.object(deploy, "azure_request") as request,
            patch("builtins.print"),
        ):
            tuning.apply(state, ENDPOINT, rollback=True, conditional_writes_verified=True)
        self.assertEqual(request.call_args.kwargs["body"]["definition"], state["source"]["definition"])
        self.assertNotIn("speed", request.call_args.kwargs["body"]["definition"]["audio"]["output"])

    def test_candidate_creation_never_writes_to_the_source_or_existing_candidate(self):
        state = live_state()
        observed = {**state["source"], "name": "isolated-candidate", "definition": state["candidate_definition"]}
        with (
            patch.object(tuning, "read_agent", side_effect=[
                (state["source"], "5", None),
                deploy.AzureRequestError(404, "NotFound"), (observed, "1", None),
            ]),
            patch.object(deploy, "azure_request") as request,
            patch("builtins.print"),
        ):
            tuning.publish_candidate(state, ENDPOINT, observed["name"])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], ENDPOINT + "/voice_agents?api-version=v1")
        self.assertEqual(request.call_args.kwargs["body"]["name"], "isolated-candidate")
        for name in (state["source"]["name"], "existing-candidate"):
            with (
                self.subTest(name=name),
                patch.object(tuning, "read_agent", return_value=(state["source"], "5", None)),
                patch.object(deploy, "azure_request") as request,
                self.assertRaises(deploy.DeploymentError),
            ):
                tuning.publish_candidate(state, ENDPOINT, name)
            request.assert_not_called()

    def test_live_updates_require_a_candidate_precondition_probe(self):
        with (
            patch.object(deploy, "azure_request") as request,
            self.assertRaises(deploy.DeploymentError),
        ):
            tuning.apply(live_state(), ENDPOINT)
        request.assert_not_called()

    def test_candidate_probe_requires_a_real_stale_etag_rejection(self):
        state = live_state()
        observed = {**state["source"], "name": "candidate", "definition": state["candidate_definition"]}
        for rejected in (False, True):
            with (
                self.subTest(rejected=rejected),
                patch.object(tuning, "read_agent", side_effect=[
                    (state["source"], "5", '"v5"'),
                    deploy.AzureRequestError(404, "NotFound"), (observed, "1", '"v1"'),
                ]),
                patch.object(deploy, "azure_request", side_effect=[
                    {}, deploy.AzureRequestError(412, "PreconditionFailed") if rejected else {},
                ]) as request,
                patch("builtins.print"),
            ):
                if rejected:
                    self.assertTrue(tuning.publish_candidate(state, ENDPOINT, "candidate"))
                else:
                    with self.assertRaises(deploy.DeploymentError):
                        tuning.publish_candidate(state, ENDPOINT, "candidate")
            self.assertEqual(request.call_count, 2)
            self.assertIn("/voice_agents/candidate?", request.call_args.args[0])
            self.assertEqual(request.call_args.kwargs["if_match"], '"voice-tuning-deliberately-stale"')

    def test_transport_preserves_etag_and_sends_preconditions_without_logging_credentials(self):
        response = io.BytesIO(b'{"versions": {}}')
        response.headers = {"ETag": '"v5"'}
        metadata = {}
        with (
            patch.object(deploy, "az", return_value={"accessToken": "private-token"}),
            patch.object(deploy.urllib.request, "urlopen", return_value=response) as request,
        ):
            deploy.azure_request(
                ENDPOINT, method="POST", body={}, scope=tuning.SCOPE,
                if_match='"v5"', response_metadata=metadata,
            )
        self.assertEqual(metadata, {"etag": '"v5"'})
        self.assertEqual(request.call_args.args[0].get_header("If-match"), '"v5"')


if __name__ == "__main__":
    unittest.main()
