'use client';

import { Card } from '@/components/common/Card';
import { CheckCircle } from 'lucide-react';

export function SonarQubeWidget() {
  return (
    <Card className="bg-gradient-to-br from-green-50 to-emerald-50 border-green-200 dark:from-emerald-900 dark:to-emerald-800 dark:border-emerald-700 h-full">
      <div className="p-6">
        <div className="flex items-center gap-2 mb-4">
          <div className="w-8 h-8 bg-green-600 dark:bg-green-500 rounded-lg flex items-center justify-center">
            <CheckCircle className="w-4 h-4 text-white" />
          </div>
          <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100">SonarQube Latest Scan</h2>
        </div>
        <div className="text-center py-6">
          <div className="text-6xl font-bold text-green-600 dark:text-green-400 mb-2">A</div>
          <div className="text-sm font-medium text-gray-700 dark:text-gray-200 mb-4">Quality Gate Status</div>
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <div className="text-2xl font-bold text-green-600 dark:text-green-400">0</div>
              <div className="text-gray-600 dark:text-gray-200">Bugs</div>
            </div>
            <div>
              <div className="text-2xl font-bold text-amber-600">3</div>
              <div className="text-gray-600 dark:text-gray-200">Code Smells</div>
            </div>
          </div>
          <div className="mt-4 pt-4 border-t border-green-200 dark:border-emerald-700">
            <div className="text-lg font-bold text-gray-900 dark:text-gray-100">87.3%</div>
            <div className="text-xs text-gray-600 dark:text-gray-200">Coverage</div>
          </div>
        </div>
      </div>
    </Card>
  );
}

