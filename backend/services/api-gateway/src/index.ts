import express from 'express';
import helmet from 'helmet';
import cors from 'cors';
import compression from 'compression';
import session from 'express-session';
import RedisStore from 'connect-redis';
import rateLimit from 'express-rate-limit';
import { config } from './config';
import { initRedis, redisClient } from './redis';
import { createLogger } from '@devops-control-center/shared';
import { requestId } from './middleware/request-id';
import { logApiCall } from './middleware/audit';
import router from './routes';

const logger = createLogger('api-gateway');

const app = express();

// Trust proxy (for correct IP addresses behind load balancer)
app.set('trust proxy', 1);

// Security middleware
app.use(helmet({
  contentSecurityPolicy: false, // Allow frontend to load resources
}));

// CORS
app.use(cors({
  origin: config.cors.origins,
  credentials: true,
}));

// Compression
app.use(compression());

// Body parsing (skip for proxy routes)
app.use((req, res, next) => {
  // Skip body parsing for routes that will be proxied
  if (req.path.startsWith('/api/auth') || 
      req.path.startsWith('/api/azure-devops') ||
      req.path.startsWith('/api/sonarqube') ||
      req.path.startsWith('/api/artifactory') ||
      req.path.startsWith('/api/servicenow') ||
      req.path.startsWith('/api/ai-chatbot') ||
      req.path.startsWith('/api/approvals')) {
    return next();
  }
  express.json({ limit: '10mb' })(req, res, next);
});
app.use((req, res, next) => {
  if (req.path.startsWith('/api/auth') || 
      req.path.startsWith('/api/azure-devops') ||
      req.path.startsWith('/api/sonarqube') ||
      req.path.startsWith('/api/artifactory') ||
      req.path.startsWith('/api/servicenow') ||
      req.path.startsWith('/api/ai-chatbot') ||
      req.path.startsWith('/api/approvals')) {
    return next();
  }
  express.urlencoded({ extended: true, limit: '10mb' })(req, res, next);
});

// Request ID
app.use(requestId);

// Session management (Redis-backed)
async function setupSession() {
  await initRedis();
  
  app.use(session({
    store: new RedisStore({ client: redisClient }),
    secret: config.session.secret,
    resave: false,
    saveUninitialized: false,
    cookie: {
      secure: config.nodeEnv === 'production',
      httpOnly: true,
      maxAge: config.session.expiryHours * 60 * 60 * 1000, // Convert hours to ms
    },
  }));
}

// Rate limiting
const limiter = rateLimit({
  windowMs: config.rateLimit.windowMs,
  max: config.rateLimit.maxRequests,
  message: {
    success: false,
    error: 'Too many requests, please try again later',
  },
  standardHeaders: true,
  legacyHeaders: false,
});

app.use('/api', limiter);

// API call logging (for metrics)
app.use('/api', logApiCall);

// Mount API routes
app.use('/api', router);

// Root endpoint
app.get('/', (req, res) => {
  res.json({
    name: 'DevOps Control Center API Gateway',
    version: '1.0.0',
    status: 'running',
    timestamp: new Date(),
  });
});

// 404 handler
app.use((req, res) => {
  res.status(404).json({
    success: false,
    error: 'Endpoint not found',
    path: req.path,
  });
});

// Error handler
app.use((err: any, req: express.Request, res: express.Response, next: express.NextFunction) => {
  logger.error('Unhandled error', {
    error: err.message,
    stack: err.stack,
    path: req.path,
  }, req.user?.id, req.requestId);

  res.status(err.status || 500).json({
    success: false,
    error: config.nodeEnv === 'production' ? 'Internal server error' : err.message,
    requestId: req.requestId,
  });
});

// Start server
async function start() {
  try {
    await setupSession();
    
    app.listen(config.port, config.host, () => {
      logger.info(`API Gateway started`, {
        host: config.host,
        port: config.port,
        env: config.nodeEnv,
      });
    });
  } catch (error: any) {
    logger.error('Failed to start server', { error: error.message });
    process.exit(1);
  }
}

// Graceful shutdown
process.on('SIGTERM', async () => {
  logger.info('SIGTERM received, shutting down gracefully');
  await redisClient.quit();
  process.exit(0);
});

process.on('SIGINT', async () => {
  logger.info('SIGINT received, shutting down gracefully');
  await redisClient.quit();
  process.exit(0);
});

start();

