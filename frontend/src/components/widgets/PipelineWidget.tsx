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
import { PlayCircle, CheckCircle, Clock, AlertCircle } from 'lucide-react';

interface Pipeline {
  id: number;
  name: string;
  status: string;
  result: string;
  created_date: string;
  finished_date?: string;
  url: string;
  run_id?: number;
}

export function PipelineWidget() {
  const connection = useAzureDevOpsConnection();
  const [pipeline, setPipeline] = useState<Pipeline | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchLatestPipeline = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await apiClient.getPipelines();
      if (response.success && response.data.length > 0) {
        setPipeline(response.data[0]);
      }
    } catch (err: any) {
      setError(err?.response?.data?.detail ?? err?.message ?? 'Failed to load pipelines');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (connection.connected && !connection.checking) fetchLatestPipeline();
  }, [connection.connected, connection.checking, fetchLatestPipeline]);
  useAutoRefresh(fetchLatestPipeline, 60_000, connection.connected);

  const header = (
    <div className="flex items-center gap-2">
      <div className="w-8 h-8 bg-purple-600 rounded-lg flex items-center justify-center">
        <PlayCircle className="w-4 h-4 text-white" />
      </div>
      <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100">Latest Pipeline</h2>
    </div>
  );

  if (connection.checking || (connection.connected && loading)) {
    return (
      <Card className="bg-white dark:bg-gray-800 border-purple-200 dark:border-purple-500 h-full">
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
      <Card className="bg-white dark:bg-gray-800 border-purple-200 dark:border-purple-500 h-full">
        <div className="p-6">
          {header}
          <AzureConnectPrompt connection={connection} accentClass="bg-purple-600" />
        </div>
      </Card>
    );
  }

  if (error) {
    return (
      <Card className="bg-white dark:bg-gray-800 border-purple-200 dark:border-purple-500 h-full">
        <div className="p-6">
          {header}
          <p className="text-sm text-red-500 mt-4 mb-2">{error}</p>
          <AzureConnectPrompt connection={connection} accentClass="bg-purple-600" />
        </div>
      </Card>
    );
  }

  const resultColor =
    pipeline?.result === 'succeeded' ? 'text-green-600' :
    pipeline?.result === 'failed' ? 'text-red-600' : 'text-gray-600';

  const resultIcon =
    pipeline?.result === 'succeeded' ? <CheckCircle className="w-3 h-3" /> :
    pipeline?.result === 'failed' ? <AlertCircle className="w-3 h-3" /> : <Clock className="w-3 h-3" />;

  return (
    <Card className="bg-white dark:bg-gray-800 border-purple-200 dark:border-purple-500 h-full">
      <div className="p-6">
        {header}
        {pipeline ? (
          <a href={pipeline.url} target="_blank" rel="noopener noreferrer" className="block mt-4 hover:opacity-80 transition">
            <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-1 line-clamp-1">{pipeline.name}</h3>
            <p className="text-sm text-gray-600 dark:text-gray-300 mb-3">{formatRelativeTime(pipeline.created_date)}</p>
            <div className="space-y-2 mb-4">
              <div className="flex items-center justify-between text-sm">
                <div className="flex items-center gap-2">
                  <CheckCircle className={`w-4 h-4 ${pipeline.status === 'completed' ? 'text-green-600' : 'text-gray-400'}`} />
                  <span className="text-gray-700 dark:text-gray-300">{pipeline.status}</span>
                </div>
                <span className={`text-xs font-semibold ${resultColor}`}>{pipeline.result}</span>
              </div>
            </div>
            <Badge className={`${
              pipeline.result === 'succeeded' ? 'bg-green-100 text-green-700' :
              pipeline.result === 'failed' ? 'bg-red-100 text-red-700' : 'bg-gray-100 text-gray-700'
            } w-full justify-center`}>
              {resultIcon}
              <span className="ml-1 capitalize">{pipeline.result || 'pending'}</span>
            </Badge>
          </a>
        ) : (
          <p className="text-sm text-gray-500 mt-4">No pipelines found</p>
        )}
        <AzureConnectPrompt connection={connection} accentClass="bg-purple-600" />
      </div>
    </Card>
  );
}
