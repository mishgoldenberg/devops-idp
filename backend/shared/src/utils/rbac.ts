// RBAC utility functions

import { RoleHierarchy, AuthUser } from '../types';

/**
 * Check if user has required role level or higher
 */
export function hasRoleLevel(user: AuthUser, requiredLevel: number): boolean {
  return user.hierarchy_level <= requiredLevel;
}

/**
 * Check if user has specific permission
 */
export function hasPermission(user: AuthUser, permission: string): boolean {
  // Platform Admin has all permissions
  if (user.permissions.includes('*')) {
    return true;
  }

  // Check exact permission
  if (user.permissions.includes(permission)) {
    return true;
  }

  // Check wildcard permissions (e.g., "view:*" matches "view:dashboard")
  return user.permissions.some(p => {
    if (p.endsWith(':*')) {
      const prefix = p.slice(0, -1); // Remove '*'
      return permission.startsWith(prefix);
    }
    return false;
  });
}

/**
 * Check if user can approve a specific request type
 */
export function canApprove(
  user: AuthUser,
  requiredApproverLevel: number
): boolean {
  return user.hierarchy_level <= requiredApproverLevel;
}

/**
 * Get role name from hierarchy level
 */
export function getRoleName(level: number): string {
  const roleNames: Record<number, string> = {
    [RoleHierarchy.PLATFORM_ADMIN]: 'Platform Admin',
    [RoleHierarchy.UNIT_COMMANDER]: 'Unit Commander',
    [RoleHierarchy.BRANCH_HEAD]: 'Branch Head',
    [RoleHierarchy.HEAD_OF_SECTION]: 'Head of Section',
    [RoleHierarchy.PROJECT_MANAGER]: 'Project Manager',
    [RoleHierarchy.TEAM_LEAD]: 'Team Lead',
    [RoleHierarchy.REGULAR_USER]: 'Regular User',
  };
  return roleNames[level] || 'Unknown';
}

/**
 * Check if user can see observability dashboards
 */
export function canViewObservability(user: AuthUser): boolean {
  // Platform Admin, Head of Section, Team Lead
  return [
    RoleHierarchy.PLATFORM_ADMIN,
    RoleHierarchy.HEAD_OF_SECTION,
    RoleHierarchy.TEAM_LEAD,
  ].includes(user.hierarchy_level);
}

/**
 * Check if user can see aggregated metrics
 */
export function canViewAggregatedMetrics(user: AuthUser): boolean {
  // Platform Admin, Unit Commander, Branch Head
  return user.hierarchy_level <= RoleHierarchy.BRANCH_HEAD;
}

/**
 * Check if user can manage other users
 */
export function canManageUsers(user: AuthUser): boolean {
  return user.hierarchy_level === RoleHierarchy.PLATFORM_ADMIN;
}

