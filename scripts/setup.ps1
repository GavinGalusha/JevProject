<#
.SYNOPSIS
  One-time setup for Jev Remote on Windows.

.DESCRIPTION
  Installs missing prerequisites (Git, uv, Google Chrome) with winget, installs the Python
  dependencies, creates .env with a generated access token, and opens the Private-network
  firewall rule. You only need to add TYPESAFE_API_KEY and OPENAI_API_KEY to .env afterwards.
  Safe to run again: existing .env values are never overwritten.

.PARAMETER InstallStartup
  Also register the sign-in Scheduled Task (requires an Administrator PowerShell).

.PARAMETER SkipFirewall
  Do not create the firewall rule.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
#>
param(
    [switch]$InstallStartup,
    [switch]$SkipFirewall
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectDir

function Write-Step($Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Write-Ok($Text) { Write-Host "    OK  $Text" -ForegroundColor Green }
function Write-Warn($Text) { Write-Host "    !!  $Text" -ForegroundColor Yellow }

function Update-SessionPath {
    # winget installs update PATH for new shells only; pick the change up in this one.
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Test-Command($Name) { return [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

function Test-Chrome {
    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)
    foreach ($root in $roots) {
        if ($root -and (Test-Path (Join-Path $root "Google\Chrome\Application\chrome.exe"))) { return $true }
    }
    return $false
}

function Install-WithWinget($Id, $Label) {
    if (-not (Test-Command "winget")) {
        throw "winget is not available. Install $Label manually, then re-run this script."
    }
    Write-Host "    Installing $Label with winget..."
    winget install --id $Id -e --accept-source-agreements --accept-package-agreements
    Update-SessionPath
}

function Get-EnvValue($Path, $Key) {
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line -match "^\s*$([regex]::Escape($Key))=(.*)$") { return $Matches[1].Trim() }
    }
    return ""
}

function Set-EnvValue($Path, $Key, $Value) {
    # Write UTF-8 without a BOM; a BOM would corrupt the first key for python-dotenv.
    $lines = [System.Collections.Generic.List[string]]([System.IO.File]::ReadAllLines($Path))
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^\s*$([regex]::Escape($Key))=") {
            $lines[$i] = "$Key=$Value"
            $found = $true
            break
        }
    }
    if (-not $found) { $lines.Add("$Key=$Value") }
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, ($lines -join "`r`n") + "`r`n", $utf8)
}

function New-Token {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# --- 1. Prerequisites ---------------------------------------------------------------------
Write-Step "Checking prerequisites"

if (Test-Command "git") { Write-Ok "Git" } else { Install-WithWinget "Git.Git" "Git" }
if (Test-Command "uv") { Write-Ok "uv" } else { Install-WithWinget "astral-sh.uv" "uv" }
if (Test-Chrome) { Write-Ok "Google Chrome" } else { Install-WithWinget "Google.Chrome" "Google Chrome" }

foreach ($tool in @("git", "uv")) {
    if (-not (Test-Command $tool)) {
        throw "$tool was installed but is not on PATH yet. Close this window, open a new PowerShell, and re-run the script."
    }
}

# --- 2. Python dependencies -----------------------------------------------------------------
Write-Step "Installing Python dependencies (uv sync)"
uv sync
if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
Write-Ok "Dependencies installed"

# --- 3. .env --------------------------------------------------------------------------------
Write-Step "Configuring .env"
$EnvFile = Join-Path $ProjectDir ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $ProjectDir ".env.example") $EnvFile
    Write-Ok "Created .env from .env.example"
} else {
    Write-Ok ".env already exists (keeping your values)"
}

if ((Get-EnvValue $EnvFile "JEV_REMOTE_TOKEN").Length -lt 32) {
    Set-EnvValue $EnvFile "JEV_REMOTE_TOKEN" (New-Token)
    Write-Ok "Generated JEV_REMOTE_TOKEN"
} else {
    Write-Ok "JEV_REMOTE_TOKEN already set"
}
foreach ($pair in @(
        @("BU_CDP_URL", "http://127.0.0.1:9222"),
        @("JEV_REMOTE_HOST", "0.0.0.0"),
        @("JEV_REMOTE_PORT", "8787"),
        @("JEV_AUTO_LAUNCH_CHROME", "1"))) {
    if (-not (Get-EnvValue $EnvFile $pair[0])) { Set-EnvValue $EnvFile $pair[0] $pair[1] }
}

# --- 4. Firewall and startup ------------------------------------------------------------------
Write-Step "Network access for your phone"
$port = [int](Get-EnvValue $EnvFile "JEV_REMOTE_PORT")
if ($SkipFirewall) {
    Write-Warn "Skipped firewall rule (-SkipFirewall)"
} elseif (Test-Admin) {
    if (-not (Get-NetFirewallRule -DisplayName "Jev Remote (Private LAN)" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName "Jev Remote (Private LAN)" -Direction Inbound `
            -Action Allow -Protocol TCP -LocalPort $port -Profile Private | Out-Null
    }
    Write-Ok "Firewall allows TCP $port on Private networks only"
} else {
    Write-Warn "Not running as Administrator, so the firewall rule was not created."
    Write-Warn "Re-run this script from an Administrator PowerShell, or allow Python on Private networks when Windows asks."
}

if ($InstallStartup) {
    if (Test-Admin) {
        & (Join-Path $PSScriptRoot "install-startup.ps1")
        Write-Ok "Start-at-sign-in task installed"
    } else {
        Write-Warn "-InstallStartup needs an Administrator PowerShell; skipped."
    }
}

# --- 5. Summary -------------------------------------------------------------------------------
Write-Step "Setup finished"
$missing = @()
foreach ($key in @("TYPESAFE_API_KEY", "OPENAI_API_KEY")) {
    if (-not (Get-EnvValue $EnvFile $key)) { $missing += $key }
}

if ($missing.Count -gt 0) {
    Write-Host "`nNext: open this file and fill in the missing keys:" -ForegroundColor Yellow
    Write-Host "    $EnvFile"
    foreach ($key in $missing) { Write-Host "    $key=" }
} else {
    Write-Host "`nAll required keys are set." -ForegroundColor Green
}

Write-Host "`nThen start the remote from this folder:"
Write-Host "    uv run jev-remote"
Write-Host "It opens the dedicated Chrome profile for you and prints a QR code to pair your phone."
Write-Host "Log into your streaming sites in that Chrome window only."
