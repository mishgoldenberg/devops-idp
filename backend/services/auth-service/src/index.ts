import express from 'express';
import helmet from 'helmet';
import cors from 'cors';
import { Pool } from 'pg';
import jwt, { SignOptions } from 'jsonwebtoken';
import { createLogger, AuthUser } from '@devops-control-center/shared';
import dotenv from 'dotenv';

dotenv.config();

const logger = createLogger('auth-service');
const app = express();

const PORT = parseInt(process.env.PORT || '8001', 10);
const JWT_SECRET = process.env.JWT_SECRET || 'change_this_in_production';
const JWT_EXPIRY = process.env.JWT_EXPIRY || '8h';

// Database
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
});

// Middleware
app.use(helmet());
app.use(cors());
app.use(express.json());

/**
 * SSO login (mock implementation - replace with real SSO)
 * In production, this would redirect to your SSO provider
 */
app.post('/login', async (req, res) => {
  try {
    const { username } = req.body;

    if (!username) {
      return res.status(400).json({
        success: false,
        error: 'Username required',
      });
    }

    // Fetch user from database
    const result = await pool.query(
      `SELECT u.*, r.name as role_name, r.hierarchy_level, r.permissions
       FROM users u
       JOIN roles r ON u.role_id = r.id
       WHERE u.username = $1 AND u.is_active = true`,
      [username]
    );

    if (result.rows.length === 0) {
      return res.status(401).json({
        success: false,
        error: 'Invalid credentials',
      });
    }

    const user = result.rows[0];

    // Update last login
    await pool.query(
      'UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = $1',
      [user.id]
    );

    // Create JWT
    const authUser: AuthUser = {
      id: user.id,
      username: user.username,
      email: user.email,
      role: user.role_name,
      hierarchy_level: user.hierarchy_level,
      permissions: user.permissions,
    };

    const token = jwt.sign(authUser, JWT_SECRET, { 
      expiresIn: JWT_EXPIRY 
    } as SignOptions);

    logger.info('User logged in', undefined, user.id);

    res.json({
      success: true,
      data: {
        token,
        user: authUser,
      },
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Login error', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Authentication failed',
    });
  }
});

/**
 * Verify JWT token
 */
app.post('/verify', async (req, res) => {
  try {
    const { token } = req.body;

    if (!token) {
      return res.status(400).json({
        success: false,
        error: 'Token required',
      });
    }

    const decoded = jwt.verify(token, JWT_SECRET) as AuthUser;

    res.json({
      success: true,
      data: decoded,
      timestamp: new Date(),
    });
  } catch (error: any) {
    res.status(401).json({
      success: false,
      error: 'Invalid or expired token',
    });
  }
});

/**
 * SSO callback (for real SSO integration)
 * This would handle the callback from your SSO provider
 */
app.get('/callback', async (req, res) => {
  // TODO: Implement real SSO callback handling
  // 1. Exchange code for token with SSO provider
  // 2. Get user info from SSO
  // 3. Map SSO groups to internal roles
  // 4. Create/update user in database
  // 5. Generate JWT and redirect to frontend

  res.json({
    success: true,
    message: 'SSO callback - implement with your SSO provider',
  });
});

/**
 * Map domain groups to role
 */
app.post('/map-role', async (req, res) => {
  try {
    const { domainGroups } = req.body;

    if (!domainGroups || !Array.isArray(domainGroups)) {
      return res.status(400).json({
        success: false,
        error: 'Domain groups required',
      });
    }

    // Role mapping logic (customize based on your AD groups)
    const roleMapping: Record<string, number> = {
      'DOMAIN\\DevOps-Admins': 1,
      'DOMAIN\\Unit-Commanders': 2,
      'DOMAIN\\Branch-Heads': 3,
      'DOMAIN\\Section-Heads': 4,
      'DOMAIN\\Project-Managers': 5,
      'DOMAIN\\Team-Leads': 6,
      'DOMAIN\\Engineers': 7,
    };

    // Find highest priority role (lowest hierarchy level)
    let roleId = 7; // Default to Regular User
    for (const group of domainGroups) {
      if (roleMapping[group] && roleMapping[group] < roleId) {
        roleId = roleMapping[group];
      }
    }

    const result = await pool.query(
      'SELECT * FROM roles WHERE id = $1',
      [roleId]
    );

    res.json({
      success: true,
      data: result.rows[0],
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Role mapping error', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Role mapping failed',
    });
  }
});

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

// Start server
app.listen(PORT, () => {
  logger.info('Auth service started', { port: PORT });
});

// Graceful shutdown
process.on('SIGTERM', async () => {
  logger.info('SIGTERM received, shutting down');
  await pool.end();
  process.exit(0);
});

