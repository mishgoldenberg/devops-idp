'use client';

import { useEffect, useState } from 'react';
import { Card, CardHeader, CardBody } from '../common/Card';
import { Badge } from '../common/Badge';
import { Skeleton } from '../common/Skeleton';
import { AzureConnectPrompt } from '../common/AzureConnectPrompt';
import { apiClient } from '@/lib/api-client';
import { getUser } from '@/lib/auth';
import { useAzureDevOpsConnection } from '@/hooks/useAzureDevOpsConnection';
import { getStatusColor, formatRelativeTime } from '@/lib/utils';
import { CheckSquare } from 'lucide-react';

interface WorkItem {
  id: number;
  title: string;
  state: string;
  type: string;
  url: string;
  changed_date: string;
}

export function AzureDevOpsWorkItems() {
  const connection = useAzureDevOpsConnection();
  const [workItems, setWorkItems] = useState<WorkItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (connection.connected && !connection.checking) {
      fetchWorkItems();
    }
  }, [connection.connected, connection.checking]);

  async function fetchWorkItems() {
    try {
      setLoading(true);
      setError(null);
      const user = getUser();
      const response = await apiClient.getWorkItems(user?.email || user?.username || '');
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
    <div className="flex items-center gap-2">
      <CheckSquare className="w-5 h-5 text-primary" />
      <h3 className="font-semibold">My Work Items</h3>
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

  const toDoCount = workItems.filter(wi => wi.state === 'To Do').length;
  const inProgressCount = workItems.filter(wi => wi.state === 'In Progress').length;

  return (
    <Card className="h-full flex flex-col">
      <CardHeader>{header}</CardHeader>
      <CardBody className="flex-1 overflow-auto">
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

        <div className="space-y-3">
          {workItems.slice(0, 5).map(item => (
            <a
              key={item.id}
              href={item.url}
              target="_blank"
              rel="noopener noreferrer"
              className="block p-3 border border-gray-200 dark:border-gray-700 rounded-lg hover:border-primary dark:hover:border-primary-500 hover:bg-primary-50 dark:hover:bg-primary-900/30 transition-colors"
            >
              <div className="flex items-start justify-between gap-2 mb-2">
                <span className="text-sm font-medium text-gray-900 dark:text-gray-100 flex-1 line-clamp-2">
                  {item.title}
                </span>
                <Badge variant={getStatusColor(item.state) as any}>
                  {item.state}
                </Badge>
              </div>
              <div className="flex items-center gap-2 text-xs text-secondary-500 dark:text-secondary-400">
                <span>{item.type}</span>
                <span>•</span>
                <span>{formatRelativeTime(item.changed_date)}</span>
              </div>
            </a>
          ))}
        </div>

        {workItems.length === 0 && (
          <div className="text-center py-8 text-secondary-500">
            <CheckSquare className="w-12 h-12 mx-auto mb-2 opacity-30" />
            <p>No work items assigned</p>
          </div>
        )}

        <AzureConnectPrompt connection={connection} accentClass="bg-primary" />
      </CardBody>
    </Card>
  );
}
