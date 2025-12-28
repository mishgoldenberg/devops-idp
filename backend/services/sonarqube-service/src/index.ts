import express from 'express';
import helmet from 'helmet';
import cors from 'cors';
import { createLogger, SonarProject } from '@devops-control-center/shared';
import dotenv from 'dotenv';

dotenv.config();

const logger = createLogger('sonarqube-service');
const app = express();

const PORT = parseInt(process.env.PORT || '8003', 10);
const USE_MOCK = process.env.USE_MOCK_DATA !== 'false';

app.use(helmet());
app.use(cors());
app.use(express.json());

/**
 * Mock data for SonarQube projects
 */
function getMockProjects(): SonarProject[] {
  return [
    {
      key: 'backend-api',
      name: 'Backend API',
      quality_gate: { status: 'OK' },
      metrics: {
        coverage: 78.5,
        bugs: 3,
        vulnerabilities: 0,
        code_smells: 12,
        technical_debt: '2h 30m',
        duplications: 1.2,
        lines_of_code: 15420,
      },
      last_analysis: new Date('2024-01-15T08:30:00'),
    },
    {
      key: 'frontend-app',
      name: 'Frontend Application',
      quality_gate: { status: 'WARN' },
      metrics: {
        coverage: 62.3,
        bugs: 5,
        vulnerabilities: 1,
        code_smells: 28,
        technical_debt: '4h 15m',
        duplications: 3.8,
        lines_of_code: 22100,
      },
      last_analysis: new Date('2024-01-15T09:15:00'),
    },
  ];
}

/**
 * Get all projects
 */
app.get('/projects', async (req, res) => {
  try {
    const projects = getMockProjects();

    res.json({
      success: true,
      data: projects,
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to fetch projects', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to fetch projects',
    });
  }
});

/**
 * Get project by key
 */
app.get('/projects/:key', async (req, res) => {
  try {
    const { key } = req.params;
    const projects = getMockProjects();
    const project = projects.find(p => p.key === key);

    if (!project) {
      return res.status(404).json({
        success: false,
        error: 'Project not found',
      });
    }

    res.json({
      success: true,
      data: project,
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to fetch project', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to fetch project',
    });
  }
});

/**
 * Enable PR scanning (called after approval)
 */
app.post('/enable-pr-scanning', async (req, res) => {
  try {
    const { projectKey } = req.body;

    if (!projectKey) {
      return res.status(400).json({
        success: false,
        error: 'Project key required',
      });
    }

    // Mock enabling PR scanning
    logger.info('PR scanning enabled', { projectKey });

    res.json({
      success: true,
      message: 'PR scanning enabled successfully',
      data: { projectKey, enabled: true },
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to enable PR scanning', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to enable PR scanning',
    });
  }
});

/**
 * Health check
 */
app.get('/health', (req, res) => {
  res.json({
    status: 'healthy',
    service: 'sonarqube-service',
    useMock: USE_MOCK,
    timestamp: new Date(),
  });
});

app.listen(PORT, () => {
  logger.info('SonarQube service started', { port: PORT });
});

