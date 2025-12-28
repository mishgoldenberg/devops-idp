import { Request, Response, NextFunction } from 'express';
import jwt from 'jsonwebtoken';
import { config } from '../config';
import { AuthUser } from '@devops-control-center/shared';
import { createLogger } from '@devops-control-center/shared';

const logger = createLogger('api-gateway:auth');

declare global {
  namespace Express {
    interface Request {
      user?: AuthUser;
      requestId?: string;
    }
  }
}

declare module 'express-session' {
  interface SessionData {
    token?: string;
  }
}

/**
 * Authentication middleware - validates JWT token
 */
export function authenticate(req: Request, res: Response, next: NextFunction) {
  try {
    // Development bypass
    if (config.dev.bypassAuth) {
      req.user = {
        id: 'a0000000-0000-0000-0000-000000000001',
        username: config.dev.defaultUser,
        email: config.dev.defaultUser,
        role: 'Platform Admin',
        hierarchy_level: 1,
        permissions: ['*'],
      };
      return next();
    }

    // Get token from Authorization header or session
    const authHeader = req.headers.authorization;
    const token = authHeader?.startsWith('Bearer ') 
      ? authHeader.substring(7) 
      : req.session?.token;

    if (!token) {
      return res.status(401).json({
        success: false,
        error: 'Authentication required',
      });
    }

    // Verify JWT
    const decoded = jwt.verify(token, config.jwt.secret) as AuthUser;
    req.user = decoded;

    logger.debug('User authenticated', undefined, decoded.id, req.requestId);
    next();
  } catch (error: any) {
    logger.warn('Authentication failed', { error: error.message }, undefined, req.requestId);
    return res.status(401).json({
      success: false,
      error: 'Invalid or expired token',
    });
  }
}

/**
 * Optional authentication - doesn't block if no token
 */
export function optionalAuth(req: Request, res: Response, next: NextFunction) {
  try {
    const authHeader = req.headers.authorization;
    const token = authHeader?.startsWith('Bearer ') 
      ? authHeader.substring(7) 
      : req.session?.token;

    if (token) {
      const decoded = jwt.verify(token, config.jwt.secret) as AuthUser;
      req.user = decoded;
    }
  } catch (error) {
    // Continue without user
  }
  next();
}

