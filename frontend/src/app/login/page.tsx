'use client';

import { useState, FormEvent, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { apiClient } from '@/lib/api-client';
import { isAuthenticated } from '@/lib/auth';
import { Button } from '@/components/common/Button';
import { Card, CardHeader, CardBody } from '@/components/common/Card';

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // Immediately check authentication - if logged in, redirect to dashboard
  useEffect(() => {
    if (isAuthenticated()) {
      // Use replace to prevent going back to login page via browser back button
      router.replace('/dashboard');
    }
  }, [router]);

  // Don't render login form if already authenticated
  // This prevents any flash of the login form
  if (typeof window !== 'undefined' && isAuthenticated()) {
    return null;
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      const response = await apiClient.login(username);
      
      if (response.success) {
        router.push('/dashboard');
      } else {
        setError(response.error || 'Login failed');
      }
    } catch (err: any) {
      setError(err.response?.data?.error || 'Login failed');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 py-12 px-4 sm:px-6 lg:px-8">
      <div className="max-w-md w-full">
        <div className="text-center mb-8">
          <div className="w-16 h-16 bg-primary rounded-xl flex items-center justify-center mx-auto mb-4">
            <span className="text-white font-bold text-2xl">DC</span>
          </div>
          <h2 className="text-3xl font-bold text-gray-900 dark:text-gray-100 mb-2">
            DevOps Control Center
          </h2>
          <p className="text-secondary-500 dark:text-secondary-400">
            Sign in to access your dashboard
          </p>
        </div>

        <Card>
          <CardHeader>
            <h3 className="text-lg font-semibold text-center">Login</h3>
          </CardHeader>
          <CardBody>
            <form onSubmit={handleSubmit} className="space-y-4">
              {error && (
                <div className="p-3 bg-error-50 dark:bg-error-900/30 border border-error-200 dark:border-error-800 rounded-lg">
                  <p className="text-sm text-error-700 dark:text-error-400">{error}</p>
                </div>
              )}

              <div>
                <label htmlFor="username" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
                  Username
                </label>
                <input
                  id="username"
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  className="input"
                  placeholder="Enter your username"
                  required
                  autoFocus
                />
              </div>

              <Button
                type="submit"
                variant="primary"
                className="w-full"
                isLoading={loading}
              >
                Sign In
              </Button>

              <div className="mt-4 p-4 bg-primary-50 dark:bg-primary-900/30 rounded-lg border border-primary-200 dark:border-primary-800">
                <p className="text-xs text-primary-700 dark:text-primary-300 font-medium mb-2">Demo Users:</p>
                <div className="space-y-1 text-xs text-primary-600 dark:text-primary-400">
                  <p>• admin@internal (Platform Admin)</p>
                  <p>• lead@internal (Team Lead)</p>
                  <p>• user@internal (Regular User)</p>
                </div>
              </div>
            </form>
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

