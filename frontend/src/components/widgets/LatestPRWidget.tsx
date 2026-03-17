'use client';

import { useEffect, useState, useCallback } from 'react';
import { useAutoRefresh } from '@/hooks/useAutoRefresh';
import { Card } from '@/components/common/Card';
import { Badge } from '@/components/common/Badge';
import { Skeleton } from '@/components/common/Skeleton';
import { AzureConnectPrompt } from '@/components/common/AzureConnectPrompt';
import { apiClient } from '@/lib/api-client';
import { useAzureDevOpsConnection } from '@/hooks/useAzureDevOpsConnection';
import { formatRelativeTime } from '@/lib/utils';
import { GitPullRequest } from 'lucide-react';

interface PullRequest {
  id: number;
  title: string;
  status: string;
  repository: string;
  source_branch: string;
  target_branch: string;
  created_date: string;
  url: string;
  created_by?: string;
}

export function LatestPRWidget() {
  const connection = useAzureDevOpsConnection();
  const [pr, setPr] = useState<PullRequest | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchLatestPR = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await apiClient.getPullRequests('');
      if (response.success && response.data.length > 0) {
        setPr(response.data[0]);
      }
    } catch (err: any) {
      setError(err?.response?.data?.detail ?? err?.message ?? 'Failed to load pull requests');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (connection.connected && !connection.checking) fetchLatestPR();
  }, [connection.connected, connection.checking, fetchLatestPR]);
  useAutoRefresh(fetchLatestPR, 60_000, connection.connected);

  const header = (
    <div className="flex items-center gap-2">
      <div className="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center">
        <GitPullRequest className="w-4 h-4 text-white" />
      </div>
      <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100">Latest PR</h2>
    </div>
  );

  if (connection.checking || (connection.connected && loading)) {
    return (
      <Card className="bg-white dark:bg-gray-800 border-blue-200 dark:border-blue-500 h-full">
        <div className="p-6">
          {header}
          <div className="space-y-3 mt-4">
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-8 w-full" />
          </div>
        </div>
      </Card>
    );
  }

  if (!connection.connected) {
    return (
      <Card className="bg-white dark:bg-gray-800 border-blue-200 dark:border-blue-500 h-full">
        <div className="p-6">
          {header}
          <AzureConnectPrompt connection={connection} accentClass="bg-blue-600" />
        </div>
      </Card>
    );
  }

  if (error) {
    return (
      <Card className="bg-white dark:bg-gray-800 border-blue-200 dark:border-blue-500 h-full">
        <div className="p-6">
          {header}
          <p className="text-sm text-red-500 mt-4 mb-2">{error}</p>
          <AzureConnectPrompt connection={connection} accentClass="bg-blue-600" />
        </div>
      </Card>
    );
  }

  return (
    <Card className="bg-white dark:bg-gray-800 border-blue-200 dark:border-blue-500 h-full">
      <div className="p-6">
        {header}
        {pr ? (
          <a href={pr.url} target="_blank" rel="noopener noreferrer" className="block mt-4 hover:opacity-80 transition">
            <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-1 line-clamp-1">{pr.title}</h3>
            <p className="text-sm text-gray-600 dark:text-gray-300 mb-3">{pr.source_branch} → {pr.target_branch}</p>
            <div className="flex items-center gap-2 mb-4">
              <span className="text-xs text-gray-500 dark:text-gray-400">{pr.repository}</span>
              <span className="text-xs text-gray-500 dark:text-gray-400">•</span>
              <span className="text-xs text-gray-500 dark:text-gray-400">{formatRelativeTime(pr.created_date)}</span>
            </div>
            <Badge className={`${pr.status === 'active' ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 text-gray-700'} w-full justify-center`}>
              {pr.status}
            </Badge>
          </a>
        ) : (
          <p className="text-sm text-gray-500 mt-4">No pull requests found</p>
        )}
        <AzureConnectPrompt connection={connection} accentClass="bg-blue-600" />
      </div>
    </Card>
  );
}
