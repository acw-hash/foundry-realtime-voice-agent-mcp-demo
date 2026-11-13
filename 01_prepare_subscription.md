# 01 - Prepare the Customer Subscription

Complete this guide before configuring or deploying the Contoso Bank Voice Agent package.
The result must be an East US 2 Microsoft Foundry project with the required role
assignments.

This project intentionally requires:

```text
definition.kind: voice
definition.model: gpt-realtime
```

A standard `definition.kind: prompt` Agent is not an acceptable substitute
because it does not preserve the same realtime audio, greeting, or MCP tool
configuration.

The customer owns the subscription, resource group, Foundry resource, Foundry
project, deployed Agent, project connection, and all Azure usage charges created
by this procedure.

Use East US 2 for this package. Voice Agents private-preview availability is
region-gated; do not substitute a different region without confirmation from
the Voice Agent team.

## Completion criteria

Do not continue to
[`02_build_and_deploy_voice_agent.md`](./02_build_and_deploy_voice_agent.md)
until all of these statements are true:

- The `Microsoft.CognitiveServices` provider reports `Registered`.
- The Foundry resource and project both report `Succeeded`.
- The deployment user has `Foundry User` and `Foundry Project Manager`.
- The project managed identity has `Foundry User`.
- You captured `PROJECT_ENDPOINT` and `PROJECT_ID`.

## 1. Choose names

The Foundry resource name must be globally unique because it becomes part of the
service endpoint.

```bash
SUBSCRIPTION="<customer-subscription-name-or-id>"
LOCATION="eastus2"
RESOURCE_GROUP="rg-contoso-voice-agent"
FOUNDRY_RESOURCE="<globally-unique-foundry-resource-name>"
FOUNDRY_PROJECT="contoso-voice-agent-demo"
```

Keep these variables in the same shell while completing this guide.

## 2. Install tools and sign in

Install the
[Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), Bash 4 or
later, Python 3.10 or later, `curl`, and `jq`. On Windows, use WSL.

```bash
az login
az account set --subscription "${SUBSCRIPTION}"
az account show \
  --query "{name:name,id:id,state:state}" \
  --output table
```

Confirm that the selected subscription is the customer's subscription before
creating anything.

## 3. Register the Azure resource provider

```bash
az provider register \
  --namespace Microsoft.CognitiveServices \
  --wait

az provider show \
  --namespace Microsoft.CognitiveServices \
  --query registrationState \
  --output tsv
```

The final command must also return `Registered`.

## 4. Create the East US 2 Foundry resource

Create a dedicated resource group:

```bash
az group create \
  --name "${RESOURCE_GROUP}" \
  --location "${LOCATION}"
```

Create an `AIServices` Foundry resource with project management and a
system-assigned managed identity:

```bash
az cognitiveservices account create \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${FOUNDRY_RESOURCE}" \
  --kind AIServices \
  --sku S0 \
  --location "${LOCATION}" \
  --custom-domain "${FOUNDRY_RESOURCE}" \
  --assign-identity \
  --allow-project-management true \
  --yes
```

Verify the resource:

```bash
az cognitiveservices account show \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${FOUNDRY_RESOURCE}" \
  --query "{location:location,state:properties.provisioningState,projectManagement:properties.allowProjectManagement}" \
  --output table
```

Expected values are `eastus2`, `Succeeded`, and `true`.

## 5. Create the customer Foundry project

```bash
az cognitiveservices account project create \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${FOUNDRY_RESOURCE}" \
  --project-name "${FOUNDRY_PROJECT}" \
  --location "${LOCATION}"
```

Capture the two identifiers required by the deployment package:

```bash
PROJECT_ENDPOINT="$(
  az cognitiveservices account project show \
    --resource-group "${RESOURCE_GROUP}" \
    --name "${FOUNDRY_RESOURCE}" \
    --project-name "${FOUNDRY_PROJECT}" \
    --query "properties.endpoints.\"AI Foundry API\"" \
    --output tsv
)"

PROJECT_ID="$(
  az cognitiveservices account project show \
    --resource-group "${RESOURCE_GROUP}" \
    --name "${FOUNDRY_RESOURCE}" \
    --project-name "${FOUNDRY_PROJECT}" \
    --query id \
    --output tsv
)"

printf 'PROJECT_ENDPOINT=%s\nPROJECT_ID=%s\n' \
  "${PROJECT_ENDPOINT}" "${PROJECT_ID}"
```

