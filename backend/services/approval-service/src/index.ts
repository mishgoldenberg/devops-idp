import express from 'express';
import helmet from 'helmet';
import cors from 'cors';
import { Pool } from 'pg';
import axios from 'axios';
import {
  createLogger,
  ApprovalRequest,
  ApprovalRequestType,
  ApprovalStatus,
  canApprove,
  AuthUser,
} from '@devops-control-center/shared';
import dotenv from 'dotenv';

dotenv.config();

const logger = createLogger('approval-service');
const app = express();

const PORT = parseInt(process.env.PORT || '8007', 10);

// Database
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
});

// Service URLs
const AZURE_DEVOPS_SERVICE_URL = process.env.AZURE_DEVOPS_SERVICE_URL || 'http://localhost:8002';
const SONARQUBE_SERVICE_URL = process.env.SONARQUBE_SERVICE_URL || 'http://localhost:8003';
const AI_CHATBOT_SERVICE_URL = process.env.AI_CHATBOT_SERVICE_URL || 'http://localhost:8006';

app.use(helmet());
app.use(cors());
app.use(express.json());

// Middleware to extract user from header (set by API Gateway)
function extractUser(req: express.Request): AuthUser | null {
  const userHeader = req.headers['x-user-data'];
  if (userHeader && typeof userHeader === 'string') {
    try {
      return JSON.parse(userHeader);
    } catch {
      return null;
    }
  }
  return null;
}

/**
 * Create approval request
 */
app.post('/requests', async (req, res) => {
  try {
    const user = extractUser(req);
    if (!user) {
      return res.status(401).json({
        success: false,
        error: 'Unauthorized',
      });
    }

    const { request_type, request_title, request_payload } = req.body;

    if (!request_type || !request_title || !request_payload) {
      return res.status(400).json({
        success: false,
        error: 'Missing required fields',
      });
    }

    // Check if user has permission to create this type of request
    const canCreate = await checkCreatePermission(user, request_type);
    if (!canCreate) {
      return res.status(403).json({
        success: false,
        error: 'Insufficient permissions to create this request type',
      });
    }

    const result = await pool.query(
      `INSERT INTO approval_requests (requester_id, request_type, request_title, request_payload, status)
       VALUES ($1, $2, $3, $4, 'PENDING')
       RETURNING *`,
      [user.id, request_type, request_title, JSON.stringify(request_payload)]
    );

    // Log audit
    await logAudit(user.id, 'CREATE_REQUEST', 'approval_request', result.rows[0].id);

    logger.info('Approval request created', { requestId: result.rows[0].id }, user.id);

    res.status(201).json({
      success: true,
      data: result.rows[0],
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to create request', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to create approval request',
    });
  }
});

/**
 * Get approval requests (filtered by role)
 */
app.get('/requests', async (req, res) => {
  try {
    const user = extractUser(req);
    if (!user) {
      return res.status(401).json({
        success: false,
        error: 'Unauthorized',
      });
    }

    const { status } = req.query;

    let query = `
      SELECT ar.*, 
             u.username as requester_username, 
             u.email as requester_email,
             approver.username as approver_username
      FROM approval_requests ar
      JOIN users u ON ar.requester_id = u.id
      LEFT JOIN users approver ON ar.approver_id = approver.id
      WHERE 1=1
    `;
    const params: any[] = [];

    // Filter by status if provided
    if (status) {
      params.push(status);
      query += ` AND ar.status = $${params.length}`;
    }

    // Show only user's own requests if not an approver
    if (!canApproveAny(user)) {
      params.push(user.id);
      query += ` AND ar.requester_id = $${params.length}`;
    }

    query += ' ORDER BY ar.created_at DESC LIMIT 100';

    const result = await pool.query(query, params);

    res.json({
      success: true,
      data: result.rows,
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to fetch requests', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to fetch approval requests',
    });
  }
});

