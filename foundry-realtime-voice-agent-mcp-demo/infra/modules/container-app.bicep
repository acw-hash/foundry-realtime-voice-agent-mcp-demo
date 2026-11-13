param appName string
param location string
param managedEnvironmentId string
param containerImage string
param registryLoginServer string
param keyVaultName string
param identityResourceId string
param identityClientId string
param storageTableEndpoint string
param appInsightsConnectionString string
param tags object

var placeholderImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
var isPlaceholder = empty(containerImage) || containerImage == placeholderImage
var effectiveImage = isPlaceholder ? placeholderImage : containerImage
var effectivePort = isPlaceholder ? 80 : 8000
var readinessPath = isPlaceholder ? '/' : '/readyz'
var livenessPath = isPlaceholder ? '/' : '/healthz'

resource app 'Microsoft.App/containerApps@2026-07-01' = {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityResourceId}': {}
    }
  }
  properties: {
    managedEnvironmentId: managedEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: effectivePort
        transport: 'auto'
        allowInsecure: false
      }
      registries: isPlaceholder
        ? []
        : [
            {
              server: registryLoginServer
              identity: identityResourceId
            }
          ]
      secrets: isPlaceholder
        ? []
        : [
            {
              name: 'mcp-token'
              // #disable-next-line no-hardcoded-env-urls
              keyVaultUrl: 'https://${keyVaultName}.vault.azure.net/secrets/mcp-token'
              identity: identityResourceId
            }
            {
              name: 'otp-admin-token'
              // #disable-next-line no-hardcoded-env-urls
              keyVaultUrl: 'https://${keyVaultName}.vault.azure.net/secrets/otp-admin-token'
              identity: identityResourceId
            }
          ]
    }
    template: {
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      containers: [
        {
          name: 'contoso-bank-mcp'
          image: effectiveImage
          resources: {
            cpu: '0.5'
            memory: '1Gi'
          }
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: livenessPath
                port: effectivePort
              }
              initialDelaySeconds: 10
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: readinessPath
                port: effectivePort
              }
              initialDelaySeconds: 10
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ]
          env: concat(
            [
              {
                name: 'PORT'
                value: string(effectivePort)
              }
              {
                name: 'CONTOSO_STATE_BACKEND'
                value: 'azure_table'
              }
              {
                name: 'CONTOSO_TABLE_ENDPOINT'
                value: storageTableEndpoint
              }
              {
                name: 'CONTOSO_TABLE_NAME'
                value: 'bankstate'
              }
              {
                name: 'CONTOSO_DATA_DIR'
                value: '/app/data'
              }
              {
                name: 'CONTOSO_CUSTOMERS_FILE'
                value: '/app/customers.json'
              }
              {
                name: 'AZURE_CLIENT_ID'
                value: identityClientId
              }
              {
                name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
                value: appInsightsConnectionString
              }
            ],
            isPlaceholder
              ? []
              : [
                  {
                    name: 'CONTOSO_MCP_TOKEN'
                    secretRef: 'mcp-token'
                  }
                  {
                    name: 'CONTOSO_OTP_ADMIN_TOKEN'
                    secretRef: 'otp-admin-token'
                  }
                ]
          )
        }
      ]
    }
  }
}

output fqdn string = app.properties.configuration.ingress.fqdn
