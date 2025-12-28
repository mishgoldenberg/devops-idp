import express from 'express';
import helmet from 'helmet';
import cors from 'cors';
import { createLogger } from '@devops-control-center/shared';
import { MockAzureDevOpsAdapter } from './adapters/mock-adapter';
import { RealAzureDevOpsAdapter } from './adapters/real-adapter';
import dotenv from 'dotenv';

dotenv.config();

const logger = createLogger('azure-devops-service');
const app = express();

const PORT = parseInt(process.env.PORT || '8002', 10);
const USE_MOCK = process.env.USE_MOCK_DATA !== 'false';

// Initialize adapter
const adapter = USE_MOCK
  ? new MockAzureDevOpsAdapter()
  : new RealAzureDevOpsAdapter(
      process.env.AZURE_DEVOPS_ORG!,
      process.env.AZURE_DEVOPS_PAT!
    );

logger.info('Azure DevOps service initialized', { useMock: USE_MOCK });

// Middleware
app.use(helmet());
app.use(cors());
app.use(express.json());

/**
 * Get work items for user
 */
app.get('/work-items', async (req, res) => {
  try {
    const username = req.query.username as string || req.headers['x-user-username'] as string;
    
    if (!username) {
      return res.status(400).json({
        success: false,
        error: 'Username required',
      });
    }

    const workItems = await adapter.getWorkItems(username);

    res.json({
      success: true,
      data: workItems,
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to fetch work items', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to fetch work items',
      message: error.message,
    });
  }
});

/**
 * Get pull requests for user
 */
app.get('/pull-requests', async (req, res) => {
  try {
    const username = req.query.username as string || req.headers['x-user-username'] as string;
    
    if (!username) {
      return res.status(400).json({
        success: false,
        error: 'Username required',
      });
    }

    const pullRequests = await adapter.getPullRequests(username);

    res.json({
      success: true,
      data: pullRequests,
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to fetch pull requests', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to fetch pull requests',
      message: error.message,
    });
  }
});

/**
 * Get pipeline runs
 */
app.get('/pipelines', async (req, res) => {
  try {
    const projectName = req.query.project as string;
    const pipelines = await adapter.getPipelineRuns(projectName);

    res.json({
      success: true,
      data: pipelines,
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to fetch pipelines', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to fetch pipelines',
      message: error.message,
    });
  }
});

/**
 * Create Azure DevOps project (called after approval)
 */
app.post('/projects', async (req, res) => {
  try {
    const projectData = req.body;

    if (!projectData.name) {
      return res.status(400).json({
        success: false,
        error: 'Project name required',
      });
    }

    const project = await adapter.createProject(projectData);

    logger.info('Project created', { projectName: projectData.name });

    res.status(201).json({
      success: true,
      data: project,
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to create project', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to create project',
      message: error.message,
    });
  }
});

/**
 * Health check
 */
app.get('/health', (req, res) => {
  res.json({
    status: 'healthy',
    service: 'azure-devops-service',
    useMock: USE_MOCK,
    timestamp: new Date(),
  });
});

// Start server
app.listen(PORT, () => {
  logger.info('Azure DevOps service started', { port: PORT });
});

