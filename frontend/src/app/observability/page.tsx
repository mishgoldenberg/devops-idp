'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { isAuthenticated, canViewObservability } from '@/lib/auth';

export default function ObservabilityPage() {
  const router = useRouter();

  useEffect(() => {
    if (!isAuthenticated()) {
      router.push('/login');
    }
  }, [router]);

  if (!isAuthenticated()) {
    return null;
  }

  if (!canViewObservability()) {
    return (
      <div className="max-w-2xl mx-auto px-4 sm:px-6 lg:px-8 py-12">
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-red-200 dark:border-red-700 p-8 text-center">
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 mb-3">
            Permission Denied
          </h1>
          <p className="text-secondary-500 dark:text-secondary-400">
            You do not have permission to access the Observability and Monitoring area.
            Please contact an administrator if you believe this is a mistake.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900 mb-2">Observability</h1>
        <p className="text-secondary-500">
          System health, metrics, and usage analytics
        </p>
      </div>

      <div className="bg-white rounded-lg border border-gray-200 p-8 text-center">
        <p className="text-secondary-500">
          Observability metrics and dashboards will be displayed here
        </p>
      </div>
    </div>
  );
}

