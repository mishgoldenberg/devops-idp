# Stage 1: Build
FROM node:20-alpine AS builder
WORKDIR /app

# Install dependencies
COPY package*.json ./
COPY frontend/package*.json ./frontend/
RUN npm install

# Build the app
COPY . .
WORKDIR /app/frontend
# Ensure next.config.js has output: 'standalone'
RUN npm run build

# Stage 2: Runner
FROM node:20-alpine AS runner
WORKDIR /app

# Create a non-root user for security
RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# Copy the standalone build and static assets
# Next.js puts server.js and its own pruned node_modules here
COPY --from=builder /app/frontend/.next/standalone ./
COPY --from=builder /app/frontend/.next/static ./.next/static
COPY --from=builder /app/frontend/public ./public

# Set permissions
RUN chown -R nextjs:nodejs /app

USER nextjs
EXPOSE 3000

CMD ["node", "server.js"]