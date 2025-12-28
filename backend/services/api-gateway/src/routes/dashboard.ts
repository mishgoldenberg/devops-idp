import { Router } from 'express';
import { query } from '../db';
import { authenticate } from '../middleware/auth';
import { auditLog } from '../middleware/audit';
import { AuditAction } from '@devops-control-center/shared';
import { v4 as uuidv4 } from 'uuid';

const router = Router();

/**
 * Get user's dashboards
 */
router.get('/', authenticate, async (req, res) => {
  try {
    const dashboards = await query(
      `SELECT id, user_id, name, is_default, layout, widgets, created_at, updated_at
       FROM dashboards
       WHERE user_id = $1
       ORDER BY is_default DESC, name ASC`,
      [req.user!.id]
    );

    res.json({
      success: true,
      data: dashboards,
      timestamp: new Date(),
    });
  } catch (error: any) {
    res.status(500).json({
      success: false,
      error: 'Failed to fetch dashboards',
      message: error.message,
    });
  }
});

/**
 * Get user's default dashboard
 */
router.get('/default', authenticate, async (req, res) => {
  try {
    const [dashboard] = await query(
      `SELECT id, user_id, name, is_default, layout, widgets, created_at, updated_at
       FROM dashboards
       WHERE user_id = $1 AND is_default = true
       LIMIT 1`,
      [req.user!.id]
    );

    if (!dashboard) {
      // Create default dashboard if doesn't exist
      const newDashboard = await createDefaultDashboard(req.user!.id, req.user!.hierarchy_level);
      return res.json({
        success: true,
        data: newDashboard,
        timestamp: new Date(),
      });
    }

    res.json({
      success: true,
      data: dashboard,
      timestamp: new Date(),
    });
  } catch (error: any) {
    res.status(500).json({
      success: false,
      error: 'Failed to fetch default dashboard',
      message: error.message,
    });
  }
});

/**
 * Update dashboard
 */
router.put('/:id', authenticate, auditLog(AuditAction.UPDATE_DASHBOARD, 'dashboard'), async (req, res) => {
  try {
    const { id } = req.params;
    const { name, layout, widgets } = req.body;

    const [dashboard] = await query(
      `UPDATE dashboards
       SET name = COALESCE($1, name),
           layout = COALESCE($2, layout),
           widgets = COALESCE($3, widgets),
           updated_at = CURRENT_TIMESTAMP
       WHERE id = $4 AND user_id = $5
       RETURNING *`,
      [name, layout ? JSON.stringify(layout) : null, widgets ? JSON.stringify(widgets) : null, id, req.user!.id]
    );

    if (!dashboard) {
      return res.status(404).json({
        success: false,
        error: 'Dashboard not found',
      });
    }

    res.json({
      success: true,
      data: dashboard,
      timestamp: new Date(),
    });
  } catch (error: any) {
    res.status(500).json({
      success: false,
      error: 'Failed to update dashboard',
      message: error.message,
    });
  }
});

/**
 * Create new dashboard
 */
router.post('/', authenticate, async (req, res) => {
  try {
    const { name, layout, widgets } = req.body;

    const [dashboard] = await query(
      `INSERT INTO dashboards (user_id, name, layout, widgets)
       VALUES ($1, $2, $3, $4)
       RETURNING *`,
      [req.user!.id, name || 'New Dashboard', JSON.stringify(layout || []), JSON.stringify(widgets || [])]
    );

    res.status(201).json({
      success: true,
      data: dashboard,
      timestamp: new Date(),
    });
  } catch (error: any) {
    res.status(500).json({
      success: false,
      error: 'Failed to create dashboard',
      message: error.message,
    });
  }
});

/**
 * Delete dashboard
 */
router.delete('/:id', authenticate, async (req, res) => {
  try {
    const { id } = req.params;

    const [dashboard] = await query(
      `DELETE FROM dashboards
       WHERE id = $1 AND user_id = $2 AND is_default = false
       RETURNING id`,
      [id, req.user!.id]
    );

    if (!dashboard) {
      return res.status(404).json({
        success: false,
        error: 'Dashboard not found or cannot delete default dashboard',
      });
    }

    res.json({
      success: true,
      message: 'Dashboard deleted successfully',
      timestamp: new Date(),
    });
  } catch (error: any) {
    res.status(500).json({
      success: false,
      error: 'Failed to delete dashboard',
      message: error.message,
    });
  }
});

/**
 * Get available widget types for user's role
 */
router.get('/widget-types', authenticate, async (req, res) => {
  try {
    const widgets = await query(
      `SELECT id, widget_key, name, description, category, min_role_level, default_config, icon
       FROM widget_types
       WHERE min_role_level >= $1
       ORDER BY category, name`,
      [req.user!.hierarchy_level]
    );

    res.json({
      success: true,
      data: widgets,
      timestamp: new Date(),
    });
  } catch (error: any) {
    res.status(500).json({
      success: false,
      error: 'Failed to fetch widget types',
      message: error.message,
    });
  }
});

// Helper function to create default dashboard
async function createDefaultDashboard(userId: string, roleLevel: number) {
  const defaultWidgets = getDefaultWidgetsForRole(roleLevel);
  const defaultLayout = generateDefaultLayout(defaultWidgets.length);

  const [dashboard] = await query(
    `INSERT INTO dashboards (user_id, name, is_default, layout, widgets)
     VALUES ($1, $2, true, $3, $4)
     RETURNING *`,
    ['My Dashboard', 'My Dashboard', JSON.stringify(defaultLayout), JSON.stringify(defaultWidgets)]
  );

  return dashboard;
}

function getDefaultWidgetsForRole(roleLevel: number): any[] {
  const commonWidgets = [
    { id: uuidv4(), widget_key: 'ado_my_work_items', config: {} },
    { id: uuidv4(), widget_key: 'ado_my_pull_requests', config: {} },
    { id: uuidv4(), widget_key: 'sonar_quality_gate', config: {} },
    { id: uuidv4(), widget_key: 'snow_my_tickets', config: {} },
  ];

  if (roleLevel <= 3) {
    // Executives get aggregated widgets
    return [
      ...commonWidgets,
      { id: uuidv4(), widget_key: 'executive_branch_dashboard', config: {} },
      { id: uuidv4(), widget_key: 'executive_deployment_frequency', config: {} },
    ];
  } else if (roleLevel <= 6) {
    // Team leads get team widgets
    return [
      ...commonWidgets,
      { id: uuidv4(), widget_key: 'ado_sprint_progress', config: {} },
      { id: uuidv4(), widget_key: 'snow_team_tickets', config: {} },
    ];
  }

  return commonWidgets;
}

function generateDefaultLayout(widgetCount: number): any[] {
  const layout = [];
  let x = 0;
  let y = 0;

  for (let i = 0; i < widgetCount; i++) {
    layout.push({
      i: i.toString(),
      x: x * 6,
      y: y * 4,
      w: 6,
      h: 4,
      minW: 3,
      minH: 3,
    });

    x++;
    if (x >= 2) {
      x = 0;
      y++;
    }
  }

  return layout;
}

export default router;

