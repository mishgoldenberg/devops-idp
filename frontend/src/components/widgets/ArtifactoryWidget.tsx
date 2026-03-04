'use client';

import { Card } from '@/components/common/Card';
import { Package } from 'lucide-react';

const repositories = [
  { name: 'docker-local', size: '1.2 GB', lastPush: '3 hours ago', artifacts: 245 },
  { name: 'npm-local', size: '856 MB', lastPush: '1 day ago', artifacts: 1.2 },
  { name: 'maven-local', size: '2.1 GB', lastPush: '2 days ago', artifacts: 89 },
];

export function ArtifactoryWidget() {
  return (
    <Card className="h-full">
      <div className="p-6">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 bg-purple-100 dark:bg-purple-900/40 rounded-lg flex items-center justify-center">
              <Package className="w-4 h-4 text-purple-600 dark:text-purple-300" />
            </div>
            <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100">Artifactory Repositories</h2>
          </div>
          <a href="#" className="text-sm font-medium text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300">Manage</a>
        </div>
        <div className="space-y-3">
          {repositories.map((repo) => (
            <div
              key={repo.name}
              className="p-4 bg-gray-50 dark:bg-gray-800 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors cursor-pointer"
            >
              <div className="flex items-center justify-between mb-2">
                <div>
                  <div className="flex items-center gap-2 mb-1">
                    <div className="w-2 h-2 bg-blue-500 rounded-full"></div>
                    <span className="font-semibold text-gray-900 dark:text-gray-100">{repo.name}</span>
                  </div>
                  <p className="text-xs text-gray-600 dark:text-gray-400">Last push: {repo.lastPush}</p>
                </div>
                <div className="text-right">
                  <div className="font-bold text-gray-900 dark:text-gray-100">{repo.size}</div>
                  <div className="text-xs text-gray-600 dark:text-gray-400">{repo.artifacts}k artifacts</div>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}

