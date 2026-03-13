'use client';

import { useEffect, useState } from 'react';
import { Card, CardHeader, CardBody } from '../common/Card';
import { Badge } from '../common/Badge';
import { Skeleton } from '../common/Skeleton';
import { AzureConnectPrompt } from '../common/AzureConnectPrompt';
import { apiClient } from '@/lib/api-client';
import { useAzureDevOpsConnection } from '@/hooks/useAzureDevOpsConnection';
import { getStatusColor, formatRelativeTime } from '@/lib/utils';
import { CheckSquare, ChevronDown } from 'lucide-react';

interface WorkItem {
  id: number;
  title: string;
  state: string;
  state_category: string;
  type: string;
  project: string;
  url: string;
  changed_date: string;
}

interface Project {
  id: string;
  name: string;
}

// Known Azure DevOps state category strings
const IN_PROGRESS_CATEGORIES = new Set(['InProgress', 'Resolved']);
const TODO_CATEGORIES = new Set(['Proposed']);
const DONE_CATEGORIES = new Set(['Completed', 'Removed']);

// Name-based heuristic used when the backend couldn't fetch the state category
// (e.g. API permission issue on the process endpoint).
const IN_PROGRESS_NAMES = [
  'doing', 'in progress', 'active', 'committed', 'in development',
  'in review', 'in progress', 'started', 'wip',
];
const DONE_NAMES = ['done', 'closed', 'completed', 'resolved', 'removed', 'cancelled', 'canceled'];

function getEffectiveCategory(item: WorkItem): string {
  const cat = item.state_category;
  // Prefer the authoritative category from the backend when available
  if (cat && (IN_PROGRESS_CATEGORIES.has(cat) || TODO_CATEGORIES.has(cat) || DONE_CATEGORIES.has(cat))) {
    return cat;
  }
  // Fall back to state name matching (handles custom names like "Doing")
  const lower = (item.state ?? '').toLowerCase();
  if (DONE_NAMES.includes(lower)) return 'Completed';
  if (IN_PROGRESS_NAMES.some(k => lower.includes(k))) return 'InProgress';
  return 'Proposed';
}

