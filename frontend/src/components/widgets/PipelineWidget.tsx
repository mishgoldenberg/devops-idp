'use client';

import { Card } from '@/components/common/Card';
import { Badge } from '@/components/common/Badge';
import { PlayCircle, CheckCircle, Clock } from 'lucide-react';

export function PipelineWidget() {
  return (
    <Card className="bg-white dark:bg-gray-800 border-purple-200 dark:border-purple-500 h-full">
      <div className="p-6">
        <div className="flex items-center gap-2 mb-4">
          <div className="w-8 h-8 bg-purple-600 rounded-lg flex items-center justify-center">
            <PlayCircle className="w-4 h-4 text-white" />
          </div>
          <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100">Latest Pipeline</h2>
        </div>
        <div>
          <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-1">Build #456</h3>
          <p className="text-sm text-gray-600 dark:text-gray-300 mb-3">main branch • 12 minutes ago</p>
          <div className="space-y-2 mb-4">
            <div className="flex items-center justify-between text-sm">
              <div className="flex items-center gap-2">
                <CheckCircle className="w-4 h-4 text-green-600" />
                <span>Build</span>
              </div>
              <span className="text-gray-600 dark:text-gray-400">2m 34s</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <div className="flex items-center gap-2">
                <CheckCircle className="w-4 h-4 text-green-600" />
                <span>Test</span>
              </div>
              <span className="text-gray-600 dark:text-gray-400">4m 12s</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <div className="flex items-center gap-2">
                <Clock className="w-4 h-4 text-gray-400" />
                <span>Deploy</span>
              </div>
              <span className="text-gray-600 dark:text-gray-400">3m 45s</span>
            </div>
          </div>
          <Badge className="bg-green-100 text-green-700 w-full justify-center">
            <CheckCircle className="w-3 h-3 mr-1" />
            Success
          </Badge>
        </div>
      </div>
    </Card>
  );
}

