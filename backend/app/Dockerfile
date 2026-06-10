# DevOps Control Center - Python Backend Dockerfile
#
# This Dockerfile builds the FastAPI backend application.
# 
# BUILD CONTEXT: Repository root (. from docker build -f backend/app/Dockerfile .)
# OUTPUT IMAGE: Contains FastAPI + Uvicorn running on port 8000
# 
# LAYERS:
# 1. Base image: python:3.11-slim (lightweight Python image)
# 2. System dependencies for psycopg2 (PostgreSQL client)
# 3. Python dependencies from requirements.txt
# 4. Application code from backend/python_backend/
#
# ENVIRONMENT:
# - PYTHONDONTWRITEBYTECODE=1: Don't generate .pyc files (saves space)
# - PYTHONUNBUFFERED=1: Output is unbuffered (important for Docker logs)
# - PYTHONPATH=/app: So app modules can be imported

ARG PYTHON_BASE_IMAGE=python:3.11-slim
FROM ${PYTHON_BASE_IMAGE} AS base

# Environment variables for Python runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# The app uses psycopg2-binary, so no apt packages are required during the
# closed-network build. Keeping this image free of apt-get avoids needing a
# Debian mirror inside the closed network.

# Install Python dependencies
# requirements.txt includes: fastapi, uvicorn, psycopg2-binary, redis, PyJWT, pydantic, httpx
ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}
COPY backend/app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code into the container
# backend/app/ becomes /app/backend_app/
COPY backend/app ./

# Copy frontend assets into the container so the HTMX UI can be served
# frontend/ becomes /app/frontend/
COPY frontend ./frontend

# Set Python path so imports work correctly
ENV PYTHONPATH=/app

# Expose port for Kubernetes & docker-compose
EXPOSE 8000

# START COMMAND
# Uvicorn: ASGI server
# app.main:app: Module path to FastAPI application instance
# --host 0.0.0.0: Listen on all interfaces (required for container networking)
# --port 8000: Standard port for this service
#
# NOTE: For auto-reload during development, use --reload flag
# For production, configure multiple workers with --workers
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

