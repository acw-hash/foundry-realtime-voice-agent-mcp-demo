# Call the Voice Agent by telephone

This optional bridge requires your own ACS resource and owned inbound telephone
number. No telephone number or deployment is included with this source package.
Number rental, calling, streaming, bridge hosting, taxes, and Foundry usage
incur charges; confirm current availability, lifecycle, and costs before use.

## Architecture

```text
Phone -> ACS number -> Call Automation
                        <-> dedicated Container App audio bridge
                        <-> existing Foundry Voice Agent
                               -> existing contoso-bank-mcp tools

ACS IncomingCall -> Event Grid -> authenticated bridge /events
ACS call events -> JWT-validated bridge /callbacks/<per-call-token>
```

The bridge is `ca-contoso-voice-bridge` in `rg-contoso-bank-mcp`. It shares the
existing Container Apps environment and registry, not the MCP process or its
credentials. The existing `contoso-call-routing-agent` agent, project,
instructions, model, voice, OTP flow, and directory remain unchanged.

This is a custom ACS audio bridge, not a native Foundry Teams/Twilio phone
binding. The Foundry **Channels** card can therefore remain empty even when
this number works. ACS `IncomingCall` delivery and the dedicated bridge are the
source of truth for this route.

## Call acceptance

1. Dial **<your-owned-phone-number>** from a normal phone. No caller app or Azure sign-in
   is required. Carrier and international-dialing restrictions still apply.
2. Verify that the Contoso Bank greeting is audible.
3. Speak the dummy fictional access code `12345678`, or obtain a generated
   code from `/otp` on your own protected MCP deployment.
4. Ask for `Michael Brennan` and confirm the returned name. Repeat in another
   call with `Priya Raghavan` to verify distinct directory matches.
5. Test `Brennan` alone: the agent must ask for clarification, not select an
   arbitrary officer. Test interruption by speaking during a response.
6. Confirm the final acknowledgement says the request was recorded. Officer
   transfers are still simulated; the bridge does not connect a real officer.
7. Hang up and call again. Each call must start a new, independent session.

Automated protocol checks do not prove PSTN audio quality, spoken-input
recognition, caller-device playback, perceived latency, or barge-in. Verify
those with actual phone calls. Use only fictional customer data. Existing
Foundry conversation/audio storage settings are preserved.

## Local tests

Run from the project root using Python 3.11 or later. The container uses 3.13.
The deployment entry point reuses the parent project's Azure deployment helpers.

```powershell
.\mcp_server\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r .\mcp_server\requirements-deploy.txt -r .\telephony_bridge\requirements.txt
.\mcp_server\.venv\Scripts\python.exe -m unittest discover -s .\telephony_bridge\tests -p "test_*.py" -v
az bicep build --file .\infra\telephony.bicep --stdout | Out-Null
```

The tests cover authenticated event delivery, resource/number filtering, duplicate
events, JWT signatures/issuer/audience/expiry, bidirectional PCM24K media,
interruption, disconnect cleanup, isolated calls, and scoped deployment.
They use synthetic call contexts and protocol fakes, not real PSTN calls.

## Deploy and verify

The ACS resource and number already exist; the deployment script never buys or
releases numbers. Review and approve infrastructure costs and scoped identity
grants before deploying.

