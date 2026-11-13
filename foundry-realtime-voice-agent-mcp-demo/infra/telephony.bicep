targetScope = 'resourceGroup'

param location string = 'eastus2'
param containerImage string
param foundryProjectEndpoint string
param foundryAgentName string = 'contoso-call-routing-agent'
param phoneNumber string
param acsImmutableId string

@secure()
param acsAccessKey string

@secure()
@minLength(32)
param eventGridToken string

var appName = 'ca-contoso-voice-bridge'
var publicUrl = 'https://${appName}.${environment.properties.defaultDomain}'
var tags = {
  environment: 'demo'
  purpose: 'voice-first-telephony'
}

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' existing = {
  name: 'cae-contosomcp-private-demo'
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-04-01' existing = {
  name: 'crcontosomcpdevdemo'
}

resource acs 'Microsoft.Communication/communicationServices@2025-09-01' existing = {
  name: 'acs-contoso-bank-demo'
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: 'id-contoso-voice-bridge'
  location: location
  tags: tags
}

resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, 'AcrPull')
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [{ server: registry.properties.loginServer, identity: identity.id }]
      secrets: [
        { name: 'acs-access-key', value: acsAccessKey }
        { name: 'eventgrid-token', value: eventGridToken }
      ]
    }
    template: {
      scale: { minReplicas: 1, maxReplicas: 1 }
      containers: [
        {
          name: 'voice-bridge'
          image: containerImage
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'BRIDGE_PUBLIC_URL', value: publicUrl }
            { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
            { name: 'FOUNDRY_AGENT_NAME', value: foundryAgentName }
            { name: 'ACS_ENDPOINT', value: 'https://${acs.properties.hostName}' }
            { name: 'ACS_RESOURCE_ID', value: acs.id }
            { name: 'ACS_IMMUTABLE_ID', value: acsImmutableId }
            { name: 'ACS_PHONE_NUMBER', value: phoneNumber }
            { name: 'ACS_ACCESS_KEY', secretRef: 'acs-access-key' }
            { name: 'EVENTGRID_TOKEN', secretRef: 'eventgrid-token' }
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'PORT', value: '8000' }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8000 }
              initialDelaySeconds: 15
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/readyz', port: 8000 }
              initialDelaySeconds: 15
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 12
            }
          ]
        }
      ]
    }
  }
  dependsOn: [pull]
}

resource incoming 'Microsoft.EventGrid/eventSubscriptions@2022-06-15' = {
  name: 'contoso-voice-bridge-incoming'
  scope: acs
  properties: {
    eventDeliverySchema: 'EventGridSchema'
    destination: {
      endpointType: 'WebHook'
      properties: {
        endpointUrl: '${publicUrl}/events'
        deliveryAttributeMappings: [
          {
            name: 'Authorization'
            type: 'Static'
            properties: { value: 'Bearer ${eventGridToken}', isSecret: true }
          }
        ]
      }
    }
    filter: {
      includedEventTypes: ['Microsoft.Communication.IncomingCall']
    }
    retryPolicy: { maxDeliveryAttempts: 10, eventTimeToLiveInMinutes: 10 }
  }
  dependsOn: [app]
}

output bridgeUrl string = publicUrl
output appResourceId string = app.id
output identityPrincipalId string = identity.properties.principalId
output identityClientId string = identity.properties.clientId
output eventSubscriptionId string = incoming.id
output phoneNumber string = phoneNumber
output containerImage string = containerImage
output foundryAgentName string = foundryAgentName
