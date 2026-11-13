# 03 - Use the Published Contoso Bank Voice Agent

Complete [agent deployment](./02_build_and_deploy_voice_agent.md) first.
The text client reads the project endpoint from the ignored `config/deploy.env`
and agent name from the ignored `config/agent.json`. It obtains a Foundry
identity token through Azure CLI; it never sends the MCP service credential
to the caller.

## Prepare and connect

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
az account show
python3 debug_agent.py --connect-only
```

A successful connection reports `session=ready`, receives an opening response,
and ends with `connect_smoke=passed`. This is not an audio or PSTN test.

## Generate a fictional caller code

For the self-hosted service, open `/otp` on **your own** deployment's base URL.
Locally this is `http://127.0.0.1:8000/otp`; see the
[MCP setup guide](./mcp_server/README.md).

Unlock with your separately managed OTP-administrator key, select a fictional
profile, and generate a code. Generated codes expire after 48 hours, work once,
and replace the same profile's previous generated code. The permanently
configured fictional profile is Avery Example, dummy demo code `12345678`.
That code is public sample data and is not a service credential.

If you save the administrator key locally, use the ignored
`config/otp.password` or an approved secret manager. Never enter it into the
agent conversation. Supply only the fictional caller's eight-digit code.

## Exercise the routing flow

```bash
python3 debug_agent.py
```

Enter one caller turn at a time, for example:

```text
you> <generated-eight-digit-code>
you> Michael Brennan
you> Yes, that is the person.
```

These are synthetic directory records. `Brennan` alone must require
clarification; `Priya Raghavan` identifies a different fictional officer.
Unknown and unavailable officers must never silently select another person.
Transfers record simulated outcomes, not real telephone connections.
Use `/quit` or `/exit` to leave.

A scripted check is also available:

```bash
OTP="<generated-eight-digit-code>"
python3 debug_agent.py --message "${OTP}" --message "Michael Brennan" --message "Yes"
unset OTP
```

The successful path is `start_call`, `verify_caller_otp`,
`search_loan_officer`, `confirm_loan_officer`, `transfer_to_officer`.
Repeated event output does not mean a business operation ran twice.

Use `--verbose` for protocol event names. The client prints transcript text;
do not redirect real conversations into committed logs. Foundry's packaged
`store: true` setting can persist conversations/audio, so use only synthetic
identities and apply your own retention/access policies.

## Audio testing

Open [Microsoft Foundry](https://ai.azure.com/), select your project and
configured Voice Agent, and use the voice experience if available.
Verify greeting audio, partial and complete codes, ambiguous names, correction,
explicit confirmation, interruption, and fresh sessions.

The client does not capture microphones, play audio, or test real telephone
routing. Follow the [telephone guide](./04_connect_phone_number.md) for that.
