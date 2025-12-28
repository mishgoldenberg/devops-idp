import { Router } from 'express';
import { healthCheck } from '../db';
import { redisHealthCheck } from '../redis';
import { config } from '../config';
import axios from 'axios';

const router = Router();

/**
 * Health check endpoint
 */
router.get('/', async (req, res) => {
  const dbHealthy = await healthCheck();
  const redisHealthy = await redisHealthCheck();

  const status = dbHealthy && redisHealthy ? 'healthy' : 'unhealthy';
  const statusCode = status === 'healthy' ? 200 : 503;

  res.status(statusCode).json({
    status,
    timestamp: new Date(),
    services: {
      database: dbHealthy ? 'up' : 'down',
      redis: redisHealthy ? 'up' : 'down',
    },
  });
});

/**
 * Readiness check (for Kubernetes)
 */
router.get('/ready', async (req, res) => {
  const dbHealthy = await healthCheck();
  const redisHealthy = await redisHealthCheck();

  if (dbHealthy && redisHealthy) {
    res.status(200).json({ ready: true });
  } else {
    res.status(503).json({ ready: false });
  }
});

/**
 * Liveness check (for Kubernetes)
 */
router.get('/live', (req, res) => {
  res.status(200).json({ alive: true });
});

/**
 * Detailed health check (requires authentication)
 */
router.get('/detailed', async (req, res) => {
  const checks = await Promise.allSettled([
    checkService('auth', config.services.auth),
    checkService('azure-devops', config.services.azureDevOps),
    checkService('sonarqube', config.services.sonarqube),
    checkService('artifactory', config.services.artifactory),
    checkService('servicenow', config.services.servicenow),
    checkService('ai-chatbot', config.services.aiChatbot),
    checkService('approval', config.services.approval),
  ]);

  const services: any = {};
  checks.forEach((result, index) => {
    const serviceName = ['auth', 'azure-devops', 'sonarqube', 'artifactory', 'servicenow', 'ai-chatbot', 'approval'][index];
    services[serviceName] = result.status === 'fulfilled' ? result.value : { status: 'down', error: 'timeout' };
  });

  res.json({
    status: 'ok',
    timestamp: new Date(),
    database: await healthCheck() ? 'up' : 'down',
    redis: await redisHealthCheck() ? 'up' : 'down',
    services,
  });
});

async function checkService(name: string, url: string): Promise<any> {
  try {
    const start = Date.now();
    const response = await axios.get(`${url}/health`, { timeout: 2000 });
    const responseTime = Date.now() - start;
    
    return {
      status: response.status === 200 ? 'up' : 'down',
      responseTime,
    };
  } catch (error) {
    return {
      status: 'down',
      error: 'unreachable',
    };
  }
}

export default router;

