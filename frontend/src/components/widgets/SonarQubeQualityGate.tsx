'use client';

import { useEffect, useState } from 'react';
import { Card, CardHeader, CardBody } from '../common/Card';
import { Badge } from '../common/Badge';
import { Skeleton } from '../common/Skeleton';
import { apiClient } from '@/lib/api-client';
import { Shield } from 'lucide-react';

interface Project {
  key: string;
  name: string;
  quality_gate: {
    status: 'OK' | 'WARN' | 'ERROR';
  };
  metrics: {
    coverage?: number;
    bugs?: number;
    vulnerabilities?: number;
    code_smells?: number;
  };
}

export function SonarQubeQualityGate() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchProjects();
  }, []);

  async function fetchProjects() {
    try {
      setLoading(true);
      const response = await apiClient.getSonarProjects();
      
      if (response.success) {
        setProjects(response.data);
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  if (loading) {
    return (
      <Card className="h-full">
        <CardHeader>
          <div className="flex items-center gap-2">
            <Shield className="w-5 h-5 text-primary" />
            <h3 className="font-semibold">Quality Gate</h3>
          </div>
        </CardHeader>
        <CardBody>
          <Skeleton className="h-24 w-full" />
        </CardBody>
      </Card>
    );
  }

  if (error || projects.length === 0) {
    return (
      <Card className="h-full">
        <CardHeader>
          <div className="flex items-center gap-2">
            <Shield className="w-5 h-5 text-primary" />
            <h3 className="font-semibold">Quality Gate</h3>
          </div>
        </CardHeader>
        <CardBody>
          <div className="text-center py-8 text-secondary-500">
            <Shield className="w-12 h-12 mx-auto mb-2 opacity-30" />
            <p>No projects available</p>
          </div>
        </CardBody>
      </Card>
    );
  }

  const mainProject = projects[0];
  const statusColor =
    mainProject.quality_gate.status === 'OK'
      ? 'success'
      : mainProject.quality_gate.status === 'WARN'
      ? 'warning'
      : 'error';

  return (
    <Card className="h-full">
      <CardHeader>
        <div className="flex items-center gap-2">
          <Shield className="w-5 h-5 text-primary" />
          <h3 className="font-semibold">Quality Gate</h3>
        </div>
      </CardHeader>
      <CardBody>
        <div className="text-center mb-6">
          <div
            className={`inline-flex items-center justify-center w-24 h-24 rounded-full ${
              statusColor === 'success'
                ? 'bg-success-100 dark:bg-success-900/30'
                : statusColor === 'warning'
                ? 'bg-warning-100 dark:bg-warning-900/30'
                : 'bg-error-100 dark:bg-error-900/30'
            } mb-3`}
          >
            <Shield
              className={`w-12 h-12 ${
                statusColor === 'success'
                  ? 'text-success-600'
                  : statusColor === 'warning'
                  ? 'text-warning-600'
                  : 'text-error-600'
              }`}
            />
          </div>
          <h4 className="font-medium text-lg mb-1 text-gray-900 dark:text-gray-100">{mainProject.name}</h4>
          <Badge variant={statusColor as any} className="text-sm">
            {mainProject.quality_gate.status}
          </Badge>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div className="text-center p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
            <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
              {mainProject.metrics.coverage?.toFixed(1) || '0'}%
            </div>
            <div className="text-xs text-secondary-500 dark:text-secondary-400 mt-1">Coverage</div>
          </div>
          <div className="text-center p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
            <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
              {mainProject.metrics.bugs || 0}
            </div>
            <div className="text-xs text-secondary-500 dark:text-secondary-400 mt-1">Bugs</div>
          </div>
          <div className="text-center p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
            <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
              {mainProject.metrics.vulnerabilities || 0}
            </div>
            <div className="text-xs text-secondary-500 dark:text-secondary-400 mt-1">Vulnerabilities</div>
          </div>
          <div className="text-center p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
            <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">
              {mainProject.metrics.code_smells || 0}
            </div>
            <div className="text-xs text-secondary-500 dark:text-secondary-400 mt-1">Code Smells</div>
          </div>
        </div>
      </CardBody>
    </Card>
  );
}

