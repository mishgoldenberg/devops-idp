import { Router } from 'express';
import { createProxyMiddleware } from 'http-proxy-middleware';
import { config } from '../config';
import { authenticate } from '../middleware/auth';
import dashboardRouter from './dashboard';
import healthRouter from './health';
import metricsRouter from './metrics';

const router = Router();

// Health check (no auth required)
router.use('/health', healthRouter);

// Dashboard routes
router.use('/dashboards', dashboardRouter);

// Metrics routes
router.use('/metrics', metricsRouter);

// Proxy to auth service
router.use('/auth', createProxyMiddleware({
  target: config.services.auth,
  changeOrigin: true,
  pathRewrite: { '^/api/auth': '' },
}));

// Proxy to approval service
router.use('/approvals', authenticate, createProxyMiddleware({
  target: config.services.approval,
  changeOrigin: true,
  pathRewrite: { '^/api/approvals': '' },
}));

// Proxy to Azure DevOps service
router.use('/azure-devops', authenticate, createProxyMiddleware({
  target: config.services.azureDevOps,
  changeOrigin: true,
  pathRewrite: { '^/api/azure-devops': '' },
}));

// Proxy to SonarQube service
router.use('/sonarqube', authenticate, createProxyMiddleware({
  target: config.services.sonarqube,
  changeOrigin: true,
  pathRewrite: { '^/api/sonarqube': '' },
}));

// Proxy to Artifactory service
router.use('/artifactory', authenticate, createProxyMiddleware({
  target: config.services.artifactory,
  changeOrigin: true,
  pathRewrite: { '^/api/artifactory': '' },
}));

// Proxy to ServiceNow service
router.use('/servicenow', authenticate, createProxyMiddleware({
  target: config.services.servicenow,
  changeOrigin: true,
  pathRewrite: { '^/api/servicenow': '' },
}));

// Proxy to AI Chatbot service
router.use('/ai-chatbot', authenticate, createProxyMiddleware({
  target: config.services.aiChatbot,
  changeOrigin: true,
  pathRewrite: { '^/api/ai-chatbot': '' },
}));

export default router;

