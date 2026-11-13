param vaultName string
param location string
param tags object

resource vault 'Microsoft.KeyVault/vaults@2026-05-15' = {
  name: vaultName
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

// Secret values are generated and set by deploy.py after RBAC propagation.
// These existing declarations document the names used by the phase-two
// Container Apps secret references without creating or exposing values here.
resource mcpToken 'Microsoft.KeyVault/vaults/secrets@2026-05-15' existing = {
  parent: vault
  name: 'mcp-token'
}

resource otpAdminToken 'Microsoft.KeyVault/vaults/secrets@2026-05-15' existing = {
  parent: vault
  name: 'otp-admin-token'
}

output vaultName string = vault.name
output vaultUri string = vault.properties.vaultUri
