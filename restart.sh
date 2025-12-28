#!/bin/bash

# DevOps Control Center - Restart Script for Linux/Mac
# Quick script to restart services after code changes

REBUILD=false
REBUILD_FRONTEND=false
FULL=false
SERVICE=""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --rebuild)
            REBUILD=true
            shift
            ;;
        --rebuild-frontend)
            REBUILD_FRONTEND=true
            shift
            ;;
        --full)
            FULL=true
            shift
            ;;
        --service)
            SERVICE="$2"
            shift 2
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo "Usage: ./restart.sh [--rebuild] [--rebuild-frontend] [--full] [--service SERVICE_NAME]"
            exit 1
            ;;
    esac
done

# Don't use set -e here - we want to check docker commands manually
# set -e  # Exit on error

echo -e "${CYAN}🔄 DevOps Control Center - Restart${NC}"
echo -e "${CYAN}==================================${NC}"
echo ""

# If specific service provided, just restart that
if [ -n "$SERVICE" ]; then
    echo -e "${YELLOW}🔄 Restarting service: $SERVICE${NC}"
    if docker compose restart "$SERVICE"; then
        echo -e "  ${GREEN}✅ Service restarted${NC}"
    else
        echo -e "  ${RED}❌ Failed to restart service (check if service name is correct)${NC}"
        exit 1
    fi
    exit 0
fi

# Rebuild logic
if [ "$FULL" = true ]; then
    echo -e "${YELLOW}🔨 Rebuilding all services...${NC}"
    if ! docker compose build --no-cache; then
        echo -e "  ${RED}❌ Failed to rebuild services${NC}"
        exit 1
    fi
    echo -e "  ${GREEN}✅ All services rebuilt${NC}"
    echo ""
    echo -e "${YELLOW}🚀 Starting services...${NC}"
    if ! docker compose up -d; then
        echo -e "  ${RED}❌ Failed to start services${NC}"
        exit 1
    fi
    echo -e "  ${GREEN}✅ All services restarted${NC}"
elif [ "$REBUILD_FRONTEND" = true ]; then
    echo -e "${YELLOW}🔨 Rebuilding frontend...${NC}"
    if ! docker compose build frontend; then
        echo -e "  ${RED}❌ Failed to rebuild frontend${NC}"
        exit 1
    fi
    echo -e "  ${GREEN}✅ Frontend rebuilt${NC}"
    echo ""
    echo -e "${YELLOW}🔄 Restarting frontend...${NC}"
    if ! docker compose up -d frontend; then
        echo -e "  ${RED}❌ Failed to restart frontend${NC}"
        exit 1
    fi
    echo -e "  ${GREEN}✅ Frontend restarted${NC}"
elif [ "$REBUILD" = true ]; then
    echo -e "${YELLOW}🔨 Rebuilding services...${NC}"
    if ! docker compose build; then
        echo -e "  ${RED}❌ Failed to rebuild services${NC}"
        exit 1
    fi
    echo -e "  ${GREEN}✅ Services rebuilt${NC}"
    echo ""
    echo -e "${YELLOW}🔄 Restarting services...${NC}"
    if ! docker compose restart; then
        echo -e "  ${RED}❌ Failed to restart services${NC}"
        exit 1
    fi
    echo -e "  ${GREEN}✅ Services restarted${NC}"
else
    echo -e "${YELLOW}🔄 Restarting all services...${NC}"
    echo -e "  ${CYAN}(Use --rebuild-frontend for frontend changes, --rebuild for all services, --full for clean rebuild)${NC}"
    if ! docker compose restart; then
        echo -e "  ${RED}❌ Failed to restart services${NC}"
        exit 1
    fi
    echo -e "  ${GREEN}✅ All services restarted${NC}"
fi

echo ""
echo -e "${YELLOW}📊 Service Status:${NC}"
docker compose ps

echo ""
echo -e "${CYAN}💡 Tips:${NC}"
echo -e "  ${NC}- Backend code changes: ./restart.sh (uses volume mounts, no rebuild needed)"
echo -e "  ${NC}- Frontend changes: ./restart.sh --rebuild-frontend"
echo -e "  ${NC}- After dependency changes: ./restart.sh --rebuild"
echo -e "  ${NC}- Clean rebuild: ./restart.sh --full"
echo -e "  ${NC}- Restart specific service: ./restart.sh --service api-gateway"
echo ""
echo -e "${CYAN}📋 View logs: docker compose logs -f [service-name]${NC}"
echo ""