Set `AZURE_SUBSCRIPTION_ID` in the process environment and select the same
subscription in Azure CLI. Configure your Foundry settings in the ignored
`config/deploy.env`, adding `ACS_PHONE_NUMBER` (your owned E.164 number) and
`ACS_IMMUTABLE_ID` (your ACS resource's immutable GUID). No real number or
resource GUID is hardcoded in the template. Review the generic hosting,
registry, ACS resource, and Foundry resource-group names in the helpers and
templates together before deployment.

```powershell
$env:AZURE_SUBSCRIPTION_ID = "<subscription-id>"
az account set --subscription $env:AZURE_SUBSCRIPTION_ID
.\mcp_server\.venv\Scripts\python.exe .\telephony_bridge\deploy.py --deploy
.\mcp_server\.venv\Scripts\python.exe .\telephony_bridge\deploy.py --check
```

This builds the dedicated bridge image in the existing registry, deploys
`infra/telephony.bicep` incrementally, configures the IncomingCall subscription,
and grants its own identity:

- **AcrPull** on `crcontosomcpdevdemo` only.
- **Foundry User** on `contoso-project-example` only.

No agent version is published and no MCP connection or credential is changed.
The exact new image must be healthy with all traffic before checks pass.
The authenticated diagnostic opens a Foundry session through the bridge's
managed identity and requires synthesized audio, not just a successful HTTP
response. It incurs a small amount of normal Foundry usage.

Non-secret deployment outputs are saved to `config/telephony.deployment.json`.
If deployment is still running, do not launch another build:

```powershell
.\mcp_server\.venv\Scripts\python.exe .\telephony_bridge\deploy.py --resume
```

The ACS access key and separate Event Grid delivery token are kept in Container
Apps encrypted secrets and secure deployment parameters. They are never written
to the config outputs or sent to callers. The bridge has neither the MCP token
nor the OTP administrator key.

## Runtime and security

- One small warm replica uses 0.25 vCPU and 0.5 GiB in the existing environment.
- Live transport state is per-process memory; no new database is needed.
  Active calls do not survive bridge replacement. Redeploy when no calls are active.
- Event delivery requires a separate secret header and the approved ACS source
  and destination number. The Event Grid subscription validation handshake is
  supported without disabling authentication.
- Call Automation callbacks require signed JWTs with the ACS issuer, immutable
  resource audience, and expiry validation.
- Media connects using an unguessable, single-use per-call URL and must match
  the call connection ID. Unknown, reused, or wrong-format streams are rejected.
- Both media sides use PCM16 mono at 24 kHz. Caller speech clears queued ACS
  playback; the existing Foundry voice configuration handles turn detection.
- Access logs are disabled and server WebSocket logging is restricted to warnings
  and errors to avoid logging per-call capability URLs. Application
  logs contain call IDs/status, not raw audio, transcripts, OTPs, keys, or call contexts.
- A media connection that fails to attach is cleaned up rather than leaving an
  answered call open. Upstream failures explicitly log an error and end the call.

## Troubleshooting

| Symptom | Check |
|---|---|
| Number rings but no agent | Owned number's inbound capability; ACS IncomingCall delivery and Event Grid subscription state |
| Immediate hangup | Bridge logs for `call_answer_failed`, `voice_bridge_failed`, or `media_connection_timeout` |
| Event Grid delivery fails | Secret delivery header, `/events` HTTPS reachability, and validation status; never disable authentication |
| Foundry diagnostic fails | Bridge identity's project-scoped Foundry User assignment, propagation, endpoint, and agent name |
| Callback returns 401 | ACS callback JWT issuer/audience/signature; expected audience is the immutable ACS resource GUID, not its ARM path |
| Silent or distorted audio | PCM16 mono at 24 kHz on both sides; inspect metadata/errors without logging audio |
| Foundry Channels shows no number | Expected for this custom bridge; inspect ACS/Event Grid instead |

Use the existing Container App log viewer. Do not enable HTTP body logging or
paste credentials, call contexts, or per-call media URLs into support/chat.

## Latency and naturalness testing (not deployed)

Local changes add metadata-only timings and opt-in
[isolated voice experiments](./mcp_server/README.md#voice-naturalness-and-latency-experiments-not-deployed).
They do **not** change the deployed number, bridge, agent, or voice settings.
The existing model is already realtime, the voice is already Ava HD, and both
Container Apps already have a warm replica. There is no measured latency gain
until comparable audio tests have been performed.

After approval to deploy instrumentation, logs distinguish these stages:

| Log stage | What it measures |
|---|---|
| `acs_answer` | Call Automation answer request; keyed by incoming event ID before a call ID exists |
| `media_handshake` | WebSocket validation and ACS media metadata, including failed/cancelled handshakes |
| `incoming_event_to_media_attached` | Bridge receipt of the incoming event to validated media; not time since the caller dialed |
| `credential_acquisition` | Foundry credential retrieval, including cached retrieval |
| `foundry_connect` | Foundry WebSocket establishment |
| `session_connected_to_first_audio_forwarded` | First audio handed to ACS after Foundry connected; usually the greeting |
| `speech_stop_event_to_first_audio` | Received service speech-stop event to first received audio, only for explicitly correlated, completed turns |
| `speech_start_event_to_stop_audio_forwarded` | Received speech-start event to `StopAudio` sent to ACS; not acoustic interruption latency |

Stages use monotonic `duration_ms`; `completed=False` marks failed or cancelled
operations. MCP tool logs separately measure execution, including state storage,
but not the full network roundtrip from Foundry. Do not sum overlapping stages.
Logs contain identifiers/status/timings, not audio, transcripts, codes, customer
profiles, capability URLs, or credentials. Existing telemetry retention applies;
additional log volume can incur normal ingestion charges.

Turn timing requires a speech-stop item ID, an assistant conversation item whose
explicit previous-item ID is that caller item, and a completed response with
matching audio item/response IDs. Greetings, tool-only responses, cancelled
responses, and uncorrelated audio are not valid samples. Missing correlation
logs `voice_turn_timing_unavailable`, never a zero-duration success.
Tool-result chains may therefore lack this metric on this preview protocol.

These are **transport/service observations**, not audible latency:

- The service speech-stop event occurs after turn detection.
- Forwarding a delta to ACS does not prove that the caller heard it.
- The Foundry playground is externally owned; this package cannot instrument
  its microphone/playback pipeline. `debug_agent.py` is text-only.
- In controlled browser and real-phone tests, separately measure the interval
  from the human's last speech sound to the first audible reply. Do not enable
  persistent audio recording without approval.

### Comparison and quality gate

Use the same fictional inputs, environment, caller device, network conditions,
and scenario mix for baseline and candidate. Separate first connections from
established calls and isolated sessions from concurrent load. Collect at least
30 comparable turns **per channel and configuration** as an initial comparison;
report sample counts and small-sample limitations rather than claiming
statistical proof of equal accuracy.

For each route/scenario/configuration, record only a test label, sample count,
median and p95 audible response latency, missing timing count, functional
pass/fail counts, interruption success, and a blinded naturalness/listening-effort
rating. Keep service-event timings in separate columns from audible timings.
Use the same percentile calculation throughout (for example nearest-rank p95).

Target at least a 15% lower median audible response latency, with no worse p95,
on comparable turns in each channel. Calculate reduction as
`100 * (baseline_median - candidate_median) / baseline_median`; report missing
or zero baseline data as unavailable, not a successful reduction.
This is a target, not a promise. Speech pacing or improved phrasing may instead
improve listening effort without reducing response-start latency.

Reject any observed regression in:

- Complete, partial, slowly grouped, and incorrect OTPs, including lockout.
- Exact names, aliases, close spellings, ambiguous surnames, unavailable
  officers, and no-match handling; returned names/titles must remain authoritative.
- Explicit yes/no/correction before confirmation and simulated transfer.
- Barge-in, noise robustness, language switching, independent hangup/redial,
  honest recorded-request wording, and overall flow completion.

Test VAD 650 ms first; consider 600 ms only if paused digits/names remain intact.
Test 1.05 speech speed and concise dialogue separately, not simultaneously.
Keep the current semantic end-of-turn safeguards, voice identity, greeting,
noise suppression, and sequential six-tool flow. Static interim speech,
backend optimizations, new voices/models, and resource increases remain
evidence-dependent alternatives, not enabled defaults.

### Separately approved candidate phone test and restore

First pass candidate tests in the Foundry playground. After explicit approval
to redirect the existing number, drain active calls and use the exact approved
candidate name:

```powershell
.\mcp_server\.venv\Scripts\python.exe .\telephony_bridge\deploy.py --deploy --agent-name contoso-call-routing-agent-latency650
```

This is a real bridge deployment/build, incurs ordinary charges, replaces the
bridge revision, and temporarily directs new calls to the candidate. It never
publishes or modifies either Foundry agent. The protected audio diagnostic now
reports the effective `agent_name`, and deployment verifies it against the
requested target before acceptance. A normal `--deploy` with no `--agent-name`
preserves the existing target rather than silently reverting a candidate test.

Record the original target and exact working bridge image before the test.
After restore approval and draining active calls, restore the original target:

```powershell
.\mcp_server\.venv\Scripts\python.exe .\telephony_bridge\deploy.py --deploy --agent-name contoso-call-routing-agent
```

Verify a fresh call, interruption, hangup/redial, and the original target.
These target-switch commands rebuild the current local bridge code; reverting
its code/image is a separate approved revision/image rollback. Never route
around a failed diagnostic or replace an active call's bridge.

## Rollback and lifecycle

To disconnect this route, remove only the new IncomingCall event subscription
after approval, then stop or remove the dedicated bridge after active calls end.
Do not delete the shared environment, registry, resource group, MCP, or agent.
Keeping the ACS number continues its rental charge; releasing it requires a
separate explicit decision.

Microsoft documents standalone ACS onboarding/lifecycle changes and transition
requirements. This demo is not a long-term production telephony commitment.
Review the [ACS lifecycle guide](https://learn.microsoft.com/en-us/azure/communication-services/acs-retirement-and-breaking-changes-guide)
before expanding it.
