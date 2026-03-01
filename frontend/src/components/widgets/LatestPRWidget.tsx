'use client';

import { Card } from '@/components/common/Card';
import { Badge } from '@/components/common/Badge';
import { GitPullRequest } from 'lucide-react';

export function LatestPRWidget() {
  return (
    <Card className="bg-white dark:bg-gray-800 border-blue-200 dark:border-blue-500 h-full">
      <div className="p-6">
        <div className="flex items-center gap-2 mb-4">
          <div className="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center">
            <GitPullRequest className="w-4 h-4 text-white" />
          </div>
          <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100">Latest PR</h2>
        </div>
        <div>
          <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-1">Feature/user-authentication</h3>
          <p className="text-sm text-gray-600 dark:text-gray-300 mb-3">Add OAuth2 integration for user login</p>
          <div className="flex items-center gap-2 mb-3 text-xs">
            <span className="text-green-600">+ 127</span>
            <span className="text-red-600">− 43</span>
            <span className="text-gray-500 dark:text-gray-400">5 files</span>
          </div>
          <div className="flex items-center gap-2 mb-4">
            <div className="w-8 h-8 bg-gradient-to-br from-blue-500 to-purple-600 rounded-full flex items-center justify-center text-white text-xs font-bold">
              SJ
            </div>
            <span className="text-sm text-gray-700 dark:text-gray-300">Sarah reviewed</span>
            <Badge className="bg-yellow-100 text-yellow-700">Pending</Badge>
          </div>
        </div>
      </div>
    </Card>
  );
}

