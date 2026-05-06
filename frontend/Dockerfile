# Frontend runtime for production:
# the UI is rendered by the backend FastAPI service, so this container
# simply proxies incoming traffic to backend-service.
ARG NGINX_BASE_IMAGE=nginx:1.29-alpine
FROM ${NGINX_BASE_IMAGE}

COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 3000

CMD ["nginx", "-g", "daemon off;"]