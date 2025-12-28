'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { isAuthenticated, canViewObservability } from '@/lib/auth';

export default function ObservabilityPage() {
  const router = useRouter();

  useEffect(() => {
    if (!isAuthenticated()) {
      router.push('/login');
    } else if (!canViewObservability()) {
      router.push('/dashboard');
    }
  }, [router]);

  if (!isAuthenticated() || !canViewObservability()) {
    return null;
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

