$ErrorActionPreference = 'Continue'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$artifactDir = Join-Path $root "artifacts\$stamp"
New-Item -ItemType Directory -Path $artifactDir -Force | Out-Null

Push-Location (Join-Path $root 'backend')
try {
  uv run --group dev pytest --junitxml (Join-Path $artifactDir 'pytest.xml') --cov=mahjourney --cov-report "xml:$artifactDir\coverage.xml" *>&1 |
    ForEach-Object { $_ -replace '(?i)(api[_-]?key|token|password)(\s*[:=]\s*)\S+', '$1$2[REDACTED]' } |
    Tee-Object -FilePath (Join-Path $artifactDir 'backend.log')
  uv run python -m mahjourney.smoke *>&1 |
    ForEach-Object { $_ -replace '(?i)(authorization|accountkey|api[_-]?key|token|password)(\s*[:=]\s*)\S+', '$1$2[REDACTED]' } |
    Tee-Object -FilePath (Join-Path $artifactDir 'live-read-smoke.log')
} finally { Pop-Location }

Push-Location (Join-Path $root 'frontend')
try {
  npm run lint *>&1 | Tee-Object -FilePath (Join-Path $artifactDir 'frontend-lint.log')
  npm run build *>&1 | Tee-Object -FilePath (Join-Path $artifactDir 'frontend-build.log')
  $npmCommand = (Get-Command npm.cmd).Source
  & $npmCommand audit --omit=dev --json 2>$null |
    Set-Content -Path (Join-Path $artifactDir 'npm-audit.json') -Encoding utf8
} finally { Pop-Location }

Write-Output "Overnight artifacts: $artifactDir"
