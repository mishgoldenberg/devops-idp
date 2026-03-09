# Multi-stage build for Next.js frontend
FROM node:20-alpine AS base

# Install dependencies
FROM base AS deps
RUN apk add --no-cache libc6-compat
WORKDIR /app

# Copy package files
COPY package.json ./
COPY frontend/package.json ./frontend/

# Install dependencies in BOTH root and frontend
RUN npm install
RUN cd frontend && npm install

# Build stage
FROM base AS builder
WORKDIR /app

# Copy all source
COPY . .

# Copy node_modules from deps to both locations
COPY --from=deps /app/node_modules ./node_modules
COPY --from=deps /app/frontend/node_modules ./frontend/node_modules

# Set build-time environment variables
ARG NEXT_PUBLIC_API_GATEWAY_URL
ARG NEXT_PUBLIC_APP_NAME
ENV NEXT_PUBLIC_API_GATEWAY_URL=$NEXT_PUBLIC_API_GATEWAY_URL
ENV NEXT_PUBLIC_APP_NAME=$NEXT_PUBLIC_APP_NAME

# Build
WORKDIR /app/frontend
RUN npm run build

# Production stage
FROM base AS runner
WORKDIR /app
ENV NODE_ENV=production

RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# Copy built application from /app/frontend
COPY --from=builder /app/frontend/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/frontend/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/frontend/.next/static ./.next/static

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]