'use client';

import { useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { getUser, isAuthenticated } from '@/lib/auth';
import { apiClient } from '@/lib/api-client';
import { Home, BookOpen, FileText, Zap, FileCheck, Activity, LogOut, Menu, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { ThemeToggle } from '@/components/common/ThemeToggle';

export function Header() {
  const pathname = usePathname();
  const user = getUser();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  const isAdmin = user?.role === 'Platform Admin' || user?.role === 'Head of Section';

  const navigation = [
    { name: 'Home', href: '/dashboard', icon: Home },
    { name: 'How-To Articles', href: '/how-to', icon: BookOpen },
    { name: 'Requests', href: '/requests', icon: FileText },
    { name: 'Automation', href: '/automation', icon: Zap },
  ];

  const adminNavigation = [
    { name: 'Approvals', href: '/approvals', icon: FileCheck },
    { name: 'Observability', href: '/observability', icon: Activity },
  ];

  async function handleLogout() {
    await apiClient.logout();
    window.location.href = '/login';
  }

  if (!isAuthenticated()) {
    return null;
  }

  return (
    <header className="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 sticky top-0 z-50 transition-colors duration-200">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex justify-between items-center h-16">
          {/* Logo and App Name */}
          <div className="flex items-center">
            <Link href="/dashboard" className="flex items-center gap-3">
              <div className="w-8 h-8 bg-primary rounded-lg flex items-center justify-center">
                <span className="text-white font-bold text-sm">DC</span>
              </div>
              <span className="font-semibold text-lg text-gray-900 dark:text-gray-100 hidden sm:block">
                DevOps Control Center
              </span>
            </Link>
          </div>

          {/* Desktop Navigation */}
          <nav className="hidden md:flex items-center gap-1">
            {navigation.map((item) => {
              const isActive = pathname.startsWith(item.href) || (item.href === '/dashboard' && pathname === '/');
              return (
                <Link
                  key={item.name}
                  href={item.href}
                  className={cn(
                    'flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors',
                    isActive
                      ? 'bg-primary-50 dark:bg-primary-900/30 text-primary dark:text-primary-400'
                      : 'text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-50 dark:hover:bg-gray-700'
                  )}
                >
                  {item.name}
                </Link>
              );
            })}
            {isAdmin && (
              <>
                <div className="w-px h-6 bg-gray-200 dark:bg-gray-700 mx-2" />
                {adminNavigation.map((item) => {
                  const isActive = pathname.startsWith(item.href);
                  return (
                    <Link
                      key={item.name}
                      href={item.href}
                      className={cn(
                        'flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors',
                        isActive
                          ? 'bg-primary-50 dark:bg-primary-900/30 text-primary dark:text-primary-400'
                          : 'text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-50 dark:hover:bg-gray-700'
                      )}
                    >
                      <item.icon className="w-4 h-4" />
                      {item.name}
                    </Link>
                  );
                })}
              </>
            )}
          </nav>

          {/* User Menu */}
          <div className="flex items-center gap-4">
            <div className="hidden md:flex flex-col items-end">
              <span className="text-sm font-medium text-gray-900 dark:text-gray-100">{user?.username}</span>
              <span className="text-xs text-secondary-500 dark:text-secondary-400">{user?.role}</span>
            </div>
            <div className="hidden sm:block">
              <ThemeToggle />
            </div>
            <button
              onClick={handleLogout}
              className="p-2 text-secondary-600 dark:text-secondary-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
              title="Logout"
            >
              <LogOut className="w-5 h-5" />
            </button>

            {/* Mobile menu button */}
            <button
              onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
              className="md:hidden p-2 text-secondary-600 dark:text-secondary-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg"
            >
              {mobileMenuOpen ? (
                <X className="w-6 h-6" />
              ) : (
                <Menu className="w-6 h-6" />
              )}
            </button>
          </div>
        </div>

        {/* Mobile Navigation */}
        {mobileMenuOpen && (
          <div className="md:hidden py-4 border-t border-gray-200 dark:border-gray-700">
            <nav className="flex flex-col gap-2">
              {navigation.map((item) => {
                const isActive = pathname.startsWith(item.href) || (item.href === '/dashboard' && pathname === '/');
                return (
                  <Link
                    key={item.name}
                    href={item.href}
                    onClick={() => setMobileMenuOpen(false)}
                    className={cn(
                      'flex items-center gap-2 px-4 py-3 rounded-lg text-sm font-medium transition-colors',
                      isActive
                        ? 'bg-primary-50 text-primary'
                        : 'text-secondary-600 dark:text-secondary-400 hover:bg-gray-100 dark:hover:bg-gray-700'
                    )}
                  >
                    <item.icon className="w-4 h-4" />
                    {item.name}
                  </Link>
                );
              })}
              {isAdmin && (
                <>
                  <div className="my-2 border-t border-gray-200 dark:border-gray-700" />
                  {adminNavigation.map((item) => {
                    const isActive = pathname.startsWith(item.href);
                    return (
                      <Link
                        key={item.name}
                        href={item.href}
                        onClick={() => setMobileMenuOpen(false)}
                        className={cn(
                          'flex items-center gap-2 px-4 py-3 rounded-lg text-sm font-medium transition-colors',
                          isActive
                            ? 'bg-primary-50 text-primary'
                            : 'text-secondary-600 dark:text-secondary-400 hover:bg-gray-100 dark:hover:bg-gray-700'
                        )}
                      >
                        <item.icon className="w-4 h-4" />
                        {item.name}
                      </Link>
                    );
                  })}
                </>
              )}
            </nav>
            <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-700 px-4">
              <p className="text-sm font-medium text-gray-900 dark:text-gray-100">{user?.username}</p>
              <p className="text-xs text-secondary-500 dark:text-secondary-400">{user?.role}</p>
            </div>
          </div>
        )}
      </div>
    </header>
  );
}

