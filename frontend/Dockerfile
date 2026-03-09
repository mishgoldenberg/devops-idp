# Stage 1: Base
FROM node:20-alpine AS base
RUN apk add --no-cache libc6-compat
WORKDIR /app

# Stage 2: Dependencies
FROM base AS deps
COPY package.json ./
COPY frontend/package.json ./frontend/
RUN npm install

# Stage 3: Builder
FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
WORKDIR /app/frontend
# Build will generate .next/standalone
RUN npm run build

# Stage 4: Runner
FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production

RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# FIX: Copy the entire contents of the standalone folder to /app
# This includes the server.js and the pruned node_modules
COPY --from=builder /app/frontend/.next/standalone ./
# Copy static files to the .next/static folder
COPY --from=builder /app/frontend/.next/static ./.next/static
# Copy public folder
COPY --from=builder /app/frontend/public ./public

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]