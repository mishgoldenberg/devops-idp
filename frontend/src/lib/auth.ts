import { apiClient } from './api-client';

export interface User {
  id: string;
  username: string;
  email: string;
  role: string;
  hierarchy_level: number;
  permissions: string[];
}

export function getUser(): User | null {
  if (typeof window === 'undefined') return null;
  
  const userData = localStorage.getItem('user_data');
  if (!userData) return null;
  
  try {
    return JSON.parse(userData);
  } catch {
    return null;
  }
}

export function isAuthenticated(): boolean {
  return apiClient.getToken() !== null && getUser() !== null;
}

export function hasPermission(permission: string): boolean {
  const user = getUser();
  if (!user) return false;
  
  // Platform Admin has all permissions
  if (user.permissions.includes('*')) return true;
  
  // Check exact permission
  if (user.permissions.includes(permission)) return true;
  
  // Check wildcard permissions
  return user.permissions.some(p => {
    if (p.endsWith(':*')) {
      const prefix = p.slice(0, -1);
      return permission.startsWith(prefix);
    }
    return false;
  });
}

export function hasRoleLevel(requiredLevel: number): boolean {
  const user = getUser();
  if (!user) return false;
  return user.hierarchy_level <= requiredLevel;
}

export function canViewObservability(): boolean {
  const user = getUser();
  if (!user) return false;
  // Observability and monitoring are Admin-only in the UI layer
  return user.role === 'Admin';
}

export function canViewAggregatedMetrics(): boolean {
  const user = getUser();
  if (!user) return false;
  // Platform Admin (1), Unit Commander (2), Branch Head (3)
  return user.hierarchy_level <= 3;
}

export function isPlatformAdmin(): boolean {
  const user = getUser();
  return user?.role === 'Admin';
}

