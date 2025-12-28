// Shared TypeScript types for DevOps Control Center

// ============================================
// RBAC Types
// ============================================

export enum RoleHierarchy {
  PLATFORM_ADMIN = 1,
  UNIT_COMMANDER = 2,
  BRANCH_HEAD = 3,
  HEAD_OF_SECTION = 4,
  PROJECT_MANAGER = 5,
  TEAM_LEAD = 6,
  REGULAR_USER = 7,
}

export interface Role {
  id: number;
  name: string;
  hierarchy_level: number;
  permissions: string[];
  description?: string;
}

export interface User {
  id: string;
  username: string;
  email: string;
  full_name?: string;
  role_id: number;
  role?: Role;
  domain_groups: string[];
  is_active: boolean;
  last_login_at?: Date;
  metadata?: Record<string, any>;
  created_at: Date;
  updated_at: Date;
}

export interface AuthUser {
  id: string;
  username: string;
  email: string;
  role: string;
  hierarchy_level: number;
  permissions: string[];
}

// ============================================
// Dashboard & Widget Types
// ============================================

export interface WidgetConfig {
  id: string;
  widget_key: string;
  title?: string;
  config?: Record<string, any>;
}

export interface LayoutItem {
  i: string; // Widget instance ID
  x: number;
  y: number;
  w: number;
  h: number;
  minW?: number;
  minH?: number;
  maxW?: number;
  maxH?: number;
}

export interface Dashboard {
  id: string;
  user_id: string;
  name: string;
  is_default: boolean;
  layout: LayoutItem[];
  widgets: WidgetConfig[];
  created_at: Date;
  updated_at: Date;
}

export interface WidgetType {
  id: number;
  widget_key: string;
  name: string;
  description?: string;
  category: string;
  min_role_level: number;
  default_config?: Record<string, any>;
  icon?: string;
}

// ============================================
// Approval System Types
// ============================================

export enum ApprovalRequestType {
  ADO_PROJECT_CREATE = 'ADO_PROJECT_CREATE',
  SONAR_PR_SCANNING_ENABLE = 'SONAR_PR_SCANNING_ENABLE',
  AI_MODEL_ACCESS = 'AI_MODEL_ACCESS',
  CUSTOM = 'CUSTOM',
}

export enum ApprovalStatus {
  PENDING = 'PENDING',
  APPROVED = 'APPROVED',
  REJECTED = 'REJECTED',
  EXECUTED = 'EXECUTED',
  FAILED = 'FAILED',
}

export interface ApprovalRequest {
  id: string;
  requester_id: string;
  requester?: User;
  request_type: ApprovalRequestType;
  request_title: string;
  request_payload: Record<string, any>;
  status: ApprovalStatus;
  approver_id?: string;
  approver?: User;
  approver_comments?: string;
  execution_result?: Record<string, any>;
  approved_at?: Date;
  executed_at?: Date;
  created_at: Date;
  updated_at: Date;
}

export interface ApprovalRule {
  id: number;
  request_type: ApprovalRequestType;
  min_approver_role_level: number;
  auto_approve_threshold?: number;
  requires_multiple_approvers: boolean;
  approver_count: number;
}

// ============================================
// Audit & Metrics Types
// ============================================

export enum AuditAction {
  USER_LOGIN = 'USER_LOGIN',
  USER_LOGOUT = 'USER_LOGOUT',
  CREATE_REQUEST = 'CREATE_REQUEST',
  APPROVE_REQUEST = 'APPROVE_REQUEST',
  REJECT_REQUEST = 'REJECT_REQUEST',
  EXECUTE_REQUEST = 'EXECUTE_REQUEST',
  UPDATE_DASHBOARD = 'UPDATE_DASHBOARD',
  ADD_WIDGET = 'ADD_WIDGET',
  REMOVE_WIDGET = 'REMOVE_WIDGET',
  VIEW_PAGE = 'VIEW_PAGE',
  API_CALL = 'API_CALL',
  SYSTEM_ERROR = 'SYSTEM_ERROR',
}

export interface AuditLog {
  id: string;
  user_id?: string;
  action: AuditAction;
  resource_type?: string;
  resource_id?: string;
  details?: Record<string, any>;
  ip_address?: string;
  user_agent?: string;
  success: boolean;
  error_message?: string;
  created_at: Date;
}

