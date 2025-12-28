import { Router } from 'express';
import { query } from '../db';
import { authenticate } from '../middleware/auth';
import { canViewObservability } from '@devops-control-center/shared';

const router = Router();

/**
 * Get usage metrics (observability access required)
 */
router.get('/usage', authenticate, async (req, res) => {
  try {
    if (!canViewObservability(req.user!)) {
      return res.status(403).json({
        success: false,
        error: 'Insufficient permissions',
      });
    }

    const { period = '7d' } = req.query;
    const days = period === '30d' ? 30 : 7;

    // Most used widgets
    const widgetUsage = await query(
      `SELECT metric_name, COUNT(*) as count, COUNT(DISTINCT user_id) as unique_users
       FROM usage_metrics
       WHERE metric_type = 'WIDGET_VIEW'
         AND created_at > NOW() - INTERVAL '${days} days'
       GROUP BY metric_name
       ORDER BY count DESC
       LIMIT 10`
    );

    // Self-service usage
    const selfServiceUsage = await query(
      `SELECT metric_name, COUNT(*) as count
       FROM usage_metrics
       WHERE metric_type = 'SELF_SERVICE_USE'
         AND created_at > NOW() - INTERVAL '${days} days'
       GROUP BY metric_name
       ORDER BY count DESC`
    );

    // Daily active users
    const dailyActiveUsers = await query(
      `SELECT DATE(created_at) as date, COUNT(DISTINCT user_id) as active_users
       FROM usage_metrics
       WHERE created_at > NOW() - INTERVAL '${days} days'
       GROUP BY DATE(created_at)
       ORDER BY date DESC`
    );

    res.json({
      success: true,
      data: {
        widgetUsage,
        selfServiceUsage,
        dailyActiveUsers,
      },
      timestamp: new Date(),
    });
  } catch (error: any) {
    res.status(500).json({
      success: false,
      error: 'Failed to fetch usage metrics',
      message: error.message,
    });
  }
});

/**
 * Get service health metrics
 */
router.get('/services', authenticate, async (req, res) => {
  try {
    if (!canViewObservability(req.user!)) {
      return res.status(403).json({
        success: false,
        error: 'Insufficient permissions',
      });
    }

    const services = await query(
      `SELECT service_name, status, last_check_at, last_success_at, last_error_at, 
              error_message, response_time_ms, updated_at
       FROM service_health
       ORDER BY service_name`
    );

    res.json({
      success: true,
      data: services,
      timestamp: new Date(),
    });
  } catch (error: any) {
    res.status(500).json({
      success: false,
      error: 'Failed to fetch service metrics',
      message: error.message,
    });
  }
});

/**
 * Get approval metrics
 */
router.get('/approvals', authenticate, async (req, res) => {
  try {
    if (!canViewObservability(req.user!)) {
      return res.status(403).json({
        success: false,
        error: 'Insufficient permissions',
      });
    }

    const stats = await query(
      `SELECT 
         request_type,
         COUNT(*) as total,
         COUNT(*) FILTER (WHERE status = 'PENDING') as pending,
         COUNT(*) FILTER (WHERE status = 'APPROVED') as approved,
         COUNT(*) FILTER (WHERE status = 'REJECTED') as rejected,
         AVG(EXTRACT(EPOCH FROM (approved_at - created_at))) FILTER (WHERE approved_at IS NOT NULL) as avg_approval_time_seconds
       FROM approval_requests
       WHERE created_at > NOW() - INTERVAL '30 days'
       GROUP BY request_type`
    );

    res.json({
      success: true,
      data: stats,
      timestamp: new Date(),
    });
  } catch (error: any) {
    res.status(500).json({
      success: false,
      error: 'Failed to fetch approval metrics',
      message: error.message,
    });
  }
});

export default router;

