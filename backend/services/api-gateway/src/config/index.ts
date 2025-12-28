import dotenv from 'dotenv';

dotenv.config();

export const config = {
  port: parseInt(process.env.PORT || '8000', 10),
  host: process.env.HOST || '0.0.0.0',
  nodeEnv: process.env.NODE_ENV || 'development',
  
  // Database
  database: {
    url: process.env.DATABASE_URL || 'postgresql://devops_user:devops_secure_password@localhost:5432/devops_control_center',
  },
  
  // Redis
  redis: {
    host: process.env.REDIS_HOST || 'localhost',
    port: parseInt(process.env.REDIS_PORT || '6379', 10),
    password: process.env.REDIS_PASSWORD,
  },
  
  // Session
  session: {
    secret: process.env.SESSION_SECRET || 'change_this_in_production',
    expiryHours: parseInt(process.env.SESSION_EXPIRY_HOURS || '8', 10),
  },
  
  // JWT
  jwt: {
    secret: process.env.JWT_SECRET || 'change_this_in_production',
    expiresIn: process.env.JWT_EXPIRY || '8h',
  },
  
  // CORS
  cors: {
    origins: (process.env.CORS_ORIGINS || 'http://localhost:3000').split(','),
  },
  
  // Backend services
  services: {
    auth: process.env.AUTH_SERVICE_URL || 'http://localhost:8001',
    azureDevOps: process.env.AZURE_DEVOPS_SERVICE_URL || 'http://localhost:8002',
    sonarqube: process.env.SONARQUBE_SERVICE_URL || 'http://localhost:8003',
    artifactory: process.env.ARTIFACTORY_SERVICE_URL || 'http://localhost:8004',
    servicenow: process.env.SERVICENOW_SERVICE_URL || 'http://localhost:8005',
    aiChatbot: process.env.AI_CHATBOT_SERVICE_URL || 'http://localhost:8006',
    approval: process.env.APPROVAL_SERVICE_URL || 'http://localhost:8007',
  },
  
  // Rate limiting
  rateLimit: {
    windowMs: parseInt(process.env.RATE_LIMIT_WINDOW_MS || '60000', 10),
    maxRequests: parseInt(process.env.RATE_LIMIT_MAX_REQUESTS || '100', 10),
  },
  
  // Development
  dev: {
    bypassAuth: process.env.DEV_MODE_BYPASS_AUTH === 'true',
    defaultUser: process.env.DEV_MODE_DEFAULT_USER || 'admin@internal',
  },
};

