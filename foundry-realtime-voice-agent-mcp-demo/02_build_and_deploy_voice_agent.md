# 02 - Build and Deploy the Contoso Bank Voice Agent

Prepare a Foundry project using [guide 01](./01_prepare_subscription.md).
This package does not include access to a pre-existing MCP service. Host your
own using the [MCP guide](./mcp_server/README.md), or obtain an approved service
endpoint and credential from its operator.

## Local configuration

Run from the project root:

```bash
cp config/deploy.env.example config/deploy.env
cp config/agent.example.json config/agent.json
chmod 600 config/deploy.env config/agent.json
```

In the ignored `config/deploy.env`, replace the Foundry project endpoint and
ARM project ID. The account and project must agree in both values.
In the ignored `config/agent.json`, replace
`https://mcp.example.invalid/mcp/contoso-bank` with your own HTTPS MCP endpoint.
Choose an agent name and project connection name for your environment.
The example URL is deliberately non-routable.

The example preserves the flat voice contract: `kind: voice`, managed
`gpt-realtime`, Ava HD audio, one MCP binding, six sequential business tools,
and `store: true`. Conversation storage can retain transcripts/audio; use only
synthetic data and apply appropriate retention controls.

| Tool | Purpose |
|---|---|
| `start_call` | Open the call |
| `verify_caller_otp` | Verify the demo caller code |
| `search_loan_officer` | Search the synthetic directory |
| `confirm_loan_officer` | Require explicit selection confirmation |
| `transfer_to_officer` | Record a simulated transfer |
| `end_call` | Close a non-transferred call |

The backend, not just the prompt, enforces authentication and confirmation.
Use the provided instructions only with a backend implementing this contract.

## Supply the MCP credential

Obtain the raw bearer token through an approved secret-management channel.
For a self-hosted deployment, use its separately managed MCP credential,
not the OTP-administrator credential.

```bash
read -rsp "Paste your MCP bearer token: " MCP_TOKEN
printf '\n'
printf '%s' "${MCP_TOKEN}" > config/mcp.token
unset MCP_TOKEN
chmod 600 config/mcp.token
```

Never put credentials in the agent JSON, documentation, shell arguments,
source control, or support messages. The project connection uses the
`Authorization` header with a `Bearer ` prefix added by the deployer.
The raw local file must not contain that prefix.

## Validate and deploy

Install Bash 4+, Python 3.10+, Azure CLI, `curl`, and `jq`; use WSL on Windows.
Select your approved subscription, then validate without remote calls:

```bash
az login
az account set --subscription "<subscription-id>"
bash deploy.sh --dry-run
```

After deployment approval:

```bash
bash deploy.sh
```

The script validates the configuration, requires HTTP 401 without MCP
credentials and successful authenticated initialization, creates/updates the
project connection, publishes the agent only when its definition changes, and
requires matching published readback. The configured MCP hostname must be
reachable over HTTPS from both the deployment machine and Foundry.

Check later without changing the connection:

```bash
bash deploy.sh --check --skip-connection
```

Exit status `3` indicates definition drift. Published agent versions are
immutable; changed definitions create new versions.

The local `config/agent.json`, `config/deploy.env`, `config/mcp.token`,
`config/otp.password`, generated definitions, and deployment outputs are
excluded from Git. Publish only the sanitized examples.

## Troubleshooting

- **Endpoint/ARM ID mismatch:** copy both values from the same project.
- **MCP HTTP 401 with credentials:** check the endpoint and obtain a current
  token securely. Do not disable authentication.
- **Foundry HTTP 403:** verify project access and role propagation.
- **Voice/model unavailable:** verify preview enablement and regional support.
  A regular prompt agent is not a substitute for this voice definition.
- **Readback drift:** inspect the intended local definition before publishing
  a new version, especially after an intentional self-hosted cutover.

Continue with [client testing](./03_use_published_voice_agent.md).
