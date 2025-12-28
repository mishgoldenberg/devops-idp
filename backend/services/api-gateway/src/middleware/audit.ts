import { Request, Response, NextFunction } from 'express';
import { query } from '../db';
import { AuditAction } from '@devops-control-center/shared';
import { createLogger } from '@devops-control-center/shared';

const logger = createLogger('api-gateway:audit');

/**
 * Audit logging middleware
 */
export function auditLog(action: AuditAction, resourceType?: string) {
  return async (req: Request, res: Response, next: NextFunction) => {
    try {
      const userId = req.user?.id;
      const resourceId = req.params.id || req.body?.id;
      const details = {
        method: req.method,
        path: req.path,
        query: req.query,
        body: req.body,
      };

      await query(
        `INSERT INTO audit_logs (user_id, action, resource_type, resource_id, details, ip_address, user_agent)
         VALUES ($1, $2, $3, $4, $5, $6, $7)`,
        [
          userId,
          action,
          resourceType,
          resourceId,
          JSON.stringify(details),
          req.ip,
          req.get('user-agent'),
        ]
      );

      logger.debug('Audit log created', { action, resourceType }, userId, req.requestId);
    } catch (error: any) {
      logger.error('Audit log failed', { error: error.message }, req.user?.id, req.requestId);
      // Don't block request if audit fails
    }

    next();
  };
}

/**
 * Log API calls (for metrics)
 */
export async function logApiCall(req: Request, res: Response, next: NextFunction) {
  const start = Date.now();

  res.on('finish', async () => {
    const duration = Date.now() - start;
    
    try {
      await query(
        `INSERT INTO usage_metrics (user_id, metric_type, metric_name, value, metadata)
         VALUES ($1, $2, $3, $4, $5)`,
        [
          req.user?.id,
          'API_CALL',
          `${req.method} ${req.path}`,
          duration,
          JSON.stringify({
            status: res.statusCode,
            duration,
          }),
        ]
      );
    } catch (error: any) {
      logger.error('API call logging failed', { error: error.message });
    }
  });

  next();
}

