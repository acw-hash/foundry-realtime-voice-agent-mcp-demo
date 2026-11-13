#!/usr/bin/env bash
# Deploy the packaged Contoso Bank Voice Agent to a customer-owned Foundry project.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_CONFIG="${ROOT}/config/agent.json"
DEFAULT_ENV_FILE="${ROOT}/config/deploy.env"
MCP_TOKEN_FILE="${CONTOSO_MCP_TOKEN_FILE:-${ROOT}/config/mcp.token}"
API_VERSION="v1"
TOKEN_SCOPE="https://ai.azure.com/.default"
VOICE_AGENT_FEATURE="VoiceAgents=V1Preview"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  bash deploy.sh [options]

Actions:
  --apply             Create or update the MCP connection and Agent (default).
  --check             Verify that the deployed Agent definition matches.
  --dry-run           Validate local configuration; use no cloud APIs.

Configuration:
  --env-file FILE     Read the customer Foundry endpoint and ARM project ID.
  --skip-connection   Reuse an existing connection instead of updating it.
  -h, --help          Show this help.

The Agent name, MCP endpoint, and connection name come from config/agent.json.
EOF
}

ENV_FILE="${DEFAULT_ENV_FILE}"
ENV_FILE_EXPLICIT=false
ARGS=("$@")
for ((index = 0; index < ${#ARGS[@]}; index++)); do
  if [[ "${ARGS[index]}" == "--env-file" ]]; then
    ((index + 1 < ${#ARGS[@]})) || die "--env-file needs a value"
    ENV_FILE="${ARGS[index + 1]}"
    ENV_FILE_EXPLICIT=true
    index=$((index + 1))
  fi
done

if [[ ! -f "${ENV_FILE}" && "${ENV_FILE_EXPLICIT}" == true ]]; then
  die "env file not found: ${ENV_FILE}"
fi

command -v python3 >/dev/null 2>&1 || die "missing command: python3"

ACTION="apply"
SKIP_CONNECTION=false
FILE_PROJECT_ENDPOINT=""
FILE_PROJECT_ID=""
if [[ -f "${ENV_FILE}" ]]; then
  IFS=$'\t' read -r FILE_PROJECT_ENDPOINT FILE_PROJECT_ID <<< "$(
    python3 - "${ENV_FILE}" <<'PY'
import re
import shlex
import sys

assignment = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
values = {}
with open(sys.argv[1], encoding="utf-8") as stream:
    for line_number, raw_line in enumerate(stream, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = assignment.fullmatch(line)
        if match is None:
            raise SystemExit(f"{sys.argv[1]}:{line_number}: expected KEY=VALUE")
        parsed = shlex.split(match.group(2), comments=True, posix=True)
        if len(parsed) != 1:
            raise SystemExit(f"{sys.argv[1]}:{line_number}: value must be one string")
        values[match.group(1)] = parsed[0]
print(
    "\t".join(
        [
            values.get("AZURE_VOICE_AGENTS_ENDPOINT", ""),
            values.get("AZURE_AI_PROJECT_ID", ""),
        ]
    )
)
PY
  )"
fi
PROJECT_ENDPOINT="${AZURE_VOICE_AGENTS_ENDPOINT:-${FILE_PROJECT_ENDPOINT}}"
PROJECT_ID="${AZURE_AI_PROJECT_ID:-${FILE_PROJECT_ID}}"

while (($#)); do
  case "$1" in
    --apply)
      ACTION="apply"
      shift
      ;;
    --check)
      ACTION="check"
      shift
      ;;
    --dry-run)
      ACTION="dry-run"
      shift
      ;;
    --env-file)
      (($# >= 2)) || die "--env-file needs a value"
      shift 2
      ;;
    --skip-connection)
      SKIP_CONNECTION=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

[[ -f "${AGENT_CONFIG}" ]] || die "Copy config/agent.example.json to config/agent.json and configure your MCP endpoint"

CONFIG_SUMMARY="$(
  python3 - "${AGENT_CONFIG}" <<'PY'
import hashlib
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    document = json.load(stream)

if not isinstance(document, dict):
    raise SystemExit("Agent config must be a JSON object")
name = document.get("name")
definition = document.get("definition")
if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
    raise SystemExit("Agent config has an invalid name")
if not isinstance(definition, dict) or definition.get("kind") != "voice":
    raise SystemExit("Agent definition kind must be voice")
if "handoff" in definition:
    raise SystemExit("Contoso Bank v2 Agent definition must be flat and contain no handoff")

tools = definition.get("tools")
if not isinstance(tools, list) or len(tools) != 1 or tools[0].get("type") != "mcp":
    raise SystemExit("Contoso Bank v2 Agent definition must contain one top-level MCP binding")

expected_tools = [
    "start_call",
    "verify_caller_otp",
    "search_loan_officer",
    "confirm_loan_officer",
    "transfer_to_officer",
    "end_call",
]
if tools[0].get("allowed_tools") != expected_tools:
    raise SystemExit("Contoso Bank v2 Agent definition has an unexpected MCP tool list")
if definition.get("tool_choice") != "auto":
    raise SystemExit("Contoso Bank v2 flat Agent must use tool_choice=auto")

urls = set()
connections = set()
bindings = 0

def collect(value):
    global bindings
    if isinstance(value, dict):
        if value.get("type") == "mcp":
            bindings += 1
            urls.add(str(value.get("server_url", "")).strip())
            connections.add(str(value.get("project_connection_id", "")).strip())
        for child in value.values():
            collect(child)
    elif isinstance(value, list):
        for child in value:
            collect(child)

collect(definition)
if bindings == 0:
    raise SystemExit("Agent definition contains no MCP bindings")
if len(urls) != 1 or "" in urls:
    raise SystemExit("Agent definition must contain exactly one MCP URL")
if len(connections) != 1 or "" in connections:
    raise SystemExit("Agent definition must contain exactly one MCP connection ID")

payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
print(
    "\t".join(
        [
            name,
            next(iter(urls)),
            next(iter(connections)),
            str(bindings),
            hashlib.sha256(payload).hexdigest(),
        ]
    )
)
PY
)"

IFS=$'\t' read -r AGENT_NAME MCP_URL CONNECTION_ID MCP_BINDINGS PAYLOAD_SHA <<< "${CONFIG_SUMMARY}"

[[ "${PROJECT_ENDPOINT}" =~ ^https://.+/api/projects/[^/]+/?$ ]] ||
  die "AZURE_VOICE_AGENTS_ENDPOINT must be HTTPS and end in /api/projects/<name>"
[[ "${PROJECT_ENDPOINT}" != *'<'* && "${PROJECT_ENDPOINT}" != *'>'* ]] ||
  die "AZURE_VOICE_AGENTS_ENDPOINT still contains a placeholder"
PROJECT_ENDPOINT="${PROJECT_ENDPOINT%/}"
[[ "${PROJECT_ID}" =~ ^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.CognitiveServices/accounts/[^/]+/projects/[^/]+$ ]] ||
  die "AZURE_AI_PROJECT_ID must be the full Foundry project ARM resource ID"
[[ "${PROJECT_ID}" != *'<'* && "${PROJECT_ID}" != *'>'* ]] ||
  die "AZURE_AI_PROJECT_ID still contains a placeholder"
ENDPOINT_ACCOUNT="${PROJECT_ENDPOINT#https://}"
ENDPOINT_ACCOUNT="${ENDPOINT_ACCOUNT%%.*}"
ENDPOINT_PROJECT="${PROJECT_ENDPOINT##*/}"
[[ "${PROJECT_ID,,}" == *"/accounts/${ENDPOINT_ACCOUNT,,}/projects/${ENDPOINT_PROJECT,,}" ]] ||
  die "AZURE_AI_PROJECT_ID does not match AZURE_VOICE_AGENTS_ENDPOINT"
[[ "${MCP_URL}" =~ ^https://.+/mcp(/[^/?]+)?([/?].*)?$ ]] ||
  die "packaged MCP URL is invalid: ${MCP_URL}"
[[ "${CONNECTION_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] ||
  die "packaged MCP connection ID is invalid: ${CONNECTION_ID}"

echo "config_valid agent=${AGENT_NAME} kind=voice shape=flat mcp_bindings=${MCP_BINDINGS} allowed_tools=6"
echo "customer_project=${PROJECT_ENDPOINT}"
echo "fixed_mcp=${MCP_URL} connection=${CONNECTION_ID}"

if [[ "${ACTION}" == "dry-run" ]]; then
  echo "dry_run=passed payload_sha256=${PAYLOAD_SHA}"
  exit 0
fi

for command_name in az curl jq; do
  command -v "${command_name}" >/dev/null 2>&1 ||
    die "missing command: ${command_name}"
done
if [[ "${ACTION}" == "apply" && "${SKIP_CONNECTION}" == false ]]; then
  [[ -s "${MCP_TOKEN_FILE}" ]] ||
    die "Contoso Bank MCP credential is missing: place the securely delivered token at ${MCP_TOKEN_FILE}"
fi

az account show --output none >/dev/null 2>&1 ||
  die "Azure CLI is not logged in; run az login"

UNAUTHENTICATED_STATUS="$(
  curl --silent --show-error \
    --output /dev/null \
    --write-out '%{http_code}' \
    --max-time 15 \
    "${MCP_URL}" || true
)"
[[ "${UNAUTHENTICATED_STATUS}" == "401" ]] ||
  die "MCP endpoint must reject unauthenticated requests with HTTP 401; got ${UNAUTHENTICATED_STATUS}"
echo "mcp_reachability=passed unauthenticated_status=401"

umask 077
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/contoso-agent-deploy.XXXXXX")"
REMOTE_FILE="${WORK_DIR}/remote.json"
RESPONSE_FILE="${WORK_DIR}/response.json"
AZURE_HEADER_FILE="${WORK_DIR}/azure.headers"
MCP_HEADER_FILE="${WORK_DIR}/mcp.headers"
MCP_RESPONSE_FILE="${WORK_DIR}/mcp-response.txt"
CONNECTION_FILE="${WORK_DIR}/connection.json"

cleanup() {
  rm -f -- \
    "${REMOTE_FILE}" \
    "${RESPONSE_FILE}" \
    "${AZURE_HEADER_FILE}" \
    "${MCP_HEADER_FILE}" \
    "${MCP_RESPONSE_FILE}" \
    "${CONNECTION_FILE}"
  rmdir -- "${WORK_DIR}" 2>/dev/null || true
}
trap cleanup EXIT

if [[ -e "${MCP_TOKEN_FILE}" && ! -s "${MCP_TOKEN_FILE}" ]]; then
  die "MCP token file is empty: ${MCP_TOKEN_FILE}"
fi
if [[ -s "${MCP_TOKEN_FILE}" ]]; then
  MCP_TOKEN="$(<"${MCP_TOKEN_FILE}")"
  printf 'Authorization: Bearer %s\n' "${MCP_TOKEN}" > "${MCP_HEADER_FILE}"
  unset MCP_TOKEN
  MCP_STATUS="$(
    curl --silent --show-error \
      --request POST \
      --header "@${MCP_HEADER_FILE}" \
      --header 'Accept: application/json, text/event-stream' \
      --header 'Content-Type: application/json' \
      --data-binary '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"contoso-agent-deployer","version":"1.0"}}}' \
      --output "${MCP_RESPONSE_FILE}" \
      --write-out '%{http_code}' \
      --max-time 30 \
      "${MCP_URL}" || true
  )"
  [[ "${MCP_STATUS}" =~ ^2[0-9][0-9]$ ]] ||
    die "authenticated MCP initialize failed with HTTP ${MCP_STATUS}"
  echo "mcp_authentication=passed initialize_status=${MCP_STATUS}"
fi

if [[ "${ACTION}" == "apply" && "${SKIP_CONNECTION}" == false ]]; then
  MCP_TOKEN="$(<"${MCP_TOKEN_FILE}")"
  echo "connection_upsert_start id=${CONNECTION_ID}"
  jq -n \
    --arg target "${MCP_URL}" \
    --arg authorization "Bearer ${MCP_TOKEN}" \
    '{
      properties: {
        category: "RemoteTool",
        target: $target,
        authType: "CustomKeys",
        credentials: {
          keys: {
            Authorization: $authorization
          }
        }
      }
    }' > "${CONNECTION_FILE}"
  unset MCP_TOKEN
  az rest \
    --method put \
    --url "https://management.azure.com${PROJECT_ID}/connections/${CONNECTION_ID}?api-version=2025-06-01" \
    --body "@${CONNECTION_FILE}" \
    --output none
  echo "connection_upsert_complete id=${CONNECTION_ID}"
fi

if ! ACCESS_TOKEN="$(
  az account get-access-token \
    --scope "${TOKEN_SCOPE}" \
    --query accessToken \
    --output tsv
)"; then
  die "could not obtain a Foundry access token"
fi
[[ -n "${ACCESS_TOKEN}" ]] || die "Azure CLI returned an empty Foundry access token"
printf 'Authorization: Bearer %s\n' "${ACCESS_TOKEN}" > "${AZURE_HEADER_FILE}"
unset ACCESS_TOKEN

COLLECTION_URL="${PROJECT_ENDPOINT}/voice_agents?api-version=${API_VERSION}"
AGENT_URL="${PROJECT_ENDPOINT}/voice_agents/${AGENT_NAME}?api-version=${API_VERSION}"

read_agent() {
  curl --silent --show-error \
    --request GET \
    --header "@${AZURE_HEADER_FILE}" \
    --header "Foundry-Features: ${VOICE_AGENT_FEATURE}" \
    --header 'Accept: application/json' \
    --output "${REMOTE_FILE}" \
    --write-out '%{http_code}' \
    --max-time 90 \
    "${AGENT_URL}" || true
}

print_api_error() {
  python3 - "$1" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        payload = json.load(stream)
except (OSError, json.JSONDecodeError):
    raise SystemExit(0)

error = payload.get("error")
if isinstance(error, dict):
    message = error.get("message")
else:
    message = payload.get("message")
if message:
    print(f"service_error={message}", file=sys.stderr)
PY
}

compare_definition() {
  python3 - "${AGENT_CONFIG}" "${REMOTE_FILE}" <<'PY'
import hashlib
import json
import sys
from copy import deepcopy

expected_path, remote_path = sys.argv[1:]
with open(expected_path, encoding="utf-8") as stream:
    expected = json.load(stream)
with open(remote_path, encoding="utf-8") as stream:
    remote = json.load(stream)

latest = remote.get("versions", {}).get("latest", {})
observed = latest.get("definition")
if not isinstance(observed, dict):
    print("deployed Agent has no latest definition", file=sys.stderr)
    raise SystemExit(3)

def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()

def canonicalize(definition):
    value = deepcopy(definition)
    audio = value.get("audio")
    if not isinstance(audio, dict):
        return value

    input_config = audio.get("input")
    turn_detection = (
        input_config.get("turn_detection") if isinstance(input_config, dict) else None
    )
    if (
        isinstance(turn_detection, dict)
        and turn_detection.get("remove_filler_words") is False
    ):
        turn_detection.pop("remove_filler_words")

    output_config = audio.get("output")
    if isinstance(output_config, dict):
        voice = output_config.get("voice")
        voice_type = output_config.get("voice_type")
        if isinstance(voice, str) and isinstance(voice_type, str):
            output_config["voice"] = {"type": voice_type, "name": voice}
            output_config.pop("voice_type")
    return value

expected_definition = canonicalize(expected["definition"])
observed_definition = canonicalize(observed)
expected_hash = digest(expected_definition)
observed_hash = digest(observed_definition)
if expected_definition != observed_definition:
    print(
        f"agent_drift expected_sha256={expected_hash} "
        f"observed_sha256={observed_hash}",
        file=sys.stderr,
    )
    raise SystemExit(3)

print(
    f"agent_verified name={remote.get('name')} "
    f"version={latest.get('version')} definition_sha256={observed_hash}"
)
PY
}

GET_STATUS="$(read_agent)"
case "${GET_STATUS}" in
  200)
    if compare_definition; then
      echo "deploy_result=already_current agent=${AGENT_NAME}"
      exit 0
    fi
    if [[ "${ACTION}" == "check" ]]; then
      exit 3
    fi
    DEPLOY_URL="${AGENT_URL}"
    DEPLOY_MODE="update"
    ;;
  404)
    if [[ "${ACTION}" == "check" ]]; then
      die "Agent does not exist: ${AGENT_NAME}"
    fi
    DEPLOY_URL="${COLLECTION_URL}"
    DEPLOY_MODE="create"
    ;;
  *)
    print_api_error "${REMOTE_FILE}"
    die "Agent read failed with HTTP ${GET_STATUS}"
    ;;
esac

DEPLOY_STATUS="$(
  curl --silent --show-error \
    --request POST \
    --header "@${AZURE_HEADER_FILE}" \
    --header "Foundry-Features: ${VOICE_AGENT_FEATURE}" \
    --header 'Content-Type: application/json' \
    --header 'Accept: application/json' \
    --data-binary "@${AGENT_CONFIG}" \
    --output "${RESPONSE_FILE}" \
    --write-out '%{http_code}' \
    --max-time 120 \
    "${DEPLOY_URL}" || true
)"
if [[ ! "${DEPLOY_STATUS}" =~ ^2[0-9][0-9]$ ]]; then
  print_api_error "${RESPONSE_FILE}"
  die "Agent ${DEPLOY_MODE} failed with HTTP ${DEPLOY_STATUS}"
fi
echo "agent_${DEPLOY_MODE}=accepted status=${DEPLOY_STATUS}"

GET_STATUS="$(read_agent)"
if [[ "${GET_STATUS}" != "200" ]]; then
  print_api_error "${REMOTE_FILE}"
  die "Agent readback failed with HTTP ${GET_STATUS}"
fi
compare_definition || die "Agent readback does not match the packaged definition"
echo "deploy_result=completed agent=${AGENT_NAME} payload_sha256=${PAYLOAD_SHA}"
