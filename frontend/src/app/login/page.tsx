'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { isAuthenticated } from '@/lib/auth';
import { Button } from '@/components/common/Button';
import { Card, CardHeader, CardBody } from '@/components/common/Card';

export default function LoginPage() {
  const router = useRouter();

  useEffect(() => {
    if (isAuthenticated()) {
      // If already authenticated, redirect to dashboard
      router.replace('/dashboard');
      return;
    }

    function handleMessage(event: MessageEvent) {
      if (event.data && event.data.type === 'auth:success') {
        const { token, user } = event.data.data || {};
        if (token && user) {
          const { apiClient } = require('@/lib/api-client');
          apiClient.completeOAuthLoginFromMessage({ token, user });
          router.push('/dashboard');
        }
      }
    }

    window.addEventListener('message', handleMessage);
    return () => {
      window.removeEventListener('message', handleMessage);
    };
  }, [router]);

  function startGoogleLogin() {
    const authWindow = window.open(
      '/api/auth/login',
      'google-oauth',
      'width=500,height=600'
    );
    if (!authWindow) {
      // Popup blocked; fallback to full redirect
      window.location.href = '/api/auth/login';
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
            <h3 className="text-lg font-semibold text-center">Sign in with Google</h3>
          </CardHeader>
          <CardBody>
            <div className="space-y-6">
              <Button
                type="button"
                variant="primary"
                className="w-full"
                onClick={startGoogleLogin}
              >
                Continue with Google
              </Button>
            </div>
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

