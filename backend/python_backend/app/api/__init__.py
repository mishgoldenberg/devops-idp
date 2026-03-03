from fastapi import APIRouter

from . import auth, dashboards, health, metrics, azure_devops, sonarqube, artifactory, servicenow, ai_chatbot, approvals


api_router = APIRouter(prefix="/api")

# Health
api_router.include_router(health.router, prefix="/health", tags=["health"])

# Auth
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])

# Dashboards
api_router.include_router(dashboards.router, prefix="/dashboards", tags=["dashboards"])

# Metrics
api_router.include_router(metrics.router, prefix="/metrics", tags=["metrics"])

# External integrations (mocked behavior, similar to Node services)
api_router.include_router(azure_devops.router, prefix="/azure-devops", tags=["azure-devops"])
api_router.include_router(sonarqube.router, prefix="/sonarqube", tags=["sonarqube"])
api_router.include_router(artifactory.router, prefix="/artifactory", tags=["artifactory"])
api_router.include_router(servicenow.router, prefix="/servicenow", tags=["servicenow"])
api_router.include_router(ai_chatbot.router, prefix="/ai-chatbot", tags=["ai-chatbot"])

# Approvals
api_router.include_router(approvals.router, prefix="/approvals", tags=["approvals"])


