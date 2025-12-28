import { Request, Response, NextFunction } from 'express';
import { hasPermission, hasRoleLevel } from '@devops-control-center/shared';
import { createLogger } from '@devops-control-center/shared';

const logger = createLogger('api-gateway:authorization');

/**
 * Check if user has required permission
 */
export function requirePermission(permission: string) {
  return (req: Request, res: Response, next: NextFunction) => {
    if (!req.user) {
      return res.status(401).json({
        success: false,
        error: 'Authentication required',
      });
    }

    if (!hasPermission(req.user, permission)) {
      logger.warn('Permission denied', { 
        permission, 
        user: req.user.username 
      }, req.user.id, req.requestId);
      
      return res.status(403).json({
        success: false,
        error: 'Insufficient permissions',
      });
    }

    next();
  };
}

/**
 * Check if user has required role level or higher
 */
export function requireRoleLevel(requiredLevel: number) {
  return (req: Request, res: Response, next: NextFunction) => {
    if (!req.user) {
      return res.status(401).json({
        success: false,
        error: 'Authentication required',
      });
    }

    if (!hasRoleLevel(req.user, requiredLevel)) {
      logger.warn('Role level insufficient', { 
        required: requiredLevel, 
        actual: req.user.hierarchy_level,
        user: req.user.username 
      }, req.user.id, req.requestId);
      
      return res.status(403).json({
        success: false,
        error: 'Insufficient role level',
      });
    }

    next();
  };
}

/**
 * Platform Admin only
 */
export const requirePlatformAdmin = requireRoleLevel(1);

/**
 * Team Lead or higher
 */
export const requireTeamLead = requireRoleLevel(6);

/**
 * Branch Head or higher
 */
export const requireBranchHead = requireRoleLevel(3);

