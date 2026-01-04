# DevOps Control Center - Setup Script for Windows
# This script automates the local development setup

$ErrorActionPreference = "Stop"

Write-Host "DevOps Control Center - Local Setup" -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host ""

# Function to check if command exists
function Test-Command {
    param($Command)
    $null = Get-Command $Command -ErrorAction SilentlyContinue
    return $?
}

# Check prerequisites
Write-Host "Checking prerequisites..." -ForegroundColor Yellow

$prerequisites = @{
    "Docker" = "docker"
    "Docker Compose" = "docker compose"
    "Node.js" = "node"
    "Git" = "git"
}

$missing = @()
foreach ($prereq in $prerequisites.GetEnumerator()) {
    $found = $false
    $version = ""
    
    if ($prereq.Value -eq "docker compose") {
        # Special handling for docker compose
        try {
            $version = (docker compose version --short 2>$null)
            if ($LASTEXITCODE -eq 0) {
                $found = $true
            }
        } catch {
            $found = $false
        }
    } else {
        if (Test-Command $prereq.Value) {
            $found = $true
            $version = (& $prereq.Value --version 2>$null | Select-Object -First 1)
        }
    }
    
    if ($found) {
        Write-Host "  [OK] $($prereq.Key): $version" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] $($prereq.Key): Not found" -ForegroundColor Red
        $missing += $prereq.Key
    }
}

if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host "[FAIL] Missing prerequisites: $($missing -join ', ')" -ForegroundColor Red
    Write-Host "Please install the missing tools and run this script again." -ForegroundColor Yellow
    exit 1
}

Write-Host ""

# Check if Docker daemon is running
Write-Host "Checking Docker daemon..." -ForegroundColor Yellow
try {
    docker ps | Out-Null
    Write-Host "  [OK] Docker daemon is running" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Docker daemon is not running" -ForegroundColor Red
    Write-Host "Please start Docker Desktop and run this script again." -ForegroundColor Yellow
    exit 1
}

Write-Host ""

# Check if .env file exists
Write-Host "Setting up environment variables..." -ForegroundColor Yellow
if (-Not (Test-Path ".env")) {
    if (Test-Path "env.example") {
        Copy-Item "env.example" ".env"
        Write-Host "  [OK] Created .env file from env.example" -ForegroundColor Green
        Write-Host "  [INFO] Using default values. Edit .env if you need to customize." -ForegroundColor Cyan
    } else {
        Write-Host "  [FAIL] env.example file not found!" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "  [OK] .env file already exists (skipping)" -ForegroundColor Green
}

Write-Host ""

# Build Docker images
Write-Host "Building Docker images..." -ForegroundColor Yellow
Write-Host "  This may take 5-10 minutes on first run..." -ForegroundColor Cyan
try {
    docker compose build
    if ($LASTEXITCODE -ne 0) {
        throw "Docker compose build failed"
    }
    Write-Host "  [OK] Docker images built successfully" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to build Docker images" -ForegroundColor Red
    Write-Host "Error: $_" -ForegroundColor Red
    exit 1
}

Write-Host ""

# Start services
Write-Host "Starting services..." -ForegroundColor Yellow
try {
    docker compose up -d
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to start services"
    }
    Write-Host "  [OK] Services started" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to start services" -ForegroundColor Red
    Write-Host "Error: $_" -ForegroundColor Red
    exit 1
}

Write-Host ""

# Wait for database to be healthy
Write-Host "Waiting for database to be ready..." -ForegroundColor Yellow
$maxAttempts = 30
$attempt = 0
$healthy = $false

while ($attempt -lt $maxAttempts -and -not $healthy) {
    Start-Sleep -Seconds 2
    $attempt++
    
    $status = docker compose ps postgres --format json | ConvertFrom-Json
    if ($status.Health -eq "healthy") {
        $healthy = $true
        Write-Host "  [OK] Database is healthy" -ForegroundColor Green
    } else {
        Write-Host "  Waiting for database... ($attempt/$maxAttempts)" -ForegroundColor Cyan
    }
}

