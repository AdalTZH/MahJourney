param(
  [string]$AdminPassword = $env:MAHJOURNEY_ADMIN_PASSWORD
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$generatedSecretsPath = Join-Path $root '.env.generated-secrets'

if (-not (Test-Path $generatedSecretsPath)) {
  throw 'Run scripts/generate-secrets.ps1 before the container smoke test.'
}

foreach ($line in Get-Content $generatedSecretsPath) {
  $name, $value = $line -split '=', 2
  [Environment]::SetEnvironmentVariable($name, $value, 'Process')
}

if (-not $AdminPassword) {
  $securePassword = Read-Host 'Admin password for the smoke test' -AsSecureString
  $credential = [System.Management.Automation.PSCredential]::new('admin', $securePassword)
  $AdminPassword = $credential.GetNetworkCredential().Password
}

$adminUsername = 'admin'
foreach ($line in Get-Content (Join-Path $root '.env')) {
  if ($line -match '^ADMIN_USERNAME=(.*)$') {
    $adminUsername = $Matches[1]
    break
  }
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$artifactDir = Join-Path $root "artifacts\container-$stamp"
New-Item -ItemType Directory -Path $artifactDir -Force | Out-Null

Push-Location $root
try {
  docker compose up -d --build

  $ready = $false
  foreach ($attempt in 1..30) {
    try {
      $response = Invoke-RestMethod http://localhost/api/v1/ready -TimeoutSec 3
      if ($response.status -eq 'ready') {
        $ready = $true
        break
      }
    } catch {
      Start-Sleep -Seconds 2
    }
  }
  if (-not $ready) { throw 'MahJourney did not become ready within 60 seconds.' }

  Invoke-RestMethod http://localhost/api/v1/auth/login -Method Post `
    -ContentType 'application/json' `
    -Body (@{ username = $adminUsername; password = $AdminPassword } | ConvertTo-Json) `
    -SessionVariable adminSession -TimeoutSec 10 | Out-Null

  $dispatcher = Invoke-WebRequest http://localhost/dispatcher -UseBasicParsing -TimeoutSec 15
  $scenario = Invoke-WebRequest http://localhost/scenario -UseBasicParsing -TimeoutSec 15
  $operations = Invoke-WebRequest http://localhost/operations -UseBasicParsing -TimeoutSec 15
  $map = Invoke-RestMethod http://localhost/api/v1/map/state `
    -WebSession $adminSession -TimeoutSec 15
  $evaluation = Invoke-RestMethod -Method Post http://localhost/api/v1/evaluations/run `
    -WebSession $adminSession -TimeoutSec 180
  $database = docker compose exec -T db psql -U mahjourney -d mahjourney -Atc `
    "SELECT string_agg(extname, ',') FROM pg_extension WHERE extname IN ('postgis','vector'); SELECT count(*) FROM information_schema.tables WHERE table_schema='public';"

  $summary = [ordered]@{
    timestamp = (Get-Date).ToString('o')
    dispatcher_status = $dispatcher.StatusCode
    scenario_status = $scenario.StatusCode
    operations_status = $operations.StatusCode
    trucks = $map.trucks.Count
    routes = $map.plan.routes.Count
    hard_violations = $map.plan.hard_violations.Count
    evaluation_scenarios = $evaluation.scenario_count
    cost_improvement_percent = $evaluation.median_cost_improvement_percent
    database_checks = @($database)
  }
  $summary | ConvertTo-Json -Depth 4 |
    Set-Content -Path (Join-Path $artifactDir 'summary.json') -Encoding utf8
  docker compose ps | Set-Content -Path (Join-Path $artifactDir 'compose-ps.txt') -Encoding utf8
  docker compose logs --no-color --tail 300 |
    ForEach-Object { $_ -replace '(?i)(authorization|accountkey|api[_-]?key|token|password)(\s*[:=]\s*)\S+', '$1$2[REDACTED]' } |
    Set-Content -Path (Join-Path $artifactDir 'compose.log') -Encoding utf8
  Write-Output "Container smoke artifacts: $artifactDir"
} finally {
  Pop-Location
}
