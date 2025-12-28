import express from 'express';
import helmet from 'helmet';
import cors from 'cors';
import { createLogger, Artifact, Repository, StorageInfo } from '@devops-control-center/shared';
import dotenv from 'dotenv';

dotenv.config();

const logger = createLogger('artifactory-service');
const app = express();

const PORT = parseInt(process.env.PORT || '8004', 10);
const USE_MOCK = process.env.USE_MOCK_DATA !== 'false';

app.use(helmet());
app.use(cors());
app.use(express.json());

/**
 * Get recent artifacts
 */
app.get('/artifacts', async (req, res) => {
  try {
    const artifacts: Artifact[] = [
      {
        name: 'backend-api-1.2.5.jar',
        path: '/releases/com/company/backend-api/1.2.5/',
        repo: 'libs-release',
        size: 45231872,
        created: new Date('2024-01-15T10:00:00'),
        modified: new Date('2024-01-15T10:00:00'),
        created_by: 'jenkins',
        download_count: 127,
      },
      {
        name: 'frontend-app-2.1.3.tar.gz',
        path: '/releases/com/company/frontend-app/2.1.3/',
        repo: 'npm-release',
        size: 12458912,
        created: new Date('2024-01-15T09:30:00'),
        modified: new Date('2024-01-15T09:30:00'),
        created_by: 'gitlab-ci',
        download_count: 89,
      },
      {
        name: 'shared-lib-0.9.2.jar',
        path: '/releases/com/company/shared-lib/0.9.2/',
        repo: 'libs-release',
        size: 2341504,
        created: new Date('2024-01-14T16:45:00'),
        modified: new Date('2024-01-14T16:45:00'),
        created_by: 'jenkins',
        download_count: 234,
      },
    ];

    res.json({
      success: true,
      data: artifacts,
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to fetch artifacts', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to fetch artifacts',
    });
  }
});

/**
 * Get repositories
 */
app.get('/repositories', async (req, res) => {
  try {
    const repos: Repository[] = [
      {
        key: 'libs-release',
        type: 'LOCAL',
        description: 'Release binaries',
        url: 'https://artifactory.internal/libs-release',
        package_type: 'maven',
      },
      {
        key: 'npm-release',
        type: 'LOCAL',
        description: 'NPM packages',
        url: 'https://artifactory.internal/npm-release',
        package_type: 'npm',
      },
      {
        key: 'docker-local',
        type: 'LOCAL',
        description: 'Docker images',
        url: 'https://artifactory.internal/docker-local',
        package_type: 'docker',
      },
    ];

    res.json({
      success: true,
      data: repos,
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to fetch repositories', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to fetch repositories',
    });
  }
});

/**
 * Get storage info
 */
app.get('/storage', async (req, res) => {
  try {
    const storage: StorageInfo = {
      used: 523487621120, // ~487 GB
      total: 1099511627776, // 1 TB
      percentage: 47.6,
      repositories: [
        { name: 'libs-release', used: 256743809024, percentage: 49.0 },
        { name: 'docker-local', used: 178956970752, percentage: 34.2 },
        { name: 'npm-release', used: 87786841344, percentage: 16.8 },
      ],
    };

    res.json({
      success: true,
      data: storage,
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to fetch storage info', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to fetch storage info',
    });
  }
});

/**
 * Health check
 */
app.get('/health', (req, res) => {
  res.json({
    status: 'healthy',
    service: 'artifactory-service',
    useMock: USE_MOCK,
    timestamp: new Date(),
  });
});

app.listen(PORT, () => {
  logger.info('Artifactory service started', { port: PORT });
});