export function AzureDevOpsWorkItems() {
  const connection = useAzureDevOpsConnection();
  const [workItems, setWorkItems] = useState<WorkItem[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState<string>('');
  const [loading, setLoading] = useState(false);
  const [projectsLoading, setProjectsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load project list once connected
  useEffect(() => {
    if (connection.connected && !connection.checking) {
      loadProjects();
    }
  }, [connection.connected, connection.checking]);

  // Reload work items whenever the selected project changes
  useEffect(() => {
    if (connection.connected && !connection.checking) {
      fetchWorkItems(selectedProject);
    }
  }, [connection.connected, connection.checking, selectedProject]);

  async function loadProjects() {
    setProjectsLoading(true);
    try {
      const res = await apiClient.getAzureDevOpsProjects();
      if (res.success) setProjects(res.data);
    } catch {
      // Non-critical – project list just won't be available
    } finally {
      setProjectsLoading(false);
    }
  }

  async function fetchWorkItems(project: string) {
    try {
      setLoading(true);
      setError(null);
      const response = await apiClient.getWorkItems(project || undefined);
      if (response.success) {
        setWorkItems(response.data);
      }
    } catch (err: any) {
      const detail = err?.response?.data?.detail ?? err?.message ?? 'Failed to load work items';
      setError(detail);
    } finally {
      setLoading(false);
    }
  }

  const header = (
    <div className="flex items-center justify-between gap-2">
      <div className="flex items-center gap-2">
        <CheckSquare className="w-5 h-5 text-primary" />
        <h3 className="font-semibold">My Work Items</h3>
      </div>

      {/* Project selector */}
      {connection.connected && !connection.checking && (
        <div className="relative">
          <select
            value={selectedProject}
            onChange={e => setSelectedProject(e.target.value)}
            disabled={projectsLoading}
            className="appearance-none pl-2 pr-6 py-1 text-xs rounded-md border border-gray-200 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200 focus:outline-none focus:ring-1 focus:ring-primary cursor-pointer disabled:opacity-50"
          >
            <option value="">All projects</option>
            {projects.map(p => (
              <option key={p.id} value={p.name}>{p.name}</option>
            ))}
          </select>
          <ChevronDown className="pointer-events-none absolute right-1 top-1/2 -translate-y-1/2 w-3 h-3 text-gray-400" />
        </div>
      )}
    </div>
  );

  if (connection.checking || (connection.connected && loading)) {
    return (
      <Card className="h-full">
        <CardHeader>{header}</CardHeader>
        <CardBody>
          <div className="space-y-3">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
          </div>
        </CardBody>
      </Card>
    );
  }

  if (!connection.connected) {
    return (
      <Card className="h-full">
        <CardHeader>{header}</CardHeader>
        <CardBody>
          <AzureConnectPrompt connection={connection} accentClass="bg-primary" />
        </CardBody>
      </Card>
    );
  }

  if (error) {
    return (
      <Card className="h-full">
        <CardHeader>{header}</CardHeader>
        <CardBody>
          <p className="text-sm text-red-500 mb-4">{error}</p>
          <AzureConnectPrompt connection={connection} accentClass="bg-primary" />
        </CardBody>
      </Card>
    );
  }

  // Count by effective category (uses name heuristic if state_category is missing)
  const toDoCount = workItems.filter(wi => TODO_CATEGORIES.has(getEffectiveCategory(wi))).length;
  const inProgressCount = workItems.filter(wi => IN_PROGRESS_CATEGORIES.has(getEffectiveCategory(wi))).length;

  return (
    <Card className="h-full flex flex-col">
      <CardHeader>{header}</CardHeader>
      <CardBody className="flex-1 overflow-auto flex flex-col">
        <div className="flex gap-4 mb-4">
          <div className="flex items-center gap-2">
            <span className="text-2xl font-bold text-secondary-600 dark:text-secondary-300">{toDoCount}</span>
            <span className="text-sm text-secondary-500 dark:text-secondary-400">To Do</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-2xl font-bold text-primary">{inProgressCount}</span>
            <span className="text-sm text-secondary-500 dark:text-secondary-400">In Progress</span>
          </div>
        </div>

        <div className="space-y-3 flex-1">
          {workItems.slice(0, 5).map(item => (
            <a
              key={item.id}
              href={item.url}
              target="_blank"
              rel="noopener noreferrer"
              className="block p-3 border border-gray-200 dark:border-gray-700 rounded-lg hover:border-primary dark:hover:border-primary-500 hover:bg-primary-50 dark:hover:bg-primary-900/30 transition-colors"
            >
              <div className="flex items-start justify-between gap-2 mb-1">
                <span className="text-sm font-medium text-gray-900 dark:text-gray-100 flex-1 line-clamp-2">
                  {item.title}
                </span>
                <Badge variant={getStatusColor(item.state) as any} className="shrink-0">
                  {item.state}
                </Badge>
              </div>
              <div className="flex items-center gap-2 text-xs text-secondary-500 dark:text-secondary-400">
                <span>{item.type}</span>
                {item.project && <><span>•</span><span className="truncate max-w-[120px]">{item.project}</span></>}
                <span>•</span>
                <span>{formatRelativeTime(item.changed_date)}</span>
              </div>
            </a>
          ))}
        </div>

        {workItems.length === 0 && (
          <div className="text-center py-8 text-secondary-500 flex-1">
            <CheckSquare className="w-12 h-12 mx-auto mb-2 opacity-30" />
            <p>No open work items assigned to you</p>
          </div>
        )}

        <AzureConnectPrompt connection={connection} accentClass="bg-primary" />
      </CardBody>
    </Card>
  );
}
