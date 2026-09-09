$ErrorActionPreference = 'Stop'
$target = Join-Path $PSScriptRoot '..\.env.generated-secrets'
$names = @(
  'POSTGRES_PASSWORD',
  'APP_SESSION_SECRET',
  'TELEGRAM_WEBHOOK_SECRET',
  'ENROLLMENT_TOKEN_PEPPER',
  'AUDIT_CHAIN_HMAC_KEY',
  'APPROVAL_ACTION_HMAC_KEY'
)
$generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try {
  $lines = foreach ($name in $names) {
    $bytes = New-Object byte[] 32
    $generator.GetBytes($bytes)
    $value = [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
    "$name=$value"
  }
} finally {
  $generator.Dispose()
}
[System.IO.File]::WriteAllLines($target, $lines)
Write-Output "Generated secrets at $target. Copy them into .env; the script never overwrites .env."
