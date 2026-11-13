targetScope = 'resourceGroup'

@description('Azure region of the existing workload.')
param location string = 'eastus2'

@description('Application image built in the existing registry.')
param containerImage string

@secure()
@minLength(32)
@description('Independent MCP bearer token; supplied in memory by the deployment SDK.')
param mcpToken string

@secure()
@minLength(32)
@description('Independent OTP-administrator token; supplied in memory by the deployment SDK.')
param otpAdminToken string

@description('Resource tags shared with the existing workload.')
param tags object = {
  'app-onboard-skill': 'true'
  'app-onboard-session-id': 'demo-session'
  'created-at': 'demo-date'
  environment: 'contosomcp-dev-demo'
  'deployed-by': 'demo-operator'
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: 'id-contosomcp-dev-demo'
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-04-01' existing = {
  name: 'crcontosomcpdevdemo'
}

resource storage 'Microsoft.Storage/storageAccounts@2025-01-01' existing = {
  name: 'stcontosomcpdevdemo'
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: 'log-contosomcp-dev-demo'
}

resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: 'appi-contosomcp-dev-demo'
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-contosomcp-demo'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: ['10.208.0.0/22']
    }
    subnets: [
      {
        name: 'snet-aca'
        properties: {
          addressPrefix: '10.208.0.0/26'
          delegations: [
            {
              name: 'container-apps'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'snet-endpoints'
        properties: {
          addressPrefix: '10.208.1.0/27'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.table.${az.environment().suffixes.storage}'
  location: 'global'
  tags: tags
}

resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: 'link-contosomcp-demo'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}

resource endpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pep-contosomcp-table-demo'
  location: location
  tags: tags
  properties: {
    subnet: { id: '${vnet.id}/subnets/snet-endpoints' }
    privateLinkServiceConnections: [
      {
        name: 'table'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: ['table']
        }
      }
    ]
  }
}

resource zoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'table'
        properties: { privateDnsZoneId: zone.id }
      }
    ]
  }
}

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: 'cae-contosomcp-private-demo'
  location: location
  tags: tags
  properties: {
    infrastructureResourceGroup: 'rg-contosomcp-managed-demo'
    vnetConfiguration: {
      infrastructureSubnetId: '${vnet.id}/subnets/snet-aca'
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: 'ca-contosomcp-private-demo'
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
      registries: [
        { server: registry.properties.loginServer, identity: identity.id }
      ]
      secrets: [
        { name: 'mcp-token', value: mcpToken }
        { name: 'otp-admin-token', value: otpAdminToken }
      ]
    }
    template: {
      scale: { minReplicas: 1, maxReplicas: 1 }
      containers: [
        {
          name: 'contoso-bank-mcp'
          image: containerImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'CONTOSO_MCP_TOKEN', secretRef: 'mcp-token' }
            { name: 'CONTOSO_OTP_ADMIN_TOKEN', secretRef: 'otp-admin-token' }
            { name: 'CONTOSO_STATE_BACKEND', value: 'azure_table' }
            { name: 'CONTOSO_TABLE_ENDPOINT', value: storage.properties.primaryEndpoints.table }
            { name: 'CONTOSO_TABLE_NAME', value: 'bankstate' }
            { name: 'CONTOSO_DATA_DIR', value: '/app/data' }
            { name: 'CONTOSO_CUSTOMERS_FILE', value: '/app/customers.json' }
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
            { name: 'PORT', value: '8000' }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8000 }
              initialDelaySeconds: 15
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
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
  dependsOn: [zoneGroup, link]
}

output resourceGroupName string = resourceGroup().name
output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output mcpBaseUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output mcpAppResourceId string = app.id
output managedIdentityClientId string = identity.properties.clientId
output tableEndpoint string = storage.properties.primaryEndpoints.table
