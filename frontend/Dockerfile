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
# Copy dependencies from the deps stage
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Run the build inside the frontend directory
WORKDIR /app/frontend
RUN npm run build

# Stage 4: Runner (Minimal production runtime)
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# Copy only the standalone output and static assets
COPY --from=builder /app/frontend/.next/standalone/frontend ./
COPY --from=builder /app/frontend/.next/static ./.next/static
COPY --from=builder /app/frontend/public ./public

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]