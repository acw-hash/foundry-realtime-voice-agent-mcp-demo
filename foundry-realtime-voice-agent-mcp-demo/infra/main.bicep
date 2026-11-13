targetScope = 'subscription'

@minLength(1)
@maxLength(64)
param environmentName string

@minLength(1)
param location string

param sessionId string
param deployedBy string
param createdAt string

@minLength(1)
param deployerObjectId string

// Empty means bootstrap with the public placeholder image. The deployment tool
// supplies the ACR image for the actual-image phase.
param containerImage string = ''

var resourceGroupName = 'rg-contoso-bank-mcp'
var containerAppName = 'ca-contosomcp-dev-demo'
var managedEnvironmentName = 'cae-contosomcp-dev-demo'
var registryName = 'crcontosomcpdevdemo'
var storageAccountName = 'stcontosomcpdevdemo'
var keyVaultName = 'kv-contosomcp-dev-demo'
var logAnalyticsName = 'log-contosomcp-dev-demo'
var applicationInsightsName = 'appi-contosomcp-dev-demo'
var managedIdentityName = 'id-contosomcp-dev-demo'

var tags = {
  'app-onboard-skill': 'true'
  'app-onboard-session-id': sessionId
  'created-at': createdAt
  environment: environmentName
  'deployed-by': deployedBy
}

resource rg 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module identity './modules/managed-identity.bicep' = {
  name: 'managed-identity'
  scope: rg
  params: {
    identityName: managedIdentityName
    location: location
    tags: tags
  }
}

module logAnalytics './modules/log-analytics.bicep' = {
  name: 'log-analytics'
  scope: rg
  params: {
    workspaceName: logAnalyticsName
    location: location
    tags: tags
  }
}

module applicationInsights './modules/application-insights.bicep' = {
  name: 'application-insights'
  scope: rg
  params: {
    componentName: applicationInsightsName
    location: location
    workspaceResourceId: logAnalytics.outputs.resourceId
    tags: tags
  }
}

module keyVault './modules/key-vault.bicep' = {
  name: 'key-vault'
  scope: rg
  params: {
    vaultName: keyVaultName
    location: location
    tags: tags
  }
}

module registry './modules/container-registry.bicep' = {
  name: 'container-registry'
  scope: rg
  params: {
    registryName: registryName
    location: location
    tags: tags
  }
}

module storage './modules/storage.bicep' = {
  name: 'storage'
  scope: rg
  params: {
    storageAccountName: storageAccountName
    tableName: 'bankstate'
    location: location
    tags: tags
  }
}

module managedEnvironment './modules/container-environment.bicep' = {
  name: 'container-environment'
  scope: rg
  params: {
    environmentName: managedEnvironmentName
    location: location
    workspaceName: logAnalyticsName
    workspaceCustomerId: logAnalytics.outputs.customerId
    tags: tags
  }
}

module roleAssignments './modules/role-assignments.bicep' = {
  name: 'role-assignments'
  scope: rg
  dependsOn: [
    keyVault
    registry
    storage
  ]
  params: {
    keyVaultName: keyVaultName
    registryName: registryName
    storageAccountName: storageAccountName
    identityPrincipalId: identity.outputs.principalId
    deployerObjectId: deployerObjectId
  }
}

module containerApp './modules/container-app.bicep' = {
  name: 'container-app'
  scope: rg
  dependsOn: [
    roleAssignments
  ]
  params: {
    appName: containerAppName
    location: location
    managedEnvironmentId: managedEnvironment.outputs.resourceId
    containerImage: containerImage
    registryLoginServer: registry.outputs.loginServer
    keyVaultName: keyVaultName
    identityResourceId: identity.outputs.resourceId
    identityClientId: identity.outputs.clientId
    storageTableEndpoint: storage.outputs.tableEndpoint
    appInsightsConnectionString: applicationInsights.outputs.connectionString
    tags: tags
  }
}

output resourceGroupName string = rg.name
output registryName string = registry.outputs.registryName
output registryLoginServer string = registry.outputs.loginServer
output vaultName string = keyVault.outputs.vaultName
output mcpBaseUrl string = 'https://${containerApp.outputs.fqdn}'
output managedIdentityClientId string = identity.outputs.clientId
output tableEndpoint string = storage.outputs.tableEndpoint