The values have these formats:

```text
PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
PROJECT_ID=/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.CognitiveServices/accounts/<resource>/projects/<project>
```

## 6. Assign Foundry roles

The deployment user needs both **Foundry User** and
**Foundry Project Manager** on the Foundry resource. The project managed
identity needs **Foundry User** on the same resource.

```bash
FOUNDRY_SCOPE="$(
  az cognitiveservices account show \
    --resource-group "${RESOURCE_GROUP}" \
    --name "${FOUNDRY_RESOURCE}" \
    --query id \
    --output tsv
)"

ARM_ACCESS_TOKEN="$(
  az account get-access-token \
    --resource https://management.azure.com/ \
    --query accessToken \
    --output tsv
)"

USER_OBJECT_ID="$(
  ARM_ACCESS_TOKEN="${ARM_ACCESS_TOKEN}" python3 - <<'PY'
import base64
import json
import os

payload = os.environ["ARM_ACCESS_TOKEN"].split(".")[1]
payload += "=" * (-len(payload) % 4)
print(json.loads(base64.urlsafe_b64decode(payload))["oid"])
PY
)"

unset ARM_ACCESS_TOKEN

PROJECT_PRINCIPAL_ID="$(
  az cognitiveservices account project show \
    --resource-group "${RESOURCE_GROUP}" \
    --name "${FOUNDRY_RESOURCE}" \
    --project-name "${FOUNDRY_PROJECT}" \
    --query identity.principalId \
    --output tsv
)"

az role assignment create \
  --assignee-object-id "${USER_OBJECT_ID}" \
  --assignee-principal-type User \
  --role "Foundry User" \
  --scope "${FOUNDRY_SCOPE}"

az role assignment create \
  --assignee-object-id "${USER_OBJECT_ID}" \
  --assignee-principal-type User \
  --role "Foundry Project Manager" \
  --scope "${FOUNDRY_SCOPE}"

az role assignment create \
  --assignee-object-id "${PROJECT_PRINCIPAL_ID}" \
  --assignee-principal-type ServicePrincipal \
  --role "Foundry User" \
  --scope "${FOUNDRY_SCOPE}"
```

The person performing these assignments needs permission to create role
assignments, such as subscription or resource-group **Owner**.

Verify the effective assignments:

```bash
az role assignment list \
  --assignee-object-id "${USER_OBJECT_ID}" \
  --scope "${FOUNDRY_SCOPE}" \
  --query "[?roleDefinitionName=='Foundry User' || roleDefinitionName=='Foundry Project Manager'].roleDefinitionName" \
  --output tsv

az role assignment list \
  --assignee-object-id "${PROJECT_PRINCIPAL_ID}" \
  --scope "${FOUNDRY_SCOPE}" \
  --query "[?roleDefinitionName=='Foundry User'].roleDefinitionName" \
  --output tsv
```

The first command must list both roles, and the second must list
`Foundry User`. New assignments can take several minutes to propagate; wait and
rerun these checks before treating an immediate authorization failure as a
deployment defect.

## 7. Verify the project for deployment

Run the final checks:

```bash
az cognitiveservices account project show \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${FOUNDRY_RESOURCE}" \
  --project-name "${FOUNDRY_PROJECT}" \
  --query "{state:properties.provisioningState,endpoint:properties.endpoints.\"AI Foundry API\",id:id}" \
  --output json
```

Print shell-safe assignments for the next guide:

```bash
printf 'PROJECT_ENDPOINT=%q\n' "${PROJECT_ENDPOINT}"
printf 'PROJECT_ID=%q\n' "${PROJECT_ID}"
```

Copy both output lines. Guide 02 uses them to create `config/deploy.env`.

Continue with
[`02_build_and_deploy_voice_agent.md`](./02_build_and_deploy_voice_agent.md).

## References

- [Create a Microsoft Foundry project](https://learn.microsoft.com/azure/foundry/how-to/create-projects)
- [Microsoft Foundry RBAC](https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry)
- [Voice Agent with Foundry Agent Service](https://learn.microsoft.com/azure/ai-services/speech-service/voice-live-agents-quickstart)