/**
 * Get single approval request
 */
app.get('/requests/:id', async (req, res) => {
  try {
    const user = extractUser(req);
    if (!user) {
      return res.status(401).json({
        success: false,
        error: 'Unauthorized',
      });
    }

    const { id } = req.params;

    const result = await pool.query(
      `SELECT ar.*, 
              u.username as requester_username, 
              u.email as requester_email,
              approver.username as approver_username
       FROM approval_requests ar
       JOIN users u ON ar.requester_id = u.id
       LEFT JOIN users approver ON ar.approver_id = approver.id
       WHERE ar.id = $1`,
      [id]
    );

    if (result.rows.length === 0) {
      return res.status(404).json({
        success: false,
        error: 'Request not found',
      });
    }

    res.json({
      success: true,
      data: result.rows[0],
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to fetch request', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to fetch approval request',
    });
  }
});

/**
 * Approve request
 */
app.post('/requests/:id/approve', async (req, res) => {
  try {
    const user = extractUser(req);
    if (!user) {
      return res.status(401).json({
        success: false,
        error: 'Unauthorized',
      });
    }

    const { id } = req.params;
    const { comments } = req.body;

    // Get request
    const requestResult = await pool.query(
      'SELECT * FROM approval_requests WHERE id = $1',
      [id]
    );

    if (requestResult.rows.length === 0) {
      return res.status(404).json({
        success: false,
        error: 'Request not found',
      });
    }

    const request = requestResult.rows[0];

    if (request.status !== 'PENDING') {
      return res.status(400).json({
        success: false,
        error: 'Request is not pending',
      });
    }

    // Check if user can approve
    const ruleResult = await pool.query(
      'SELECT * FROM approval_rules WHERE request_type = $1',
      [request.request_type]
    );

    if (ruleResult.rows.length === 0 || !canApprove(user, ruleResult.rows[0].min_approver_role_level)) {
      return res.status(403).json({
        success: false,
        error: 'Insufficient permissions to approve this request',
      });
    }

    // Update request status
    await pool.query(
      `UPDATE approval_requests 
       SET status = 'APPROVED', approver_id = $1, approver_comments = $2, approved_at = CURRENT_TIMESTAMP
       WHERE id = $3`,
      [user.id, comments, id]
    );

    // Log audit
    await logAudit(user.id, 'APPROVE_REQUEST', 'approval_request', id);

    // Execute the approved action
    await executeApprovedRequest(request);

    logger.info('Request approved', { requestId: id }, user.id);

    res.json({
      success: true,
      message: 'Request approved successfully',
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to approve request', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to approve request',
    });
  }
});

/**
 * Reject request
 */
app.post('/requests/:id/reject', async (req, res) => {
  try {
    const user = extractUser(req);
    if (!user) {
      return res.status(401).json({
        success: false,
        error: 'Unauthorized',
      });
    }

    const { id } = req.params;
    const { comments } = req.body;

    // Get request
    const requestResult = await pool.query(
      'SELECT * FROM approval_requests WHERE id = $1',
      [id]
    );

    if (requestResult.rows.length === 0) {
      return res.status(404).json({
        success: false,
        error: 'Request not found',
      });
    }

    const request = requestResult.rows[0];

    if (request.status !== 'PENDING') {
      return res.status(400).json({
        success: false,
        error: 'Request is not pending',
      });
    }

    // Check if user can approve (can reject)
    const ruleResult = await pool.query(
      'SELECT * FROM approval_rules WHERE request_type = $1',
      [request.request_type]
    );

    if (ruleResult.rows.length === 0 || !canApprove(user, ruleResult.rows[0].min_approver_role_level)) {
      return res.status(403).json({
        success: false,
        error: 'Insufficient permissions to reject this request',
      });
    }

    // Update request status
    await pool.query(
      `UPDATE approval_requests 
       SET status = 'REJECTED', approver_id = $1, approver_comments = $2, approved_at = CURRENT_TIMESTAMP
       WHERE id = $3`,
      [user.id, comments || 'Rejected', id]
    );

    // Log audit
    await logAudit(user.id, 'REJECT_REQUEST', 'approval_request', id);

    logger.info('Request rejected', { requestId: id }, user.id);

    res.json({
      success: true,
      message: 'Request rejected',
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to reject request', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to reject request',
    });
  }
});

