import express from 'express';
import helmet from 'helmet';
import cors from 'cors';
import { createLogger, Ticket, IncidentStats } from '@devops-control-center/shared';
import dotenv from 'dotenv';

dotenv.config();

const logger = createLogger('servicenow-service');
const app = express();

const PORT = parseInt(process.env.PORT || '8005', 10);
const USE_MOCK = process.env.USE_MOCK_DATA !== 'false';

app.use(helmet());
app.use(cors());
app.use(express.json());

/**
 * Get tickets for user
 */
app.get('/tickets', async (req, res) => {
  try {
    const username = req.query.username as string || req.headers['x-user-username'] as string;

    const tickets: Ticket[] = [
      {
        sys_id: 'INC0012345',
        number: 'INC0012345',
        short_description: 'Unable to access Azure DevOps',
        state: 'In Progress',
        priority: '2 - High',
        assigned_to: username,
        created: new Date('2024-01-14T10:00:00'),
        updated: new Date('2024-01-15T09:30:00'),
        url: 'https://servicenow.internal/nav_to.do?uri=incident.do?sys_id=INC0012345',
      },
      {
        sys_id: 'INC0012346',
        number: 'INC0012346',
        short_description: 'Dashboard widget not loading',
        state: 'New',
        priority: '3 - Moderate',
        assigned_to: username,
        created: new Date('2024-01-15T08:15:00'),
        updated: new Date('2024-01-15T08:15:00'),
        url: 'https://servicenow.internal/nav_to.do?uri=incident.do?sys_id=INC0012346',
      },
      {
        sys_id: 'RITM0045678',
        number: 'RITM0045678',
        short_description: 'Request access to Artifactory',
        state: 'Pending Approval',
        priority: '4 - Low',
        assigned_to: username,
        created: new Date('2024-01-13T14:30:00'),
        updated: new Date('2024-01-14T16:00:00'),
        url: 'https://servicenow.internal/nav_to.do?uri=sc_req_item.do?sys_id=RITM0045678',
      },
    ];

    res.json({
      success: true,
      data: tickets,
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to fetch tickets', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to fetch tickets',
    });
  }
});

/**
 * Get incident statistics
 */
app.get('/stats', async (req, res) => {
  try {
    const stats: IncidentStats = {
      open: 127,
      in_progress: 43,
      resolved: 892,
      total: 1062,
    };

    res.json({
      success: true,
      data: stats,
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to fetch stats', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to fetch statistics',
    });
  }
});

/**
 * Health check
 */
app.get('/health', (req, res) => {
  res.json({
    status: 'healthy',
    service: 'servicenow-service',
    useMock: USE_MOCK,
    timestamp: new Date(),
  });
});

app.listen(PORT, () => {
  logger.info('ServiceNow service started', { port: PORT });
});