if (-not $healthy) {
    Write-Host "  [FAIL] Database failed to become healthy within timeout" -ForegroundColor Red
    Write-Host "Check logs with: docker compose logs postgres" -ForegroundColor Yellow
    exit 1
}

Write-Host ""

# Set DATABASE_URL for migration scripts
# Read .env file to get database credentials
$dbUser = "devops"
$dbPass = "Devops4ever"
$dbName = "devops_control_center"
$dbHost = "localhost"
$dbPort = "5432"

if (Test-Path ".env") {
    $envLines = Get-Content ".env"
    foreach ($line in $envLines) {
        if ($line -match "^POSTGRES_USER=(.+)$") { $dbUser = $matches[1].Trim() }
        if ($line -match "^POSTGRES_PASSWORD=(.+)$") { $dbPass = $matches[1].Trim() }
        if ($line -match "^POSTGRES_DB=(.+)$") { $dbName = $matches[1].Trim() }
        if ($line -match "^POSTGRES_HOST=(.+)$") { $dbHost = $matches[1].Trim() }
        if ($line -match "^POSTGRES_PORT=(.+)$") { $dbPort = $matches[1].Trim() }
    }
}

$env:DATABASE_URL = "postgresql://${dbUser}:${dbPass}@${dbHost}:${dbPort}/${dbName}"

# Run migrations
Write-Host "Running database migrations..." -ForegroundColor Yellow
try {
    npm run migrate
    if ($LASTEXITCODE -ne 0) {
        throw "Migration failed"
    }
    Write-Host "  [OK] Migrations completed successfully" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to run migrations" -ForegroundColor Red
    Write-Host "Error: $_" -ForegroundColor Red
    Write-Host "Check database connection in .env file" -ForegroundColor Yellow
    exit 1
}

Write-Host ""

# Run seeds
Write-Host "Seeding database with initial data..." -ForegroundColor Yellow
try {
    npm run seed
    if ($LASTEXITCODE -ne 0) {
        throw "Seeding failed"
    }
    Write-Host "  [OK] Database seeded successfully" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to seed database" -ForegroundColor Red
    Write-Host "Error: $_" -ForegroundColor Red
    exit 1
}

Write-Host ""

# Wait a bit more for all services to be ready
Write-Host "Waiting for all services to be ready..." -ForegroundColor Yellow
Start-Sleep -Seconds 5

# Check service status
Write-Host ""
Write-Host "Service Status:" -ForegroundColor Yellow
docker compose ps

Write-Host ""
Write-Host "[OK] Setup completed successfully!" -ForegroundColor Green
Write-Host ""
Write-Host "Access the application:" -ForegroundColor Cyan
Write-Host "  Frontend:  http://localhost:3000" -ForegroundColor White
Write-Host "  API Gateway: http://localhost:8000" -ForegroundColor White
Write-Host ""
Write-Host "Test users (login with any @internal username):" -ForegroundColor Cyan
Write-Host "  - admin@internal (Platform Admin)" -ForegroundColor White
Write-Host "  - lead@internal (Team Lead)" -ForegroundColor White
Write-Host "  - user@internal (Regular User)" -ForegroundColor White
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Cyan
Write-Host "  Quick restart: .\restart.ps1 (after backend code changes)" -ForegroundColor White
Write-Host "  Frontend rebuild: .\restart.ps1 -RebuildFrontend (after frontend changes)" -ForegroundColor White
Write-Host "  View logs:    docker compose logs -f" -ForegroundColor White
Write-Host "  Stop services: docker compose down" -ForegroundColor White
Write-Host ""

# Ask if user wants to open browser
$openBrowser = Read-Host "Open application in browser? (Y/n)"
if ($openBrowser -ne "n" -and $openBrowser -ne "N") {
    Start-Process "http://localhost:3000"
    Write-Host "  [OK] Opened browser" -ForegroundColor Green
}

Write-Host ""
Write-Host "Happy coding!" -ForegroundColor Cyan
