$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path ".env")) {
    Write-Error "Missing .env. Copy .env.example to .env and fill in the required values."
}

uv run jev-remote
