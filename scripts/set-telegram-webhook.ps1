# set-telegram-webhook.ps1
# Reads TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, and APP_PUBLIC_HOST from
# .env, then does a clean delete + re-register so the secret token is always
# in sync. Prints getWebhookInfo at the end to confirm.
#
# Usage (from repo root):
#   .\scripts\set-telegram-webhook.ps1
#   .\scripts\set-telegram-webhook.ps1 -Host https://culprit-overstuff-singing.ngrok-free.dev
#   .\scripts\set-telegram-webhook.ps1 -CheckOnly

param(
    [string]$Host    = "",   # override APP_PUBLIC_HOST; or pass the ngrok URL directly
    [switch]$CheckOnly       # just print current webhook info, no changes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Load .env from repo root -------------------------------------------------
$envFile = Join-Path $PSScriptRoot ".." ".env"
if (-not (Test-Path $envFile)) {
    Write-Error ".env not found at $envFile — run from the repo root or pass -Host explicitly."
    exit 1
}

$env_vars = @{}
foreach ($line in Get-Content $envFile) {
    if ($line -match "^\s*#" -or $line -notmatch "=") { continue }
    $parts = $line -split "=", 2
    $env_vars[$parts[0].Trim()] = $parts[1].Trim()
}

$botToken     = $env_vars["TELEGRAM_BOT_TOKEN"]
$webhookSecret = $env_vars["TELEGRAM_WEBHOOK_SECRET"]
$publicHost   = if ($Host) { $Host } else { $env_vars["APP_PUBLIC_HOST"] }

if (-not $botToken)      { Write-Error "TELEGRAM_BOT_TOKEN not set in .env"; exit 1 }
if (-not $webhookSecret) { Write-Error "TELEGRAM_WEBHOOK_SECRET not set in .env"; exit 1 }
if (-not $publicHost)    { Write-Error "APP_PUBLIC_HOST not set in .env and -Host not provided"; exit 1 }

$publicHost   = $publicHost.TrimEnd("/")
$webhookUrl   = "$publicHost/api/v1/telegram/webhook"
$apiBase      = "https://api.telegram.org/bot$botToken"

# --- Check-only mode ----------------------------------------------------------
if ($CheckOnly) {
    Write-Host "`n=== Current webhook info ===" -ForegroundColor Cyan
    (Invoke-RestMethod "$apiBase/getWebhookInfo").result | Format-List
    exit 0
}

# --- Delete existing webhook (drops any pending queued updates) ---------------
Write-Host "`n--- Deleting existing webhook ---" -ForegroundColor Yellow
$del = Invoke-RestMethod -Method POST "$apiBase/deleteWebhook" -Body @{ drop_pending_updates = "true" }
if ($del.ok) { Write-Host "Deleted. Pending updates dropped." -ForegroundColor Green }
else         { Write-Error "deleteWebhook failed: $($del | ConvertTo-Json)" }

Start-Sleep -Seconds 1

# --- Register fresh with secret token ----------------------------------------
Write-Host "`n--- Registering webhook ---" -ForegroundColor Yellow
Write-Host "URL: $webhookUrl"
$set = Invoke-RestMethod -Method POST "$apiBase/setWebhook" -Body @{
    url          = $webhookUrl
    secret_token = $webhookSecret
}
if ($set.ok) { Write-Host "Registered: $($set.description)" -ForegroundColor Green }
else         { Write-Error "setWebhook failed: $($set | ConvertTo-Json)" }

Start-Sleep -Seconds 2

# --- Confirm ------------------------------------------------------------------
Write-Host "`n=== Webhook info ===" -ForegroundColor Cyan
$info = (Invoke-RestMethod "$apiBase/getWebhookInfo").result
$info | Format-List

if ($info.last_error_message) {
    Write-Host "Warning: last delivery error — $($info.last_error_message)" -ForegroundColor Yellow
} else {
    Write-Host "No delivery errors. Webhook is healthy." -ForegroundColor Green
}
