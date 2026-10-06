# The backend image: FastAPI under uvicorn on port 8000, with the frontend's
# templates and static files. Build from the repository root:
#   docker build -f backend/app/Dockerfile .
# Offline-friendly: no apt packages, and pip reads PIP_INDEX_URL.

ARG PYTHON_BASE_IMAGE=python:3.11-slim
FROM ${PYTHON_BASE_IMAGE} AS base

# Environment variables for Python runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# The app uses psycopg2-binary, so no apt packages are required during the
# offline build. Keeping this image free of apt-get avoids needing a Debian
# package mirror inside a network without outbound internet access.

ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}
COPY backend/app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# backend/app becomes /app
COPY backend/app ./

# Copy frontend assets into the container so the HTMX UI can be served
# frontend/ becomes /app/frontend/
COPY frontend ./frontend

ENV PYTHONPATH=/app

EXPOSE 8000

# Behind the ingress: trust its X-Forwarded-* headers.
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

