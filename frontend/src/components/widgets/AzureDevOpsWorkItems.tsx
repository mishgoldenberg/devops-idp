'use client';

import { useEffect, useState } from 'react';
import { Card, CardHeader, CardBody } from '../common/Card';
import { Badge } from '../common/Badge';
import { Skeleton } from '../common/Skeleton';
import { apiClient } from '@/lib/api-client';
import { getUser } from '@/lib/auth';
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
  const [workItems, setWorkItems] = useState<WorkItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [patConfigured, setPatConfigured] = useState<boolean | null>(null);
  const [pat, setPat] = useState('');
  const [savingPat, setSavingPat] = useState(false);
  const [patError, setPatError] = useState<string | null>(null);
  const [patSuccess, setPatSuccess] = useState<string | null>(null);

  useEffect(() => {
    initialize();
  }, []);

  async function initialize() {
    try {
      const status = await apiClient.getAzureDevOpsPatStatus();
      const configured = !!status?.data?.configured;
      setPatConfigured(configured);
      if (configured) {
        await fetchWorkItems();
      } else {
        setLoading(false);
      }
    } catch (err: any) {
      setError('Failed to check Azure DevOps connection');
      setLoading(false);
    }
  }

  async function fetchWorkItems() {
    try {
      setLoading(true);
      const user = getUser();
      const response = await apiClient.getWorkItems(user?.username || '');
      
      if (response.success) {
        setWorkItems(response.data);
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleSavePat(e: React.FormEvent) {
    e.preventDefault();
    setPatError(null);
    setPatSuccess(null);
    try {
      setSavingPat(true);
      await apiClient.setAzureDevOpsPat(pat);
      setPat('');
      setPatConfigured(true);
      setPatSuccess('Azure DevOps connection saved. Loading your work items...');
      await fetchWorkItems();
    } catch (err: any) {
      setPatError('Failed to save Personal Access Token. Please verify the value and try again.');
    } finally {
      setSavingPat(false);
    }
  }

  async function handleDisconnect() {
    setPatError(null);
    setPatSuccess(null);
    try {
      await apiClient.deleteAzureDevOpsPat();
      setPatConfigured(false);
      setWorkItems([]);
    } catch {
      setPatError('Failed to remove Azure DevOps connection.');
    }
  }

  if (loading) {
    return (
      <Card className="h-full">
        <CardHeader>
          <div className="flex items-center gap-2">
            <CheckSquare className="w-5 h-5 text-primary" />
            <h3 className="font-semibold">My Work Items</h3>
          </div>
        </CardHeader>
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

  if (error) {
    return (
      <Card className="h-full">
        <CardHeader>
          <div className="flex items-center gap-2">
            <CheckSquare className="w-5 h-5 text-primary" />
            <h3 className="font-semibold">My Work Items</h3>
          </div>
        </CardHeader>
        <CardBody>
          <p className="text-error text-sm">Failed to load work items</p>
        </CardBody>
      </Card>
    );
  }

  if (patConfigured === false) {
    return (
      <Card className="h-full flex flex-col">
        <CardHeader>
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <CheckSquare className="w-5 h-5 text-primary" />
              <h3 className="font-semibold">Connect Azure DevOps</h3>
            </div>
          </div>
        </CardHeader>
        <CardBody className="flex-1 flex flex-col justify-between gap-4">
          <div>
            <p className="text-sm text-secondary-600 dark:text-secondary-400 mb-4">
              To show your Azure DevOps work items, connect your account using a Personal Access Token (PAT).
              The token is stored securely on the backend (Vault or encrypted config) and is never shown in the UI or logs.
            </p>
            <form onSubmit={handleSavePat} className="space-y-3">
              <div className="flex flex-col gap-1">
                <label className="text-sm font-medium text-secondary-700 dark:text-secondary-300">
                  Azure DevOps Personal Access Token
                </label>
                <input
                  type="password"
                  value={pat}
                  onChange={(e) => setPat(e.target.value)}
                  className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary focus:border-primary"
                  placeholder="Paste your PAT here"
                  autoComplete="off"
                  required
                />
                <p className="text-xs text-secondary-500 dark:text-secondary-400">
                  Required scopes typically include Work Items (Read), Code (Read), and Build (Read). Do not reuse this token elsewhere.
                </p>
              </div>
              {patError && (
                <p className="text-xs text-red-600 dark:text-red-400">
                  {patError}
                </p>
              )}
              {patSuccess && (
                <p className="text-xs text-green-600 dark:text-green-400">
                  {patSuccess}
                </p>
              )}
              <div className="flex items-center gap-2">
                <button
                  type="submit"
                  disabled={savingPat || !pat}
                  className="inline-flex items-center justify-center px-4 py-2 rounded-md bg-primary text-white text-sm font-medium hover:bg-primary-600 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
                >
                  {savingPat ? 'Saving…' : 'Save & Connect'}
                </button>
              </div>
            </form>
          </div>
          <p className="text-xs text-secondary-500 dark:text-secondary-400">
            You can update or remove this connection at any time. The token is only used by the backend to call Azure DevOps on your behalf.
          </p>
        </CardBody>
      </Card>
    );
  }

  const toDoCount = workItems.filter(wi => wi.state === 'To Do').length;
  const inProgressCount = workItems.filter(wi => wi.state === 'In Progress').length;

  return (
    <Card className="h-full flex flex-col">
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <CheckSquare className="w-5 h-5 text-primary" />
            <h3 className="font-semibold">My Work Items</h3>
          </div>
          {patConfigured && (
            <button
              type="button"
              onClick={handleDisconnect}
              className="text-xs text-secondary-500 hover:text-red-600 dark:hover:text-red-400 underline-offset-2 hover:underline"
            >
              Disconnect
            </button>
          )}
        </div>
      </CardHeader>
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
      </CardBody>
    </Card>
  );
}

