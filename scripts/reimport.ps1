<#
.SYNOPSIS
  Re-import the operational Excel workbook into PostgreSQL and reload the plan.

.DESCRIPTION
  Runs the importer inside the running api container against the workbook that is
  mounted at /data, then restarts the api so it rebuilds the plan from the
  refreshed data. Use this after editing
  database/Singapore_Logistics_Delivery_Planning_Dataset.xlsx.

  By default the importer upserts (insert/update by id). Pass -Replace to clear
  the depots/drivers/vehicles/orders tables first, so rows you deleted from the
  workbook are also removed from the database.

.EXAMPLE
  powershell -File scripts/reimport.ps1
  powershell -File scripts/reimport.ps1 -Replace
#>
param(
  [switch]$Replace,
  [string]$Workbook = '/data/Singapore_Logistics_Delivery_Planning_Dataset.xlsx',
  # Admin password for the post-import summary (fleet/plan counts), which
  # calls authenticated endpoints. Omit to skip the login and the summary;
  # the import and api restart still happen either way.
  [string]$AdminPassword
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

Push-Location $root
try {
  # Make sure the database and api are up before importing.
  docker compose up -d db api | Out-Null

  $importArgs = @('exec', '-T', 'api', 'python', '-m', 'mahjourney.import_operational', '--workbook', $Workbook)
  if ($Replace) {
    Write-Host 'Mode: REPLACE (tables cleared before import)' -ForegroundColor Yellow
    $importArgs += '--replace'
  } else {
    Write-Host 'Mode: UPSERT (insert/update by id; deletions not removed)' -ForegroundColor Cyan
  }

  Write-Host 'Importing workbook into PostgreSQL...'
  docker compose @importArgs

  Write-Host 'Restarting api to rebuild the plan from refreshed data...'
  docker compose restart api | Out-Null

  # Wait for readiness and report the resulting plan shape.
  $ready = $false
  foreach ($attempt in 1..30) {
    try {
      $response = Invoke-RestMethod http://localhost:8000/api/v1/ready -TimeoutSec 3
      if ($response.status -eq 'ready') { $ready = $true; break }
    } catch { Start-Sleep -Seconds 2 }
  }
  if (-not $ready) { throw 'api did not become ready after reimport.' }

  Write-Host ''
  Write-Host 'Reimport complete.' -ForegroundColor Green
  Write-Host ("  data_source : {0}" -f $response.data_source)

  # /fleet and /map/state require an admin session; skip the detailed summary
  # (rather than failing the whole script) if no password was supplied.
  if (-not $AdminPassword) {
    Write-Host '  (pass -AdminPassword to see depot/vehicle/order/plan counts)' -ForegroundColor DarkGray
    return
  }

  $adminUsername = $env:ADMIN_USERNAME
  if (-not $adminUsername) {
    foreach ($line in Get-Content (Join-Path $root '.env')) {
      if ($line -match '^ADMIN_USERNAME=(.*)$') { $adminUsername = $Matches[1]; break }
    }
  }
  if (-not $adminUsername) { $adminUsername = 'admin' }

  $session = $null
  try {
    Invoke-RestMethod http://localhost:8000/api/v1/auth/login -Method Post `
      -ContentType 'application/json' `
      -Body (@{ username = $adminUsername; password = $AdminPassword } | ConvertTo-Json) `
      -SessionVariable session -TimeoutSec 5 | Out-Null
  } catch {
    Write-Host '  Login failed; skipping detailed summary.' -ForegroundColor Yellow
    return
  }

  $fleet = Invoke-RestMethod http://localhost:8000/api/v1/fleet -WebSession $session -TimeoutSec 5
  $map = Invoke-RestMethod 'http://localhost:8000/api/v1/map/state?scenario_id=demo' -WebSession $session -TimeoutSec 5
  $assigned = ($map.plan.routes | ForEach-Object { $_.stops.Count } | Measure-Object -Sum).Sum
  $unassigned = ($map.plan.hard_violations | Where-Object { $_ -like 'unassigned*' }).Count

  Write-Host ("  depots      : {0}" -f $fleet.depots.Count)
  Write-Host ("  vehicles    : {0}" -f $fleet.vehicles.Count)
  Write-Host ("  orders      : {0}" -f $fleet.orders.Count)
  Write-Host ("  plan status : {0}" -f $map.plan.status)
  Write-Host ("  assigned    : {0} stops" -f $assigned)
  Write-Host ("  unassigned  : {0} orders" -f $unassigned)
}
finally {
  Pop-Location
}