export enum MetricType {
  WIDGET_VIEW = 'WIDGET_VIEW',
  WIDGET_INTERACTION = 'WIDGET_INTERACTION',
  SELF_SERVICE_USE = 'SELF_SERVICE_USE',
  PAGE_VIEW = 'PAGE_VIEW',
  API_CALL = 'API_CALL',
  SEARCH_QUERY = 'SEARCH_QUERY',
  EXTERNAL_REDIRECT = 'EXTERNAL_REDIRECT',
}

export interface UsageMetric {
  id: string;
  user_id?: string;
  metric_type: MetricType;
  metric_name: string;
  value: number;
  metadata?: Record<string, any>;
  created_at: Date;
}

// ============================================
// Service Health Types
// ============================================

export enum ServiceStatus {
  HEALTHY = 'HEALTHY',
  DEGRADED = 'DEGRADED',
  DOWN = 'DOWN',
  UNKNOWN = 'UNKNOWN',
}

export interface ServiceHealth {
  id: number;
  service_name: string;
  status: ServiceStatus;
  last_check_at?: Date;
  last_success_at?: Date;
  last_error_at?: Date;
  error_message?: string;
  response_time_ms?: number;
  metadata?: Record<string, any>;
  updated_at: Date;
}

// ============================================
// Azure DevOps Types
// ============================================

export interface WorkItem {
  id: number;
  title: string;
  state: string;
  type: string;
  assigned_to?: string;
  created_date: Date;
  changed_date: Date;
  url: string;
}

export interface PullRequest {
  id: number;
  title: string;
  status: string;
  created_by: string;
  created_date: Date;
  repository: string;
  source_branch: string;
  target_branch: string;
  url: string;
  reviewers?: Array<{ name: string; vote: number }>;
}

export interface Pipeline {
  id: number;
  name: string;
  run_id: number;
  status: string;
  result?: string;
  created_date: Date;
  finished_date?: Date;
  url: string;
}

// ============================================
// SonarQube Types
// ============================================

export interface QualityGate {
  status: 'OK' | 'WARN' | 'ERROR';
  conditions?: Array<{
    metric: string;
    operator: string;
    value: string;
    status: string;
  }>;
}

export interface CodeMetrics {
  coverage?: number;
  bugs?: number;
  vulnerabilities?: number;
  code_smells?: number;
  technical_debt?: string;
  duplications?: number;
  lines_of_code?: number;
}

export interface SonarProject {
  key: string;
  name: string;
  quality_gate: QualityGate;
  metrics: CodeMetrics;
  last_analysis?: Date;
}

// ============================================
// Artifactory Types
// ============================================

export interface Artifact {
  name: string;
  path: string;
  repo: string;
  size: number;
  created: Date;
  modified: Date;
  created_by: string;
  download_count?: number;
}

export interface Repository {
  key: string;
  type: string;
  description?: string;
  url: string;
  package_type: string;
}

export interface StorageInfo {
  used: number;
  total: number;
  percentage: number;
  repositories: Array<{
    name: string;
    used: number;
    percentage: number;
  }>;
}

// ============================================
// ServiceNow Types
// ============================================

export interface Ticket {
  sys_id: string;
  number: string;
  short_description: string;
  state: string;
  priority: string;
  assigned_to?: string;
  created: Date;
  updated: Date;
  url: string;
}

export interface IncidentStats {
  open: number;
  in_progress: number;
  resolved: number;
  total: number;
}

// ============================================
// AI Chatbot Types
// ============================================

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: Date;
}

export interface Conversation {
  id: string;
  user_id: string;
  title: string;
  messages: ChatMessage[];
  created_at: Date;
  updated_at: Date;
}

// ============================================
// API Response Types
// ============================================

export interface ApiResponse<T = any> {
  success: boolean;
  data?: T;
  error?: string;
  message?: string;
  timestamp: Date;
}

export interface PaginatedResponse<T> {
  success: boolean;
  data: T[];
  pagination: {
    page: number;
    limit: number;
    total: number;
    total_pages: number;
  };
  timestamp: Date;
}

// ============================================
// Request Context Types
// ============================================

export interface RequestContext {
  user: AuthUser;
  request_id: string;
  ip_address?: string;
  user_agent?: string;
}

