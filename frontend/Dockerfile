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
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# Copy standalone build from builder
COPY --from=builder /app/frontend/.next/standalone ./  
COPY --from=builder /app/frontend/.next/static ./.next/static
COPY --from=builder /app/frontend/public ./public

# Install only production dependencies inside standalone
RUN npm install --production

USER nextjs
EXPOSE 3000

# Run the Next.js standalone server
CMD ["node", "server.js"]