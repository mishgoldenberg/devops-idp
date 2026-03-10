# -------------------------------
# Stage 1: Base image
# -------------------------------
FROM node:20-alpine AS base
RUN apk add --no-cache libc6-compat
WORKDIR /app

# -------------------------------
# Stage 2: Dependencies
# -------------------------------
FROM base AS deps
# Copy root and frontend package.json (for hoisting)
COPY package.json ./ 
COPY frontend/package.json ./frontend/
# Install all dependencies (hoisted)
RUN npm install

# -------------------------------
# Stage 3: Builder
# -------------------------------
FROM base AS builder
# Copy node_modules from deps
COPY --from=deps /app/node_modules ./node_modules
# Copy source code
COPY . .

# Build environment variables
ARG NEXT_PUBLIC_API_GATEWAY_URL
ARG NEXT_PUBLIC_APP_NAME
ENV NEXT_PUBLIC_API_GATEWAY_URL=$NEXT_PUBLIC_API_GATEWAY_URL
ENV NEXT_PUBLIC_APP_NAME=$NEXT_PUBLIC_APP_NAME

# Build Next.js app in frontend
WORKDIR /app/frontend
RUN npm run build

# -------------------------------
# Stage 4: Runner (Production)
# -------------------------------
# Stage 4: Runner (Minimal production runtime)
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production

RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# 1. Copy the entire standalone folder
# This ALREADY contains the pruned node_modules you need
COPY --from=builder /app/frontend/.next/standalone ./

# 2. Copy static files and public folder
COPY --from=builder /app/frontend/.next/static ./.next/static
COPY --from=builder /app/frontend/public ./public

COPY --from=builder /app/frontend/.next/standalone/package.json ./package.json
RUN npm install --production

# 3. Ensure permissions are correct for the nextjs user
RUN chown -R nextjs:nodejs /app

USER nextjs
EXPOSE 3000

# Next.js standalone server entry point
CMD ["node", "server.js"]