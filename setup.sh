#!/bin/bash

# DevOps Control Center - Setup Script for Linux/Mac
# This script automates the local development setup

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}🚀 DevOps Control Center - Local Setup${NC}"
echo -e "${CYAN}=====================================${NC}"
echo ""

# Function to check if command exists
check_command() {
    if command -v "$1" &> /dev/null; then
        return 0
    else
        return 1
    fi
}

# Check prerequisites
echo -e "${YELLOW}📋 Checking prerequisites...${NC}"

check_prerequisite() {
    local name=$1
    local command=$2
    
    if check_command "$command"; then
        local version
        if [ "$command" = "docker" ]; then
            version=$(docker --version 2>/dev/null | head -n 1)
        elif [ "$command" = "docker compose" ]; then
            version=$(docker compose version --short 2>/dev/null || echo "installed")
        else
            version=$($command --version 2>/dev/null | head -n 1)
        fi
        echo -e "  ${GREEN}✅ $name: $version${NC}"
        return 0
    else
        echo -e "  ${RED}❌ $name: Not found${NC}"
        return 1
    fi
}

MISSING=0

check_prerequisite "Docker" "docker" || MISSING=1
check_prerequisite "Docker Compose" "docker compose" || MISSING=1
check_prerequisite "Git" "git" || MISSING=1

if [ $MISSING -eq 1 ]; then
    echo ""
    echo -e "${RED}❌ Missing prerequisites${NC}"
    echo -e "${YELLOW}Please install the missing tools and run this script again.${NC}"
    exit 1
fi

echo ""

# Check if Docker daemon is running
echo -e "${YELLOW}🐳 Checking Docker daemon...${NC}"
if docker ps &> /dev/null; then
    echo -e "  ${GREEN}✅ Docker daemon is running${NC}"
else
    echo -e "  ${RED}❌ Docker daemon is not running${NC}"
    echo -e "${YELLOW}Please start Docker and run this script again.${NC}"
    exit 1
fi

echo ""

# Check if .env file exists
echo -e "${YELLOW}⚙️  Setting up environment variables...${NC}"
if [ ! -f ".env" ]; then
    if [ -f "env.example" ]; then
        cp env.example .env
        echo -e "  ${GREEN}✅ Created .env file from env.example${NC}"
        echo -e "  ${CYAN}ℹ️  Using default values. Edit .env if you need to customize.${NC}"
    else
        echo -e "  ${RED}❌ env.example file not found!${NC}"
        exit 1
    fi
else
    echo -e "  ${GREEN}✅ .env file already exists (skipping)${NC}"
fi

echo ""

# Build Docker images
echo -e "${YELLOW}🔨 Building Docker images...${NC}"
echo -e "  ${CYAN}This may take 5-10 minutes on first run...${NC}"
if docker compose build; then
    echo -e "  ${GREEN}✅ Docker images built successfully${NC}"
else
    echo -e "  ${RED}❌ Failed to build Docker images${NC}"
    exit 1
fi

echo ""

# Start services
echo -e "${YELLOW}🚀 Starting services...${NC}"
if docker compose up -d; then
    echo -e "  ${GREEN}✅ Services started${NC}"
else
    echo -e "  ${RED}❌ Failed to start services${NC}"
    exit 1
fi

echo ""

# Wait for database to be healthy
echo -e "${YELLOW}⏳ Waiting for database to be ready...${NC}"
MAX_ATTEMPTS=30
ATTEMPT=0
HEALTHY=0

while [ $ATTEMPT -lt $MAX_ATTEMPTS ] && [ $HEALTHY -eq 0 ]; do
    sleep 2
    ATTEMPT=$((ATTEMPT + 1))
    
    HEALTH_STATUS=$(docker compose ps postgres --format json 2>/dev/null | grep -o '"Health":"[^"]*"' | cut -d'"' -f4 || echo "")
    if [ "$HEALTH_STATUS" = "healthy" ]; then
        HEALTHY=1
        echo -e "  ${GREEN}✅ Database is healthy${NC}"
    else
        echo -e "  ${CYAN}⏳ Waiting for database... ($ATTEMPT/$MAX_ATTEMPTS)${NC}"
    fi
done

if [ $HEALTHY -eq 0 ]; then
    echo -e "  ${RED}❌ Database failed to become healthy within timeout${NC}"
    echo -e "${YELLOW}Check logs with: docker compose logs postgres${NC}"
    exit 1
fi

echo ""

