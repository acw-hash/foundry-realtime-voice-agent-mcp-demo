# Customer-owned Contoso Bank MCP

This is the self-hosted demo backend for the existing flat, six-tool Foundry
Voice Agent. It does not create a new model deployment or change the voice,
audio, VAD, conversation storage, or agent name.

## Deployment prerequisites

This source package includes no live service, internal endpoint, or credentials.
Run locally using the instructions below, or provision your own approved Azure
resources. Example hostnames ending in `.invalid` are deliberately non-routable.
Review the generic resource names in the deployment helpers and Bicep templates
before deploying; they do not identify an existing shared environment.

Copy `config/agent.example.json` to the ignored `config/agent.json` and
`config/deploy.env.example` to the ignored `config/deploy.env`. Configure your
own project and MCP endpoint. A Foundry cutover is a separate opt-in operation.

The implementation adapts the routing, OTP fixtures, and dual-transport host
patterns from [microsoft/voice-agent, commit d0bdcf5](https://github.com/microsoft/voice-agent/tree/d0bdcf575fa9ec23ca3948a5ef92350a6dc0651a/shared_mcp).
The Microsoft MIT notice is retained in [LICENSE](./LICENSE).

## Deliberate demo behavior

- The six MCP tools are `start_call`, `verify_caller_otp`,
  `search_loan_officer`, `confirm_loan_officer`, `transfer_to_officer`, `end_call`.
- There is no loan-offer or lending-interest gate.
- **Name search covers the full 20,000-record directory**, not a random sample.
  Exact names and aliases take priority over partial names and close spellings.
  Case, punctuation, and extra whitespace are normalized. The normalization and
  `difflib` similarity approach adapts the previous demo's name-matching helper;
  that demo's active lookup used three random sample employees, not a database.
  This service loads the existing JSON directory into in-memory search indexes;
  Azure Table Storage continues to store call/OTP state, not officer search data.
  Up to five candidates are returned with the shortlisted `match_count`,
  `returned_count`, `truncated`, and `match_type`. Ambiguous names require caller
  clarification. Unknown names do not fall back to a random available officer.
  Unavailable officers remain visible and cannot be confirmed or transferred.
- Caller-facing dialogue is natural and does not proactively describe the
  system as a demo or narrate backend limitations. After a simulated transfer,
  the agent acknowledges that the request was recorded; it does not falsely
  claim a telephone connection or callback occurred. Direct capability questions
  are answered truthfully.
- Selection, explicit confirmation, availability, and authentication are
  enforced in the backend. A new search invalidates previous confirmation.
- Transfers are **simulated business outcomes**, not PSTN/SIP/ACS transfers or
  real callbacks. Ending the business record does not close a voice connection.
- The permanent fictional demo identity is **Avery Example**, dummy code **12345678**.
  Other customers receive generated, eight-digit, single-use codes lasting
  48 hours. Reissuing for the same customer replaces that customer's active code.
- There are 20 fictional customer profiles. All dates and financial attributes,
  including those on the Avery Example profile, are synthetic sample values.
- Three complete invalid OTP attempts lock out a call. Partial codes are
  accumulated server-side and do not consume attempts. Responses never echo
  collected digits.

These are not production banking authentication or decisioning controls.
Do not use real customer identities, loan records, or credentials.

## Architecture

```text
Existing Foundry Voice Agent
  -> project connection (separate MCP bearer secret)
  -> Container Apps HTTPS /mcp/contoso-bank
  -> six-tool routing service
  -> VNet / private endpoint -> Azure Table Storage (managed identity)

Operator browser -> /otp -> /api/otp/* (separate administrator bearer secret)
```

For inbound telephone access, the separate [ACS audio bridge](../04_connect_phone_number.md)
connects the purchased number to this same Voice Agent. It does not host or
execute the MCP tools itself and receives neither service credential. Real
inbound calling does not change `transfer_to_officer`: that outcome remains a
simulated business record, not a telephone transfer.

The two service credentials use Container Apps' built-in encrypted secret
storage. Authorized operators manage them through Azure Resource Manager;
no VPN, Key Vault access, maintenance job, or administration VM is required.
Only the Table Storage connection needs a private endpoint. Public HTTPS
application ingress remains available to the existing Foundry service.

Both Streamable HTTP and legacy HTTP+SSE are supported on the same MCP route.
Every MCP request, including legacy message POSTs, requires the MCP credential.
The OTP page itself is public; listing profiles or generating codes requires
the separate administrator credential. Neither credential is sent to callers.
The browser keeps the entered administrator key only in memory, not cookies,
local storage, or URL parameters. Lock the console when finished.

Call state and generated OTP digests live in a `bankstate` Azure table. A
partition-local revision ETag serializes demo mutations. OTP consumption and
the authenticated call identity commit in the **same atomic transaction**.
Competing writes retry from fresh state; storage failures are surfaced, not
replaced with an in-memory success.

Generated complete codes are stored only as digests. Partial digits temporarily
live in the call record while the caller is reading a code and are cleared on
verification or closure. Azure Storage encryption and RBAC protect persisted
records. Do not enable request-body/argument logging.

Business records survive process/revision replacement. A live MCP/SSE or voice
connection does **not** automatically resume after restart. The app deliberately
uses one warm replica; this is not a highly available production design.
Expired OTPs cannot authenticate, but there is no automatic record-retention
cleanup job. Retain/delete this fictional demo data according to your policy.

## Local setup and tests (PowerShell)

Run from the project root. Use Python 3.10 or later. First copy the example
agent configuration to the ignored local configuration as described above.

```powershell
python -m venv .\mcp_server\.venv
.\mcp_server\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r .\mcp_server\requirements-deploy.txt
$env:PYTHONPATH = (Resolve-Path .\mcp_server\app).Path
.\mcp_server\.venv\Scripts\python.exe -m unittest discover -s .\mcp_server\tests -p "test_*.py" -v
```

`--only-binary=:all:` avoids an unnecessary cryptography source build on Windows
ARM64. The Linux container uses the normal dependency installation path.
The deploy requirements include the ARM SDK used by the deployment tests.
The Linux runtime image installs only the smaller runtime requirements.
No test creates Azure resources or modifies the published Foundry agent.
The suite covers real localhost MCP exchanges over both transports, the OTP
HTTP APIs, authentication separation, three-attempt lockout, generated-code
replacement/expiry/replay, concurrent consumption, fresh-process SQLite
recovery, exact/alias/partial/fuzzy name lookup across the directory, unavailable
and unknown officers, confirmation reset, preservation of live voice settings,
the exact-image rollout gate, and the Table Storage transaction adapter with an
in-process fake.
The fake is **not** evidence that a deployed Azure table or its RBAC was tested.

For local development, SQLite is the explicit durable backend:

```powershell
$env:CONTOSO_MCP_TOKEN = python -c "import secrets; print(secrets.token_hex(32))"
$env:CONTOSO_OTP_ADMIN_TOKEN = python -c "import secrets; print(secrets.token_hex(32))"
$env:CONTOSO_STATE_BACKEND = "sqlite"
$env:PYTHONPATH = (Resolve-Path .\mcp_server\app).Path
.\mcp_server\.venv\Scripts\python.exe -m contoso_mcp.server
```

Keep those generated values in the current shell or your approved secret
manager. Do not reuse credentials from another environment. Open
`http://127.0.0.1:8000/otp` and use your local `CONTOSO_OTP_ADMIN_TOKEN` value as
the administrator access key. Stop the server with Ctrl+C. Local state is in
the ignored `mcp_server/state/contoso.db`. Local HTTP is not a public Foundry
endpoint; deploy to Azure before connecting the published voice agent.

### Runtime settings

| Variable | Meaning |
|---|---|
| `CONTOSO_MCP_TOKEN` | Required distinct bearer credential, minimum 32 characters |
| `CONTOSO_OTP_ADMIN_TOKEN` | Required OTP-administrator credential, minimum 32 characters |
| `CONTOSO_STATE_BACKEND` | `sqlite` locally; `azure_table` in Azure |
| `CONTOSO_SQLITE_PATH` | Local durable database override |
| `CONTOSO_TABLE_ENDPOINT` | HTTPS Azure Table endpoint; required in Azure |
| `CONTOSO_TABLE_NAME` | Table name; default `bankstate` |
| `CONTOSO_DATA_DIR` | Directory containing `officers_20k.json` |
| `CONTOSO_CUSTOMERS_FILE` | Fictional customer fixture |
| `AZURE_CLIENT_ID` | User-assigned managed identity client ID in Azure |
| `CONTOSO_ALLOWED_HOSTS` | Optional comma-separated MCP DNS-rebinding allowlist |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Optional Azure Monitor instrumentation |
| `PORT` | HTTP port; default `8000` |

`/healthz` is public liveness. `/readyz` checks the state backend and returns
503 if unavailable. There is no silent Azure-to-SQLite fallback.

## Azure deployment

Set `AZURE_SUBSCRIPTION_ID` in the deployment process environment to your own
approved subscription ID and explicitly select that subscription in Azure CLI.
No subscription ID is embedded in the source.
The example resource group is `rg-contoso-bank-mcp`, in East US 2.
The simplified entry point is
[`infra/demo.bicep`](../infra/demo.bicep), an additive resource-group deployment.
It references the existing Basic ACR, Standard LRS Table Storage, Log Analytics,
Application Insights, and managed identity without recreating them. It adds
one VNet with separate workload/endpoint subnets, a Table private endpoint with
private DNS, and a new Consumption-workload-profile environment and MCP app.
No additional Foundry model deployment is required.

### Subscription governance prerequisite

Review inherited policies and networking restrictions in your own subscription.
Do not disable or bypass governance controls to run the demo.

The revised design **does not use Key Vault** for the application's two demo
credentials; it uses standard Container Apps encrypted secrets instead. Storage
stays private, shared-key access stays disabled, and managed-identity Table
access stays enabled. The original Key Vault and bootstrap app/environment are
retained unchanged; the original subscription template is historical, not the
entry point for the simplified runtime.

The existing non-VNet environment cannot be converted in place to the required
network type. A separate VNet-integrated environment is therefore necessary.
Platform-managed network resources use `rg-contosomcp-managed-demo`.
Do not manually modify that managed group.

The deployer needs resource-deployment permissions on the existing group,
private-endpoint connection approval rights on Storage, existing workspace
configuration access, ACR image-build permission, and Container Apps
`listSecrets` permission. Generated values are passed to Bicep's `secureString`
parameters through the ARM SDK **in memory**, never CLI arguments, temporary
parameter files, template outputs, logs, or saved deployment-output JSON.
Existing app secrets are reused; missing/malformed existing credentials cause
an explicit error rather than an implicit rotation.

**Resource creation and Foundry cutover are separate explicit operations.**
Do not run the following until deployment is approved:

```powershell
$env:AZURE_SUBSCRIPTION_ID = "<subscription-id>"
az account set --subscription $env:AZURE_SUBSCRIPTION_ID
.\mcp_server\.venv\Scripts\python.exe .\mcp_server\deploy.py --preview
.\mcp_server\.venv\Scripts\python.exe .\mcp_server\deploy.py --deploy
```

The command:

1. Confirms the approved CLI subscription.
2. Reads the existing new app's encrypted platform secrets, or generates two
   distinct random values in memory when that app does not exist yet.
3. Builds the image in the existing ACR using the root allowlist-only `.dockerignore`.
   Existing project tokens, environment files, recordings, and session files
   are not in the build context.
4. Deploys the additive Bicep template in incremental mode, using SDK secure
   parameters rather than a plaintext parameter file. No Key Vault call occurs.
5. Verifies liveness, backing-store readiness,
   unauthenticated HTTP 401, exact six-tool inventory, OTP administration and
   candidate binding, consumed-code replay rejection, simulated transfer, and
   both MCP transports.
6. Saves nonsecret deployment outputs and an opt-in agent definition under
   ignored `config/` files. It does **not** change the current voice agent.

`--preview` runs only Azure what-if: it creates no resources, builds no image,
and does not persist generated credentials. It rejects deletion or out-of-scope
changes. Deploy only after reviewing this preview.

Open the printed `/otp` URL. Retrieve `otp-admin-token` from **Container Apps >
ca-contosomcp-private-demo > Secrets** in the Azure portal, using an identity
authorized to read app secrets. Enter it in the console, choose
a fictional customer, and generate an OTP. Avery Example's permanent demo code is
shown after unlocking. Never paste either service credential into chat.

Verify the server later:

```powershell
.\mcp_server\.venv\Scripts\python.exe .\mcp_server\deploy.py --check
```

The server check creates fictional probe call records and generates/consumes an
Avery Example OTP, replacing that profile's previous generated code. The permanent
code is unaffected. It does not publish an agent or create Azure resources.

If Azure takes longer than the local deployment wait limit, the helper reports
`PENDING` (exit 2). The Azure operation continues; do not start another deployment.
After provisioning, verification waits for the requested image to be healthy
with 100% traffic so a healthy old revision cannot satisfy the deployment gate.
Resume waiting and verification without rebuilding or redeploying:

```powershell
.\mcp_server\.venv\Scripts\python.exe .\mcp_server\deploy.py --resume
```

## Explicit Foundry cutover

Only after server validation and approval:

```powershell
.\mcp_server\.venv\Scripts\python.exe .\mcp_server\deploy.py --cutover
```

This uses the existing `config/deploy.env`, validates endpoint/account/project/
subscription agreement, creates a new project-scoped `contoso-bank-mcp`
connection, and publishes a new version of the **same** Voice Agent only when
its definition differs. It starts from the current live definition to preserve
model, audio, greeting, storage, and protocol settings, changes the MCP binding,
and replaces the instructions with the directory-search and
natural receptionist contract in `agent_instructions.txt`. Published readback
is required to match after the same normalization used by the original deployer.

The original `config/agent.json`, `deploy.sh`, Microsoft-hosted connection, and
credential files are not overwritten. The text client continues to work because
the agent name and Foundry project are unchanged:

```powershell
$env:PYTHONIOENCODING = "utf-8"
python .\debug_agent.py --message "12345678" --message "Michael Brennan" --message "Yes"
```

That client needs the separate parent `requirements.txt` dependencies.
To test audio, use the existing Foundry voice experience. Verify recognition,
turn-taking, interruption, and natural routing dialogue separately. Verify there
is no unsolicited demo disclaimer and no false claim of a live telephone transfer.

### Directory acceptance cases

| Caller name | Expected result |
|---|---|
| `Michael Brennan` | Exact match, `lo-00001` |
| `Priya Raghavan` | A different exact match, `lo-00005` |
| `Cecilia Adams` | Exact match near the end of the directory, `lo-20000` |
| `Mich Brennan` | Alias match for Michelle Brennan |
| `Micheal Brennan` | Close-spelling suggestion for Michael Brennan, requiring confirmation |
| `Brennan` | Multiple matches; ask for the first name, never select one arbitrarily |
| `Michael Brendan` | Exact but unavailable; do not substitute another person |
| `Qzxv Plmok` | No match; ask for spelling or another name |

**Do not run the original `bash deploy.sh` after cutover unless intentionally
restoring the Microsoft-hosted backend.** That script still publishes the
original packaged definition. Conversely, the original definition checker will
report drift after an intentional customer-owned cutover.

## Voice naturalness and latency experiments (not deployed)

The local tuning helper prepares **one change at a time**. It preserves the
managed model, Ava HD voice identity, six tools, OTP rules, directory matching,
confirmation gates, language switching, greeting, and unrelated/unknown fields.
No faster settings have been deployed or proven faster by preparing these files.

Run from the project root. This first command is entirely offline; it reads the
saved customer-owned definition, not the current remote agent:

```powershell
.\mcp_server\.venv\Scripts\python.exe -m mcp_server.voice_tuning --preview --experiment silence-650 --state .\.azure\voice-silence650.local.json
```

The available experiments are:

| Experiment | Only proposed change |
|---|---|
| `silence-650` | Reduce Azure semantic VAD silence from the existing 800 ms to 650 ms |
| `silence-600` | Test 600 ms separately, only after the 650 ms quality gate passes |
| `speech-speed` | Set output `speed` to 1.05 from the effective default of 1.0; keep the voice |
| `dialogue` | Append warm, concise delivery guidance without replacing any existing instructions |

Use a different `--state` file for each experiment. Snapshots refuse overwrites.
The speed experiment targets speaking duration, not a guaranteed reduction in
time to first audio. Silence settings interact with semantic end-of-turn
detection; do not add configuration timeouts to calculate an expected delay.
Do not edit `agent.json` as a tuning shortcut: it is the original backend's
rollback template. The normal MCP cutover intentionally preserves live audio.

### Approved remote candidate testing

**Separate approval is required before publishing a candidate, deploying timing
instrumentation, changing the live agent, or redirecting the telephone bridge.**
Read-only inspection does not grant permission for subsequent writes.

After confirming the intended project/subscription, make a fresh read-only live
preview. This reads the latest definition/version and saves a local snapshot.
It does not publish an agent or modify its connection:

```powershell
.\mcp_server\.venv\Scripts\python.exe -m mcp_server.voice_tuning --preview --read-live --experiment silence-650 --state .\.azure\voice-silence650.live.json
```

After approval for the named candidate and its normal metered test usage:

```powershell
.\mcp_server\.venv\Scripts\python.exe -m mcp_server.voice_tuning --publish-candidate --candidate-name contoso-call-routing-agent-latency650 --state .\.azure\voice-silence650.live.json
```

The helper rechecks the source version and definition, refuses an existing
candidate name, uses the collection's create operation, and verifies readback.
It does not alter the original agent, project connection, credentials, or phone
route. Test the candidate in the Foundry voice playground. Candidate tools
create ordinary fictional call records; use only fictional customers and codes.
Keep local snapshots private: they contain agent definitions and internal URLs.

If the candidate returns a strong ETag, the helper also probes a deliberately
stale `If-Match` **on that candidate only**, expecting HTTP 412. A successful
write despite the stale precondition is an explicit error. The companion
`.candidate.json` receipt binds the outcome to the exact preview. No ETag or an
unverified precondition leaves automated live apply/rollback blocked. This
preview service's actual conditional-write support has not been verified yet.

### Separately approved live apply and rollback

Only after browser and actual telephone acceptance, and separate approval for
the exact live patch, use:

```powershell
.\mcp_server\.venv\Scripts\python.exe -m mcp_server.voice_tuning --apply --state .\.azure\voice-silence650.live.json
```

This requires a successful candidate precondition probe, an unchanged source
version/definition, and a fresh strong ETag. It sends `If-Match` and requires
normalized readback. Unsupported conditions fail closed; do not bypass the guard
or use the old backend's deployment script to force the update. A changed
source requires a new preview and candidate test, not a forced overwrite.

Keep the original snapshot and receipt. After rollback approval:

```powershell
.\mcp_server\.venv\Scripts\python.exe -m mcp_server.voice_tuning --rollback --state .\.azure\voice-silence650.live.json
```

Rollback refuses intervening definition changes, restores only the experimented
field (including removing an originally absent speed), and verifies readback.
Never delete the working snapshot before audio acceptance. If a publication
succeeds but readback fails, inspect the remote version before retrying; retain
the snapshot for recovery. Existing sessions may retain their original settings;
test with fresh sessions.

Tool logs now include monotonic `duration_ms` with `completed`, `ok`, and the
existing error code. Exceptions still propagate; timings never include tool
arguments, returned profiles, names, OTPs, or raw exception messages.
Timing changes remain local until a separately approved MCP deployment.

Follow the [phone latency and quality procedure](../04_connect_phone_number.md#latency-and-naturalness-testing-not-deployed)
for the shared acceptance set and measurement limits. Do not enable interim
filler, change models, weaken confirmation, alter recognition thresholds, or
increase replica count without evidence and a separate decision.

## Cost and cleanup

Estimate costs for your own subscription, region, and usage before deployment.
Include the warm replica, private endpoint, private DNS, managed networking,
registry, storage, inference, telemetry, builds, and any telephone usage.
Bootstrap resources are not removed automatically; obtain separate cleanup
approval rather than deleting shared resources.

Logs use 30-day retention. Business records remain until explicitly removed.
Before deleting hosting, restore/switch any agent still pointing to it.
Deleting `rg-contoso-bank-mcp` removes its hosting, secrets, and durable demo
state; it does not delete the existing Foundry project or its connections.