/**
 * Execute approved request
 */
async function executeApprovedRequest(request: any): Promise<void> {
  try {
    const payload = typeof request.request_payload === 'string'
      ? JSON.parse(request.request_payload)
      : request.request_payload;

    let executionResult: any;

    switch (request.request_type) {
      case ApprovalRequestType.ADO_PROJECT_CREATE:
        executionResult = await axios.post(`${AZURE_DEVOPS_SERVICE_URL}/projects`, payload);
        break;

      case ApprovalRequestType.SONAR_PR_SCANNING_ENABLE:
        executionResult = await axios.post(`${SONARQUBE_SERVICE_URL}/enable-pr-scanning`, payload);
        break;

      case ApprovalRequestType.AI_MODEL_ACCESS:
        // TODO: Implement AI model access granting
        executionResult = { data: { message: 'AI access granted' } };
        break;

      default:
        logger.warn('Unknown request type', { type: request.request_type });
        return;
    }

    // Update request with execution result
    await pool.query(
      `UPDATE approval_requests 
       SET status = 'EXECUTED', execution_result = $1, executed_at = CURRENT_TIMESTAMP
       WHERE id = $2`,
      [JSON.stringify(executionResult.data), request.id]
    );

    logger.info('Request executed', { requestId: request.id });
  } catch (error: any) {
    logger.error('Failed to execute request', {
      requestId: request.id,
      error: error.message,
    });

    // Update request with error
    await pool.query(
      `UPDATE approval_requests 
       SET status = 'FAILED', execution_result = $1
       WHERE id = $2`,
      [JSON.stringify({ error: error.message }), request.id]
    );
  }
}

/**
 * Check if user can create request type
 */
async function checkCreatePermission(user: AuthUser, requestType: string): Promise<boolean> {
  // Platform Admin can create any request
  if (user.hierarchy_level === 1) return true;

  // Role-based checks
  switch (requestType) {
    case ApprovalRequestType.ADO_PROJECT_CREATE:
      return user.hierarchy_level <= 5; // PM and above

    case ApprovalRequestType.SONAR_PR_SCANNING_ENABLE:
      return user.hierarchy_level <= 5; // PM and above

    case ApprovalRequestType.AI_MODEL_ACCESS:
      return true; // Anyone can request

    default:
      return false;
  }
}

/**
 * Check if user can approve any requests
 */
function canApproveAny(user: AuthUser): boolean {
  return user.hierarchy_level <= 5; // PM and above can see all requests
}

/**
 * Log audit entry
 */
async function logAudit(userId: string, action: string, resourceType: string, resourceId: string): Promise<void> {
  try {
    await pool.query(
      `INSERT INTO audit_logs (user_id, action, resource_type, resource_id, details)
       VALUES ($1, $2, $3, $4, '{}')`,
      [userId, action, resourceType, resourceId]
    );
  } catch (error: any) {
    logger.error('Audit log failed', { error: error.message });
  }
}

/**
 * Health check
 */
app.get('/health', async (req, res) => {
  try {
    await pool.query('SELECT 1');
    res.json({ status: 'healthy', timestamp: new Date() });
  } catch (error) {
    res.status(503).json({ status: 'unhealthy', timestamp: new Date() });
  }
});

app.listen(PORT, () => {
  logger.info('Approval service started', { port: PORT });
});

process.on('SIGTERM', async () => {
  logger.info('SIGTERM received, shutting down');
  await pool.end();
  process.exit(0);
});

