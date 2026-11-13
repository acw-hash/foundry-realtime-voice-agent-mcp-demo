# Foundry Realtime Voice Agent MCP Demo

A fictional banking receptionist built with a Microsoft Foundry Voice Agent,
an MCP backend, and an optional Azure Communication Services telephone bridge.
Contoso Bank is fictional. No actual financial institution or customer is
represented by this package.

The backend verifies demo access codes, searches a synthetic 20,000-officer
directory, requires explicit officer confirmation, and records simulated
transfers. It does not perform real lending decisions or connect real officers.
Names, identifiers, dates, and financial attributes in the committed fixtures
are synthetic, not records about real people.

**Trying the phone demo?** Follow the [quick demo guide](./DEMO.md).
Your host will share the dial-in number and demo access code separately.

## Get started

1. Follow [subscription preparation](./01_prepare_subscription.md) if needed.
2. Copy `config/deploy.env.example` to `config/deploy.env` and
   `config/agent.example.json` to `config/agent.json`. Supply your own project
   settings and MCP endpoint in those ignored local files.
3. Use the [self-hosted MCP guide](./mcp_server/README.md) to run locally or
   prepare hosting. Deployment requires your own approved Azure resources.
4. Follow [agent deployment](./02_build_and_deploy_voice_agent.md) and
   [client testing](./03_use_published_voice_agent.md).
5. Optionally configure the [telephone bridge](./04_connect_phone_number.md).

No live deployment, endpoint, phone number, or credential is distributed.
Deployment helpers use generic resource names; review them and the Bicep
templates for your environment before deploying. Nothing is deployed by
running the local tests. The scrubbed customer IDs and dummy passcode target
fresh demo state; do not reuse an existing deployment's issued OTP records.

## Publication safety

The Git exclusions cover local configuration, tokens/passwords/private keys,
deployment outputs and parameters, Azure/session artifacts, virtual
environments, state databases, logs, recordings, and unreviewed data exports.
Only explicitly named example configuration and the two synthetic JSON fixtures
are intended for publication. Keep private datasets outside those fixtures.

- Never put real names, customer records, tenant/subscription IDs, internal
  URLs, real phone numbers, or credentials into source or examples.
- `config/agent.json` is private local configuration; the committed template is
  `config/agent.example.json`. Local settings and recordings are **not erased**.
- The fixed eight-digit code in source/tests is an intentionally public demo
  fixture, not a service key. It is not production authentication.
- `.gitignore` does not protect a file already tracked or added with `git add -f`.
  Inspect `git status --short --untracked-files=all` and `git diff --cached`
  before the first commit. Enable repository secret scanning/push protection
  where available.
- If an actual credential was previously shared or committed elsewhere, rotate
  it. Ignoring or deleting a local file does not revoke a credential or remove
  it from another repository's history.

Use `foundry-realtime-voice-agent-mcp-demo` for both the project folder and
GitHub repository name.
