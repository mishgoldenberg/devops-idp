# Stage 1: Base (for shared environment)
FROM node:20-alpine AS base
RUN apk add --no-cache libc6-compat
WORKDIR /app

# Stage 2: Dependencies (Install everything in one go)
FROM base AS deps
# Copy root and frontend package.json
COPY package.json ./
COPY frontend/package.json ./frontend/
# Run install at root (hoisting dependencies)
RUN npm install

# Stage 3: Builder (Compile the app)
FROM base AS builder

# --- ADD THESE ARGS ---
ARG NEXT_PUBLIC_API_GATEWAY_URL
ARG NEXT_PUBLIC_APP_NAME

# --- MAP ARGS TO ENV ---
ENV NEXT_PUBLIC_API_GATEWAY_URL=$NEXT_PUBLIC_API_GATEWAY_URL
ENV NEXT_PUBLIC_APP_NAME=$NEXT_PUBLIC_APP_NAME

# Copy dependencies from the deps stage
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Run the build inside the frontend directory
WORKDIR /app/frontend
RUN npm run build

# Stage 4: Runner
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# 1. Copy everything from the nested standalone folder to the root
# The 'server.js' and the 'node_modules' are inside the 'frontend' folder
COPY --from=builder /app/frontend/.next/standalone/ ./
COPY --from=builder /app/node_modules ./node_modules

# 2. Copy static files (these are NOT inside the standalone folder)
COPY --from=builder /app/frontend/.next/static ./.next/static
COPY --from=builder /app/frontend/public ./public

# 3. Fix permissions
RUN chown -R nextjs:nodejs /app

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]