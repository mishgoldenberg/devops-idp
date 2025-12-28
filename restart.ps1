# DevOps Control Center - Restart Script for Windows
# Quick script to restart services after code changes

param(
    [switch]$Rebuild,
    [switch]$RebuildFrontend,
    [switch]$Full,
    [string]$Service
)

# Set error preference - docker commands don't throw exceptions, they return exit codes
# We'll check $LASTEXITCODE after each docker command
$ErrorActionPreference = "Continue"

Write-Host "🔄 DevOps Control Center - Restart" -ForegroundColor Cyan
Write-Host "==================================" -ForegroundColor Cyan
Write-Host ""

# Helper function to check if last command succeeded
function Test-ExitCode {
    return ($LASTEXITCODE -eq 0)
}

# If specific service provided, just restart that
if ($Service) {
    Write-Host "🔄 Restarting service: $Service" -ForegroundColor Yellow
    docker compose restart $Service
    if (Test-ExitCode) {
        Write-Host "  ✅ Service restarted" -ForegroundColor Green
    } else {
        Write-Host "  ❌ Failed to restart service (check if service name is correct)" -ForegroundColor Red
        exit 1
    }
    exit 0
}

# Rebuild logic
if ($Full) {
    Write-Host "🔨 Rebuilding all services..." -ForegroundColor Yellow
    docker compose build --no-cache
    if (-not (Test-ExitCode)) {
        Write-Host "  ❌ Failed to rebuild services" -ForegroundColor Red
        exit 1
    }
    Write-Host "  ✅ All services rebuilt" -ForegroundColor Green
    Write-Host ""
    Write-Host "🚀 Starting services..." -ForegroundColor Yellow
    docker compose up -d
    if (-not (Test-ExitCode)) {
        Write-Host "  ❌ Failed to start services" -ForegroundColor Red
        exit 1
    }
    Write-Host "  ✅ All services restarted" -ForegroundColor Green
} elseif ($RebuildFrontend) {
    Write-Host "🔨 Rebuilding frontend..." -ForegroundColor Yellow
    docker compose build frontend
    if (-not (Test-ExitCode)) {
        Write-Host "  ❌ Failed to rebuild frontend" -ForegroundColor Red
        exit 1
    }
    Write-Host "  ✅ Frontend rebuilt" -ForegroundColor Green
    Write-Host ""
    Write-Host "🔄 Restarting frontend..." -ForegroundColor Yellow
    docker compose up -d frontend
    if (-not (Test-ExitCode)) {
        Write-Host "  ❌ Failed to restart frontend" -ForegroundColor Red
        exit 1
    }
    Write-Host "  ✅ Frontend restarted" -ForegroundColor Green
} elseif ($Rebuild) {
    Write-Host "🔨 Rebuilding services..." -ForegroundColor Yellow
    docker compose build
    if (-not (Test-ExitCode)) {
        Write-Host "  ❌ Failed to rebuild services" -ForegroundColor Red
        exit 1
    }
    Write-Host "  ✅ Services rebuilt" -ForegroundColor Green
    Write-Host ""
    Write-Host "🔄 Restarting services..." -ForegroundColor Yellow
    docker compose restart
    if (-not (Test-ExitCode)) {
        Write-Host "  ❌ Failed to restart services" -ForegroundColor Red
        exit 1
    }
    Write-Host "  ✅ Services restarted" -ForegroundColor Green
} else {
    Write-Host "🔄 Restarting all services..." -ForegroundColor Yellow
    Write-Host "  (Use -RebuildFrontend for frontend changes, -Rebuild for all services, -Full for clean rebuild)" -ForegroundColor Cyan
    docker compose restart
    if (-not (Test-ExitCode)) {
        Write-Host "  ❌ Failed to restart services" -ForegroundColor Red
        exit 1
    }
    Write-Host "  ✅ All services restarted" -ForegroundColor Green
}

Write-Host ""
Write-Host "📊 Service Status:" -ForegroundColor Yellow
docker compose ps

Write-Host ""
Write-Host "💡 Tips:" -ForegroundColor Cyan
Write-Host "  - Backend code changes: .\restart.ps1 (uses volume mounts, no rebuild needed)" -ForegroundColor White
Write-Host "  - Frontend changes: .\restart.ps1 -RebuildFrontend" -ForegroundColor White
Write-Host "  - After dependency changes: .\restart.ps1 -Rebuild" -ForegroundColor White
Write-Host "  - Clean rebuild: .\restart.ps1 -Full" -ForegroundColor White
Write-Host "  - Restart specific service: .\restart.ps1 -Service api-gateway" -ForegroundColor White
Write-Host ""
Write-Host "📋 View logs: docker compose logs -f [service-name]" -ForegroundColor Cyan
Write-Host ""