# Set DATABASE_URL for migration scripts
# Read .env file to get database credentials with defaults
DB_USER="devops"
DB_PASS="Devops4ever"
DB_NAME="devops_control_center"
DB_HOST="localhost"
DB_PORT="5432"

if [ -f ".env" ]; then
    # Extract values from .env file
    while IFS= read -r line; do
        # Skip comments and empty lines
        case "$line" in
            \#*|'') continue ;;
        esac
        
        # Extract key and value
        key=$(echo "$line" | cut -d'=' -f1)
        value=$(echo "$line" | cut -d'=' -f2-)
        
        case "$key" in
            POSTGRES_USER) DB_USER="$value" ;;
            POSTGRES_PASSWORD) DB_PASS="$value" ;;
            POSTGRES_DB) DB_NAME="$value" ;;
            POSTGRES_HOST) DB_HOST="$value" ;;
            POSTGRES_PORT) DB_PORT="$value" ;;
        esac
    done < <(grep -E "^POSTGRES_" .env 2>/dev/null || true)
fi

export DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}"

# Run migrations
echo -e "${YELLOW}📊 Running database migrations...${NC}"
if docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "${DB_USER}" -d "${DB_NAME}" -f deployment/charts/infrastructure/database/00_schema.sql; then
    echo -e "  ${GREEN}✅ Migrations completed successfully${NC}"
else
    echo -e "  ${RED}❌ Failed to run migrations${NC}"
    echo -e "${YELLOW}Check database connection in .env file${NC}"
    exit 1
fi

echo ""

# Run seeds
echo -e "${YELLOW}🌱 Seeding database with initial data...${NC}"
SEED_FILES=(deployment/charts/infrastructure/database/0[1-9]_*.sql)
for seed_file in "${SEED_FILES[@]}"; do
    echo -e "  ${CYAN}Applying seed: ${seed_file}${NC}"
    if ! docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "${DB_USER}" -d "${DB_NAME}" -f "${seed_file}"; then
        echo -e "  ${RED}❌ Failed to apply seed: ${seed_file}${NC}"
        exit 1
    fi
done

echo -e "  ${GREEN}✅ Database seeded successfully${NC}"

echo ""

# Wait a bit more for all services to be ready
echo -e "${YELLOW}⏳ Waiting for all services to be ready...${NC}"
sleep 5

# Check service status
echo ""
echo -e "${YELLOW}📊 Service Status:${NC}"
docker compose ps

echo ""
echo -e "${GREEN}✅ Setup completed successfully!${NC}"
echo ""
echo -e "${CYAN}🌐 Access the application:${NC}"
echo -e "  ${NC}UI:  http://localhost:8000/ui/${NC}"
echo -e "  ${NC}API: http://localhost:8000/api/${NC}"
echo -e "  ${NC}UI:  http://localhost:8000/ui/${NC}"
echo -e "  ${NC}API: http://localhost:8000/api/${NC}"
echo ""
echo -e "${CYAN}👤 Test users (login with any @internal username):${NC}"
echo -e "  ${NC}- admin@internal (Platform Admin)${NC}"
echo -e "  ${NC}- lead@internal (Team Lead)${NC}"
echo -e "  ${NC}- user@internal (Regular User)${NC}"
echo ""
echo -e "${CYAN}📚 Useful commands:${NC}"
echo -e "  ${NC}Quick restart: ./restart.sh (after backend code changes)${NC}"
echo -e "  ${NC}Quick restart: ./restart.sh (after backend code changes)${NC}"
echo -e "  ${NC}View logs:    docker compose logs -f${NC}"
echo -e "  ${NC}Stop services: docker compose down${NC}"
echo ""

# Ask if user wants to open browser (Linux/Mac)
if command -v xdg-open &> /dev/null; then
    # Linux
    read -p "Open application in browser? (Y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        xdg-open "http://localhost:8000/ui/" 2>/dev/null || true
        xdg-open "http://localhost:8000/ui/" 2>/dev/null || true
        echo -e "  ${GREEN}✅ Opened browser${NC}"
    fi
elif command -v open &> /dev/null; then
    # Mac
    read -p "Open application in browser? (Y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        open "http://localhost:8000/ui/" 2>/dev/null || true
        open "http://localhost:8000/ui/" 2>/dev/null || true
        echo -e "  ${GREEN}✅ Opened browser${NC}"
    fi
fi

echo ""
echo -e "${CYAN}Happy coding! 🎉${NC}"

