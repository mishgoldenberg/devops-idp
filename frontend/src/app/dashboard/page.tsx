'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { isAuthenticated, getUser } from '@/lib/auth';
import { Button } from '@/components/common/Button';
import { 
  PlayCircle, FileText, Upload, BookOpen, Settings, Plus, Search, BarChart3
} from 'lucide-react';
import { WidgetManager } from '@/components/dashboard/WidgetManager';
import { getWidgetById } from '@/lib/widget-library';

const DEFAULT_WIDGETS = ['devops-tickets', 'artifactory', 'sonarqube-scan', 'latest-pr', 'pipeline'];

export default function DashboardPage() {
  const router = useRouter();
  const user = getUser();
  const [activeWidgets, setActiveWidgets] = useState<string[]>([]);
  const [showWidgetManager, setShowWidgetManager] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    if (!isAuthenticated()) {
      router.push('/login');
    } else {
      // Load user's widget preferences from localStorage
      const saved = localStorage.getItem(`dashboard_widgets_${user?.id}`);
      if (saved) {
        setActiveWidgets(JSON.parse(saved));
      } else {
        setActiveWidgets(DEFAULT_WIDGETS);
      }
      setIsLoading(false);
    }
  }, [router, user?.id]);

  const saveWidgets = (widgets: string[]) => {
    setActiveWidgets(widgets);
    if (user?.id) {
      localStorage.setItem(`dashboard_widgets_${user.id}`, JSON.stringify(widgets));
    }
  };

  const handleAddWidget = (widgetId: string) => {
    if (!activeWidgets.includes(widgetId)) {
      saveWidgets([...activeWidgets, widgetId]);
    }
  };

  const handleRemoveWidget = (widgetId: string) => {
    saveWidgets(activeWidgets.filter(id => id !== widgetId));
  };

  if (!isAuthenticated() || isLoading) {
    return null;
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Welcome Header */}
        <div className="mb-8 flex items-start justify-between">
          <div>
            <h1 className="text-3xl font-bold text-gray-900 mb-1">
              Welcome back, {user?.username?.split('@')[0]}!
            </h1>
            <p className="text-gray-600 flex items-center gap-2">
              Here&apos;s what&apos;s happening with your development workflow
              <span className="text-xs text-gray-500">Last updated: 2 minutes ago</span>
            </p>
          </div>
          <button
            onClick={() => setShowWidgetManager(true)}
            className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 transition-colors shadow-lg"
          >
            <Settings className="w-4 h-4" />
            Customize Dashboard
          </button>
        </div>

        {/* Quick Actions - Always visible */}
        <div className={`mb-8 ${activeWidgets.length === 0 ? 'mt-8' : ''}`}>
          <h2 className="text-xl font-bold text-gray-900 mb-4">Quick Actions</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4">
            {[
              { icon: FileText, label: 'New Ticket', color: 'from-blue-500 to-blue-600', description: 'Create ServiceNow ticket' },
              { icon: Upload, label: 'Upload Artifact', color: 'from-purple-500 to-purple-600', description: 'Upload to Artifactory' },
              { icon: PlayCircle, label: 'Run Pipeline', color: 'from-green-500 to-green-600', description: 'Trigger build' },
              { icon: BookOpen, label: 'View Docs', color: 'from-orange-500 to-orange-600', description: 'Documentation' },
              { icon: BarChart3, label: 'Metrics', color: 'from-indigo-500 to-indigo-600', description: 'View analytics' },
              { icon: Search, label: 'Search', color: 'from-pink-500 to-pink-600', description: 'Quick search' },
            ].map((action, index) => (
              <button
                key={index}
                className={`p-6 bg-gradient-to-br ${action.color} text-white rounded-xl hover:shadow-xl hover:scale-105 transition-all group relative overflow-hidden`}
                title={action.description}
              >
                <div className="absolute inset-0 bg-white opacity-0 group-hover:opacity-10 transition-opacity"></div>
                <action.icon className="w-8 h-8 mb-2 group-hover:scale-110 transition-transform relative z-10" />
                <div className="font-semibold text-sm relative z-10">{action.label}</div>
              </button>
            ))}
          </div>
        </div>

        {/* Empty State */}
        {activeWidgets.length === 0 && (
          <div className="text-center py-12 bg-white rounded-xl border-2 border-dashed border-gray-200">
            <div className="w-16 h-16 bg-gray-100 rounded-full flex items-center justify-center mx-auto mb-4">
              <Plus className="w-8 h-8 text-gray-400" />
            </div>
            <h3 className="text-lg font-semibold text-gray-900 mb-2">No widgets added yet</h3>
            <p className="text-gray-600 mb-6">Start customizing your dashboard by adding widgets to track your work</p>
            <button
              onClick={() => setShowWidgetManager(true)}
              className="px-6 py-3 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 transition-colors shadow-md hover:shadow-lg"
            >
              <Plus className="w-4 h-4 inline mr-2" />
              Add Widgets
            </button>
          </div>
        )}

        {/* Widget Grid */}
        {activeWidgets.length > 0 && (
          <div className="grid lg:grid-cols-3 gap-6">
            {activeWidgets.map((widgetId) => {
              const widgetDef = getWidgetById(widgetId);
              if (!widgetDef) return null;
              const WidgetComponent = widgetDef.component;
              return (
                <div key={widgetId} className="relative">
                  <WidgetComponent />
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Widget Manager Modal */}
      {showWidgetManager && (
        <WidgetManager
          activeWidgets={activeWidgets}
          onAddWidget={handleAddWidget}
          onRemoveWidget={handleRemoveWidget}
          onClose={() => setShowWidgetManager(false)}
        />
      )}
    </div>
  );
}

